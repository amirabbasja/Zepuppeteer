"""Durable execution history. Reading history never creates a database."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .config import json_text
from .models import Workflow


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, workflow TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, artifact_dir TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile_runs (
                run_id TEXT NOT NULL REFERENCES runs(id), profile TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT,
                PRIMARY KEY (run_id, profile)
            );
            CREATE TABLE IF NOT EXISTS step_runs (
                run_id TEXT NOT NULL, profile TEXT NOT NULL, step_id TEXT NOT NULL,
                task TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT, finished_at TEXT, outputs_json TEXT NOT NULL DEFAULT '{}',
                message TEXT NOT NULL DEFAULT '', error TEXT, artifact_dir TEXT,
                PRIMARY KEY (run_id, profile, step_id),
                FOREIGN KEY (run_id, profile) REFERENCES profile_runs(run_id, profile)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def begin_run(self, run_id: str, workflow: Workflow, profiles, artifact_dir: Path) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO runs VALUES (?, ?, 'running', ?, NULL, ?)",
                                    (run_id, workflow.name, now(), str(artifact_dir)))
            for profile in profiles:
                self.connection.execute("INSERT INTO profile_runs (run_id, profile, status) VALUES (?, ?, 'pending')",
                                        (run_id, profile.name))
                self.connection.executemany(
                    "INSERT INTO step_runs (run_id, profile, step_id, task, status) VALUES (?, ?, ?, ?, 'pending')",
                    [(run_id, profile.name, step.id, step.task) for step in workflow.steps])

    def start_profile(self, run_id: str, profile: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE profile_runs SET status='running', started_at=? WHERE run_id=? AND profile=?",
                                    (now(), run_id, profile))

    def finish_profile(self, run_id: str, profile: str, status: str, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute("UPDATE profile_runs SET status=?, finished_at=?, error=? WHERE run_id=? AND profile=?",
                                    (status, now(), error, run_id, profile))

    def start_step(self, run_id: str, profile: str, step_id: str, artifact_dir: Path) -> None:
        with self.connection:
            self.connection.execute("""UPDATE step_runs SET status='running', attempts=attempts+1,
                started_at=?, finished_at=NULL, error=NULL, artifact_dir=?
                WHERE run_id=? AND profile=? AND step_id=?""",
                (now(), str(artifact_dir), run_id, profile, step_id))

    def finish_step(self, run_id: str, profile: str, step_id: str, status: str, *,
                    outputs: dict | None = None, message: str = "", error: str | None = None) -> None:
        encoded = json_text(outputs if outputs is not None else {})
        with self.connection:
            self.connection.execute("""UPDATE step_runs SET status=?, finished_at=?, outputs_json=?, message=?, error=?
                WHERE run_id=? AND profile=? AND step_id=?""",
                (status, now(), encoded, message, error, run_id, profile, step_id))

    def skip_pending(self, run_id: str, profile: str, reason: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE step_runs SET status='skipped', finished_at=?, message=?
                WHERE run_id=? AND profile=? AND status='pending'""", (now(), reason, run_id, profile))

    def cancel_running(self, run_id: str, profile: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE step_runs SET status='cancelled', finished_at=?, error='Cancelled'
                WHERE run_id=? AND profile=? AND status='running'""", (now(), run_id, profile))

    def fail_running(self, run_id: str, profile: str, error: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE step_runs SET status='failed', finished_at=?, error=?
                WHERE run_id=? AND profile=? AND status='running'""", (now(), error, run_id, profile))

    def finish_run(self, run_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?", (status, now(), run_id))
            self.connection.execute("""UPDATE profile_runs SET status='skipped', finished_at=?, error='Run stopped before this profile'
                WHERE run_id=? AND status='pending'""", (now(), run_id))
            self.connection.execute("""UPDATE step_runs SET status='skipped', finished_at=?, message='Run stopped before this step'
                WHERE run_id=? AND status='pending'""", (now(), run_id))


def read_history(path: str | Path, *, limit: int = 20, run_id: str | None = None):
    if not 1 <= limit <= 1000:
        raise ValueError("History limit must be between 1 and 1000.")
    path = Path(path).expanduser().resolve()
    if not path.exists():
        if run_id:
            raise ValueError(f"Unknown run: {run_id}")
        return []
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if run_id is None:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))]
        row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id}")
        result = dict(row)
        result["profiles"] = [dict(item) for item in connection.execute(
            "SELECT * FROM profile_runs WHERE run_id=? ORDER BY rowid", (run_id,))]
        result["steps"] = []
        for row in connection.execute("SELECT * FROM step_runs WHERE run_id=? ORDER BY rowid", (run_id,)):
            step = dict(row)
            step["outputs"] = json.loads(step.pop("outputs_json"))
            result["steps"].append(step)
        return result
    finally:
        connection.close()
