"""Tiny SQLite layer. No ORM — stdlib sqlite3 is plenty for one user."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings
from .crypto import decrypt_json, encrypt_json

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


_conn = _connect()


def init_db() -> None:
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                provider   TEXT PRIMARY KEY,
                blob       BLOB NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_samples (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                provider   TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                label      TEXT NOT NULL,
                utilization REAL NOT NULL,
                resets_at  TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_samples_lookup
                ON usage_samples (provider, metric_key, fetched_at);

            CREATE TABLE IF NOT EXISTS notify_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        _conn.commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


# ---- Credentials ----

def save_credentials(provider: str, data: dict[str, Any]) -> None:
    blob = encrypt_json(data)
    with tx() as c:
        c.execute(
            "INSERT INTO credentials (provider, blob, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(provider) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
            (provider, blob),
        )


def get_credentials(provider: str) -> dict[str, Any] | None:
    with tx() as c:
        row = c.execute("SELECT blob FROM credentials WHERE provider=?", (provider,)).fetchone()
    if not row:
        return None
    try:
        return decrypt_json(row["blob"])
    except Exception:
        return None


def list_credential_providers() -> list[str]:
    with tx() as c:
        rows = c.execute("SELECT provider FROM credentials").fetchall()
    return [r["provider"] for r in rows]


def delete_credentials(provider: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM credentials WHERE provider=?", (provider,))


# ---- Usage samples ----

def insert_sample(provider: str, metric_key: str, label: str,
                  utilization: float, resets_at: str | None, fetched_at: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO usage_samples (provider, metric_key, label, utilization, resets_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, metric_key, label, utilization, resets_at, fetched_at),
        )


def latest_samples() -> list[dict[str, Any]]:
    """Most recent sample for each (provider, metric_key)."""
    with tx() as c:
        rows = c.execute(
            """
            SELECT s.* FROM usage_samples s
            JOIN (
                SELECT provider, metric_key, MAX(id) AS max_id
                FROM usage_samples GROUP BY provider, metric_key
            ) m ON s.id = m.max_id
            ORDER BY s.provider, s.metric_key
            """
        ).fetchall()
    return [dict(r) for r in rows]


def history(provider: str, metric_key: str, hours: int) -> list[dict[str, Any]]:
    with tx() as c:
        rows = c.execute(
            "SELECT utilization, fetched_at FROM usage_samples "
            "WHERE provider=? AND metric_key=? AND fetched_at >= datetime('now', ?) "
            "ORDER BY fetched_at",
            (provider, metric_key, f"-{int(hours)} hours"),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_old_samples(days: int = 30) -> None:
    with tx() as c:
        c.execute("DELETE FROM usage_samples WHERE fetched_at < datetime('now', ?)", (f"-{int(days)} days",))


# ---- Notify dedup state ----

def get_state(key: str) -> str | None:
    with tx() as c:
        row = c.execute("SELECT value FROM notify_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str | None) -> None:
    with tx() as c:
        if value is None:
            c.execute("DELETE FROM notify_state WHERE key=?", (key,))
        else:
            c.execute(
                "INSERT INTO notify_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
