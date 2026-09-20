import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hoh.cli import build_parser, main
from hoh.core import HohStore, TaskStatus


class CliPlannerTests(unittest.TestCase):
    def test_cli_plan_outputs_json_without_creating_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            workspace = tmp_path / "workspace"
            workspace.mkdir()

            parser = build_parser()
            args = parser.parse_args([
                "--home", str(home),
                "plan",
                "--goal", "实现离线任务编辑并补测试",
                "--workspace", str(workspace),
                "--harness", "dsh",
            ])

            with patch("sys.stdout") as mock_stdout:
                ret = args.func(args)
                self.assertEqual(ret, 0)
                # Ensure no state.db was created
                self.assertFalse((home / "state.db").exists())

    def test_cli_materialize_creates_tasks_in_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            workspace = tmp_path / "workspace"
            workspace.mkdir()

            parser = build_parser()
            args = parser.parse_args([
                "--home", str(home),
                "materialize",
                "--goal", "实现离线任务编辑并补测试",
                "--workspace", str(workspace),
                "--harness", "dsh",
            ])

            ret = args.func(args)
            self.assertEqual(ret, 0)

            # Check database tasks
            self.assertTrue((home / "state.db").exists())
            store = HohStore(home / "state.db")
            try:
                tasks = store.list_tasks()
                self.assertGreaterEqual(len(tasks), 2)
                for t in tasks:
                    self.assertIn(t.status, (TaskStatus.PENDING, TaskStatus.READY))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
