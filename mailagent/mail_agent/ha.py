"""Intégration Home Assistant.

- Notifications : via l'API de HA (proxy du Supervisor, jeton SUPERVISOR_TOKEN
  fourni automatiquement à l'add-on grâce à `homeassistant_api: true`).
- Entités : via MQTT « discovery ». On publie une description de chaque entité sur
  homeassistant/<type>/mailagent/<id>/config, et HA les crée tout seul, regroupées
  dans un appareil « Mail Agent ». Les identifiants MQTT sont fournis par le
  Supervisor (`services: mqtt:want`), rien à configurer.

Hors add-on (développement sur le Mac), tout ceci est simplement désactivé.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional

import requests

from .engine import Busy, Engine

log = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN")
PREFIX = "mailagent"
DISCOVERY = "homeassistant"
DEVICE = {
    "identifiers": ["mailagent"],
    "name": "Mail Agent",
    "manufacturer": "mailAgent",
    "model": "Add-on Home Assistant",
}


def in_addon() -> bool:
    return bool(TOKEN)


def _api(method: str, path: str, **kwargs) -> Any:
    resp = requests.request(
        method, f"{SUPERVISOR}{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15, **kwargs
    )
    resp.raise_for_status()
    return resp.json() if resp.content else None


def notify_services() -> list[str]:
    """Services notify.* disponibles (ex. mobile_app_mon_telephone)."""
    if not in_addon():
        return []
    try:
        for domain in _api("GET", "/core/api/services"):
            if domain["domain"] == "notify":
                return sorted(domain["services"])
    except Exception as e:
        log.warning("Liste des services de notification indisponible : %s", e)
    return []


def notify(service: str, title: str, message: str, data: Optional[dict] = None) -> None:
    if not (in_addon() and service):
        return
    payload: dict[str, Any] = {"title": title, "message": message}
    if data:
        payload["data"] = data
    _api("POST", f"/core/api/services/notify/{service}", json=payload)


def _iso_tz(value: Optional[str]) -> Optional[str]:
    """HA exige un horodatage avec fuseau pour les capteurs de type timestamp."""
    return datetime.fromisoformat(value).astimezone().isoformat() if value else None


class HABridge:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.client = None
        self.panel_path: Optional[str] = None
        engine.on_state_changed.append(self.publish_state)
        engine.on_run_finished.append(self.send_recap)

    # --- Démarrage ---------------------------------------------------------------

    def start(self) -> None:
        if not in_addon():
            log.info("Hors Home Assistant : MQTT et notifications désactivés")
            return
        try:
            info = _api("GET", "/addons/self/info")["data"]
            self.panel_path = f"/hassio/ingress/{info['slug']}"
        except Exception as e:
            log.warning("Infos de l'add-on indisponibles : %s", e)
        try:
            mqtt_conf = _api("GET", "/services/mqtt")["data"]
        except Exception:
            log.warning("MQTT indisponible (installe l'add-on Mosquitto) : pas d'entités dans HA")
            return
        self._connect(mqtt_conf)

    def _connect(self, conf: dict[str, Any]) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mailagent-addon")
        if conf.get("username"):
            client.username_pw_set(conf["username"], conf.get("password"))
        if conf.get("ssl"):
            client.tls_set()
        client.will_set(f"{PREFIX}/status", "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_async(conf["host"], int(conf["port"]))
        client.loop_start()  # thread réseau géré par paho (reconnexion automatique)
        self.client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.error("Connexion MQTT refusée : %s", reason_code)
            return
        log.info("Connecté au broker MQTT")
        self._publish_discovery()
        client.subscribe([(f"{PREFIX}/auto/set", 0), (f"{PREFIX}/analyze", 0)])
        client.publish(f"{PREFIX}/status", "online", retain=True)
        self.publish_state()

    def _publish_discovery(self) -> None:
        base = {
            "device": DEVICE,
            "availability_topic": f"{PREFIX}/status",
            "state_topic": f"{PREFIX}/state",
        }
        entities = {
            ("sensor", "important_unread"): {
                "name": "Mails importants non lus",
                "value_template": "{{ value_json.important_unread }}",
                "json_attributes_topic": f"{PREFIX}/important",
                "unit_of_measurement": "mails",
                "state_class": "measurement",
                "icon": "mdi:email-alert",
            },
            ("sensor", "last_run"): {
                "name": "Dernière analyse",
                "value_template": "{{ value_json.last_run_at }}",
                "device_class": "timestamp",
            },
            ("sensor", "last_status"): {
                "name": "Statut dernière analyse",
                "value_template": "{{ value_json.last_status }}",
                "icon": "mdi:list-status",
            },
            ("sensor", "next_run"): {
                "name": "Prochaine analyse auto",
                "value_template": "{{ value_json.next_run }}",
                "device_class": "timestamp",
            },
            ("sensor", "tokens_today"): {
                "name": "Tokens aujourd'hui",
                "value_template": "{{ value_json.tokens_today }}",
                "unit_of_measurement": "tokens",
                "state_class": "measurement",
                "icon": "mdi:counter",
            },
            ("sensor", "cost_30d"): {
                "name": "Coût équivalent 30 jours",
                "value_template": "{{ value_json.cost_30d }}",
                "unit_of_measurement": "USD",
                "device_class": "monetary",
                "icon": "mdi:cash",
            },
            ("binary_sensor", "quota_alert"): {
                "name": "Alerte quota",
                "value_template": "{{ 'ON' if value_json.quota_alert else 'OFF' }}",
                "device_class": "problem",
            },
            ("switch", "auto"): {
                "name": "Analyse automatique",
                "value_template": "{{ 'ON' if value_json.auto_enabled else 'OFF' }}",
                "command_topic": f"{PREFIX}/auto/set",
                "icon": "mdi:robot",
            },
            ("button", "analyze"): {
                "name": "Analyser (aperçu)",
                "command_topic": f"{PREFIX}/analyze",
                "payload_press": "preview",
                "icon": "mdi:email-search",
            },
            ("button", "analyze_apply"): {
                "name": "Analyser et appliquer",
                "command_topic": f"{PREFIX}/analyze",
                "payload_press": "apply",
                "icon": "mdi:email-check",
            },
        }
        for (component, object_id), conf in entities.items():
            payload = {**base, **conf, "unique_id": f"mailagent_{object_id}", "object_id": f"mail_agent_{object_id}"}
            if component == "button":
                payload.pop("state_topic")
            self.client.publish(
                f"{DISCOVERY}/{component}/mailagent/{object_id}/config", json.dumps(payload), retain=True
            )

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode().strip()
        try:
            if msg.topic == f"{PREFIX}/auto/set":
                self.engine.set_auto(payload == "ON")
            elif msg.topic == f"{PREFIX}/analyze":
                self.engine.start_analysis("ha", apply=(payload == "apply"))
        except Busy:
            log.info("Analyse déjà en cours : commande ignorée")
        except Exception:
            log.exception("Commande MQTT en échec (%s)", msg.topic)

    # --- Publication de l'état -----------------------------------------------------

    def publish_state(self) -> None:
        if not (self.client and self.client.is_connected()):
            return
        s = self.engine.state()
        last = s["last_run"] or {}
        state = {
            "important_unread": s["important_unread"],
            "last_run_at": _iso_tz(last.get("finished_at") or last.get("started_at")),
            "last_status": last.get("status", "aucune"),
            "next_run": _iso_tz(s["next_run"]),
            "tokens_today": s["tokens_today"],
            "cost_30d": s["cost_30d"],
            "quota_alert": bool(s["quota_alerts"]),
            "auto_enabled": s["auto_enabled"],
        }
        self.client.publish(f"{PREFIX}/state", json.dumps(state), retain=True)
        mails = [
            {"compte": m["account"], "de": m["sender_name"], "objet": m["subject"], "raison": m["reason"]}
            for m in self.engine.important_unread[:20]
        ]
        self.client.publish(f"{PREFIX}/important", json.dumps({"mails": mails}, ensure_ascii=False), retain=True)

    # --- Récap sur le téléphone -------------------------------------------------------

    def send_recap(self, run: dict[str, Any]) -> None:
        """Après une analyse automatique (ou lancée depuis HA) : un seul message récapitulatif."""
        if run["trigger"] == "manual":
            return  # lancée depuis le panneau : l'utilisateur a déjà le résultat sous les yeux
        from . import store
        from .config import load_config

        service = load_config()["notifications"].get("service")
        if not service:
            return
        data = {"url": self.panel_path, "clickAction": self.panel_path} if self.panel_path else None
        if run["status"] == "quota":
            notify(service, "Mail Agent : quota épuisé", "Le quota Gemini est atteint, l'analyse est incomplète.", data)
            return
        important = [d for d in store.run_decisions(run["id"]) if d["category"] == "important"]
        if not important:
            return
        lines = []
        for d in important[:8]:
            name = (d["sender"] or "").split("<")[0].strip().strip('"') or d["sender"]
            lines.append(f"• {name} — {d['subject']}")
        if len(important) > 8:
            lines.append(f"… et {len(important) - 8} autre(s)")
        title = f"📬 {len(important)} mail(s) important(s)"
        notify(service, title, "\n".join(lines), data)


class ImportantRefresher(threading.Thread):
    """Rafraîchit régulièrement la liste des importants non lus (quand tu lis un mail
    dans Gmail, le compteur de HA doit baisser). Ne consomme aucun token."""

    def __init__(self, engine: Engine, interval: int = 600) -> None:
        super().__init__(name="important-refresher", daemon=True)
        self.engine, self.interval = engine, interval

    def run(self) -> None:
        while True:
            try:
                self.engine.refresh_important()
            except Exception:
                log.exception("Rafraîchissement des importants en échec")
            time.sleep(self.interval)
