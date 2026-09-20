import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from hoh.server import HohApi, RequestHandler, ThreadingHTTPServer


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run([
        "git", "-c", "user.name=HoH", "-c", "user.email=hoh@example.invalid",
        "commit", "-qm", "base",
    ], cwd=path, check=True)


class HohApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api = HohApi(Path(self.tmp.name))
        handler = type("BoundRequestHandler", (RequestHandler,), {"api": api})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.server.RequestHandlerClass.api.close()
        self.tmp.cleanup()

    def request_json(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        self.conn.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
        response = self.conn.getresponse()
        return response.status, json.loads(response.read())

    def wait_for(self, path, expected):
        for _ in range(100):
            status, body = self.request_json("GET", path)
            if body["status"] == expected:
                return body
            time.sleep(0.02)
        self.fail(f"task did not reach {expected}")

    def test_health_create_list_and_events(self):
        status, body = self.request_json("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, created = self.request_json("POST", "/v1/tasks", {
            "title": "api task", "prompt": "inspect", "workspace": self.tmp.name, "harness": "auto",
        })
        self.assertEqual(status, 201)
        status, listed = self.request_json("GET", "/v1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(listed["tasks"][0]["id"], created["id"])
        status, events = self.request_json("GET", f"/v1/tasks/{created['id']}/events")
        self.assertEqual(status, 200)
        self.assertEqual(events["events"][0]["type"], "task.created")

    def test_failed_task_can_retry_over_http(self):
        status, task = self.request_json("POST", "/v1/tasks", {
            "title": "retry", "prompt": "fail", "workspace": self.tmp.name,
        })
        self.assertEqual(status, 201)
        status, first = self.request_json("POST", f"/v1/tasks/{task['id']}/start", {"harness": "mock-fail"})
        self.assertEqual(status, 202)
        for _ in range(100):
            status, state = self.request_json("GET", f"/v1/tasks/{task['id']}")
            if state["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "failed")
        status, second = self.request_json("POST", f"/v1/tasks/{task['id']}/retry", {"harness": "mock"})
        self.assertEqual(status, 202)
        self.assertNotEqual(first["run_id"], second["run_id"])
        for _ in range(100):
            status, state = self.request_json("GET", f"/v1/tasks/{task['id']}")
            if state["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "completed")

    def test_plan_preview_and_materialize_over_http(self):
        # Preview plan
        status, preview = self.request_json("POST", "/v1/plans/preview", {
            "goal": "分析代码流程",
            "workspace": self.tmp.name,
            "harness": "auto",
        })
        self.assertEqual(status, 200)
        self.assertEqual(preview["planner_kind"], "deterministic-v1")
        self.assertIn("steps", preview)
        self.assertGreaterEqual(len(preview["steps"]), 1)

        # Ensure no tasks were created yet
        status, listed = self.request_json("GET", "/v1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["tasks"]), 0)

        # Materialize plan
        status, mat = self.request_json("POST", "/v1/plans/materialize", {
            "goal": "分析代码流程",
            "workspace": self.tmp.name,
            "harness": "auto",
        })
        self.assertEqual(status, 201)
        self.assertIn("tasks", mat)
        self.assertEqual(len(mat["tasks"]), len(preview["steps"]))

        # Check that tasks exist now
        status, listed = self.request_json("GET", "/v1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["tasks"]), len(preview["steps"]))

    def test_git_task_requires_approval_over_http(self):
        repo = Path(self.tmp.name) / "repo"
        init_repo(repo)
        status, task = self.request_json("POST", "/v1/tasks", {
            "title": "write", "prompt": "deterministic", "workspace": str(repo),
            "acceptance": ["test -f hoh_mock_result.txt"],
        })
        self.assertEqual(status, 201)
        status, started = self.request_json("POST", f"/v1/tasks/{task['id']}/start", {"harness": "mock-write"})
        self.assertEqual(status, 202)
        self.wait_for(f"/v1/tasks/{task['id']}", "waiting_approval")
        status, approved = self.request_json("POST", f"/v1/tasks/{task['id']}/approve", {})
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "completed")
        self.assertTrue((repo / "hoh_mock_result.txt").exists())
        self.assertTrue(approved["merge_sha"])


if __name__ == "__main__":
    unittest.main()
