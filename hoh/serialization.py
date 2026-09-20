from __future__ import annotations

from pathlib import Path
from typing import Any

from .planner import Plan


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "goal": plan.goal,
        "workspace": str(Path(plan.workspace).resolve()),
        "planner_kind": "deterministic-v1",
        "steps": [
            {
                "key": step.key,
                "title": step.title,
                "prompt": step.prompt,
                "role": step.role,
                "depends_on_keys": list(step.depends_on_keys),
                "acceptance": list(step.acceptance),
                "harness": step.harness,
            }
            for step in plan.steps
        ],
    }
