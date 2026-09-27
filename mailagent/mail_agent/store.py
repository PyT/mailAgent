"""Historique persistant (SQLite) : analyses, décisions, corrections, tokens."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import DATA_DIR
from .models import Decision, Usage

DB_PATH = DATA_DIR / "data" / "mailagent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,            -- manual | auto | ha
    status TEXT NOT NULL,             -- running | done | error | quota
    error TEXT,
    model TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    n_emails INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    account TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    sender TEXT, subject TEXT, snippet TEXT, date TEXT,
    unread INTEGER NOT NULL DEFAULT 0,
    has_unsubscribe INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL,
    original_category TEXT NOT NULL,  -- avant correction manuelle
    source TEXT NOT NULL,             -- rule | llm
    confidence REAL,
    reason TEXT,
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decisions_msg ON decisions(account, msg_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        # Une analyse interrompue (redémarrage de l'add-on) ne doit pas rester "en cours"
        c.execute("UPDATE runs SET status='error', error='interrompue' WHERE status='running'")


def _rows(rows) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# --- Analyses -------------------------------------------------------------------


def create_run(trigger: str, model: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO runs (started_at, trigger, status, model) VALUES (?, ?, 'running', ?)",
            (now(), trigger, model),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, usage: Usage, n_emails: int, error: Optional[str] = None) -> None:
    with connect() as c:
        c.execute(
            """UPDATE runs SET finished_at=?, status=?, error=?, input_tokens=?, output_tokens=?,
               requests=?, n_emails=? WHERE id=?""",
            (now(), status, error, usage.input_tokens, usage.output_tokens, usage.requests, n_emails, run_id),
        )


def mark_run_applied(run_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE runs SET applied=1, applied_at=? WHERE id=?", (now(), run_id))
        c.execute("UPDATE decisions SET applied=1 WHERE run_id=?", (run_id,))


def get_run(run_id: int) -> Optional[dict[str, Any]]:
    with connect() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))


def last_run(status: Optional[str] = None) -> Optional[dict[str, Any]]:
    with connect() as c:
        if status:
            row = c.execute("SELECT * FROM runs WHERE status=? ORDER BY id DESC LIMIT 1", (status,)).fetchone()
        else:
            row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# --- Décisions ------------------------------------------------------------------


def add_decisions(run_id: int, account: str, decisions: list[Decision]) -> None:
    with connect() as c:
        c.executemany(
            """INSERT INTO decisions (run_id, account, msg_id, sender, subject, snippet, date, unread,
               has_unsubscribe, category, original_category, source, confidence, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id, account, d.email.id, d.email.sender, d.email.subject, d.email.snippet,
                    d.email.date, int(d.email.unread), int(d.email.has_unsubscribe),
                    d.category.value, d.category.value, d.source, d.confidence, d.reason,
                )
                for d in decisions
            ],
        )


def run_decisions(run_id: int) -> list[dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY id", (run_id,)))


def get_decision(decision_id: int) -> Optional[dict[str, Any]]:
    with connect() as c:
        row = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return dict(row) if row else None


def set_category(decision_id: int, category: str) -> None:
    with connect() as c:
        c.execute("UPDATE decisions SET category=? WHERE id=?", (category, decision_id))


def applied_msg_ids(account: str) -> set[str]:
    """Mails déjà traités et appliqués : inutile de les renvoyer au LLM."""
    with connect() as c:
        rows = c.execute("SELECT DISTINCT msg_id FROM decisions WHERE account=? AND applied=1", (account,))
        return {r["msg_id"] for r in rows}


def latest_decisions(account: str, msg_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Dernière décision connue pour chacun de ces mails (pour expliquer un label)."""
    if not msg_ids:
        return {}
    with connect() as c:
        marks = ",".join("?" * len(msg_ids))
        rows = c.execute(
            f"SELECT * FROM decisions WHERE account=? AND msg_id IN ({marks}) ORDER BY id",
            [account, *msg_ids],
        )
        return {r["msg_id"]: dict(r) for r in rows}  # le plus récent écrase les anciens


# --- Consommation -----------------------------------------------------------------


def daily_usage(days: int = 30) -> list[dict[str, Any]]:
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with connect() as c:
        rows = c.execute(
            """SELECT substr(started_at, 1, 10) AS day, SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens, SUM(requests) AS requests,
                      COUNT(*) AS runs, SUM(status='quota') AS quota_errors
               FROM runs WHERE substr(started_at, 1, 10) >= ? GROUP BY day ORDER BY day""",
            (since,),
        )
        by_day = {r["day"]: dict(r) for r in rows}
    out = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        out.append(by_day.get(day) or {
            "day": day, "input_tokens": 0, "output_tokens": 0, "requests": 0, "runs": 0, "quota_errors": 0,
        })
    return out
