"""Planificateur : lance l'analyse automatique une fois par jour à l'heure choisie.

Volontairement simple : un thread qui regarde l'heure toutes les 20 secondes.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import load_config
from .engine import Busy, engine

log = logging.getLogger(__name__)


def next_run_time(config: dict[str, Any], now: Optional[datetime] = None) -> Optional[datetime]:
    schedule = config.get("schedule", {})
    if not schedule.get("enabled"):
        return None
    now = now or datetime.now()
    hh, mm = (int(x) for x in str(schedule.get("time", "07:30")).split(":"))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


class Scheduler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="scheduler", daemon=True)
        self._last_fired: Optional[str] = None  # "AAAA-MM-JJ HH:MM" du dernier déclenchement

    def run(self) -> None:
        log.info("Planificateur démarré")
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Erreur du planificateur")
            time.sleep(20)

    def tick(self) -> None:
        config = load_config()
        schedule = config.get("schedule", {})
        if not schedule.get("enabled"):
            return
        now = datetime.now()
        hh, mm = (int(x) for x in str(schedule.get("time", "07:30")).split(":"))
        slot = f"{now:%Y-%m-%d} {hh:02d}:{mm:02d}"
        if (now.hour, now.minute) == (hh, mm) and self._last_fired != slot:
            self._last_fired = slot
            log.info("Analyse automatique programmée (%s)", slot)
            try:
                engine.start_analysis("auto", apply=True)
            except Busy:
                log.warning("Analyse déjà en cours : passage automatique ignoré")
