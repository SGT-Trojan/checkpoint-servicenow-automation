from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import servicenow_checkpoint_runner as runner
import governed_cdt_artifacts as cdt_artifacts
import record_cdt_mutation as cdt_record


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


class ResumeEvidenceTests(unittest.TestCase):
    operation_id = "run_" + "9" * 64

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.operation_dir = self.root / "operation"
        self.operation_dir.mkdir(mode=0o700)
        for name in (
            "cdt_contexts",
            "cdt_mutation_receipts",
            "cdt_reconciliation",
        ):
            (self.operation_dir / name).mkdir(mode=0o700)
        self.plan = {
            "change": {
                "number": "CHG_TEST",
                "activity_type": "Major Version Upgrade",
            },
            "checkpoint": {
                "target_version": "R82",
                "target_take": "60",
                "cluster_mode": "cluster",
                "preserve_original_active": True,
                "members": [
                    {
                        "hostname": "example-fw-a",
                        "management_ip": "192.0.2.20",
                        "access_ip": "198.51.100.20",
                        "ip": "192.0.2.20",
                    },
                    {
                        "hostname": "example-fw-b",
                        "management_ip": "192.0.2.21",
                        "access_ip": "198.51.100.21",
                        "ip": "192.0.2.21",
                    },
                ],
            },
            "execution": {
                "deployment_backend": "cdt",
                "method": "CDT (Central Deployment Tool)",
                "tester_pause": True,
            },
            "package_steps": [{
                "name": "upgrade_r82",
                "action": "upgrade",
                "package_type": "blink",
                "package_name": "Check_Point_R82_T777_JHF_T60_Blink.tgz",
                "source_path": "/var/log/tmp/Check_Point_R82_T777_JHF_T60_Blink.tgz",
                "target_version": "R82",
                "target_take": "60",
                "target_build": "777",
            }],
        }
        self.plan_path = self.operation_dir / "activity_plan.json"
        self.plan_path.write_text(json.dumps(self.plan) + "\n")
        os.chmod(self.plan_path, 0o600)
        self.steps = runner.workflow_steps(self.plan)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_chain(self, phase: str) -> None:
        member = self.plan["checkpoint"]["members"][0 if phase == "first-member" else 1]
        step = "upgrade_r82"
        artifact_slug = runner.slug(f"{phase}_{step}")
        context_path = self.operation_dir / "cdt_contexts" / f"{artifact_slug}.json"
        receipt_path = (
            self.operation_dir / "cdt_mutation_receipts" / f"{artifact_slug}.json"
        )
        evidence_path = (
            self.operation_dir / "cdt_reconciliation" / f"{artifact_slug}.json"
        )
        context = {
            "schema": 1,
            "operation_id": self.operation_id,
            "change_identity": "CHG_TEST",
            "activity_plan_sha256": cdt_artifacts.plan_sha256(self.plan_path),
            "phase": phase,
            "step_name": step,
            "action": "upgrade",
            "target_host": member["access_ip"],
            "selected_candidate_ip": member["management_ip"],
            "package_name": self.plan["package_steps"][0]["package_name"],
            "package_type": "blink",
            "target_version": "R82",
            "target_take": "60",
            "target_build": "777",
            "identity_source": "immutable-activity-plan",
            "context_id": ("a" if phase == "first-member" else "b") * 64,
            "created_at_ns": __import__("time").time_ns() - 10_000,
        }
        context_bytes = cdt_artifacts.atomic_write_private_json(
            context_path, context
        )
        receipt = cdt_record.validate_context(
            context,
            context_bytes,
            self.plan_path,
            self.operation_id,
            phase,
            step,
        )
        receipt_bytes = cdt_artifacts.atomic_write_private_json(
            receipt_path, receipt
        )
        cdt_artifacts.atomic_write_private_json(
            evidence_path,
            {
                "schema": 1,
                "operation_id": self.operation_id,
                "change_identity": "CHG_TEST",
                "activity_plan_sha256": cdt_artifacts.plan_sha256(self.plan_path),
                "phase": phase,
                "step_name": step,
                "action": "upgrade",
                "target_host": member["access_ip"],
                "selected_candidate_ip": member["management_ip"],
                "package_name": context["package_name"],
                "package_type": "blink",
                "target_version": "R82",
                "target_take": "60",
                "target_build": "777",
                "identity_source": "immutable-activity-plan",
                "context_id": context["context_id"],
                "context_sha256": cdt_artifacts.sha256_bytes(context_bytes),
                "receipt_id": receipt["receipt_id"],
                "receipt_sha256": cdt_artifacts.sha256_bytes(receipt_bytes),
                "mutation_completed_at_ns": receipt["mutation_completed_at_ns"],
                "reconciled_at_ns": receipt["mutation_completed_at_ns"] + 1,
                "observed": {
                    "host": member["access_ip"],
                    "target_version": "R82",
                    "target_take": "60",
                    "target_build": "777",
                    "package_name": context["package_name"],
                    "result": "exact-target-confirmed",
                },
            },
        )

    def require(self, boundary: str) -> None:
        runner.require_resume_mutation_chain(
            self.steps,
            start_at=boundary,
            operation_dir=self.operation_dir,
            operation_id=self.operation_id,
            plan_path=self.plan_path,
        )

    def test_missing_prior_artifacts_block_every_later_resume_boundary(self) -> None:
        boundaries = (
            "mixed-version-policy-gate",
            "mvc-on",
            "failover-to-first",
            "approve-testers",
            "second-member",
            "final-policy-install",
            "mvc-off",
            "restore-original-active",
            "final-support-capture",
            "support-diff",
            "postcheck",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary), self.assertRaises(
                (FileNotFoundError, RuntimeError)
            ):
                self.require(boundary)

    def test_exact_member_specific_chain_permits_resume(self) -> None:
        self.write_chain("first-member")
        for boundary in (
            "mixed-version-policy-gate",
            "mvc-on",
            "failover-to-first",
            "approve-testers",
            "second-member",
        ):
            with self.subTest(boundary=boundary):
                self.require(boundary)
        self.write_chain("second-member")
        for boundary in (
            "final-policy-install",
            "mvc-off",
            "restore-original-active",
            "final-support-capture",
            "support-diff",
            "postcheck",
        ):
            with self.subTest(boundary=boundary):
                self.require(boundary)

    def test_cross_member_evidence_blocks_resume(self) -> None:
        self.write_chain("first-member")
        path = self.operation_dir / "cdt_reconciliation" / "first-member_upgrade_r82.json"
        payload = json.loads(path.read_text())
        path.unlink()
        payload["target_host"] = "198.51.100.21"
        cdt_artifacts.atomic_write_private_json(path, payload)
        with self.assertRaisesRegex(RuntimeError, "target_host"):
            self.require("second-member")

    def test_non_cdt_mutation_cannot_be_skipped_without_equivalent_chain(self) -> None:
        api_plan = json.loads(json.dumps(self.plan))
        api_plan["change"]["activity_type"] = "Software Patch Activity"
        api_plan["execution"].update({
            "deployment_backend": "api",
            "method": "Management Web API Central Deployment",
        })
        api_plan["package_steps"][0].update({
            "action": "install",
            "package_type": "jhf",
            "target_build": "",
        })
        with self.assertRaisesRegex(RuntimeError, "no immutable"):
            runner.require_resume_mutation_chain(
                runner.workflow_steps(api_plan),
                start_at="second-member",
                operation_dir=self.operation_dir,
                operation_id=self.operation_id,
                plan_path=self.plan_path,
            )




class GovernedOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_runs_dir = runner.RUNS_DIR
        self.temporary = tempfile.TemporaryDirectory()
        runner.RUNS_DIR = Path(self.temporary.name) / "runs"

    def tearDown(self) -> None:
        runner.RUNS_DIR = self.original_runs_dir
        self.temporary.cleanup()

    @staticmethod
    def plan(package_name: str = "Take91", generated_at: str = "first") -> dict:
        return {
            "generated_at": generated_at,
            "change": {"number": "CHG_TEST"},
            "checkpoint": {"members": [{"ip": "192.0.2.20"}]},
            "execution": {"deployment_backend": "api"},
            "package_steps": [{
                "name": "remove",
                "action": "remove",
                "package_name": package_name,
            }],
        }

    def test_manual_resume_requires_original_operation_id(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--start-at requires"):
            runner.resolve_operation_id("", "second-member")
        generated = runner.resolve_operation_id("", "")
        self.assertRegex(generated, r"^run_[0-9a-f]{64}$")
        self.assertEqual(
            runner.resolve_operation_id(generated, "second-member"),
            generated,
        )
        with self.assertRaisesRegex(SystemExit, "64 lowercase"):
            runner.resolve_operation_id("run_NOT_HEX", "")

    def test_plan_is_reused_and_authorization_drift_is_rejected(self) -> None:
        operation_id = "run_" + "c" * 64
        operation_dir, lock, created = runner.prepare_operation(operation_id)
        try:
            original, plan_path = runner.bind_operation_plan(
                operation_dir,
                operation_id,
                "change-sys-id",
                self.plan(generated_at="first"),
                is_resume=False,
                operation_dir_created=created,
            )
        finally:
            lock.close()

        operation_dir, lock, created = runner.prepare_operation(operation_id)
        try:
            resumed, resumed_path = runner.bind_operation_plan(
                operation_dir,
                operation_id,
                "change-sys-id",
                self.plan(generated_at="second"),
                is_resume=True,
                operation_dir_created=created,
            )
            self.assertEqual(resumed, original)
            self.assertEqual(resumed_path, plan_path)
            with self.assertRaisesRegex(RuntimeError, "authorization differs"):
                runner.bind_operation_plan(
                    operation_dir,
                    operation_id,
                    "change-sys-id",
                    self.plan(package_name="Take92", generated_at="third"),
                    is_resume=True,
                    operation_dir_created=created,
                )
        finally:
            lock.close()

    def test_operation_lock_and_empty_directory_collision_fail_closed(self) -> None:
        operation_id = "run_" + "d" * 64
        operation_dir, first_lock, created = runner.prepare_operation(operation_id)
        self.assertTrue(created)
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                runner.prepare_operation(operation_id)
        finally:
            first_lock.close()

        operation_dir, second_lock, created = runner.prepare_operation(operation_id)
        try:
            self.assertFalse(created)
            with self.assertRaisesRegex(RuntimeError, "collision refused"):
                runner.bind_operation_plan(
                    operation_dir,
                    operation_id,
                    "change-sys-id",
                    self.plan(),
                    is_resume=False,
                    operation_dir_created=created,
                )
        finally:
            second_lock.close()

    def test_operation_artifacts_are_private_and_digest_bound(self) -> None:
        operation_id = "run_" + "e" * 64
        operation_dir, lock, created = runner.prepare_operation(operation_id)
        try:
            _, plan_path = runner.bind_operation_plan(
                operation_dir,
                operation_id,
                "change-sys-id",
                self.plan(),
                is_resume=False,
                operation_dir_created=created,
            )
            state_path = operation_dir / "state.json"
            self.assertEqual(operation_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (operation_dir / "mutation_intents").stat().st_mode & 0o777,
                0o700,
            )
            self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            state = json.loads(state_path.read_text())
            plan_path.write_text(plan_path.read_text().replace("Take91", "Take92"))
            os.chmod(plan_path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "plan_sha256"):
                runner.bind_operation_plan(
                    operation_dir,
                    operation_id,
                    "change-sys-id",
                    self.plan(),
                    is_resume=True,
                    operation_dir_created=False,
                )
            self.assertEqual(state["operation_id"], operation_id)
        finally:
            lock.close()

    def test_runner_vars_carry_operation_intent_directory(self) -> None:
        plan = {
            "change": {"number": "CHG_TEST", "activity_type": "Software Patch Activity"},
            "checkpoint": {"members": [{"ip": "192.0.2.20"}]},
            "execution": {},
        }
        operation_id = "run_" + "f" * 64
        intent_dir = Path("/tmp/intents")
        values = runner.runner_vars(
            plan,
            Path("/tmp/plan.json"),
            "first-member",
            "remove",
            operation_id=operation_id,
            mutation_intent_dir=intent_dir,
        )
        self.assertEqual(values["operation_id"], operation_id)
        self.assertEqual(values["mutation_intent_dir"], str(intent_dir))


if __name__ == "__main__":
    unittest.main()
