"""Règles déterministes appliquées avant le LLM.

Avantages : gratuit, instantané, prévisible, et certains mails (banque, santé)
ne quittent jamais ta machine.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import Category, Decision, Email


def _match(email: Email, patterns: list[str]) -> Optional[str]:
    """Renvoie le motif qui correspond, sinon None. Formats acceptés :
    - "@domaine.fr"      : toute adresse de ce domaine (et ses sous-domaines)
    - "a@domaine.fr"     : cette adresse exacte
    - "name:vercel[bot]" : nom affiché de l'expéditeur (utile quand plusieurs
                           services partagent une adresse, ex. notifications@github.com)
    """
    address = email.sender_address
    display_name = email.sender_name.lower()
    for raw in patterns or []:
        p = raw.strip().lower()
        if p.startswith("name:"):
            if display_name == p[5:].strip():
                return raw
        elif p.startswith("@"):
            if address.endswith(p) or address.endswith("." + p[1:]):
                return raw
        elif address == p:
            return raw
    return None


# Ordre d'évaluation : "important" gagne sur tout le reste
RULE_ORDER = [Category.IMPORTANT] + [c for c in Category if c is not Category.IMPORTANT]


def apply_rules(email: Email, rules: dict[str, Any]) -> Optional[Decision]:
    """Renvoie une décision si une règle s'applique, sinon None (=> LLM)."""
    pattern = _match(email, rules.get("private_senders"))
    if pattern:
        return Decision(email, Category.IMPORTANT, "rule", f"règle privée « {pattern} » (non envoyé au LLM)")
    for category in RULE_ORDER:
        pattern = _match(email, rules.get(f"{category.value}_senders"))
        if pattern:
            return Decision(email, category, "rule", f"règle {category.value} « {pattern} »")
    if "SPAM" in email.label_ids:
        return Decision(email, Category.SPAM, "rule", "déjà classé spam par Gmail")
    return None
