"""Structures de données partagées par tout le projet."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    IMPORTANT = "important"            # demande une action / vient d'une vraie personne / admin
    PERSONNEL = "personnel"            # perso non urgent
    TRANSACTIONNEL = "transactionnel"  # commandes, livraisons, reçus, confirmations
    NOTIFICATION = "notification"      # réseaux sociaux, alertes d'applis
    NEWSLETTER = "newsletter"          # contenu éditorial
    PROMO = "promo"                    # publicité, soldes, codes promo
    SPAM = "spam"                      # arnaques, phishing, indésirables


@dataclass
class Email:
    id: str
    sender: str
    subject: str
    snippet: str
    date: str
    has_unsubscribe: bool = False     # présence du header List-Unsubscribe
    label_ids: list[str] = field(default_factory=list)

    @property
    def sender_address(self) -> str:
        """Extrait 'a@b.com' de 'Nom <a@b.com>'."""
        s = self.sender
        if "<" in s and ">" in s:
            s = s[s.index("<") + 1 : s.index(">")]
        return s.strip().lower()

    @property
    def sender_name(self) -> str:
        """Extrait 'Nom' de 'Nom <a@b.com>' (ou l'adresse s'il n'y a pas de nom)."""
        name = self.sender.split("<")[0].strip().strip('"')
        return name or self.sender_address

    @property
    def unread(self) -> bool:
        return "UNREAD" in self.label_ids


class Classification(BaseModel):
    """Ce que le LLM doit renvoyer pour chaque mail (sortie structurée)."""

    id: str = Field(description="Identifiant du mail, recopié tel quel")
    category: Category
    confidence: float = Field(ge=0, le=1, description="Confiance entre 0 et 1")
    reason: str = Field(description="Justification courte, en français")


class ClassificationBatch(BaseModel):
    results: list[Classification]


@dataclass
class Usage:
    """Consommation de tokens d'un ou plusieurs appels au LLM."""

    input_tokens: int = 0
    output_tokens: int = 0     # réponse + réflexion du modèle (facturées pareil)
    requests: int = 0

    def __iadd__(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.requests += other.requests
        return self


class QuotaExceeded(Exception):
    """Le fournisseur de LLM refuse la requête : quota (gratuit) épuisé."""


@dataclass
class Decision:
    """Résultat final pour un mail : qui a décidé (règle ou LLM) et quoi."""

    email: Email
    category: Category
    source: str                       # "rule" ou "llm"
    reason: str
    confidence: Optional[float] = None
