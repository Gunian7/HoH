import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from hoh.core import HohStore, TaskSpec, TaskStatus
from hoh.runner import TaskRunner
from hoh.server import HohApi, RequestHandler, ThreadingHTTPServer


class StreamAndRetryTests(unittest.TestCase):
    def test_events_after_returns_only_new_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            task = store.create_task(TaskSpec("events", "test", Path(tmp)))
            first = store.events(task.id)
            store.transition(task.id, TaskStatus.RUNNING, {"run_id": "run_1"})
            delta = store.events_after(task.id, first[-1]["id"])
            self.assertEqual(len(delta), 1)
            self.assertEqual(delta[0]["type"], "task.status_changed")
            store.close()

    def test_failed_task_can_retry_with_new_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store = HohStore(home / "state.db")
            task = store.create_task(TaskSpec("retry", "fail", Path(tmp)))
            runner = TaskRunner(store, home)
            first = runner.start(task.id, "mock-fail")
            for _ in range(100):
                if store.get_task(task.id).status == TaskStatus.FAILED:
                    break
                time.sleep(0.01)
            second = runner.retry(task.id)
            self.assertNotEqual(first, second)
            self.assertEqual(store.get_task(task.id).status, TaskStatus.RUNNING)
            for handle in list(runner.runs.values()):
                handle.thread.join(timeout=2)
            store.close()

    def test_sse_endpoint_returns_event_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = HohApi(Path(tmp))
            handler = type("BoundRequestHandler", (RequestHandler,), {"api": api})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            payload = json.dumps({"title": "sse", "prompt": "test", "workspace": tmp}).encode()
            conn.request("POST", "/v1/tasks", payload, {"Content-Type": "application/json"})
            task = json.loads(conn.getresponse().read())
            conn.request("GET", f"/v1/tasks/{task['id']}/stream?once=1")
            response = conn.getresponse()
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            self.assertIn("task.created", body)
            conn.close()
            server.shutdown()
            server.server_close()
            api.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
