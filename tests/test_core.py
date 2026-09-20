import sys
import tempfile
import unittest
from pathlib import Path

from hoh.core import HohStore, TaskSpec, TaskStatus, VerificationCheck, verify_workspace
from hoh.workspaces import create_worktree


class HohCoreTests(unittest.TestCase):
    def test_task_lifecycle_is_persisted_and_events_are_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            task = store.create_task(TaskSpec("smoke task", "inspect", Path(tmp) / "workspace", "auto"))
            store.transition(task.id, TaskStatus.RUNNING, {"harness": "hermes"})
            store.transition(task.id, TaskStatus.VERIFYING, {"checks": 1})
            store.transition(task.id, TaskStatus.COMPLETED, {"verified": True})
            loaded = store.get_task(task.id)
            events = store.events(task.id)
            store.close()
            self.assertEqual(loaded.status, TaskStatus.COMPLETED)
            self.assertEqual([event["type"] for event in events], [
                "task.created", "task.status_changed", "task.status_changed", "task.status_changed",
            ])
            self.assertTrue(events[-1]["payload"]["verified"])

    def test_dependency_is_not_ready_until_parent_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            parent = store.create_task(TaskSpec("parent", "inspect", Path(tmp) / "p"))
            child = store.create_task(TaskSpec("child", "implement", Path(tmp) / "c", depends_on=[parent.id]))
            self.assertEqual(store.ready_tasks(), [parent.id])
            store.transition(parent.id, TaskStatus.COMPLETED)
            self.assertEqual(store.ready_tasks(), [child.id])
            store.close()

    def test_dependency_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            first = store.create_task(TaskSpec("first", "one", Path(tmp) / "1"))
            second = store.create_task(TaskSpec("second", "two", Path(tmp) / "2", depends_on=[first.id]))
            with self.assertRaises(ValueError):
                store.add_dependency(first.id, second.id)
            store.close()

    def test_worktree_is_created_from_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.assertEqual(__import__("subprocess").run(["git", "init", "-q"], cwd=repo).returncode, 0)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            __import__("subprocess").run(["git", "add", "README.md"], cwd=repo, check=True)
            __import__("subprocess").run(["git", "-c", "user.name=HoH", "-c", "user.email=hoh@example.invalid", "commit", "-qm", "base"], cwd=repo, check=True)
            result = create_worktree(repo, Path(tmp) / "worktree", "hoh/test")
            self.assertTrue(result.path.joinpath("README.md").exists())
            self.assertEqual(result.branch, "hoh/test")

    def test_verifier_rejects_changes_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("changed", encoding="utf-8")
            result = verify_workspace(root, [VerificationCheck("outside", "true", root)], [outside])
            self.assertFalse(result.passed)
            self.assertIn("outside workspace", result.failures[0])

    def test_verifier_records_successful_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_workspace(Path(tmp), [VerificationCheck("python", f'"{sys.executable}" --version', Path(tmp))], [])
            self.assertTrue(result.passed)
            self.assertEqual(result.checks[0]["exit_code"], 0)
            self.assertTrue(result.checks[0]["output"])


if __name__ == "__main__":
    unittest.main()
