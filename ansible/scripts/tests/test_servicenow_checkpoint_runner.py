from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

import servicenow_checkpoint_runner as runner


class IcapModeResolutionTests(unittest.TestCase):
    def test_catalog_mode_is_preserved_without_cli_override(self) -> None:
        self.assertEqual(runner.resolve_icap_mode(None, "required"), "required")

    def test_explicit_cli_mode_overrides_catalog(self) -> None:
        self.assertEqual(runner.resolve_icap_mode("optional", "required"), "optional")

    def test_missing_values_fall_back_to_disabled(self) -> None:
        self.assertEqual(runner.resolve_icap_mode(None, None), "disabled")

    def test_invalid_catalog_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid ICAP mode"):
            runner.resolve_icap_mode(None, "best-effort")

    def test_build_plan_carries_catalog_mode_into_checkpoint(self) -> None:
        args = SimpleNamespace(
            activity_type="", target_ips="", mds_host="", current_version="",
            target_version="", icap_mode=None, preserve_original_active=None,
            tester_gate=None, package_source_dir="/var/log/tmp", cma_name="",
            cma_ip="", cluster_name="", policy_package="", target_take="",
            chg_number="CHG-TEST", change_number="", environment="lab",
            support_capture_script="",
        )
        plan = runner.build_base_plan(
            args,
            [],
            values={
                "activity_type": "software_patch_activity",
                "target_ips": "192.0.2.10,192.0.2.11",
                "mds_host": "192.0.2.1",
                "current_version": "R82",
                "target_version": "R82",
                "icap_mode": "required",
            },
        )
        self.assertEqual(plan["checkpoint"]["icap_mode"], "required")
        self.assertEqual(runner.runner_vars(plan, Path("plan.json"))["icap_mode"], "required")


class DeploymentBackendTests(unittest.TestCase):
    @staticmethod
    def plan(backend: str = "cdt", activity: str = "Software Patch Activity") -> dict:
        return {
            "change": {"activity_type": activity},
            "checkpoint": {
                "cluster_mode": "cluster",
                "preserve_original_active": True,
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            },
            "execution": {
                "deployment_backend": backend,
                "method": "Management Web API Central Deployment" if backend == "api" else "CDT (Central Deployment Tool)",
                "tester_pause": True,
            },
            "package_steps": [{
                "name": "install_take_76",
                "action": "install",
                "package_type": "jhf",
                "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar",
            }],
        }

    def test_backend_defaults_to_cdt_and_accepts_api_aliases(self) -> None:
        self.assertEqual(runner.resolve_deployment_backend(None), "cdt")
        self.assertEqual(runner.resolve_deployment_backend("management-api"), "api")
        self.assertEqual(runner.resolve_deployment_backend("web_api"), "api")
        with self.assertRaisesRegex(ValueError, "invalid deployment backend"):
            runner.resolve_deployment_backend("direct-ssh")

    def test_cdt_workflow_remains_separate_and_default(self) -> None:
        playbooks = [row[1] for row in runner.workflow_steps(self.plan())]
        self.assertIn("10_cdt_generate_candidates.yml", playbooks)
        self.assertIn("20_cdt_execute_guarded.yml", playbooks)
        self.assertNotIn("39_api_repository_package.yml", playbooks)
        self.assertNotIn("40_api_verify_package.yml", playbooks)
        self.assertNotIn("41_api_execute_package.yml", playbooks)

    def test_api_workflow_uses_only_api_deployment_playbooks(self) -> None:
        steps = runner.workflow_steps(self.plan("api"))
        playbooks = [row[1] for row in steps]
        self.assertNotIn("10_cdt_generate_candidates.yml", playbooks)
        self.assertNotIn("20_cdt_execute_guarded.yml", playbooks)
        self.assertEqual(playbooks.count("41_api_execute_package.yml"), 2)
        first = next(index for index, row in enumerate(steps) if row[0] == "first-member" and row[1] == "41_api_execute_package.yml")
        gate = next(index for index, row in enumerate(steps) if row[1] == "__gate__")
        second = next(index for index, row in enumerate(steps) if row[0] == "second-member" and row[1] == "41_api_execute_package.yml")
        self.assertLess(first, gate)
        self.assertLess(gate, second)

    def test_api_remove_uses_guarded_direct_fallback_with_explicit_failover(self) -> None:
        plan = self.plan("api")
        plan["package_steps"][0].update({
            "name": "remove_take_76",
            "action": "remove",
            "package_name": "Take 76",
        })
        steps = runner.workflow_steps(plan)
        playbooks = [row[1] for row in steps]
        self.assertEqual(playbooks.count("30_direct_package_step.yml"), 2)
        self.assertNotIn("41_api_execute_package.yml", playbooks)
        first = next(i for i, row in enumerate(steps) if row[0] == "first-member" and row[1] == "30_direct_package_step.yml")
        failover = next(i for i, row in enumerate(steps) if row[0] == "failover-to-first")
        gate = next(i for i, row in enumerate(steps) if row[1] == "__gate__")
        second = next(i for i, row in enumerate(steps) if row[0] == "second-member" and row[1] == "30_direct_package_step.yml")
        self.assertLess(first, failover)
        self.assertLess(failover, gate)
        self.assertLess(gate, second)

    def test_api_backend_fails_closed_for_unsupported_shapes(self) -> None:
        standalone = self.plan("api")
        standalone["checkpoint"]["cluster_mode"] = "standalone"
        with self.assertRaisesRegex(ValueError, "requires a cluster"):
            runner.workflow_steps(standalone)

        three_members = self.plan("api")
        three_members["checkpoint"]["members"].append({"ip": "192.0.2.22"})
        with self.assertRaisesRegex(ValueError, "exactly two cluster members"):
            runner.workflow_steps(three_members)

        multiple = self.plan("api")
        multiple["package_steps"].append(dict(multiple["package_steps"][0], name="second"))
        with self.assertRaisesRegex(ValueError, "exactly one package step"):
            runner.workflow_steps(multiple)


    def test_api_major_upgrade_preserves_mixed_version_controls(self) -> None:
        steps = runner.workflow_steps(self.plan("api", "Major Version Upgrade"))
        phases = [row[0] for row in steps]
        expected = [
            "first-member", "mixed-version-policy-gate", "mvc-on",
            "failover-to-first", "approve-testers", "second-member",
            "final-policy-install", "mvc-off",
        ]
        positions = [phases.index(phase) for phase in expected]
        self.assertEqual(positions, sorted(positions))


