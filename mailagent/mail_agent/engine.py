"""Moteur : lance les analyses, les applique, gère les corrections.

Utilisé par le panneau web, le planificateur et Home Assistant (MQTT). Une seule
analyse à la fois (verrou) ; elle tourne dans un thread pour ne pas bloquer l'interface.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Optional

from . import store
from .classifier import triage
from .config import NOISE_CATEGORIES, account_settings, load_config, save_config, token_prices
from .gmail_client import GmailClient, NotConnected, account_status
from .models import Category, Email, QuotaExceeded, Usage

log = logging.getLogger(__name__)

# Domaines de messageries grand public : une règle "@gmail.com" n'aurait pas de sens
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.fr", "yahoo.com", "hotmail.fr", "hotmail.com",
    "outlook.fr", "outlook.com", "live.fr", "live.com", "icloud.com", "me.com",
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "proton.me", "protonmail.com",
}


class Busy(Exception):
    """Une analyse est déjà en cours."""


def label_name(prefix: str, category: str) -> str:
    return f"{prefix}/{category.capitalize()}"


class Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current_run: Optional[int] = None
        # Appelés après chaque analyse / changement d'état (ex. publication MQTT, notification)
        self.on_run_finished: list[Callable[[dict[str, Any]], None]] = []
        self.on_state_changed: list[Callable[[], None]] = []
        self._important_cache: list[dict[str, Any]] = []

    # --- Analyse ---------------------------------------------------------------

    def start_analysis(self, trigger: str, apply: bool) -> int:
        """Lance une analyse en arrière-plan et renvoie son identifiant."""
        if not self._lock.acquire(blocking=False):
            raise Busy("Une analyse est déjà en cours.")
        try:
            config = load_config()
            run_id = store.create_run(trigger, config["llm"]["model"])
        except Exception:
            self._lock.release()
            raise
        self.current_run = run_id
        threading.Thread(
            target=self._run, args=(run_id, config, apply), name=f"run-{run_id}", daemon=True
        ).start()
        self._state_changed()
        return run_id

    def _run(self, run_id: int, config: dict[str, Any], apply: bool) -> None:
        usage = Usage()
        n_emails = 0
        status, errors = "done", []
        try:
            for account in config["accounts"]:
                email = account["email"]
                settings = account_settings(config, account)
                try:
                    gmail = GmailClient(email)
                    emails = gmail.fetch(settings["query"], settings["max_messages"])
                    # Déjà traités et appliqués : on n'y retouche pas (économise des tokens)
                    done = store.applied_msg_ids(email)
                    emails = [e for e in emails if e.id not in done]
                    log.info("%s : %s nouveaux mails à analyser", email, len(emails))
                    if emails:
                        decisions, account_usage = triage(emails, config, settings["profile"])
                        usage += account_usage
                        store.add_decisions(run_id, email, decisions)
                        n_emails += len(decisions)
                except QuotaExceeded as e:
                    status = "quota"
                    errors.append(str(e))
                    break
                except NotConnected as e:
                    errors.append(str(e))
                except Exception as e:
                    log.exception("Erreur sur %s", email)
                    errors.append(f"{email} : {e}")
            if errors and status == "done" and n_emails == 0:
                status = "error"
            store.finish_run(run_id, status, usage, n_emails, "\n".join(errors) or None)
            if apply and n_emails:
                self.apply_run(run_id)
        except Exception as e:
            log.exception("Analyse %s interrompue", run_id)
            store.finish_run(run_id, "error", usage, n_emails, str(e))
        finally:
            # Rafraîchir AVANT de signaler la fin : le panneau recharge la liste dès
            # qu'il voit que l'analyse est terminée.
            try:
                self.refresh_important()
            except Exception:
                log.exception("Rafraîchissement des importants en échec")
            self.current_run = None
            self._lock.release()
        run = store.get_run(run_id)
        for callback in self.on_run_finished:
            try:
                callback(run)
            except Exception:
                log.exception("Callback de fin d'analyse en échec")
        self._state_changed()

    def apply_run(self, run_id: int) -> None:
        """Pose les labels IA/<Catégorie> et marque le bruit comme lu dans Gmail."""
        config = load_config()
        accounts = {a["email"]: account_settings(config, a) for a in config["accounts"]}
        by_account: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for d in store.run_decisions(run_id):
            if not d["applied"]:
                by_account[d["account"]][d["category"]].append(d["msg_id"])
        for email, by_category in by_account.items():
            prefix = accounts.get(email, {}).get("label_prefix", "IA")
            gmail = GmailClient(email)
            for category, msg_ids in by_category.items():
                gmail.modify(
                    msg_ids,
                    add=[label_name(prefix, category)],
                    remove=[label_name(prefix, c.value) for c in Category if c.value != category],
                    mark_read=category in NOISE_CATEGORIES,
                )
        store.mark_run_applied(run_id)
        self.refresh_important()

    # --- Corrections -----------------------------------------------------------

    def correct(self, decision_id: int, category: str) -> dict[str, Any]:
        """Reclasse un mail. S'il a déjà été appliqué dans Gmail, met à jour ses labels.
        Renvoie des suggestions de règles pour que ça ne se reproduise pas."""
        Category(category)  # valide la catégorie
        d = store.get_decision(decision_id)
        if d is None:
            raise KeyError(decision_id)
        old = d["category"]
        store.set_category(decision_id, category)

        if d["applied"] and old != category:
            config = load_config()
            account = next((a for a in config["accounts"] if a["email"] == d["account"]), {"email": d["account"]})
            prefix = account_settings(config, account)["label_prefix"]
            was_noise, is_noise = old in NOISE_CATEGORIES, category in NOISE_CATEGORIES
            GmailClient(d["account"]).modify(
                [d["msg_id"]],
                add=[label_name(prefix, category)],
                remove=[label_name(prefix, old)],
                mark_read=is_noise and not was_noise,
                # Un mail sorti du "bruit" redevient non lu s'il l'était à l'origine
                mark_unread=was_noise and not is_noise and bool(d["unread"]),
            )
            self.refresh_important()

        e = Email(d["msg_id"], d["sender"] or "", "", "", "")
        domain = e.sender_address.split("@")[-1]
        suggestions = [{"pattern": e.sender_address, "label": f"l'adresse {e.sender_address}"}]
        if domain and domain not in GENERIC_DOMAINS:
            suggestions.append({"pattern": f"@{domain}", "label": f"tout le domaine @{domain}"})
        if e.sender_name and e.sender_name.lower() != e.sender_address:
            suggestions.append({"pattern": f"name:{e.sender_name}", "label": f"le nom affiché « {e.sender_name} »"})
        return {"category": category, "suggestions": suggestions}

    def add_rule(self, category: str, pattern: str) -> None:
        Category(category)
        config = load_config()
        key = f"{category}_senders"
        patterns = config["rules"].setdefault(key, []) or []
        # Une règle pour ce motif dans une autre catégorie serait contradictoire : on la retire
        for other in list(config["rules"]):
            if other.endswith("_senders") and other != key:
                config["rules"][other] = [p for p in (config["rules"][other] or []) if p != pattern]
        if pattern not in patterns:
            patterns.append(pattern)
        config["rules"][key] = patterns
        save_config(config)

    # --- Importants non lus ----------------------------------------------------------

    def refresh_important(self) -> list[dict[str, Any]]:
        """Interroge Gmail : mails labellisés IA/Important et non lus, sur tous les comptes,
        + les importants non lus d'un aperçu pas encore appliqué."""
        config = load_config()
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for account in config["accounts"]:
            email = account["email"]
            prefix = account_settings(config, account)["label_prefix"]
            try:
                gmail = GmailClient(email)
                label_id = gmail.label_id(label_name(prefix, "important"), create=False)
                mails = gmail.fetch(label_ids=[label_id, "UNREAD"], max_messages=50) if label_id else []
            except Exception as e:
                log.warning("Importants non lus indisponibles pour %s : %s", email, e)
                continue
            known = store.latest_decisions(email, [m.id for m in mails])
            for m in mails:
                d = known.get(m.id, {})
                seen.add((email, m.id))
                items.append({
                    "account": email, "msg_id": m.id, "sender": m.sender, "sender_name": m.sender_name,
                    "subject": m.subject, "snippet": m.snippet, "date": m.date,
                    "reason": d.get("reason"), "source": d.get("source"), "confidence": d.get("confidence"),
                    "decision_id": d.get("id"), "pending": False,
                })
        preview = store.last_run()
        if preview and not preview["applied"] and preview["status"] in ("done", "quota"):
            for d in store.run_decisions(preview["id"]):
                if d["category"] == "important" and d["unread"] and (d["account"], d["msg_id"]) not in seen:
                    e_name = d["sender"].split("<")[0].strip().strip('"') if d["sender"] else ""
                    items.append({
                        "account": d["account"], "msg_id": d["msg_id"], "sender": d["sender"],
                        "sender_name": e_name or d["sender"], "subject": d["subject"],
                        "snippet": d["snippet"], "date": d["date"], "reason": d["reason"],
                        "source": d["source"], "confidence": d["confidence"],
                        "decision_id": d["id"], "pending": True,
                    })
        self._important_cache = items
        self._state_changed()
        return items

    @property
    def important_unread(self) -> list[dict[str, Any]]:
        return self._important_cache

    # --- État global ----------------------------------------------------------------

    def set_auto(self, enabled: bool) -> None:
        config = load_config()
        config["schedule"]["enabled"] = bool(enabled)
        save_config(config)
        self._state_changed()

    def state(self) -> dict[str, Any]:
        config = load_config()
        price_in, price_out = token_prices(config)
        usage = store.daily_usage(30)
        today = usage[-1]
        tokens_today = today["input_tokens"] + today["output_tokens"]

        def cost(rows) -> float:
            return sum(r["input_tokens"] * price_in + r["output_tokens"] * price_out for r in rows) / 1e6

        last = store.last_run()
        threshold = int(config["usage"].get("daily_token_alert") or 0)
        quota_alerts = []
        if last and last["status"] == "quota":
            quota_alerts.append("Le dernier passage a atteint le quota Gemini.")
        if threshold and tokens_today > threshold:
            quota_alerts.append(f"Consommation du jour ({tokens_today:,} tokens) au-dessus du seuil ({threshold:,}).".replace(",", " "))
        from .scheduler import next_run_time

        next_run = next_run_time(config)
        return {
            "running": self.current_run,
            "auto_enabled": bool(config["schedule"].get("enabled")),
            "schedule_time": config["schedule"].get("time", "07:30"),
            "next_run": next_run.isoformat(timespec="minutes") if next_run else None,
            "last_run": last,
            "important_unread": len(self._important_cache),
            "tokens_today": tokens_today,
            "cost_today": round(cost([today]), 4),
            "cost_30d": round(cost(usage), 4),
            "quota_alerts": quota_alerts,
            "model": config["llm"]["model"],
            "accounts": [{"email": a["email"], "status": account_status(a["email"])} for a in config["accounts"]],
            "now": datetime.now().isoformat(timespec="seconds"),
        }

    def _state_changed(self) -> None:
        for callback in self.on_state_changed:
            try:
                callback()
            except Exception:
                log.exception("Callback de changement d'état en échec")


engine = Engine()
