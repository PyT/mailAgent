"""Implémentation Gemini (SDK google-genai), avec sortie JSON structurée."""
from __future__ import annotations

import logging
import os
import time

from google import genai
from google.genai import errors, types

from ..models import Classification, ClassificationBatch, Email, QuotaExceeded, Usage
from .base import SYSTEM_PROMPT, LLMClassifier, build_user_prompt

log = logging.getLogger(__name__)


class GeminiClassifier(LLMClassifier):
    def __init__(self, model: str, max_retries: int = 4):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Clé API Gemini manquante (GEMINI_API_KEY / options de l'add-on).")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.max_retries = max_retries

    def classify(self, emails: list[Email], profile: str) -> tuple[list[Classification], Usage]:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(profile=profile),
            # Sortie structurée : Gemini est contraint de respecter ce schéma,
            # donc pas de parsing hasardeux de texte libre.
            response_mime_type="application/json",
            response_schema=ClassificationBatch,
            temperature=0,
        )
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=build_user_prompt(emails), config=config
                )
                meta = resp.usage_metadata
                usage = Usage(
                    input_tokens=(meta.prompt_token_count or 0) if meta else 0,
                    # Les tokens de "réflexion" sont facturés comme de la sortie
                    output_tokens=((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
                    if meta
                    else 0,
                    requests=1,
                )
                return ClassificationBatch.model_validate_json(resp.text).results, usage
            except errors.APIError as e:
                # 429 = quota dépassé (par minute : une attente suffit ; par jour : non)
                # 5xx = surcharge passagère
                if e.code in (429, 500, 503) and attempt < self.max_retries:
                    wait = 2 ** attempt * 10
                    log.warning("Gemini %s, nouvel essai dans %ss…", e.code, wait)
                    time.sleep(wait)
                    continue
                if e.code == 429:
                    raise QuotaExceeded(f"Quota Gemini épuisé : {e.message}") from e
                raise
        raise RuntimeError("inatteignable")
