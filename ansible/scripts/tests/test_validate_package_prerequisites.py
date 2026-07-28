from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "validate_package_prerequisites.py"
SPEC = importlib.util.spec_from_file_location("validate_package_prerequisites", SCRIPT)
assert SPEC and SPEC.loader
prereq = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prereq
SPEC.loader.exec_module(prereq)


class MemberSelectionTests(unittest.TestCase):
    def plan(self) -> dict:
        return {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "cluster_mode": "cluster",
                "members": [
                    {"hostname": "member-a", "ip": "192.0.2.20"},
                    {"hostname": "member-b", "ip": "192.0.2.21"},
                ],
            },
        }

    def test_cluster_phase_requires_captured_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "must identify original active and standby"):
                prereq.target_members_for_phase(self.plan(), "first-member", Path(tmp))

    def test_captured_state_must_match_plan_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                json.dumps({"original_active_host": "192.0.2.99", "original_standby_host": "192.0.2.21"})
            )
            with self.assertRaisesRegex(SystemExit, "does not match"):
                prereq.target_members_for_phase(self.plan(), "first-member", reports)

    def test_first_member_is_captured_standby(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                json.dumps({"original_active_host": "192.0.2.20", "original_standby_host": "192.0.2.21"})
            )
            selected = prereq.target_members_for_phase(self.plan(), "first-member", reports)
            self.assertEqual(selected, [{"hostname": "member-b", "ip": "192.0.2.21"}])


if __name__ == "__main__":
    unittest.main()
