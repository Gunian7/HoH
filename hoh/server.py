from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import HohStore, TaskSpec
from .planner import plan_from_goal, materialize
from .runner import TaskRunner
from .serialization import plan_to_dict


class HohApi:
    def __init__(self, home: Path):
        self.store = HohStore(home / "state.db")
        self.lock = threading.Lock()
        self.runner = TaskRunner(self.store, home)

    def create_task(self, body: dict) -> dict:
        workspace = Path(body["workspace"]).resolve()
        spec = TaskSpec(
            title=body["title"],
            prompt=body["prompt"],
            workspace=workspace,
            harness=body.get("harness", "auto"),
            constraints=body.get("constraints", []),
            acceptance=body.get("acceptance", []),
            depends_on=body.get("depends_on", []),
        )
        with self.lock:
            task = self.store.create_task(spec)
        return asdict(task) | {"status": task.status.value}

    def preview_plan(self, body: dict) -> dict:
        preferred = None if body.get("harness", "auto") == "auto" else body.get("harness")
        plan = plan_from_goal(body["goal"], Path(body["workspace"]), preferred=preferred)
        return plan_to_dict(plan)

    def materialize_plan(self, body: dict) -> dict:
        preferred = None if body.get("harness", "auto") == "auto" else body.get("harness")
        plan = plan_from_goal(body["goal"], Path(body["workspace"]), preferred=preferred)
        with self.lock:
            tasks = materialize(self.store, plan)
        return {
            "tasks": [
                asdict(t) | {"status": t.status.value}
                for t in tasks
            ]
        }

    def close(self) -> None:
        self.store.close()


class RequestHandler(BaseHTTPRequestHandler):
    api: HohApi

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, status: int, body: object) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_POST(self) -> None:
        if self.path == "/v1/plans/preview":
            try:
                self._send_json(HTTPStatus.OK, self.api.preview_plan(self._read_json()))
            except (KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if self.path == "/v1/plans/materialize":
            try:
                self._send_json(HTTPStatus.CREATED, self.api.materialize_plan(self._read_json()))
            except (KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if self.path == "/v1/tasks":
            try:
                self._send_json(HTTPStatus.CREATED, self.api.create_task(self._read_json()))
            except (KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        parts = [part for part in self.path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "start":
            try:
                body = self._read_json() if self.headers.get("Content-Length") else {}
                run_id = self.api.runner.start(parts[2], body.get("harness", "mock"))
                self._send_json(HTTPStatus.ACCEPTED, {"run_id": run_id})
            except (KeyError, ValueError) as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "cancel":
            try:
                self.api.runner.cancel(self._read_json()["run_id"])
                self._send_json(HTTPStatus.ACCEPTED, {"status": "cancelled"})
            except (KeyError, ValueError) as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "retry":
            try:
                body = self._read_json() if self.headers.get("Content-Length") else {}
                run_id = self.api.runner.retry(parts[2], body.get("harness"))
                self._send_json(HTTPStatus.ACCEPTED, {"run_id": run_id})
            except ValueError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "approve":
            try:
                merge_sha = self.api.runner.approve(parts[2])
                self._send_json(HTTPStatus.OK, {"status": "completed", "merge_sha": merge_sha})
            except ValueError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            html_path = Path(__file__).parent / "web" / "index.html"
            if html_path.exists():
                content = html_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
        if parts == ["health"]:
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parts == ["v1", "tasks"]:
            tasks = [asdict(task) | {"status": task.status.value} for task in self.api.store.list_tasks()]
            self._send_json(HTTPStatus.OK, {"tasks": tasks})
            return
        if parts == ["v1", "tasks", "ready"]:
            self._send_json(HTTPStatus.OK, {"task_ids": self.api.store.ready_tasks()})
            return
        if len(parts) == 3 and parts[:2] == ["v1", "tasks"]:
            try:
                task = self.api.store.get_task(parts[2])
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
                return
            self._send_json(HTTPStatus.OK, asdict(task) | {"status": task.status.value})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "stream":
            self._stream_events(parts[2], parse_qs(parsed.query))
            return
        if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "events":
            try:
                events = self.api.store.events(parts[2])
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
                return
            self._send_json(HTTPStatus.OK, {"events": events})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _stream_events(self, task_id: str, query: dict[str, list[str]]) -> None:
        try:
            self.api.store.get_task(task_id)
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
            return
        try:
            last_id = int(query.get("after", ["0"])[0])
        except ValueError:
            last_id = 0
        once = query.get("once", ["0"])[0] == "1"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close" if once else "keep-alive")
        self.end_headers()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = self.api.store.events_after(task_id, last_id)
            for event in events:
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"id: {event['id']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_id = event["id"]
            if once:
                return
            time.sleep(0.1)


def serve(home: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    api = HohApi(home)
    handler = type("BoundRequestHandler", (RequestHandler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        api.close()
