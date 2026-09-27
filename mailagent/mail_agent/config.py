"""Emplacement des données et chargement/sauvegarde de la configuration.

Toutes les données vivent dans DATA_DIR :
- en local (développement) : la racine du dépôt ;
- dans l'add-on Home Assistant : /data (persistant, privé à l'add-on).

    DATA_DIR/config/profile.yaml   réglages (profils, règles, planning…)
    DATA_DIR/credentials.json      identifiant OAuth Google (Desktop app)
    DATA_DIR/tokens/<email>.json   jeton Gmail de chaque compte
    DATA_DIR/data/mailagent.db     historique des analyses (SQLite)
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MAILAGENT_DATA", PACKAGE_DIR.parent.parent))
PROFILE_PATH = DATA_DIR / "config" / "profile.yaml"
EXAMPLE_PROFILE = PACKAGE_DIR / "profile.example.yaml"

# Prix du palier payant Gemini ($ par million de tokens, sortie = réponse + réflexion).
# Sert uniquement à estimer le "coût équivalent" ; modifiable dans usage.prices.
DEFAULT_PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}

NOISE_CATEGORIES = ("promo", "newsletter", "notification", "spam")

_lock = threading.Lock()


def load_config(path: Path = PROFILE_PATH) -> dict[str, Any]:
    if not path.exists():
        if os.environ.get("MAILAGENT_DATA"):
            # Add-on : premier démarrage, on part de l'exemple
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(EXAMPLE_PROFILE, path)
        else:
            raise SystemExit(
                f"Config introuvable : {path}\n"
                f"Copie {EXAMPLE_PROFILE} vers {path} et personnalise-le."
            )
    with _lock, path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    config.setdefault("accounts", [])
    config.setdefault("rules", {})
    config.setdefault("schedule", {"enabled": False, "time": "07:30"})
    config.setdefault("notifications", {"service": ""})
    config.setdefault("usage", {"daily_token_alert": 500000})
    return config


def save_config(config: dict[str, Any], path: Path = PROFILE_PATH) -> None:
    """Écrit la config de façon atomique (fichier temporaire puis renommage).

    Attention : les commentaires du YAML ne sont pas conservés.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False, width=100)
        tmp.replace(path)


def validate_config(config: dict[str, Any]) -> None:
    """Lève ValueError si la config est inutilisable."""
    if not isinstance(config, dict):
        raise ValueError("La configuration doit être un objet YAML.")
    for key in ("llm", "profile"):
        if key not in config:
            raise ValueError(f"Section « {key} » manquante.")
    for account in config.get("accounts", []):
        if "@" not in str(account.get("email", "")):
            raise ValueError(f"Compte invalide : {account!r}")
    time = str(config.get("schedule", {}).get("time", "07:30"))
    hh, _, mm = time.partition(":")
    if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
        raise ValueError(f"Heure invalide : {time} (format HH:MM attendu)")


def token_prices(config: dict[str, Any]) -> tuple[float, float]:
    """(prix entrée, prix sortie) en $ par million de tokens."""
    custom = config.get("usage", {}).get("prices")
    if custom:
        return float(custom["input_per_m"]), float(custom["output_per_m"])
    return DEFAULT_PRICES.get(config["llm"]["model"], (0.30, 2.50))


ACCOUNT_OVERRIDES = ("query", "max_messages", "label_prefix")


def account_settings(config: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    """Fusionne les valeurs par défaut (section gmail + profile) avec celles du compte."""
    gmail = config.get("gmail", {})
    settings = {key: account.get(key) or gmail.get(key) for key in ACCOUNT_OVERRIDES}
    settings["query"] = settings["query"] or "in:inbox newer_than:3d"
    settings["max_messages"] = settings["max_messages"] or 100
    settings["label_prefix"] = settings["label_prefix"] or "IA"
    settings["profile"] = account.get("profile") or config["profile"]
    return settings
