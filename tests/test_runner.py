import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from hoh.core import HohStore, TaskSpec, TaskStatus
from hoh.runner import TaskRunner


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run([
        "git", "-c", "user.name=HoH", "-c", "user.email=hoh@example.invalid",
        "commit", "-qm", "base",
    ], cwd=path, check=True)


class RunnerTests(unittest.TestCase):
    def wait_for(self, store, task_id, status):
        for _ in range(100):
            if store.get_task(task_id).status == status:
                return
            time.sleep(0.02)
        self.fail(f"task did not reach {status}")

    def close_runner(self, store, runner):
        for handle in list(runner.runs.values()):
            handle.thread.join(timeout=2)
        store.close()

    def test_git_task_is_verified_before_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            home = Path(tmp) / "home"
            store = HohStore(home / "state.db")
            task = store.create_task(TaskSpec(
                "write", "deterministic", repo, "mock-write",
                acceptance=["test -f hoh_mock_result.txt"],
            ))
            runner = TaskRunner(store, home)
            runner.auto_pilot = False
            run_id = runner.start(task.id, "mock-write")
            self.wait_for(store, task.id, TaskStatus.WAITING_APPROVAL)
            events = store.events(task.id)
            verifying = [event for event in events if event["type"] == "task.verification"]
            self.assertEqual(len(verifying), 1)
            self.assertTrue(verifying[0]["payload"]["passed"])
            payload = events[-1]["payload"]
            self.assertEqual(payload["run_id"], run_id)
            self.assertTrue(Path(payload["workspace"]).joinpath("hoh_mock_result.txt").exists())
            self.assertFalse(repo.joinpath("hoh_mock_result.txt").exists())
            self.close_runner(store, runner)

    def test_approval_merges_verified_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            home = Path(tmp) / "home"
            store = HohStore(home / "state.db")
            task = store.create_task(TaskSpec(
                "write", "deterministic", repo, "mock-write",
                acceptance=["test -f hoh_mock_result.txt"],
            ))
            runner = TaskRunner(store, home)
            runner.auto_pilot = False
            runner.start(task.id, "mock-write")
            self.wait_for(store, task.id, TaskStatus.WAITING_APPROVAL)
            merge_sha = runner.approve(task.id)
            self.assertTrue(merge_sha)
            self.assertEqual(store.get_task(task.id).status, TaskStatus.COMPLETED)
            self.assertTrue((repo / "hoh_mock_result.txt").exists())
            self.assertFalse(Path(store.task_spec(task.id)["worktree"]).exists())
            self.assertEqual(store.events(task.id)[-1]["payload"]["merge_sha"], merge_sha)
            self.close_runner(store, runner)

    def test_approval_requires_verified_waiting_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            store = HohStore(Path(tmp) / "state.db")
            task = store.create_task(TaskSpec("pending", "no-op", repo))
            with self.assertRaises(ValueError):
                TaskRunner(store, Path(tmp) / "home").approve(task.id)
            store.close()

    def test_mock_runner_completes_task_and_writes_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store = HohStore(home / "state.db")
            task = store.create_task(TaskSpec("run", "deterministic", Path(tmp), "mock"))
            runner = TaskRunner(store, home)
            runner.auto_pilot = False
            run_id = runner.start(task.id, "mock")
            self.wait_for(store, task.id, TaskStatus.COMPLETED)
            events = store.events(task.id)
            self.assertEqual(events[-1]["payload"]["run_id"], run_id)
            log_path = Path(events[-1]["payload"]["log"])
            self.assertTrue(log_path.exists())
            self.assertIn(task.id, log_path.read_text(encoding="utf-8"))
            self.close_runner(store, runner)

    def test_runner_does_not_start_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            task = store.create_task(TaskSpec("done", "no-op", Path(tmp)))
            store.transition(task.id, TaskStatus.COMPLETED)
            with self.assertRaises(ValueError):
                TaskRunner(store, Path(tmp)).start(task.id)
            store.close()


if __name__ == "__main__":
    unittest.main()
