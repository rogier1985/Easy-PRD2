from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import RunRecord


def default_data_dir() -> Path:
    override = os.getenv("EASY_PRD2_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "Easy PRD2"


class HistoryStore:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.data_dir / "runs"
        self.artifacts_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "history.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, run: RunRecord) -> None:
        run.updated_at = datetime.now(UTC)
        payload = run.model_dump_json()
        with self._connect() as db:
            db.execute(
                """INSERT INTO runs(id, created_at, updated_at, status, payload)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                   updated_at=excluded.updated_at, status=excluded.status, payload=excluded.payload""",
                (run.id, run.created_at.isoformat(), run.updated_at.isoformat(), run.status, payload),
            )

    def get(self, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row["payload"]) if row else None

    def list(self) -> list[RunRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC").fetchall()
        return [RunRecord.model_validate_json(row["payload"]) for row in rows]

    def delete(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        artifact = self.artifacts_dir / run_id
        if artifact.exists():
            import shutil

            shutil.rmtree(artifact)

    def clear(self) -> None:
        for run in self.list():
            self.delete(run.id)

    def write_artifact(self, run_id: str, filename: str, content: str) -> Path:
        destination = self.artifacts_dir / run_id
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / filename
        path.write_text(content, encoding="utf-8")
        return path

