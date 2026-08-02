from __future__ import annotations

import unittest
from pathlib import Path

import servicenow_checkpoint_runner as runner


class GovernedCdtOrderingTests(unittest.TestCase):
    @staticmethod
    def plan(activity: str = "Software Patch Activity") -> dict:
        return {
            "change": {"activity_type": activity},
            "checkpoint": {
                "cluster_mode": "cluster",
                "preserve_original_active": True,
                "members": [
                    {"ip": "192.0.2.20"},
                    {"ip": "192.0.2.21"},
                ],
            },
            "execution": {
                "deployment_backend": "cdt",
                "method": "CDT (Central Deployment Tool)",
                "tester_pause": True,
            },
            "package_steps": [{
                "name": "package_step",
                "action": "upgrade" if activity == "Major Version Upgrade" else "install",
                "package_type": "blink" if activity == "Major Version Upgrade" else "jhf",
                "package_name": "R82_Blink.tgz" if activity == "Major Version Upgrade" else "Take91.tgz",
                "target_build": "777" if activity == "Major Version Upgrade" else "",
            }],
        }

    def assert_immediate_reconciliation(self, steps: list[tuple]) -> None:
        executions = [i for i, row in enumerate(steps) if row[1] == "20_cdt_execute_guarded.yml"]
        self.assertTrue(executions)
        for index in executions:
            mutation = steps[index]
            reconciliation = steps[index + 1]
            self.assertEqual(
                reconciliation[:3],
                (mutation[0], "21_cdt_reconcile_member.yml", mutation[2]),
            )

    def test_normal_cdt_reconciles_before_failover_gate_and_next_member(self) -> None:
        steps = runner.workflow_steps(self.plan())
        self.assert_immediate_reconciliation(steps)
        first_reconciliation = next(
            i for i, row in enumerate(steps)
            if row[0] == "first-member" and row[1] == "21_cdt_reconcile_member.yml"
        )
        failover = next(i for i, row in enumerate(steps) if row[0] == "failover-to-first")
        gate = next(i for i, row in enumerate(steps) if row[1] == "__gate__")
        second = next(i for i, row in enumerate(steps) if row[0] == "second-member")
        self.assertLess(first_reconciliation, failover)
        self.assertLess(failover, gate)
        self.assertLess(gate, second)

    def test_major_cdt_reconciles_before_policy_and_failover(self) -> None:
        steps = runner.workflow_steps(self.plan("Major Version Upgrade"))
        self.assert_immediate_reconciliation(steps)
        first_reconciliation = next(
            i for i, row in enumerate(steps)
            if row[0] == "first-member" and row[1] == "21_cdt_reconcile_member.yml"
        )
        policy = next(i for i, row in enumerate(steps) if row[0] == "mixed-version-policy-gate")
        failover = next(i for i, row in enumerate(steps) if row[0] == "failover-to-first")
        self.assertLess(first_reconciliation, policy)
        self.assertLess(first_reconciliation, failover)

    def test_context_preflight_runs_before_mutation_and_receipt_after(self) -> None:
        playbook = (
            Path(__file__).resolve().parents[2]
            / "playbooks"
            / "20_cdt_execute_guarded.yml"
        ).read_text()
        preflight = playbook.index("Validate protected candidate context before mutation")
        mutation = playbook.index("Execute CDT using controlled one-member candidate file")
        receipt = playbook.index("Bind successful CDT execution to its candidate context")
        self.assertLess(preflight, mutation)
        self.assertLess(mutation, receipt)
        self.assertIn("--validate-only", playbook[preflight:mutation])

    def test_blink_target_build_is_explicit_or_safely_inferred(self) -> None:
        common = {
            "package_type": "blink",
            "action": "upgrade",
            "sha256": "a" * 64,
        }
        explicit = runner.package_steps_from_rows([{
            **common,
            "package_name": "R82_Blink.tgz",
            "target_build": "777",
        }])
        self.assertEqual(explicit[0]["target_build"], "777")
        inferred = runner.package_steps_from_rows([{
            **common,
            "package_name": "Check_Point_R82_T777_JHF_T60_Blink.tgz",
        }])
        self.assertEqual(inferred[0]["target_build"], "777")
        with self.assertRaisesRegex(ValueError, "target_build"):
            runner.package_steps_from_rows([{
                **common,
                "package_name": "R82_Blink.tgz",
                "target_build": "777; reboot",
            }])


if __name__ == "__main__":
    unittest.main()
