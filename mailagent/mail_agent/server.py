"""Point d'entrée de l'add-on : python -m mail_agent.server

Démarre le serveur web du panneau, le planificateur, le pont Home Assistant
(MQTT + notifications) et le rafraîchissement des importants non lus.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from . import store
from .config import DATA_DIR
from .engine import engine
from .ha import HABridge, ImportantRefresher, in_addon
from .scheduler import Scheduler

OPTIONS_FILE = Path("/data/options.json")  # options de l'add-on saisies dans HA


def load_options() -> dict:
    """Clé Gemini : options de l'add-on dans HA, ou fichier .env en local."""
    if OPTIONS_FILE.exists():
        options = json.loads(OPTIONS_FILE.read_text())
        if options.get("gemini_api_key"):
            os.environ["GEMINI_API_KEY"] = options["gemini_api_key"]
        return options
    load_dotenv(DATA_DIR / ".env")
    return {}


def main() -> None:
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("google_genai.types").setLevel(logging.ERROR)
    log = logging.getLogger("mail_agent")
    log.info("Données dans %s", DATA_DIR)

    store.init()
    HABridge(engine).start()
    Scheduler().start()
    ImportantRefresher(engine).start()

    port = int(os.environ.get("PORT", "8099"))
    # Dans l'add-on, le port n'est joignable que par le proxy ingress de HA (réseau Docker
    # interne) ; en local, on n'écoute que sur la machine elle-même.
    host = "0.0.0.0" if in_addon() else "127.0.0.1"
    log.info("Panneau : http://%s:%s/", "localhost" if host == "127.0.0.1" else host, port)
    uvicorn.run("mail_agent.web:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
