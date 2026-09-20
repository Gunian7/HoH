"""Deterministic planning layer for HoH.

The planner turns a goal into a DAG of *real* HoH tasks. It is deliberately
rule-based and inspectable: the decomposition is a function of explicit
keyword/role rules, not of a model call, so it can be unit-tested and its
output explained to the operator. An LLM-backed decomposer can be added later
by producing the same ``Plan`` structure, without changing the rest of HoH.

Two distinct verification layers exist and must not be conflated:

* ``verifier`` role -- an *agent* step that independently reproduces the
  acceptance criteria (extra evidence gathering).
* ``HohStore``/Runner acceptance -- HoH's own deterministic command execution
  recorded in the task's verification record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .core import HohStore, Task, TaskSpec

#: Tools a role needs to do its job. Routing is derived from this table so the
#: choice of harness is explainable rather than arbitrary.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "explorer": frozenset({"read", "search"}),
    "implementer": frozenset({"read", "write", "terminal"}),
    "reviewer": frozenset({"read", "search"}),
    "verifier": frozenset({"read", "terminal"}),
}

#: What each harness can actually do today. ``write`` is the load-bearing
#: capability: only a harness with write+terminal may modify a worktree.
HARNESS_CAPABILITIES: dict[str, frozenset[str]] = {
    "hermes": frozenset({"read", "search", "write", "terminal"}),
    "dsh": frozenset({"read", "search", "write", "terminal"}),
}

#: Preference order used when no harness is requested and none is excluded.
DEFAULT_ORDER: tuple[str, ...] = ("hermes", "dsh")

#: Goals containing any of these word stems are treated as authoring work.
WRITE_MARKERS: tuple[str, ...] = (
    "实现", "修复", "添加", "新增", "重构", "迁移", "升级", "编写", "补",
    "implement", "fix", "add", "refactor", "migrate", "upgrade", "build",
    "create", "write", "port",
)

#: Goals that only ask for understanding produce a read-only chain. Any goal
#: matching neither list is also treated as read-only: HoH does not assume the
#: operator wants writes when they did not ask for them.
READ_MARKERS: tuple[str, ...] = (
    "分析", "报告", "调研", "审查", "梳理", "阅读", "解释", "定位", "盘点",
    "analyze", "analyse", "report", "review", "inspect", "explain",
    "investigate", "audit", "survey",
)


@dataclass
class PlannedStep:
    key: str
    title: str
    prompt: str
    role: str
    depends_on_keys: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    harness: str = "auto"


@dataclass
class Plan:
    goal: str
    workspace: Path
    steps: list[PlannedStep]

    def keys(self) -> list[str]:
        return [step.key for step in self.steps]


def route(
    role: str,
    *,
    preferred: str | None = None,
    exclude: str | None = None,
    available: Sequence[str] | None = None,
) -> str:
    """Pick a harness for ``role``.

    ``exclude`` is how HoH guarantees independent review: the reviewer gets a
    different harness than the implementer, so a defect in one runtime's habits
    does not automatically bless its own output.
    """

    needed = ROLE_CAPABILITIES.get(role)
    if needed is None:
        raise ValueError(f"unknown role: {role}")

    allowed = DEFAULT_ORDER if available is None else [name for name in DEFAULT_ORDER if name in available]

    candidates = [
        name for name in allowed
        if name != exclude and needed <= HARNESS_CAPABILITIES.get(name, frozenset())
    ]
    if preferred and preferred != exclude and preferred in allowed and needed <= HARNESS_CAPABILITIES.get(preferred, frozenset()):
        return preferred
    if not candidates:
        # Fallback: if exclude was set, try including it if allowed
        if exclude and exclude in allowed and needed <= HARNESS_CAPABILITIES.get(exclude, frozenset()):
            return exclude
        raise ValueError(f"no harness can serve role {role!r} (exclude={exclude!r}, available={available!r})")
    return candidates[0]


def plan_from_steps(goal: str, workspace: Path, steps: Sequence[PlannedStep]) -> Plan:
    """Wrap an explicit, operator-authored step list into a Plan."""

    keys = [step.key for step in steps]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise ValueError(f"duplicate step keys: {sorted(duplicates)}")
    known = set(keys)
    for step in steps:
        unknown = [dependency for dependency in step.depends_on_keys if dependency not in known]
        if unknown:
            raise ValueError(f"step {step.key!r} depends on unknown keys: {unknown}")
    return Plan(goal=goal, workspace=workspace, steps=list(steps))


def _is_write_goal(goal: str) -> bool:
    lowered = goal.lower()
    return any(marker in goal or marker in lowered for marker in WRITE_MARKERS)


def plan_from_goal(goal: str, workspace: Path, *, preferred: str | None = None) -> Plan:
    """Decompose ``goal`` with keyword rules.

    This is intentionally a small, predictable rule set -- not a claim that HoH
    understands arbitrary natural language. Ambiguous goals fall back to the
    read-only chain so nothing is written without explicit intent.
    """

    if _is_write_goal(goal):
        implementer = route("implementer", preferred=preferred)
        reviewer = route("reviewer", exclude=implementer)
        verifier = route("verifier", exclude=implementer)
        explorer = route("explorer", exclude=None)
        steps = [
            PlannedStep("explore", "探索现状", f"只读分析目标相关工作区，说明现状、约束和风险，不要修改文件。目标：{goal}", "explorer", [], [], explorer),
            PlannedStep("implement", "实现改动", f"在工作区中实现下述目标，保持改动最小、可审查。目标：{goal}", "implementer", ["explore"], [], implementer),
            PlannedStep("review", "独立审查", f"以独立视角审查上一个步骤的改动，找出缺陷、遗漏和风险，给出结论。目标：{goal}", "reviewer", ["implement"], [], reviewer),
            PlannedStep("verify", "独立复现验证", f"在干净环境下独立复现验收标准，报告真实命令与输出，不要修改实现。目标：{goal}", "verifier", ["review"], [], verifier),
        ]
    else:
        explorer = route("explorer", preferred=preferred)
        reviewer = route("reviewer", exclude=explorer)
        steps = [
            PlannedStep("explore", "探索现状", f"只读分析目标相关工作区，说明现状、约束和风险，不要修改文件。目标：{goal}", "explorer", [], [], explorer),
            PlannedStep("review", "汇总结论", f"基于探索结果整理结论与证据，明确不确定性。目标：{goal}", "reviewer", ["explore"], [], reviewer),
        ]

    return Plan(goal=goal, workspace=workspace, steps=steps)


def _route_role(step: PlannedStep, *, implementer_harness: str | None) -> str:
    """Kept for callers that build steps by hand and need the same routing rules."""
    if step.role in {"reviewer", "verifier"} and implementer_harness:
        return route(step.role, exclude=implementer_harness)
    return route(step.role)


def materialize(store: HohStore, plan: Plan) -> list[Task]:
    """Create real tasks for ``plan``, parents before children.

    Ordering matters: ``HohStore.create_task`` refuses to reference a task that
    does not exist yet, so the dependency graph must be emitted in topological
    order. Cycles are impossible here because dependencies only point backwards
    in this list, but the store still validates on every insert.
    """

    ordered = _topological(plan.steps)
    created: dict[str, Task] = {}
    tasks: list[Task] = []
    for step in ordered:
        spec = TaskSpec(
            title=step.title,
            prompt=step.prompt,
            workspace=plan.workspace,
            harness=step.harness,
            acceptance=list(step.acceptance),
            depends_on=[created[key].id for key in step.depends_on_keys],
        )
        task = store.create_task(spec)
        created[step.key] = task
        tasks.append(task)
    return tasks


def _topological(steps: Sequence[PlannedStep]) -> list[PlannedStep]:
    by_key: dict[str, PlannedStep] = {}
    for step in steps:
        if step.key in by_key:
            raise ValueError(f"duplicate step key: {step.key}")
        by_key[step.key] = step

    resolved: list[PlannedStep] = []
    seen: set[str] = set()
    pending: Iterable[PlannedStep] = list(steps)
    while isinstance(pending, list) and pending:
        progressed = False
        remaining: list[PlannedStep] = []
        for step in pending:
            missing = [key for key in step.depends_on_keys if key not in by_key]
            if missing:
                raise ValueError(f"step {step.key!r} depends on unknown keys: {missing}")
            if all(key in seen for key in step.depends_on_keys):
                resolved.append(step)
                seen.add(step.key)
                progressed = True
            else:
                remaining.append(step)
        if not progressed:
            raise ValueError("dependency cycle detected in plan")
        pending = remaining
    return resolved
