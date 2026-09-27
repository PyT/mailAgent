from __future__ import annotations

from typing import Any

from .base import LLMClassifier


def get_classifier(llm_config: dict[str, Any]) -> LLMClassifier:
    """Fabrique : choisit le fournisseur selon la config. Ajouter un fournisseur =
    écrire une classe qui implémente LLMClassifier et l'ajouter ici."""
    provider = llm_config.get("provider", "gemini")
    if provider == "gemini":
        from .gemini import GeminiClassifier

        return GeminiClassifier(model=llm_config["model"])
    raise ValueError(f"Fournisseur LLM inconnu : {provider}")
