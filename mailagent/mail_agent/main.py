"""Ligne de commande : python -m mail_agent.main [--apply]

Par défaut : mode lecture seule (dry-run), rien n'est modifié dans Gmail.
Partage l'historique (SQLite) avec le panneau web.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import store
from .classifier import triage
from .config import DATA_DIR, NOISE_CATEGORIES, PROFILE_PATH, account_settings, load_config
from .engine import label_name
from .gmail_client import GmailClient
from .models import Category, Decision, Usage

COLORS = {
    Category.IMPORTANT: "bold red",
    Category.PERSONNEL: "green",
    Category.TRANSACTIONNEL: "cyan",
    Category.NOTIFICATION: "blue",
    Category.NEWSLETTER: "magenta",
    Category.PROMO: "yellow",
    Category.SPAM: "dim",
}

console = Console()


def show(decisions: list[Decision]) -> None:
    order = list(Category)
    # Tient dans un terminal de 80 colonnes : la raison passe sous l'objet,
    # la source (règle/LLM) sous la catégorie.
    table = Table(show_lines=True)
    table.add_column("Catégorie", min_width=14, no_wrap=True)
    table.add_column("De", max_width=22, overflow="ellipsis", no_wrap=True)
    table.add_column("Objet / raison", ratio=1)
    for d in sorted(decisions, key=lambda d: order.index(d.category)):
        source = d.source + (f" {d.confidence:.0%}" if d.confidence is not None else "")
        table.add_row(
            f"[{COLORS[d.category]}]{d.category.value}[/]\n[dim]{source}[/]",
            escape(d.email.sender_name),
            f"{escape(d.email.subject)}\n[dim italic]{escape(d.reason)}[/]",
        )
    console.print(table)
    counts = Counter(d.category.value for d in decisions)
    console.print("Résumé : " + ", ".join(f"{k}={v}" for k, v in counts.most_common()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tri de mails Gmail assisté par LLM")
    parser.add_argument("--apply", action="store_true", help="poser les labels et marquer le bruit comme lu")
    parser.add_argument("--account", help="ne traiter que ce compte (adresse email)")
    parser.add_argument("--query", help="requête Gmail (remplace celle de la config)")
    parser.add_argument("--max", type=int, help="nombre max de mails par compte")
    parser.add_argument("--config", type=Path, default=PROFILE_PATH)
    args = parser.parse_args()

    load_dotenv(DATA_DIR / ".env")
    store.init()
    config = load_config(args.config)
    accounts = config["accounts"]
    if args.account:
        accounts = [a for a in accounts if a["email"].lower() == args.account.lower()]
        if not accounts:
            raise SystemExit(f"Compte {args.account} absent de la config.")

    run_id = store.create_run("manual", config["llm"]["model"])
    usage, n_emails, failures = Usage(), 0, []
    for account in accounts:
        email = account["email"]
        settings = account_settings(config, account)
        console.rule(f"[bold]{email}")
        try:
            gmail = GmailClient(email, interactive=True)
            query = args.query or settings["query"]
            console.print(f"Récupération des mails : [bold]{query}[/]")
            emails = gmail.fetch(query, args.max or settings["max_messages"])
            console.print(f"{len(emails)} mails récupérés.")
            if not emails:
                continue
            decisions, account_usage = triage(emails, config, settings["profile"])
            usage += account_usage
            n_emails += len(decisions)
            store.add_decisions(run_id, email, decisions)
            show(decisions)
            if args.apply:
                for category in Category:
                    ids = [d.email.id for d in decisions if d.category is category]
                    gmail.modify(
                        ids,
                        add=[label_name(settings["label_prefix"], category.value)],
                        remove=[label_name(settings["label_prefix"], c.value) for c in Category if c is not category],
                        mark_read=category.value in NOISE_CATEGORIES,
                    )
                console.print(f"[green]Labels appliqués sur {len(decisions)} mails.[/]")
        except Exception as e:  # un compte en erreur ne bloque pas les autres
            console.print(f"[red]Erreur sur {email} : {e}[/]")
            failures.append(email)

    store.finish_run(run_id, "done" if not failures else "error", usage, n_emails, ", ".join(failures) or None)
    if args.apply:
        store.mark_run_applied(run_id)
    else:
        console.print("[dim]Mode lecture seule : rien n'a été modifié (ajoute --apply pour appliquer).[/]")
    console.print(f"Tokens : {usage.input_tokens} en entrée, {usage.output_tokens} en sortie ({usage.requests} requêtes)")
    if failures:
        raise SystemExit(f"Échec pour : {', '.join(failures)}")


if __name__ == "__main__":
    main()
