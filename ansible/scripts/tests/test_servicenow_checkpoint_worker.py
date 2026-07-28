from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import servicenow_checkpoint_worker as worker


class FakeServiceNow:
    def __init__(self, tasks: list[dict]):
        self.tasks = tasks

    def results(self, *args, **kwargs):
        return self.tasks


class WorkerGateTests(unittest.TestCase):
    def _tester_task(self, state: str) -> dict:
        return {
            "short_description": "Tester validation gate - Check Point automation",
            "state": state,
        }

    def test_only_closed_complete_authorizes_tester_gate(self) -> None:
        for state in ("7", "Closed Skipped", "4", "Closed Incomplete", "Canceled"):
            with self.subTest(state=state):
                self.assertFalse(
                    worker.closed_tester_task_exists(FakeServiceNow([self._tester_task(state)]), "chg")
                )
        for state in ("3", "Closed Complete", "closed_complete"):
            with self.subTest(state=state):
                self.assertTrue(
                    worker.closed_tester_task_exists(FakeServiceNow([self._tester_task(state)]), "chg")
                )

    def test_lookalike_task_never_authorizes_gate(self) -> None:
        task = {"short_description": "Final validation - Check Point post-implementation checks", "state": "3"}
        self.assertFalse(worker.closed_tester_task_exists(FakeServiceNow([task]), "chg"))

    def test_pre_phase_failure_restarts_from_beginning(self) -> None:
        for phase in ("", "unknown", "initialization", "discover-targets"):
            with self.subTest(phase=phase):
                self.assertEqual(worker.remediation_start_at({"failed_phase": phase}, {}), "")
        self.assertEqual(
            worker.remediation_start_at(
                {"failed_phase": "first-member"}, {"u_checkpoint_resume_phase": "postcheck"}
            ),
            "postcheck",
        )

    def test_stale_resume_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = worker.ROOT
            try:
                worker.ROOT = Path(tmp)
                path = worker.ROOT / "runs" / "CHG_TEST_1" / "resume_state.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"failed_phase": "postcheck"}))
                os.utime(path, (100.0, 100.0))
                self.assertEqual(worker.latest_resume_state("CHG_TEST", newer_than=101.0), {})
                self.assertEqual(
                    worker.latest_resume_state("CHG_TEST", newer_than=99.0)["failed_phase"],
                    "postcheck",
                )
            finally:
                worker.ROOT = old_root


if __name__ == "__main__":
    unittest.main()
