"""Accès à Gmail via l'API officielle (OAuth).

Une seule app OAuth (credentials.json, type « Desktop app ») sert pour tous les
comptes ; chaque compte a son jeton dans tokens/<adresse>.json.

Deux façons de connecter un compte :
- en local (CLI) : un navigateur s'ouvre et renvoie vers http://localhost ;
- depuis le panneau Home Assistant : il n'y a pas de navigateur sur le Pi, donc
  l'utilisateur ouvre le lien d'autorisation, puis recopie dans le panneau
  l'adresse http://localhost/?code=... sur laquelle Google l'a redirigé (la page
  ne s'affiche pas, c'est normal : seul le code dans l'adresse nous intéresse).
"""
from __future__ import annotations

import html
import json
import logging
from typing import Optional
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import DATA_DIR
from .models import Email

log = logging.getLogger(__name__)

# gmail.modify : lire + gérer les labels + marquer lu. N'autorise PAS la suppression
# définitive ni l'envoi de mails : moindre privilège.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
TOKENS_DIR = DATA_DIR / "tokens"
WEB_REDIRECT_URI = "http://localhost:8765/"

HEADERS = ["From", "Subject", "Date", "List-Unsubscribe"]


class NotConnected(Exception):
    """Le compte n'a pas (ou plus) de jeton valide : il faut le (re)connecter."""


def _token_file(email: str):
    return TOKENS_DIR / f"{email.lower()}.json"


def _check_credentials_file() -> None:
    if not CREDENTIALS_FILE.exists():
        raise NotConnected("credentials.json absent : ajoute l'identifiant OAuth Google.")
    if "installed" not in json.loads(CREDENTIALS_FILE.read_text()):
        raise NotConnected(
            "credentials.json n'est pas un identifiant de type « Desktop app ». "
            "Recrée-le dans Google Cloud (Credentials → OAuth client ID → Desktop app)."
        )


def _save_verified(email: str, creds: Credentials) -> None:
    """Vérifie que le jeton correspond bien au compte attendu, puis l'enregistre."""
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    actual = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
    if actual != email.lower():
        raise ValueError(f"Connecté à {actual} au lieu de {email}. Recommence avec le bon compte.")
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    _token_file(email).write_text(creds.to_json())


def load_credentials(email: str) -> Credentials:
    token_file = _token_file(email)
    if not token_file.exists():
        raise NotConnected(f"{email} n'est pas connecté.")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise NotConnected(f"Jeton de {email} invalide : reconnecte le compte.")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Typiquement : app OAuth en mode "Testing" (expiration à 7 jours) ou accès révoqué
            raise NotConnected(f"Jeton de {email} expiré ou révoqué : reconnecte le compte. ({e})")
        token_file.write_text(creds.to_json())
    return creds


def account_status(email: str) -> str:
    """'connected', 'disconnected' ou 'expired'."""
    if not _token_file(email).exists():
        return "disconnected"
    try:
        load_credentials(email)
        return "connected"
    except NotConnected:
        return "expired"
    except Exception:  # réseau indisponible, etc. : on ne conclut pas à l'expiration
        return "connected"


def connect_interactive(email: str) -> None:
    """Connexion en local : ouvre un navigateur (utilisé par la ligne de commande)."""
    _check_credentials_file()
    print(f"Autorisation requise pour {email} : choisis CE compte dans le navigateur.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, login_hint=email)
    _save_verified(email, creds)


# --- Connexion depuis le panneau (copier-coller de l'URL de retour) ------------

_pending_flows: dict[str, InstalledAppFlow] = {}


def start_web_auth(email: str) -> str:
    _check_credentials_file()
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE), SCOPES, redirect_uri=WEB_REDIRECT_URI
    )
    url, _state = flow.authorization_url(
        access_type="offline", prompt="consent", login_hint=email
    )
    _pending_flows[email.lower()] = flow  # garde le code_verifier (PKCE) pour l'étape 2
    return url


