"""Serveur du panneau (FastAPI) : API JSON + page web statique.

Dans Home Assistant, le panneau est servi via « ingress » : HA authentifie
l'utilisateur puis relaie les requêtes vers l'add-on sous une URL du type
/api/hassio_ingress/<jeton>/. D'où deux précautions :
- la page n'utilise que des chemins RELATIFS (« api/status », pas « /api/status ») ;
- on refuse toute requête qui ne vient pas du proxy ingress du Supervisor.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ha, store
from .config import DEFAULT_PRICES, load_config, save_config, token_prices, validate_config
from .engine import Busy, engine
from .gmail_client import (
    credentials_present,
    finish_web_auth,
    import_token,
    save_credentials_file,
    start_web_auth,
)
from .models import Category

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
INGRESS_PROXY_IP = "172.30.32.2"

app = FastAPI(title="Mail Agent", docs_url=None, redoc_url=None)


@app.middleware("http")
async def only_from_ingress(request: Request, call_next):
    if ha.in_addon() and request.client and request.client.host != INGRESS_PROXY_IP:
        return PlainTextResponse("Accès réservé au panneau Home Assistant", status_code=403)
    return await call_next(request)


@app.exception_handler(Busy)
async def busy_handler(request: Request, exc: Busy):
    return JSONResponse({"detail": str(exc)}, status_code=409)


# --- Page ------------------------------------------------------------------------


@app.get("/")
def index():
    """La page référence les fichiers statiques avec une empreinte de leur contenu
    (static/app.js?v=…) : après une mise à jour de l'add-on, le navigateur ne peut
    pas réutiliser une ancienne version gardée en cache."""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        digest = hashlib.sha256((WEB_DIR / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"static/{name}"', f'"static/{name}?v={digest}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# --- État & analyses ------------------------------------------------------------


@app.get("/api/status")
def status() -> dict[str, Any]:
    return engine.state()


class AnalyzeBody(BaseModel):
    apply: bool = False


@app.post("/api/analyze")
def analyze(body: AnalyzeBody) -> dict[str, Any]:
    return {"run_id": engine.start_analysis("manual", apply=body.apply)}


@app.get("/api/runs")
def runs(limit: int = 30) -> list[dict[str, Any]]:
    return store.list_runs(limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Analyse introuvable")
    return {"run": run, "decisions": store.run_decisions(run_id)}


@app.post("/api/runs/{run_id}/apply")
def apply(run_id: int) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Analyse introuvable")
    if run["status"] == "running":
        raise HTTPException(409, "Analyse encore en cours")
    engine.apply_run(run_id)
    return {"ok": True}


@app.get("/api/important")
def important(refresh: bool = False) -> list[dict[str, Any]]:
    return engine.refresh_important() if refresh else engine.important_unread


class CorrectBody(BaseModel):
    category: str


@app.post("/api/decisions/{decision_id}/correct")
def correct(decision_id: int, body: CorrectBody) -> dict[str, Any]:
    try:
        return engine.correct(decision_id, body.category)
    except KeyError:
        raise HTTPException(404, "Décision introuvable")
    except ValueError:
        raise HTTPException(400, "Catégorie inconnue")


class RuleBody(BaseModel):
    category: str
    pattern: str


@app.post("/api/rules")
def add_rule(body: RuleBody) -> dict[str, Any]:
    try:
        engine.add_rule(body.category, body.pattern.strip())
    except ValueError:
        raise HTTPException(400, "Catégorie inconnue")
    return {"ok": True}


class AutoBody(BaseModel):
    enabled: bool


@app.post("/api/auto")
def set_auto(body: AutoBody) -> dict[str, Any]:
    engine.set_auto(body.enabled)
    return {"ok": True}


# --- Consommation ------------------------------------------------------------------


@app.get("/api/usage")
def usage(days: int = 30) -> dict[str, Any]:
    config = load_config()
    price_in, price_out = token_prices(config)
    return {
        "days": store.daily_usage(days),
        "runs": store.list_runs(50),
        "price_input_per_m": price_in,
        "price_output_per_m": price_out,
        "daily_token_alert": config["usage"].get("daily_token_alert") or 0,
        "model": config["llm"]["model"],
    }


# --- Réglages -----------------------------------------------------------------------


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {
        "config": load_config(),
        "categories": [c.value for c in Category],
        "models": list(DEFAULT_PRICES),
        "notify_services": ha.notify_services(),
        "in_addon": ha.in_addon(),
    }


@app.put("/api/settings")
def put_settings(config: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_config(config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    save_config(config)
    engine._state_changed()
    return {"ok": True}


@app.get("/api/settings/yaml", response_class=PlainTextResponse)
def get_settings_yaml() -> str:
    return yaml.safe_dump(load_config(), allow_unicode=True, sort_keys=False, width=100)


@app.put("/api/settings/yaml")
async def put_settings_yaml(request: Request) -> dict[str, Any]:
    text = (await request.body()).decode("utf-8")
    try:
        config = yaml.safe_load(text)
        validate_config(config)
    except (yaml.YAMLError, ValueError) as e:
        raise HTTPException(400, f"YAML invalide : {e}")
    save_config(config)
    engine._state_changed()
    return {"ok": True}


# --- Comptes Gmail ---------------------------------------------------------------------


@app.get("/api/accounts")
def accounts() -> dict[str, Any]:
    return {"credentials_present": credentials_present(), "accounts": engine.state()["accounts"]}


class TextBody(BaseModel):
    text: str


@app.post("/api/credentials")
def upload_credentials(body: TextBody) -> dict[str, Any]:
    try:
        save_credentials_file(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/accounts/{email}/auth-url")
def auth_url(email: str) -> dict[str, Any]:
    try:
        return {"url": start_web_auth(email)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/accounts/{email}/auth-code")
def auth_code(email: str, body: TextBody) -> dict[str, Any]:
    try:
        finish_web_auth(email, body.text)
    except Exception as e:
        raise HTTPException(400, str(e))
    engine.refresh_important()
    return {"ok": True}


@app.post("/api/accounts/{email}/token")
def upload_token(email: str, body: TextBody) -> dict[str, Any]:
    try:
        import_token(email, body.text)
    except Exception as e:
        raise HTTPException(400, str(e))
    engine.refresh_important()
    return {"ok": True}
