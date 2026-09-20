import json
import unittest
from pathlib import Path

from hoh.planner import PlannedStep, plan_from_steps
from hoh.serialization import plan_to_dict


class SerializationTests(unittest.TestCase):
    def test_plan_to_dict_is_json_safe_ordered_and_deterministic(self):
        plan = plan_from_steps(
            "实现功能",
            Path("relative-workspace"),
            [
                PlannedStep("one", "第一步", "read", "explorer", harness="hermes"),
                PlannedStep("two", "第二步", "write", "implementer", ["one"], ["test -f result"], "dsh"),
            ],
        )
        first = plan_to_dict(plan)
        second = plan_to_dict(plan)
        self.assertEqual(first, second)
        self.assertEqual(first["planner_kind"], "deterministic-v1")
        self.assertEqual(first["workspace"], str(Path("relative-workspace").resolve()))
        self.assertEqual([step["key"] for step in first["steps"]], ["one", "two"])
        self.assertEqual(first["steps"][1]["depends_on_keys"], ["one"])
        json.dumps(first, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
