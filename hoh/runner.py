from __future__ import annotations

import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .core import HohStore, TaskStatus, VerificationCheck, verify_workspace
from .workspaces import Worktree, create_worktree, remove_worktree


@dataclass
class RunHandle:
    run_id: str
    task_id: str
    thread: threading.Thread
    cancel: threading.Event


class TaskRunner:
    def __init__(self, store: HohStore, home: Path):
        self.store = store
        self.home = Path(home)
        self.lock = threading.RLock()
        self.runs: dict[str, RunHandle] = {}
        self.auto_pilot = True

    def start_ready(self, harness: str = "mock") -> list[str]:
        return [self.start(task_id, harness) for task_id in self.store.ready_tasks()]

    def start(self, task_id: str, harness: str = "mock") -> str:
        task = self.store.get_task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.READY):
            raise ValueError(f"task is not startable: {task.status.value}")
        cancel = threading.Event()
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        thread = threading.Thread(
            target=self._run,
            args=(run_id, task_id, harness, cancel),
            daemon=True,
            name=f"hoh-{run_id}",
        )
        handle = RunHandle(run_id, task_id, thread, cancel)
        with self.lock:
            self.runs[run_id] = handle
        self.store.transition(task_id, TaskStatus.RUNNING, {"run_id": run_id, "harness": harness})
        thread.start()
        return run_id

    def cancel(self, run_id: str) -> None:
        with self.lock:
            handle = self.runs.get(run_id)
        if handle is None:
            raise KeyError(run_id)
        handle.cancel.set()
        self.store.transition(handle.task_id, TaskStatus.CANCELLED, {"run_id": run_id})

    def retry(self, task_id: str, harness: str | None = None) -> str:
        task = self.store.get_task(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError(f"task is not retryable: {task.status.value}")
        previous = self.store.events(task_id)
        selected = harness or task.harness
        for event in reversed(previous):
            if event["type"] == "task.status_changed" and event["payload"].get("harness"):
                selected = harness or event["payload"]["harness"]
                break
        self.store.transition(task_id, TaskStatus.READY, {"retry": True, "harness": selected})
        return self.start(task_id, selected)

    def approve(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        if task.status != TaskStatus.WAITING_APPROVAL:
            raise ValueError(f"task is not awaiting approval: {task.status.value}")
        spec = self.store.task_spec(task_id)
        if not spec["worktree"] or not spec["branch"]:
            raise ValueError("task has no verified worktree")
        worktree = Worktree(Path(task.workspace), Path(spec["worktree"]), spec["branch"])
        self._git(Path(task.workspace), "merge", "--no-ff", spec["branch"], "-m", f"HoH: {task.title}")
        merge_sha = self._git(Path(task.workspace), "rev-parse", "HEAD")
        remove_worktree(worktree)
        self.store.transition(task_id, TaskStatus.COMPLETED, {
            "merge_sha": merge_sha,
            "branch": spec["branch"],
            "worktree": spec["worktree"],
        })
        self._trigger_next_ready()
        return merge_sha

    def _run(self, run_id: str, task_id: str, harness: str, cancel: threading.Event) -> None:
        task = self.store.get_task(task_id)
        log_path = self.home / "runs" / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        worktree: Worktree | None = None
        execution_path = Path(task.workspace)
        try:
            if self._is_git_repo(execution_path):
                worktree = create_worktree(
                    execution_path,
                    self.home / "workspaces" / task_id,
                    f"hoh/{task_id}",
                )
                execution_path = worktree.path
                baseline_sha = self._git(execution_path, "rev-parse", "HEAD")
                self.store.set_execution(
                    task_id,
                    worktree=execution_path,
                    branch=worktree.branch,
                    baseline_sha=baseline_sha,
                )

            output, exit_code = self._execute(harness, task, execution_path, cancel, task_id)
            log_path.write_text(output, encoding="utf-8")
            if cancel.is_set():
                return
            if exit_code != 0:
                self.store.transition(task_id, TaskStatus.FAILED, {
                    "run_id": run_id, "exit_code": exit_code, "log": str(log_path),
                })
                return

            if worktree is None:
                self.store.transition(task_id, TaskStatus.COMPLETED, {
                    "run_id": run_id, "exit_code": exit_code, "log": str(log_path),
                })
                self._trigger_next_ready()
                return

            self.store.transition(task_id, TaskStatus.VERIFYING, {"run_id": run_id})
            changed_files = self._changed_files(execution_path)
            checks = [VerificationCheck(f"acceptance-{index}", command, execution_path)
                      for index, command in enumerate(self.store.task_spec(task_id)["acceptance"], 1)]
            verification = verify_workspace(execution_path, checks, changed_files)
            verification_payload = {
                "passed": verification.passed,
                "checks": verification.checks,
                "failures": verification.failures,
                "run_id": run_id,
                "workspace": str(execution_path),
                "branch": worktree.branch,
                "baseline_sha": baseline_sha,
                "changed_files": [str(path) for path in changed_files],
            }
            if verification.passed and changed_files:
                self._git(execution_path, "add", "--", ".")
                self._git(
                    execution_path,
                    "-c", "user.name=HoH Worker",
                    "-c", "user.email=hoh-worker@example.invalid",
                    "commit", "-m", f"HoH: {task.title}",
                )
                verification_payload["worker_commit"] = self._git(execution_path, "rev-parse", "HEAD")
            self.store.set_verification(task_id, verification_payload)
            final_status = TaskStatus.WAITING_APPROVAL if verification.passed else TaskStatus.FAILED
            self.store.transition(task_id, final_status, verification_payload)
        except Exception as exc:
            log_path.write_text(str(exc), encoding="utf-8")
            if not cancel.is_set():
                self.store.transition(task_id, TaskStatus.FAILED, {"run_id": run_id, "error": str(exc)})
        finally:
            with self.lock:
                self.runs.pop(run_id, None)

    def _trigger_next_ready(self) -> None:
        if not self.auto_pilot:
            return
        # 寻找已就绪的任务，按其本身定义的 harness 启动
        for next_id in self.store.ready_tasks():
            t = self.store.get_task(next_id)
            if t.status in (TaskStatus.PENDING, TaskStatus.READY):
                try:
                    self.start(next_id, t.harness)
                except Exception:
                    pass

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        return (path / ".git").exists()

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout.strip()

    @classmethod
    def _changed_files(cls, cwd: Path) -> list[Path]:
        output = cls._git(cwd, "status", "--porcelain")
        paths = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            raw = line[3:]
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            paths.append((cwd / raw).resolve())
        return paths

    def _execute(self, harness: str, task, cwd: Path, cancel: threading.Event, task_id: str) -> tuple[str, int]:
        if harness == "mock":
            output = f"[mock] 正在分析任务 {task.id}...\n[mock] 检查工作区: {cwd}\n[mock] 模拟执行完成：已处理 {task.title}\n"
            self.store._event(task_id, "worker.output", {"chunk": output})
            return output, 0
        if harness == "mock-write":
            (cwd / "hoh_mock_result.txt").write_text(f"completed {task.id}\n", encoding="utf-8")
            output = f"[mock-write] 正在写入代码产物...\n[mock-write] 已生成: {cwd / 'hoh_mock_result.txt'}\n"
            self.store._event(task_id, "worker.output", {"chunk": output})
            return output, 0
        if harness == "mock-fail":
            output = f"[mock-fail] 任务执行出现预期错误: {task.id}\n"
            self.store._event(task_id, "worker.output", {"chunk": output})
            return output, 1

        command = self._command(harness, task.prompt)
        self.store._event(task_id, "worker.output", {"chunk": f"[HoH] 正在启动 {harness} 子进程...\n[HoH] 执行命令: {' '.join(command)}\n"})

        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        full_output = []
        while True:
            if cancel.is_set():
                proc.terminate()
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if not line and proc.poll() is not None:
                break
            if line:
                full_output.append(line)
                self.store._event(task_id, "worker.output", {"chunk": line})

        proc.wait()
        return "".join(full_output), proc.returncode

    @staticmethod
    def _command(harness: str, prompt: str) -> list[str]:
        if harness == "hermes":
            return [
                "F:/hermes/hermes-agent/venv-win/Scripts/python.exe",
                "-m", "hermes_cli.main", "chat", "-q", prompt,
            ]
        if harness == "dsh":
            return ["cmd.exe", "/c", "dsh", "--profile", "headless", prompt]
        raise ValueError(f"unsupported harness: {harness}")
