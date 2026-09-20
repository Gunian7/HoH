from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .adapters import DshAdapter, HermesAdapter, configured_harnesses
from .core import HohStore, TaskSpec, TaskStatus, VerificationCheck, verify_workspace
from .planner import plan_from_goal, materialize
from .serialization import plan_to_dict
from .server import serve


def default_home() -> Path:
    return Path(os.environ.get("HOH_HOME", Path.home() / ".hoh"))


def store_for(home: Path) -> HohStore:
    return HohStore(home / "state.db")


def command_health(_: argparse.Namespace) -> int:
    infos = configured_harnesses()
    for info in infos:
        print(json.dumps(asdict(info), ensure_ascii=False))
    ok, detail = HermesAdapter().acp_check()
    print(json.dumps({"name": "hermes-acp", "available": ok, "detail": detail}, ensure_ascii=False))
    return 0 if all(info.available for info in infos) and ok else 1


def command_create(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    store = store_for(Path(args.home))
    try:
        task = store.create_task(TaskSpec(args.title, args.prompt, workspace, args.harness))
        print(json.dumps({"task_id": task.id, "status": task.status.value, "workspace": task.workspace}, ensure_ascii=False))
        return 0
    finally:
        store.close()


def command_list(args: argparse.Namespace) -> int:
    store = store_for(Path(args.home))
    try:
        for task in store.list_tasks():
            print(f"{task.id}\t{task.status.value}\t{task.harness}\t{task.title}")
        return 0
    finally:
        store.close()


def command_ready(args: argparse.Namespace) -> int:
    store = store_for(Path(args.home))
    try:
        for task_id in store.ready_tasks():
            print(task_id)
        return 0
    finally:
        store.close()


def command_verify(args: argparse.Namespace) -> int:
    root = Path(args.workspace).resolve()
    result = verify_workspace(root, [VerificationCheck("requested", args.command, root)], [])
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


def command_run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    store = store_for(Path(args.home))
    try:
        task = store.create_task(TaskSpec(args.title, args.prompt, workspace, args.harness))
        if args.dry_run:
            store.transition(task.id, TaskStatus.READY, {"dry_run": True})
            print(json.dumps({"task_id": task.id, "status": "ready", "dry_run": True}, ensure_ascii=False))
            return 0

        adapter = HermesAdapter() if args.harness == "hermes" else DshAdapter()
        info = adapter.health()
        if not info.available:
            store.transition(task.id, TaskStatus.FAILED, {"error": info.detail})
            print(json.dumps({"task_id": task.id, "status": "failed", "error": info.detail}, ensure_ascii=False))
            return 1

        store.transition(task.id, TaskStatus.RUNNING, {"runtime": info.name})
        envelope = (
            f"HoH task id: {task.id}\nWorkspace: {workspace}\n"
            f"Objective: {args.prompt}\nReturn a concise summary and real verification evidence."
        )
        if isinstance(adapter, HermesAdapter):
            command = [str(adapter.command), "-m", "hermes_cli.main", "chat", "-q", envelope]
        elif os.name == "nt":
            command = ["cmd.exe", "/c", "dsh", "--profile", "headless", envelope]
        else:
            command = ["dsh", "--profile", "headless", envelope]

        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
        log_path = Path(args.home) / "runs" / f"{task.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
        status = TaskStatus.COMPLETED if result.returncode == 0 else TaskStatus.FAILED
        store.transition(task.id, status, {"exit_code": result.returncode, "log": str(log_path)})
        print(json.dumps({"task_id": task.id, "status": status.value, "exit_code": result.returncode, "log": str(log_path)}, ensure_ascii=False))
        return result.returncode
    finally:
        store.close()


def command_serve(args: argparse.Namespace) -> int:
    serve(Path(args.home), host=args.host, port=args.port)
    return 0


def command_plan(args: argparse.Namespace) -> int:
    preferred = None if args.harness == "auto" else args.harness
    plan = plan_from_goal(args.goal, Path(args.workspace), preferred=preferred)
    print(json.dumps(plan_to_dict(plan), ensure_ascii=False, indent=2))
    return 0


def command_materialize(args: argparse.Namespace) -> int:
    preferred = None if args.harness == "auto" else args.harness
    plan = plan_from_goal(args.goal, Path(args.workspace), preferred=preferred)
    store = store_for(Path(args.home))
    try:
        tasks = materialize(store, plan)
        res = [
            {
                "task_id": t.id,
                "title": t.title,
                "status": t.status.value,
                "harness": t.harness,
                "depends_on": t.depends_on,
            }
            for t in tasks
        ]
        print(json.dumps({"tasks": res}, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoh", description="Harness of Harness control plane")
    parser.add_argument("--home", default=str(default_home()))
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health")
    health.set_defaults(func=command_health)

    create = sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--harness", choices=["auto", "hermes", "dsh"], default="auto")
    create.set_defaults(func=command_create)

    listing = sub.add_parser("list")
    listing.set_defaults(func=command_list)

    ready = sub.add_parser("ready")
    ready.set_defaults(func=command_ready)

    verify = sub.add_parser("verify")
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--command", required=True)
    verify.set_defaults(func=command_verify)

    run = sub.add_parser("run")
    run.add_argument("--title", required=True)
    run.add_argument("--prompt", required=True)
    run.add_argument("--workspace", required=True)
    run.add_argument("--harness", choices=["hermes", "dsh"], default="hermes")
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=command_run)

    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.set_defaults(func=command_serve)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--goal", required=True)
    plan_parser.add_argument("--workspace", required=True)
    plan_parser.add_argument("--harness", choices=["auto", "hermes", "dsh"], default="auto")
    plan_parser.set_defaults(func=command_plan)

    mat_parser = sub.add_parser("materialize")
    mat_parser.add_argument("--goal", required=True)
    mat_parser.add_argument("--workspace", required=True)
    mat_parser.add_argument("--harness", choices=["auto", "hermes", "dsh"], default="auto")
    mat_parser.set_defaults(func=command_materialize)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
