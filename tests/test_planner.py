import tempfile
import unittest
from pathlib import Path

from hoh.core import HohStore, TaskStatus
from hoh.planner import PlannedStep, plan_from_goal, plan_from_steps, materialize, route


class PlannerTests(unittest.TestCase):
    def test_plan_from_steps_builds_dag_and_creates_tasks_in_topological_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            try:
                plan = plan_from_steps(
                    "为项目增加 CSV 导入",
                    Path(tmp),
                    [
                        PlannedStep("explore", "探索", "分析现有导入逻辑", "explorer", [], []),
                        PlannedStep("implement", "实现", "实现 CSV 导入", "implementer", ["explore"], ["test -f import.py"]),
                        PlannedStep("review", "审查", "独立审查实现", "reviewer", ["implement"], []),
                    ],
                )
                tasks = materialize(store, plan)
                self.assertEqual([task.title for task in tasks], ["探索", "实现", "审查"])
                self.assertEqual(store.get_task(tasks[0].id).depends_on, [])
                self.assertEqual(store.get_task(tasks[1].id).depends_on, [tasks[0].id])
                self.assertEqual(store.get_task(tasks[2].id).depends_on, [tasks[1].id])
                self.assertEqual(store.ready_tasks(), [tasks[0].id])
                self.assertEqual(store.task_spec(tasks[1].id)["acceptance"], ["test -f import.py"])
            finally:
                store.close()

    def test_materialize_rejects_step_depending_on_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HohStore(Path(tmp) / "state.db")
            try:
                with self.assertRaises(ValueError):
                    plan_from_steps(
                        "goal", Path(tmp),
                        [PlannedStep("implement", "实现", "写代码", "implementer", ["missing"], [])],
                    )
            finally:
                store.close()

    def test_router_gives_reviewer_a_different_harness_than_implementer(self):
        self.assertEqual(route("implementer", preferred="hermes"), "hermes")
        self.assertEqual(route("implementer", preferred="dsh"), "dsh")
        self.assertEqual(route("reviewer", exclude="hermes"), "dsh")
        self.assertEqual(route("reviewer", exclude="dsh"), "hermes")
        self.assertEqual(route("explorer"), "hermes")

    def test_router_respects_available_harnesses(self):
        # When only hermes is available, reviewer must fallback to hermes even if excluded, or fail gracefully
        self.assertEqual(route("reviewer", exclude="hermes", available=["hermes"]), "hermes")
        with self.assertRaises(ValueError):
            route("reviewer", available=[])

    def test_plan_from_goal_creates_explore_implement_verify_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_from_goal("实现离线任务编辑并补测试", Path(tmp), preferred="dsh")
            roles = [step.role for step in plan.steps]
            self.assertEqual(roles, ["explorer", "implementer", "reviewer", "verifier"])
            by_key = {step.key: step for step in plan.steps}
            self.assertEqual(by_key["implement"].harness, "dsh")
            self.assertEqual(by_key["verify"].depends_on_keys, ["review"])
            self.assertEqual(by_key["review"].depends_on_keys, ["implement"])
            self.assertNotEqual(by_key["implement"].harness, by_key["review"].harness)

    def test_plan_from_goal_marks_read_only_goal_without_write_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_from_goal("分析这个仓库的认证流程并给出报告", Path(tmp))
            self.assertEqual([step.role for step in plan.steps], ["explorer", "reviewer"])


if __name__ == "__main__":
    unittest.main()