class GovernanceReadinessTests(unittest.TestCase):
    def context(self, readiness: list[dict]) -> dict:
        return {
            "chg": {
                "description": runner.AUTOMATION_MARKER,
                "state": "-1",
                "approval": "approved",
            },
            "ritm_id": "ritm",
            "readiness_tasks": readiness,
            "implementation_task": {"sys_id": "impl"},
        }

    def test_failed_or_skipped_readiness_does_not_authorize_execution(self) -> None:
        for state, status in (("4", "failed"), ("7", "ready"), ("3", "failed")):
            with self.subTest(state=state, status=status):
                with self.assertRaisesRegex(SystemExit, "Closed Complete"):
                    runner.validate_service_now_governance(
                        self.context([{
                            "number": "SCTASK_TEST",
                            "state": state,
                            "u_checkpoint_readiness_status": status,
                        }])
                    )

    def test_manual_ready_task_can_follow_historical_failed_task(self) -> None:
        runner.validate_service_now_governance(
            self.context([
                {"number": "SCTASK_AUTO", "state": "4", "u_checkpoint_readiness_status": "failed"},
                {"number": "SCTASK_MANUAL", "state": "3", "u_checkpoint_readiness_status": "ready"},
            ])
        )


class PhaseBoundaryTests(unittest.TestCase):
    STEPS = [
        ("first-member", "one.yml", "", {}),
        ("postcheck", "two.yml", "", {}),
    ]

    def test_unknown_start_and_stop_phases_fail_closed(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--start-at"):
            runner.validate_phase_boundaries(
                self.STEPS, start_at="unknown", stop_after="", skip_discovery=False
            )
        with self.assertRaisesRegex(SystemExit, "--stop-after"):
            runner.validate_phase_boundaries(
                self.STEPS, start_at="", stop_after="unknown", skip_discovery=False
            )

    def test_valid_phase_boundaries_are_accepted(self) -> None:
        runner.validate_phase_boundaries(
            self.STEPS, start_at="postcheck", stop_after="postcheck", skip_discovery=False
        )

    def test_discovery_stop_requires_discovery(self) -> None:
        with self.assertRaisesRegex(SystemExit, "skip-discovery"):
            runner.validate_phase_boundaries(
                self.STEPS, start_at="", stop_after="discover-targets", skip_discovery=True
            )


if __name__ == "__main__":
    unittest.main()
