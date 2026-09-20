from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class VerificationCheck:
    name: str
    command: str
    cwd: Path


@dataclass
class TaskSpec:
    title: str
    prompt: str
    workspace: Path
    harness: str = "auto"
    constraints: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    workspace: str
    harness: str
    status: TaskStatus
    created_at: str
    acceptance: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]]
    failures: list[str]


class HohStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                workspace TEXT NOT NULL,
                harness TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id TEXT NOT NULL,
                depends_on TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on)
            );
            CREATE TABLE IF NOT EXISTS task_specs (
                task_id TEXT PRIMARY KEY,
                acceptance_json TEXT NOT NULL,
                constraints_json TEXT NOT NULL,
                worktree TEXT,
                branch TEXT,
                baseline_sha TEXT,
                verification_json TEXT
            );
            """
        )
        self.db.commit()

    def create_task(self, spec: TaskSpec) -> Task:
        task = Task(
            id=spec.task_id,
            title=spec.title,
            prompt=spec.prompt,
            workspace=str(spec.workspace),
            harness=spec.harness,
            status=TaskStatus.PENDING,
            created_at=now(),
            acceptance=spec.acceptance,
            constraints=spec.constraints,
            depends_on=spec.depends_on,
        )
        with self.lock:
            self.db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task.id, task.title, task.prompt, task.workspace, task.harness,
                 task.status.value, task.created_at),
            )
            self._event(task.id, "task.created", {"title": task.title, "harness": task.harness})
            self.db.execute(
                "INSERT INTO task_specs(task_id, acceptance_json, constraints_json) VALUES (?, ?, ?)",
                (task.id, json.dumps(spec.acceptance), json.dumps(spec.constraints)),
            )
            for dependency in spec.depends_on:
                self._assert_task_exists(dependency)
                self.db.execute(
                    "INSERT INTO task_dependencies(task_id, depends_on) VALUES (?, ?)",
                    (task.id, dependency),
                )
            self._assert_acyclic()
            self.db.commit()
        return task

    def transition(self, task_id: str, status: TaskStatus, payload: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status.value, task_id))
            self._event(task_id, "task.status_changed", {"status": status.value, **(payload or {})})
            self.db.commit()

    def task_spec(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT acceptance_json, constraints_json, worktree, branch, baseline_sha, verification_json FROM task_specs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            return {
                "acceptance": json.loads(row["acceptance_json"] or "[]"),
                "constraints": json.loads(row["constraints_json"] or "[]"),
                "worktree": row["worktree"],
                "branch": row["branch"],
                "baseline_sha": row["baseline_sha"],
                "verification": json.loads(row["verification_json"]) if row["verification_json"] else None,
            }

    def set_execution(self, task_id: str, *, worktree: Path, branch: str, baseline_sha: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE task_specs SET worktree = ?, branch = ?, baseline_sha = ? WHERE task_id = ?",
                (str(worktree), branch, baseline_sha, task_id),
            )
            self.db.commit()

    def set_verification(self, task_id: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE task_specs SET verification_json = ? WHERE task_id = ?",
                (json.dumps(result, ensure_ascii=False), task_id),
            )
            self._event(task_id, "task.verification", result)
            self.db.commit()

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        with self.lock:
            self._assert_task_exists(task_id)
            self._assert_task_exists(depends_on)
            self.db.execute(
                "INSERT OR IGNORE INTO task_dependencies(task_id, depends_on) VALUES (?, ?)",
                (task_id, depends_on),
            )
            try:
                self._assert_acyclic()
            except ValueError:
                self.db.execute(
                    "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on = ?",
                    (task_id, depends_on),
                )
                self.db.commit()
                raise
            self.db.commit()

    def ready_tasks(self) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?) ORDER BY created_at",
                (TaskStatus.PENDING.value, TaskStatus.READY.value),
            ).fetchall()
            ready: list[str] = []
            for row in rows:
                blockers = self.db.execute(
                    """SELECT t.status FROM task_dependencies d
                       JOIN tasks t ON t.id = d.depends_on
                       WHERE d.task_id = ? AND t.status != ?""",
                    (row["id"], TaskStatus.COMPLETED.value),
                ).fetchall()
                if not blockers:
                    ready.append(row["id"])
            return ready

    def get_task(self, task_id: str) -> Task:
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return self._task_from_row(row)

    def list_tasks(self) -> list[Task]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            return [self._task_from_row(row) for row in rows]

    def dependencies(self, task_id: str) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ? ORDER BY depends_on",
                (task_id,),
            ).fetchall()
            return [row["depends_on"] for row in rows]

    def _task_from_row(self, row: Any) -> Task:
        """Build the full read model: dependencies and acceptance included.

        Dependencies and acceptance live in side tables, so a task read that
        skips them silently reports an empty DAG and no acceptance criteria to
        every client. Keep this the single place that assembles a Task.
        """
        spec = self.db.execute(
            "SELECT acceptance_json, constraints_json FROM task_specs WHERE task_id = ?",
            (row["id"],),
        ).fetchone()
        return Task(
            id=row["id"], title=row["title"], prompt=row["prompt"],
            workspace=row["workspace"], harness=row["harness"],
            status=TaskStatus(row["status"]), created_at=row["created_at"],
            acceptance=json.loads(spec["acceptance_json"] or "[]") if spec else [],
            constraints=json.loads(spec["constraints_json"] or "[]") if spec else [],
            depends_on=self.dependencies(row["id"]),
        )

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return self.events_after(task_id, 0)

    def events_after(self, task_id: str, event_id: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id",
                (task_id, event_id),
            ).fetchall()
            return [
                {"id": row["id"], "type": row["type"], "payload": json.loads(row["payload_json"]),
                 "created_at": row["created_at"]}
                for row in rows
            ]

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _assert_task_exists(self, task_id: str) -> None:
        row = self.db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)

    def _assert_acyclic(self) -> None:
        edges: dict[str, list[str]] = {}
        for row in self.db.execute("SELECT task_id, depends_on FROM task_dependencies"):
            edges.setdefault(row["task_id"], []).append(row["depends_on"])
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("task dependency cycle detected")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges.get(node, []):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in edges:
            visit(node)

    def _event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, json.dumps(payload, ensure_ascii=False), now()),
        )


def verify_workspace(
    root: Path,
    checks: list[VerificationCheck],
    changed_files: list[Path],
) -> VerificationResult:
    root = Path(root).resolve()
    failures: list[str] = []
    results: list[dict[str, Any]] = []

    for changed in changed_files:
        resolved = Path(changed).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            failures.append(f"{resolved} is outside workspace {root}")

    for check in checks:
        cwd = Path(check.cwd).resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            failures.append(f"verification cwd {cwd} is outside workspace {root}")
            continue
        completed = subprocess.run(
            check.command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=os.environ.copy(),
        )
        output = (completed.stdout + completed.stderr).strip()
        results.append({
            "name": check.name,
            "command": check.command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "output": output,
        })
        if completed.returncode != 0:
            failures.append(f"{check.name} exited with {completed.returncode}")

    return VerificationResult(passed=not failures, checks=results, failures=failures)