def finish_web_auth(email: str, pasted: str) -> None:
    flow = _pending_flows.get(email.lower())
    if flow is None:
        raise ValueError("Aucune connexion en cours pour ce compte : clique d'abord sur « Connecter ».")
    pasted = pasted.strip()
    code = parse_qs(urlparse(pasted).query).get("code", [None])[0] if "code=" in pasted else pasted
    if not code:
        raise ValueError("Code introuvable dans l'adresse collée.")
    flow.fetch_token(code=code)
    _save_verified(email, flow.credentials)
    _pending_flows.pop(email.lower(), None)


def import_token(email: str, token_json: str) -> None:
    """Importe un jeton déjà obtenu ailleurs (ex. tokens/<email>.json du Mac)."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    _save_verified(email, creds)


def save_credentials_file(content: str) -> None:
    data = json.loads(content)
    if "installed" not in data:
        raise ValueError("Ce n'est pas un identifiant « Desktop app » (clé 'installed' absente).")
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(data))


def credentials_present() -> bool:
    return CREDENTIALS_FILE.exists()


# --- Client ------------------------------------------------------------------


class GmailClient:
    def __init__(self, email: str, interactive: bool = False):
        self.email = email
        try:
            creds = load_credentials(email)
        except NotConnected:
            if not interactive:
                raise
            connect_interactive(email)
            creds = load_credentials(email)
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._labels: Optional[dict[str, str]] = None  # nom -> id

    def fetch(self, query: str = "", max_messages: int = 100, label_ids: Optional[list[str]] = None) -> list[Email]:
        """Récupère les en-têtes + extrait (snippet) des mails correspondants.

        On ne télécharge volontairement PAS le corps complet : l'extrait de ~200
        caractères suffit pour classer, et limite ce qu'on envoie au LLM.
        """
        ids: list[str] = []
        params = {"userId": "me", "maxResults": min(max_messages, 500)}
        if query:
            params["q"] = query
        if label_ids:
            params["labelIds"] = label_ids
        request = self.service.users().messages().list(**params)
        while request is not None and len(ids) < max_messages:
            resp = request.execute()
            ids += [m["id"] for m in resp.get("messages", [])]
            request = self.service.users().messages().list_next(request, resp)
        return [self.get(msg_id) for msg_id in ids[:max_messages]]

    def get(self, msg_id: str) -> Email:
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=HEADERS)
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        return Email(
            id=msg_id,
            sender=headers.get("From", ""),
            subject=headers.get("Subject", "(sans objet)"),
            # Gmail renvoie l'extrait encodé en HTML (&#39; pour une apostrophe…)
            snippet=html.unescape(msg.get("snippet", "")),
            date=headers.get("Date", ""),
            has_unsubscribe="List-Unsubscribe" in headers,
            label_ids=msg.get("labelIds", []),
        )

    # --- Labels -----------------------------------------------------------

    def _label_map(self) -> dict[str, str]:
        if self._labels is None:
            resp = self.service.users().labels().list(userId="me").execute()
            self._labels = {l["name"]: l["id"] for l in resp.get("labels", [])}
        return self._labels

    def label_id(self, name: str, create: bool = True) -> Optional[str]:
        labels = self._label_map()
        if name not in labels:
            if not create:
                return None
            created = self.service.users().labels().create(userId="me", body={"name": name}).execute()
            labels[name] = created["id"]
        return labels[name]

    def modify(
        self,
        msg_ids: list[str],
        add: list[str] = (),
        remove: list[str] = (),
        mark_read: bool = False,
        mark_unread: bool = False,
    ) -> None:
        """Ajoute/retire des labels (par nom) sur plusieurs mails en une requête."""
        if not msg_ids:
            return
        body: dict = {
            "addLabelIds": [self.label_id(n) for n in add],
            "removeLabelIds": [i for i in (self.label_id(n, create=False) for n in remove) if i],
        }
        if mark_read:
            body["removeLabelIds"].append("UNREAD")
        if mark_unread:
            body["addLabelIds"].append("UNREAD")
        for i in range(0, len(msg_ids), 1000):  # limite de l'API batchModify
            self.service.users().messages().batchModify(
                userId="me", body={"ids": msg_ids[i : i + 1000], **body}
            ).execute()
