"""Orchestration du tri : règles d'abord, puis LLM par lots pour le reste."""
from __future__ import annotations

import logging
from typing import Any

from .llm import get_classifier
from .models import Decision, Email, Usage
from .prefilter import apply_rules

log = logging.getLogger(__name__)


def triage(emails: list[Email], config: dict[str, Any], profile: str) -> tuple[list[Decision], Usage]:
    decisions: dict[str, Decision] = {}
    to_llm: list[Email] = []
    usage = Usage()

    for email in emails:
        d = apply_rules(email, config.get("rules", {}))
        if d:
            decisions[email.id] = d
        else:
            to_llm.append(email)

    if to_llm:
        llm = get_classifier(config["llm"])
        batch_size = config["llm"].get("batch_size", 15)
        by_id = {e.id: e for e in to_llm}
        for i in range(0, len(to_llm), batch_size):
            batch = to_llm[i : i + batch_size]
            log.info("LLM : lot %s (%s mails)…", i // batch_size + 1, len(batch))
            results, batch_usage = llm.classify(batch, profile)
            usage += batch_usage
            for c in results:
                if c.id in by_id:  # ignore un id inventé par le modèle
                    decisions[c.id] = Decision(
                        by_id[c.id], c.category, "llm", c.reason, c.confidence
                    )

    # Mails que le LLM aurait oubliés : on ne perd rien, on les signale.
    for e in emails:
        if e.id not in decisions:
            log.warning("Non classé par le LLM : %r", e.subject)

    return [decisions[e.id] for e in emails if e.id in decisions], usage
