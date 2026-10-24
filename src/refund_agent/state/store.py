"""Saves each run (status, facts, transcript, trace) so a run can pause for approval and resume later."""
from __future__ import annotations

import sqlite3
import threading

from ..domain.models import Run, RunStatus


class RunNotFound(KeyError):
    pass


class RunStore:
    def __init__(self, db_path: str = ":memory:"):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                " run_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL, body TEXT NOT NULL)")
            self._db.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def save(self, run: Run) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (run_id, status, updated_at, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
                "body=excluded.body",
                (run.run_id, run.status.value, run.updated_at.isoformat(), run.model_dump_json()))
            self._db.commit()

    def get(self, run_id: str) -> Run:
        with self._lock:
            row = self._db.execute("SELECT body FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return Run.model_validate_json(row[0])

    def list(self, status: RunStatus | None = None, limit: int = 50) -> list[Run]:
        sql, args = "SELECT body FROM runs", []
        if status:
            sql += " WHERE status = ?"
            args.append(status.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [Run.model_validate_json(r[0]) for r in rows]
