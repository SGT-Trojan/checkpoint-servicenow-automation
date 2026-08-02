from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "checkpoint_standalone_workflow.py"
SPEC = importlib.util.spec_from_file_location("checkpoint_standalone_workflow", SCRIPT)
assert SPEC and SPEC.loader
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


def activity_plan(action: str = "install") -> dict:
    major = action == "upgrade"
    package = (
        "Check_Point_R82_Blink_T60.tar"
        if major
        else "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
    )
    return {
        "change": {"number": "STANDALONE_TEST"},
        "checkpoint": {
            "current_version": "R81.20",
            "target_version": "R82" if major else "R81.20",
            "target_take": "60" if major else "76",
            "cluster_mode": "cluster",
            "members": [
                {"hostname": "EXAMPLE-GW-A", "ip": "192.0.2.20"},
                {"hostname": "EXAMPLE-GW-B", "ip": "192.0.2.21"},
            ],
            "preserve_original_active": True,
            "icap_mode": "disabled",
            "mds_host": "192.0.2.10",
            "cma_name": "EXAMPLE-CMA",
            "domain": "EXAMPLE-DOMAIN",
            "cluster_name": "EXAMPLE-CLUSTER",
            "policy_package": "EXAMPLE-POLICY",
        },
        "execution": {
            "deployment_backend": "standalone",
            "tester_pause": True,
            "staging_method": "cprid_from_mds",
        },
        "package_steps": [
            {
                "name": "package_step",
                "action": action,
                "package_type": "blink" if major else "jhf",
                "package_name": package if action != "remove" else "Take76",
                "source_path": (
                    f"/var/log/tmp/{package}" if action != "remove" else ""
                ),
                "checksum_sha256": "a" * 64 if action != "remove" else "",
                "reboot_expected": True,
            }
        ],
    }


class StandaloneWorkflowTests(unittest.TestCase):
    def setup_run(self, action: str = "install"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        plan_path = root / "activity-plan.json"
        plan_path.write_text(json.dumps(activity_plan(action)), encoding="utf-8")
        run_dir = root / "run"
        return temporary, plan_path, run_dir

    def captured_roles(self, run_dir: Path) -> dict:
        state = {
            "original_active_host": "192.0.2.20",
            "original_standby_host": "192.0.2.21",
        }
        path = run_dir / "reports" / "cluster_initial_state_STANDALONE_TEST.json"
        workflow.write_private(path, json.dumps(state).encode())
        _, record, fd = workflow.open_captured_state(path)
        os.close(fd)
        return {
            **state,
            "state_sha256": record["sha256"],
            "state_file": record,
        }

    def plan_context_for_payload(self, payload: dict) -> dict:
        locked = Path(payload["locked_plan"]["path"])
        return workflow.plan_context(json.loads(locked.read_text(encoding="utf-8")))

    def intent_document(
        self,
        operation: dict,
        *,
        package_name: str | None = None,
    ) -> dict:
        resolved_package = (
            package_name
            if package_name is not None
            else operation["requested_package_name"]
        )
        document = {
            "schema": workflow.MUTATION_INTENT_VERSION,
            "host": operation["host"],
            "action": operation["action"],
            "step_name": operation["step_name"],
            "plan_sha256": operation["plan_sha256"],
            "requested_package_name": operation["requested_package_name"],
            "requested_source_path": operation["requested_source_path"],
            "requested_package_type": operation["requested_package_type"],
            "standalone_run_id": operation["run_id"],
            "standalone_operation_id": operation["operation_id"],
            "standalone_completion_id": operation["completion_id"],
            "standalone_event_nonce": operation["event_nonce"],
            "phase": operation["phase"],
            "package_name": resolved_package,
        }
        if operation["action"] != "remove":
            document.update(
                {
                    "target_version": operation["target_version"],
                    "target_take": operation["target_take"],
                }
            )
        return document

    def write_intent(
        self,
        payload: dict,
        operation: dict,
        *,
        package_name: str | None = None,
    ) -> tuple[str, dict]:
        run_dir = Path(payload["locked_plan"]["path"]).parent
        path = run_dir / "mutation-intents" / f"{operation['phase']}.json"
        document = self.intent_document(operation, package_name=package_name)
        workflow.write_private(
            path,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
        )
        _, evidence = workflow.read_protected_intent(
            path, not_before_ns=operation["created_at_ns"]
        )
        return (
            workflow.sha256_bytes(workflow.canonical_json(document)),
            evidence,
        )

    def synthetic_reconciliation(
        self,
        payload: dict,
        operation: dict,
        *,
        package_name: str | None = None,
    ) -> dict:
        digest, intent_evidence = self.write_intent(
            payload, operation, package_name=package_name
        )
        if operation["action"] == "remove":
            result = {
                "host": operation["host"],
                "result": "exact-package-absence-confirmed",
                "package_name": package_name or "Take76",
            }
        else:
            result = {
                "host": operation["host"],
                "result": "exact-target-confirmed",
                "target_version": operation["target_version"],
                "target_take": operation["target_take"],
                "package_name": operation["requested_package_name"],
            }
        reconciliation = {
            **result,
            "schema_version": workflow.RECONCILIATION_VERSION,
            "run_id": payload["run_id"],
            "plan_sha256": payload["plan_sha256"],
            "phase": operation["phase"],
            "operation_id": operation["operation_id"],
            "completion_id": operation["completion_id"],
            "event_nonce": operation["event_nonce"],
            "mutation_intent_sha256": digest,
        }
        reconciliation["_evidence"] = {
            "payload_sha256": workflow.sha256_bytes(
                workflow.canonical_json(reconciliation)
            )
        }
        reconciliation["_intent_evidence"] = intent_evidence
        return reconciliation

    def set_completed_phases(
        self,
        payload: dict,
        phases: list[str],
        started_at: datetime | None = None,
    ) -> datetime:
        started = started_at or datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
        context = self.plan_context_for_payload(payload)
        if not payload.get("initial_roles"):
            payload["initial_roles"] = {
                "original_active_host": context["members"][0],
                "original_standby_host": context["members"][1],
            }
        payload["completed_phases"] = []
        payload["phase_completions"] = {}
        payload["reconciliation"] = {}
        payload["pending_member_operation"] = {}
        run_dir = Path(payload["locked_plan"]["path"]).parent
        for member_phase in workflow.MEMBER_PHASES:
            candidate = run_dir / "mutation-intents" / f"{member_phase}.json"
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        for offset, phase in enumerate(phases, start=1):
            completed_at = started + timedelta(seconds=offset)
            operation = None
            if phase in workflow.MEMBER_PHASES:
                operation = workflow.create_pending_member_operation(
                    payload,
                    phase,
                    context,
                    event_nonce=f"{offset:064x}",
                    created_at=completed_at - timedelta(microseconds=1),
                    created_at_ns=time.time_ns(),
                )
                payload["pending_member_operation"] = operation
                payload["reconciliation"][phase] = self.synthetic_reconciliation(
                    payload, operation
                )
            record = workflow.completion_record(
                payload, phase, completed_at, operation
            )
            payload["completed_phases"].append(phase)
            payload["phase_completions"][phase] = record
            if operation is not None:
                payload["pending_member_operation"] = {}
        return workflow.parse_timestamp(
            payload["phase_completions"][phases[-1]]["completed_at"],
            f"{phases[-1]} completion record",
        )

    def stage_tester_gate(
        self, run_dir: Path, plan_path: Path
    ) -> tuple[dict, dict, datetime]:
        plan, plan_hash = workflow.load_plan(plan_path)
        context = workflow.plan_context(plan)
        payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
        gate_index = context["order"].index("simulate-tester-gate")
        phases = list(context["order"][:gate_index])
        failover_at = self.set_completed_phases(payload, phases)
        payload["initial_roles"] = self.captured_roles(run_dir)
        workflow.write_journal(run_dir / "workflow-state.json", payload)
        return context, payload, failover_at

    def healthy_samples(self, failover_at: datetime, count: int = 3) -> list[dict]:
        rows = []
        for sample_id in range(1, count + 1):
            rows.append(
                {
                    "sample": sample_id,
                    "timestamp": workflow.iso8601_utc(
                        failover_at + timedelta(seconds=sample_id * 20)
                    ),
                    "active_count": 1,
                    "standby_count": 1,
                    "cluster_shape_ok": True,
                    "members": [
                        {
                            "host": "192.0.2.20",
                            "cluster_state": "STANDBY",
                            "pnotes_ok": True,
                            "interfaces_ok": True,
                            "take": "75",
                            "error": "",
                        },
                        {
                            "host": "192.0.2.21",
                            "cluster_state": "ACTIVE",
                            "pnotes_ok": True,
                            "interfaces_ok": True,
                            "take": "76",
                            "error": "",
                        },
                    ],
                }
            )
        return rows

    def write_evidence(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def write_reconciliation_command(
        self,
        command: list[str],
        pass_fds: tuple[int, ...],
        value: dict,
    ) -> None:
        fd = int(command[command.index("--reconciliation-fd") + 1])
        self.assertIn(fd, pass_fds)
        plan_path = Path(command[command.index("--activity-plan-file") + 1])
        context = workflow.plan_context(
            json.loads(plan_path.read_text(encoding="utf-8"))
        )
        operation = {
            "host": value["host"],
            "action": context["action"],
            "step_name": context["step_name"],
            "plan_sha256": command[
                command.index("--standalone-plan-sha256") + 1
            ],
            "run_id": command[command.index("--standalone-run-id") + 1],
            "phase": command[command.index("--standalone-phase") + 1],
            "operation_id": command[
                command.index("--standalone-operation-id") + 1
            ],
            "completion_id": command[
                command.index("--standalone-completion-id") + 1
            ],
            "event_nonce": command[
                command.index("--standalone-event-nonce") + 1
            ],
            "requested_package_name": str(
                context["step"].get("package_name") or ""
            ),
            "requested_source_path": str(
                context["step"].get("source_path") or ""
            ),
            "requested_package_type": str(
                context["step"].get("package_type") or ""
            ),
            "target_version": context["target_version"],
            "target_take": context["target_take"],
        }
        package_name = (
            str(value.get("package_name") or "")
            if operation["action"] == "remove"
            else None
        )
        document = self.intent_document(operation, package_name=package_name)
        intent_path = Path(
            command[command.index("--mutation-intent-file") + 1]
        )
        if not intent_path.exists():
            workflow.write_private(
                intent_path,
                (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
            )
        value = {
            **value,
            "schema_version": workflow.RECONCILIATION_VERSION,
            "run_id": operation["run_id"],
            "plan_sha256": operation["plan_sha256"],
            "phase": operation["phase"],
            "operation_id": operation["operation_id"],
            "completion_id": operation["completion_id"],
            "event_nonce": operation["event_nonce"],
            "mutation_intent_sha256": workflow.sha256_bytes(
                workflow.canonical_json(document)
            ),
        }
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        self.assertEqual(os.write(fd, raw), len(raw))
        os.fsync(fd)

    def bound_reconciliation(
        self,
        payload: dict,
        phase: str,
        value: dict,
    ) -> tuple[dict, dict, Path]:
        context = self.plan_context_for_payload(payload)
        if not payload.get("initial_roles"):
            payload["initial_roles"] = {
                "original_active_host": context["members"][0],
                "original_standby_host": context["members"][1],
            }
        operation = workflow.create_pending_member_operation(
            payload,
            phase,
            context,
            event_nonce="b" * 64,
            created_at_ns=time.time_ns(),
        )
        digest, _ = self.write_intent(
            payload,
            operation,
            package_name=(
                str(value.get("package_name") or "")
                if operation["action"] == "remove"
                else None
            ),
        )
        return (
            {
                **value,
                "schema_version": workflow.RECONCILIATION_VERSION,
                "run_id": payload["run_id"],
                "plan_sha256": payload["plan_sha256"],
                "phase": phase,
                "operation_id": operation["operation_id"],
                "completion_id": operation["completion_id"],
                "event_nonce": operation["event_nonce"],
                "mutation_intent_sha256": digest,
            },
            operation,
            Path(payload["locked_plan"]["path"]).parent
            / "workflow-state.json",
        )

    def test_validate_writes_private_integrity_checked_journal(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            self.assertEqual(
                workflow.main(
                    [
                        "validate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                ),
                0,
            )
            journal = run_dir / "workflow-state.json"
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())
            payload = workflow.read_journal(journal, plan_hash)
            self.assertEqual(payload["completed_phases"], ["validate"])
            locked = run_dir / workflow.LOCKED_PLAN_NAME
            self.assertEqual(locked.read_bytes(), plan_path.read_bytes())
            self.assertEqual(locked.stat().st_mode & 0o777, 0o600)
            self.assertEqual(payload["locked_plan"]["path"], str(locked))
            self.assertEqual(payload["locked_plan"]["sha256"], plan_hash)
            self.assertEqual(payload["source_plan"]["path"], str(plan_path))

    def test_validate_creates_distinct_strict_run_identities(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        with temporary:
            root = Path(temporary.name)
            plan_path = root / "activity-plan.json"
            plan_path.write_text(json.dumps(activity_plan()), encoding="utf-8")
            run_a = root / "run-a"
            run_b = root / "run-b"
            with mock.patch.object(
                workflow.secrets,
                "token_hex",
                side_effect=("a" * 64, "b" * 64),
            ) as token_hex:
                self.assertEqual(
                    workflow.main(
                        [
                            "validate",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_a),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    workflow.main(
                        [
                            "validate",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_b),
                        ]
                    ),
                    0,
                )
            self.assertEqual(token_hex.call_args_list, [mock.call(32), mock.call(32)])
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())
            payload_a = workflow.read_journal(run_a / "workflow-state.json", plan_hash)
            payload_b = workflow.read_journal(run_b / "workflow-state.json", plan_hash)
            self.assertEqual(payload_a["run_id"], f"run_{'a' * 64}")
            self.assertEqual(payload_b["run_id"], f"run_{'b' * 64}")
            self.assertRegex(payload_a["run_id"], r"^run_[0-9a-f]{64}$")
            self.assertNotEqual(payload_a["run_id"], payload_b["run_id"])
            self.assertEqual(
                payload_a["phase_completions"]["validate"]["run_id"],
                payload_a["run_id"],
            )

    def test_journal_rejects_missing_malformed_substituted_and_mixed_run_ids(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            self.assertEqual(
                workflow.main(
                    [
                        "validate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                ),
                0,
            )
            journal = run_dir / "workflow-state.json"
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())
            payload = workflow.read_journal(journal, plan_hash)

            for bad_run_id in (None, "", "run_short", f"run_{'A' * 64}", 7):
                with self.subTest(run_id=bad_run_id):
                    candidate = json.loads(json.dumps(payload))
                    if bad_run_id is None:
                        candidate.pop("run_id")
                    else:
                        candidate["run_id"] = bad_run_id
                    workflow.write_journal(journal, candidate)
                    with self.assertRaisesRegex(workflow.WorkflowError, "run identity"):
                        workflow.read_journal(journal, plan_hash)

            substituted = json.loads(json.dumps(payload))
            substituted["run_id"] = f"run_{'b' * 64}"
            workflow.write_journal(journal, substituted)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.read_journal(journal, plan_hash)

            missing_ledger_id = json.loads(json.dumps(payload))
            missing_ledger_id["phase_completions"]["validate"].pop("run_id")
            workflow.write_journal(journal, missing_ledger_id)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.read_journal(journal, plan_hash)

            mixed = json.loads(json.dumps(payload))
            record = mixed["phase_completions"]["validate"]
            record["run_id"] = f"run_{'b' * 64}"
            core = {
                key: record[key]
                for key in (
                    "phase",
                    "sequence",
                    "plan_sha256",
                    "run_id",
                )
            }
            record["completion_id"] = workflow.sha256_bytes(
                workflow.canonical_json(core)
            )
            workflow.write_journal(journal, mixed)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.read_journal(journal, plan_hash)

    def test_cross_run_replay_is_rejected_and_same_run_resume_passes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        with temporary:
            root = Path(temporary.name)
            plan_path = root / "activity-plan.json"
            plan_path.write_text(json.dumps(activity_plan()), encoding="utf-8")
            run_a = root / "run-a"
            run_b = root / "run-b"
            for run_dir in (run_a, run_b):
                self.assertEqual(
                    workflow.main(
                        [
                            "validate",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    0,
                )

            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload_a = workflow.read_journal(
                run_a / "workflow-state.json", plan_hash
            )
            payload_b = workflow.read_journal(
                run_b / "workflow-state.json", plan_hash
            )
            self.assertNotEqual(payload_a["run_id"], payload_b["run_id"])
            gate_index = context["order"].index("simulate-tester-gate")
            phases = list(context["order"][:gate_index])
            started = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
            failover_a = self.set_completed_phases(payload_a, phases, started)
            self.set_completed_phases(
                payload_b, phases, started + timedelta(hours=1)
            )
            payload_a["initial_roles"] = self.captured_roles(run_a)
            payload_b["initial_roles"] = self.captured_roles(run_b)
            evidence_a = run_a / "monitor.jsonl"
            self.write_evidence(evidence_a, self.healthy_samples(failover_a))
            payload_a["tester_gate"] = workflow.validate_gate_evidence(
                evidence_a, context, payload_a
            )
            gate_completion = workflow.completion_record(
                payload_a,
                "simulate-tester-gate",
                failover_a + timedelta(minutes=2),
            )
            payload_a["completed_phases"].append("simulate-tester-gate")
            payload_a["phase_completions"]["simulate-tester-gate"] = gate_completion
            workflow.write_journal(run_a / "workflow-state.json", payload_a)

            same_run = workflow.read_journal(
                run_a / "workflow-state.json", plan_hash
            )
            self.assertEqual(same_run["tester_gate"]["run_id"], same_run["run_id"])
            self.assertEqual(
                same_run["tester_gate"]["failover_completion"]["run_id"],
                same_run["run_id"],
            )
            workflow.verify_order(same_run, "second-member", context["order"])

            replay = json.loads(json.dumps(payload_b))
            replay["completed_phases"] = json.loads(
                json.dumps(payload_a["completed_phases"])
            )
            replay["phase_completions"] = json.loads(
                json.dumps(payload_a["phase_completions"])
            )
            replay["tester_gate"] = json.loads(json.dumps(payload_a["tester_gate"]))
            workflow.write_journal(run_b / "workflow-state.json", replay)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.read_journal(run_b / "workflow-state.json", plan_hash)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.validate_gate_evidence(evidence_a, context, replay)

    def test_tester_record_rejects_substituted_run_and_failover_bindings(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            self.assertEqual(
                workflow.main(
                    [
                        "validate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                ),
                0,
            )
            context, payload, failover_at = self.stage_tester_gate(
                run_dir, plan_path
            )
            evidence = run_dir / "monitor.jsonl"
            self.write_evidence(evidence, self.healthy_samples(failover_at))
            payload["tester_gate"] = workflow.validate_gate_evidence(
                evidence, context, payload
            )
            gate_completion = workflow.completion_record(
                payload,
                "simulate-tester-gate",
                failover_at + timedelta(minutes=2),
            )
            payload["completed_phases"].append("simulate-tester-gate")
            payload["phase_completions"]["simulate-tester-gate"] = gate_completion
            journal = run_dir / "workflow-state.json"
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())

            wrong_gate = json.loads(json.dumps(payload))
            wrong_gate["tester_gate"]["run_id"] = f"run_{'b' * 64}"
            workflow.write_journal(journal, wrong_gate)
            with self.assertRaisesRegex(workflow.WorkflowError, "tester-gate run identity"):
                workflow.read_journal(journal, plan_hash)

            wrong_failover = json.loads(json.dumps(payload))
            wrong_failover["tester_gate"]["failover_completion"]["run_id"] = (
                f"run_{'b' * 64}"
            )
            workflow.write_journal(journal, wrong_failover)
            with self.assertRaisesRegex(workflow.WorkflowError, "failover binding"):
                workflow.read_journal(journal, plan_hash)

    def test_plan_context_rejects_unsupported_action_package_type_pairs(self) -> None:
        cases = (
            ("install", "deployment_agent", "jhf"),
            ("remove", "deployment_agent", "jhf"),
            ("upgrade", "deployment_agent", "blink"),
            ("install", "blink", "jhf"),
            ("remove", "blink", "jhf"),
            ("upgrade", "jhf", "blink"),
        )
        for action, package_type, required in cases:
            with self.subTest(action=action, package_type=package_type):
                plan = activity_plan(action)
                plan["package_steps"][0]["package_type"] = package_type
                with self.assertRaisesRegex(
                    workflow.WorkflowError,
                    f"standalone {action} requires package_type {required}",
                ):
                    workflow.plan_context(plan)

    def test_deployment_agent_plan_never_reaches_helper_command(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            plan = activity_plan()
            plan["package_steps"][0]["package_type"] = "deployment_agent"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with (
                mock.patch.object(workflow, "command_for_phase") as command,
                mock.patch.object(workflow, "run_helper") as helper,
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "validate",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    2,
                )
            command.assert_not_called()
            helper.assert_not_called()

    def test_helper_uses_locked_bytes_during_source_replacement(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            original = plan_path.read_bytes()
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan_hash = workflow.sha256_bytes(original)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            self.set_completed_phases(
                payload, ["validate", "capture-state", "baseline-capture"]
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(run_dir / "workflow-state.json", payload)

            def replace_source(command, phase, current_run_dir, pass_fds=()):
                helper_plan = Path(command[command.index("--activity-plan-file") + 1])
                self.assertTrue(str(helper_plan).startswith("/proc/self/fd/"))
                self.assertIn(int(helper_plan.name), pass_fds)
                self.assertEqual(helper_plan.read_bytes(), original)
                replacement = current_run_dir / "replacement-locked-plan.json"
                changed = activity_plan()
                changed["change"]["number"] = "REPLACED_LOCKED_PLAN"
                workflow.write_private(replacement, json.dumps(changed).encode())
                replacement.replace(current_run_dir / workflow.LOCKED_PLAN_NAME)
                self.assertEqual(helper_plan.read_bytes(), original)
                return "staged descriptor-bound plan\n"

            with mock.patch.object(workflow, "run_helper", side_effect=replace_source):
                self.assertEqual(
                    workflow.main(
                        [
                            "stage-files",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    0,
                )
            with mock.patch.object(workflow, "run_helper") as helper:
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
                helper.assert_not_called()

    def test_run_helper_inherits_verified_fd_across_path_replacement(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.protected_directory(run_dir)
            workflow.protected_directory(run_dir / "logs")
            original = plan_path.read_bytes()
            fd = os.open(plan_path, os.O_RDONLY | os.O_NOFOLLOW)
            real_run = workflow.subprocess.run

            def replace_at_dispatch(command, **kwargs):
                replacement = plan_path.with_name("dispatch-replacement.json")
                replacement.write_text('{"change":{"number":"REPLACED"}}')
                replacement.replace(plan_path)
                return real_run(command, **kwargs)

            try:
                with mock.patch.object(
                    workflow.subprocess, "run", side_effect=replace_at_dispatch
                ):
                    output = workflow.run_helper(
                        [
                            os.sys.executable,
                            "-c",
                            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                            f"/proc/self/fd/{fd}",
                        ],
                        "fd-race",
                        run_dir,
                        pass_fds=(fd,),
                    )
            finally:
                os.close(fd)
            self.assertEqual(output.strip().encode(), original)

    def test_same_byte_source_replacement_fails_identity_binding(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            replacement = plan_path.with_name("same-bytes-replacement.json")
            replacement.write_bytes(plan_path.read_bytes())
            replacement.replace(plan_path)
            with mock.patch.object(workflow, "run_helper") as helper:
                self.assertEqual(
                    workflow.main(
                        [
                            "capture-state",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
                helper.assert_not_called()

    def test_show_state_warns_but_prints_journal_for_damaged_artifacts(self) -> None:
        for attack in ("content", "mode", "replacement", "symlink"):
            with self.subTest(attack=attack):
                temporary, plan_path, run_dir = self.setup_run()
                with temporary:
                    workflow.main(
                        ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
                    )
                    locked = run_dir / workflow.LOCKED_PLAN_NAME
                    if attack == "content":
                        locked.write_bytes(locked.read_bytes() + b" ")
                    elif attack == "mode":
                        locked.chmod(0o640)
                    elif attack == "replacement":
                        replacement = run_dir / "replacement-plan.json"
                        replacement.write_bytes(locked.read_bytes())
                        replacement.chmod(0o600)
                        replacement.replace(locked)
                    else:
                        locked.unlink()
                        locked.symlink_to(plan_path)
                    with mock.patch("sys.stderr") as stderr:
                        self.assertEqual(
                            workflow.main(
                                [
                                    "show-state",
                                    "--activity-plan-file",
                                    str(plan_path),
                                    "--run-dir",
                                    str(run_dir),
                                ]
                            ),
                            0,
                        )
                    self.assertTrue(stderr.write.called)

    def test_show_state_survives_missing_source_and_locked_plan(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan_path.unlink()
            (run_dir / workflow.LOCKED_PLAN_NAME).unlink()
            with (
                mock.patch("sys.stderr") as stderr,
                mock.patch("sys.stdout") as stdout,
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "show-state",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    0,
                )
            rendered = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertIn('"completed_phases"', rendered)
            warnings = "".join(call.args[0] for call in stderr.write.call_args_list)
            self.assertIn("source plan is unavailable", warnings)
            self.assertIn("locked plan integrity", warnings)

    def test_mutating_preflight_requires_staging_and_management_fields(self) -> None:
        plan = activity_plan()
        plan["execution"]["staging_method"] = ""
        with self.assertRaisesRegex(workflow.WorkflowError, "staging_method"):
            workflow.plan_context(plan)

        plan = activity_plan()
        plan["checkpoint"]["mds_host"] = ""
        with self.assertRaisesRegex(workflow.WorkflowError, "mds_host"):
            workflow.plan_context(plan)

        for field in ("cma_name", "domain", "cluster_name", "policy_package"):
            plan = activity_plan("upgrade")
            plan["checkpoint"][field] = ""
            with self.subTest(field=field), self.assertRaisesRegex(
                workflow.WorkflowError, field
            ):
                workflow.plan_context(plan)

        removal = activity_plan("remove")
        removal["execution"].pop("staging_method")
        removal["checkpoint"]["mds_host"] = ""
        self.assertEqual(workflow.plan_context(removal)["kind"], "patch-remove")

    def test_tampered_journal_and_plan_mismatch_fail_closed(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            journal = run_dir / "workflow-state.json"
            envelope = json.loads(journal.read_text())
            envelope["payload"]["completed_phases"].append("capture-state")
            journal.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(workflow.WorkflowError, "integrity"):
                workflow.read_journal(
                    journal, workflow.sha256_bytes(plan_path.read_bytes())
                )

        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan = json.loads(plan_path.read_text())
            plan["change"]["number"] = "DIFFERENT_PLAN"
            plan_path.write_text(json.dumps(plan))
            with mock.patch("sys.stderr") as stderr:
                self.assertEqual(
                    workflow.main(
                        ["show-state", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
                    ),
                    0,
                )
            self.assertTrue(stderr.write.called)

    def test_resume_order_and_gate_evidence_are_enforced(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            self.assertEqual(
                workflow.main(
                    [
                        "first-member",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                        "--execute",
                    ]
                ),
                2,
            )
            _, _, failover_at = self.stage_tester_gate(run_dir, plan_path)
            self.assertEqual(
                workflow.main(
                    [
                        "simulate-tester-gate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                        "--execute",
                    ]
                ),
                2,
            )
            empty = run_dir / "empty.jsonl"
            empty.write_text("")
            empty.chmod(0o600)
            self.assertEqual(
                workflow.main(
                    [
                        "simulate-tester-gate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                        "--evidence",
                        str(empty),
                        "--execute",
                    ]
                ),
                2,
            )
            evidence = run_dir / "monitor.jsonl"
            samples = self.healthy_samples(failover_at)
            self.write_evidence(evidence, samples)
            reversed_evidence = run_dir / "monitor-reversed.jsonl"
            reversed_samples = json.loads(json.dumps(samples))
            for sample in reversed_samples:
                sample["members"][0]["cluster_state"] = "ACTIVE"
                sample["members"][1]["cluster_state"] = "STANDBY"
            self.write_evidence(reversed_evidence, reversed_samples)
            self.assertEqual(
                workflow.main(
                    [
                        "simulate-tester-gate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                        "--evidence",
                        str(reversed_evidence),
                        "--execute",
                    ]
                ),
                2,
            )
            self.assertEqual(
                workflow.main(
                    [
                        "simulate-tester-gate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                        "--evidence",
                        str(evidence),
                        "--execute",
                    ]
                ),
                0,
            )

    def test_gate_counts_require_exact_integer_one(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            valid = self.healthy_samples(failover_at)
            invalid_values = (True, False, 1.0, "1", None)
            for field in ("active_count", "standby_count"):
                for value in invalid_values:
                    with self.subTest(field=field, value=value):
                        rows = json.loads(json.dumps(valid))
                        rows[0][field] = value
                        evidence = run_dir / f"{field}-{type(value).__name__}.jsonl"
                        self.write_evidence(evidence, rows)
                        with self.assertRaisesRegex(
                            workflow.WorkflowError, "unhealthy cluster shape"
                        ):
                            workflow.validate_gate_evidence(evidence, context, payload)

            accepted = run_dir / "integer-counts.jsonl"
            self.write_evidence(accepted, valid)
            workflow.validate_gate_evidence(accepted, context, payload)

    def test_ipv6_member_and_tester_host_identity_is_semantic(self) -> None:
        duplicate_plan = activity_plan()
        duplicate_plan["checkpoint"]["members"] = [
            {"hostname": "A", "ip": "2001:db8::1"},
            {"hostname": "B", "ip": "2001:0db8:0:0:0:0:0:1"},
        ]
        with self.assertRaisesRegex(workflow.WorkflowError, "must be distinct"):
            workflow.plan_context(duplicate_plan)

        temporary = tempfile.TemporaryDirectory()
        with temporary:
            root = Path(temporary.name)
            plan_path = root / "activity-plan.json"
            plan = activity_plan()
            plan["checkpoint"]["members"] = [
                {"hostname": "A", "ip": "2001:db8::1"},
                {"hostname": "B", "ip": "2001:db8::2"},
            ]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            run_dir = root / "run"
            self.assertEqual(
                workflow.main(
                    [
                        "validate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                ),
                0,
            )
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(
                run_dir / "workflow-state.json",
                workflow.sha256_bytes(plan_path.read_bytes()),
            )
            gate_index = context["order"].index("simulate-tester-gate")
            failover_at = self.set_completed_phases(
                payload, list(context["order"][:gate_index])
            )
            payload["initial_roles"] = {
                "original_active_host": "2001:db8::1",
                "original_standby_host": "2001:db8::2",
            }
            rows = self.healthy_samples(failover_at)
            for row in rows:
                row["members"][0]["host"] = "2001:0db8:0:0:0:0:0:1"
                row["members"][1]["host"] = "2001:0db8:0:0:0:0:0:2"
            accepted = run_dir / "ipv6-equivalent.jsonl"
            self.write_evidence(accepted, rows)
            workflow.validate_gate_evidence(accepted, context, payload)

            hostile = json.loads(json.dumps(rows))
            hostile[0]["members"][1]["host"] = "2001:0db8:0:0:0:0:0:1"
            evidence = run_dir / "ipv6-semantic-duplicate.jsonl"
            self.write_evidence(evidence, hostile)
            with self.assertRaisesRegex(workflow.WorkflowError, "duplicate member host"):
                workflow.validate_gate_evidence(evidence, context, payload)

    def test_gate_members_reject_duplicates_non_objects_and_invalid_hosts(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            valid = self.healthy_samples(failover_at)
            member_a, member_b = valid[0]["members"]
            cases = {
                "three-with-a-duplicate": [member_a, member_a, member_b],
                "three-with-b-duplicate": [member_a, member_b, member_b],
                "two-duplicates": [member_a, member_a],
                "non-object": [member_a, "192.0.2.21"],
                "boolean-host": [member_a, {**member_b, "host": True}],
                "missing-host": [member_a, {key: value for key, value in member_b.items() if key != "host"}],
                "empty-host": [member_a, {**member_b, "host": ""}],
            }
            for name, members in cases.items():
                with self.subTest(case=name):
                    rows = json.loads(json.dumps(valid))
                    rows[0]["members"] = members
                    evidence = run_dir / f"members-{name}.jsonl"
                    self.write_evidence(evidence, rows)
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.validate_gate_evidence(evidence, context, payload)

            accepted = run_dir / "members-valid.jsonl"
            self.write_evidence(accepted, valid)
            workflow.validate_gate_evidence(accepted, context, payload)

    def test_gate_evidence_is_validated_and_hashed_from_one_descriptor_read(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            evidence = run_dir / "monitor.jsonl"
            self.write_evidence(evidence, self.healthy_samples(failover_at))
            expected_hash = workflow.sha256_bytes(evidence.read_bytes())

            def forbidden_second_read(*args, **kwargs):
                replacement = run_dir / "replacement.jsonl"
                self.write_evidence(
                    replacement, self.healthy_samples(failover_at + timedelta(hours=1))
                )
                replacement.replace(evidence)
                raise AssertionError("tester evidence path was read a second time")

            reads = 0
            real_fdopen = workflow.os.fdopen

            class ReadCountingHandle:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

                def read(self):
                    nonlocal reads
                    reads += 1
                    return self.handle.read()

            def counted_fdopen(*args, **kwargs):
                return ReadCountingHandle(real_fdopen(*args, **kwargs))

            with (
                mock.patch.object(workflow.os, "fdopen", side_effect=counted_fdopen),
                mock.patch.object(workflow.os, "open", wraps=os.open) as opened,
                mock.patch.object(Path, "read_text", side_effect=forbidden_second_read) as read_text,
                mock.patch.object(Path, "read_bytes", side_effect=forbidden_second_read) as read_bytes,
            ):
                record = workflow.validate_gate_evidence(evidence, context, payload)
            opened.assert_called_once()
            self.assertEqual(reads, 1)
            read_text.assert_not_called()
            read_bytes.assert_not_called()
            self.assertEqual(record["sha256"], expected_hash)
            self.assertEqual(record["mode"], 0o600)
            self.assertEqual(record["owner_uid"], os.geteuid())
            self.assertEqual(
                record["failover_completion"]["completion_id"],
                payload["phase_completions"]["failover-to-first"]["completion_id"],
            )

    def test_protected_reads_fail_closed_without_o_nofollow(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            run_dir.mkdir(mode=0o700)
            evidence = run_dir / "evidence.jsonl"
            self.write_evidence(evidence, [{"sample": 1}])
            with (
                mock.patch.object(workflow.os, "O_NOFOLLOW", None),
                self.assertRaisesRegex(workflow.WorkflowError, "O_NOFOLLOW is required"),
            ):
                workflow.read_protected_evidence(evidence)

    def test_gate_evidence_rejects_symlink_mode_owner_and_path_replacement(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            evidence = run_dir / "monitor.jsonl"
            self.write_evidence(evidence, self.healthy_samples(failover_at))

            link = run_dir / "monitor-link.jsonl"
            link.symlink_to(evidence)
            with self.assertRaisesRegex(workflow.WorkflowError, "protected evidence"):
                workflow.validate_gate_evidence(link, context, payload)

            evidence.chmod(0o640)
            with self.assertRaisesRegex(workflow.WorkflowError, "mode 0600"):
                workflow.validate_gate_evidence(evidence, context, payload)
            evidence.chmod(0o600)

            with (
                mock.patch.object(workflow.os, "geteuid", return_value=os.geteuid() + 1),
                self.assertRaisesRegex(workflow.WorkflowError, "owned by"),
            ):
                workflow.validate_gate_evidence(evidence, context, payload)

            opened = evidence.lstat()
            replacement = mock.Mock(st_dev=opened.st_dev, st_ino=opened.st_ino + 1)
            original_lstat = Path.lstat

            def replaced_lstat(candidate: Path):
                if candidate == Path(os.path.abspath(evidence)):
                    return replacement
                return original_lstat(candidate)

            with (
                mock.patch.object(Path, "lstat", replaced_lstat),
                self.assertRaisesRegex(workflow.WorkflowError, "changed while"),
            ):
                workflow.validate_gate_evidence(evidence, context, payload)

    def test_gate_evidence_rejects_same_inode_content_and_metadata_changes(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            original_lstat = Path.lstat

            for change in ("content", "metadata"):
                with self.subTest(change=change):
                    evidence = run_dir / f"monitor-{change}.jsonl"
                    self.write_evidence(evidence, self.healthy_samples(failover_at))
                    original_bytes = evidence.read_bytes()
                    changed = False

                    def mutate_during_lstat(candidate: Path):
                        nonlocal changed
                        if (
                            not changed
                            and candidate == Path(os.path.abspath(evidence))
                        ):
                            changed = True
                            if change == "content":
                                replacement = bytearray(original_bytes)
                                replacement[0] = ord(" ")
                                fd = os.open(evidence, os.O_WRONLY)
                                try:
                                    os.pwrite(fd, replacement, 0)
                                    os.fsync(fd)
                                    stat_before = original_lstat(evidence)
                                    os.utime(
                                        evidence,
                                        ns=(
                                            stat_before.st_atime_ns,
                                            stat_before.st_mtime_ns + 1_000_000_000,
                                        ),
                                    )
                                finally:
                                    os.close(fd)
                            else:
                                stat_before = original_lstat(evidence)
                                os.utime(
                                    evidence,
                                    ns=(
                                        stat_before.st_atime_ns,
                                        stat_before.st_mtime_ns + 1_000_000_000,
                                    ),
                                )
                        return original_lstat(candidate)

                    with (
                        mock.patch.object(Path, "lstat", mutate_during_lstat),
                        self.assertRaisesRegex(workflow.WorkflowError, "changed while"),
                    ):
                        workflow.validate_gate_evidence(evidence, context, payload)
                    self.assertTrue(changed)

    def test_gate_evidence_rejects_replayed_invalid_and_stale_samples(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            valid = self.healthy_samples(failover_at)
            cases: dict[str, list[dict]] = {}

            identical = json.loads(json.dumps(valid))
            identical[1]["sample"] = identical[0]["sample"]
            identical[1]["timestamp"] = identical[0]["timestamp"]
            cases["identical-replay"] = identical

            reversed_ids = json.loads(json.dumps(valid))
            reversed_ids[1]["sample"] = 0
            cases["reversed-id"] = reversed_ids

            negative_id = json.loads(json.dumps(valid))
            negative_id[0]["sample"] = -1
            cases["negative-id"] = negative_id

            missing_id = json.loads(json.dumps(valid))
            missing_id[0].pop("sample")
            cases["missing-id"] = missing_id

            repeated_time = json.loads(json.dumps(valid))
            repeated_time[1]["timestamp"] = repeated_time[0]["timestamp"]
            cases["repeated-time"] = repeated_time

            reversed_time = json.loads(json.dumps(valid))
            reversed_time[2]["timestamp"] = reversed_time[0]["timestamp"]
            cases["reversed-time"] = reversed_time

            missing_time = json.loads(json.dumps(valid))
            missing_time[0].pop("timestamp")
            cases["missing-time"] = missing_time

            malformed_time = json.loads(json.dumps(valid))
            malformed_time[0]["timestamp"] = "not-a-timestamp"
            cases["malformed-time"] = malformed_time

            naive_time = json.loads(json.dumps(valid))
            naive_time[0]["timestamp"] = "2026-07-31T20:00:20"
            cases["naive-time"] = naive_time

            stale = json.loads(json.dumps(valid))
            stale[0]["timestamp"] = workflow.iso8601_utc(failover_at)
            cases["pre-failover"] = stale

            for name, rows in cases.items():
                with self.subTest(case=name):
                    evidence = run_dir / f"{name}.jsonl"
                    self.write_evidence(evidence, rows)
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.validate_gate_evidence(evidence, context, payload)

            accepted = run_dir / "valid.jsonl"
            self.write_evidence(accepted, valid)
            record = workflow.validate_gate_evidence(accepted, context, payload)
            self.assertEqual(record["samples"], 3)
            self.assertEqual(record["first_sample_id"], 1)
            self.assertEqual(record["last_sample_id"], 3)
            self.assertEqual(
                record["authorization"], "simulated-after-technical-validation"
            )

    def test_gate_evidence_uses_one_clock_ceiling_and_rejects_future_samples(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            context, payload, failover_at = self.stage_tester_gate(run_dir, plan_path)
            ceiling = failover_at + timedelta(seconds=60)
            boundary_rows = self.healthy_samples(failover_at)
            boundary = run_dir / "boundary.jsonl"
            self.write_evidence(boundary, boundary_rows)

            with mock.patch.object(
                workflow, "utc_now", return_value=ceiling
            ) as clock:
                record = workflow.validate_gate_evidence(
                    boundary, context, payload
                )
            clock.assert_called_once_with()
            self.assertEqual(record["last_sample_at"], workflow.iso8601_utc(ceiling))

            near_future = json.loads(json.dumps(boundary_rows))
            near_future[-1]["timestamp"] = workflow.iso8601_utc(
                ceiling + timedelta(microseconds=1)
            )
            near_future_path = run_dir / "near-future.jsonl"
            self.write_evidence(near_future_path, near_future)
            with (
                mock.patch.object(workflow, "utc_now", return_value=ceiling) as clock,
                self.assertRaisesRegex(workflow.WorkflowError, "in the future"),
            ):
                workflow.validate_gate_evidence(
                    near_future_path, context, payload
                )
            clock.assert_called_once_with()

            next_year = json.loads(json.dumps(boundary_rows))
            for sample_id, row in enumerate(next_year, start=1):
                row["timestamp"] = workflow.iso8601_utc(
                    ceiling + timedelta(days=365, seconds=sample_id)
                )
            next_year_path = run_dir / "next-year.jsonl"
            self.write_evidence(next_year_path, next_year)
            with (
                mock.patch.object(workflow, "utc_now", return_value=ceiling),
                self.assertRaisesRegex(workflow.WorkflowError, "in the future"),
            ):
                workflow.validate_gate_evidence(
                    next_year_path, context, payload
                )

    def test_reconciliation_ledger_rejects_replay_missing_extra_and_mismatch(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            self.assertEqual(
                workflow.main(
                    [
                        "validate",
                        "--activity-plan-file",
                        str(plan_path),
                        "--run-dir",
                        str(run_dir),
                    ]
                ),
                0,
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            journal = run_dir / "workflow-state.json"
            payload = workflow.read_journal(journal, plan_hash)
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            member_index = context["order"].index("first-member")
            self.set_completed_phases(
                payload, list(context["order"][: member_index + 1])
            )
            workflow.write_journal(journal, payload)
            same_run = workflow.read_journal(journal, plan_hash)
            self.assertEqual(
                same_run["reconciliation"]["first-member"]["completion_id"],
                same_run["phase_completions"]["first-member"]["completion_id"],
            )

            candidates: dict[str, dict] = {}
            missing = json.loads(json.dumps(payload))
            missing["reconciliation"].pop("first-member")
            candidates["missing"] = missing

            extra = json.loads(json.dumps(payload))
            extra["reconciliation"]["unknown-phase"] = json.loads(
                json.dumps(extra["reconciliation"]["first-member"])
            )
            candidates["extra"] = extra

            mismatches = {
                "schema_version": 1,
                "run_id": f"run_{'b' * 64}",
                "plan_sha256": "b" * 64,
                "phase": "second-member",
                "operation_id": f"operation_{'b' * 64}",
                "completion_id": "b" * 64,
                "mutation_intent_sha256": "b" * 64,
            }
            for field, value in mismatches.items():
                candidate = json.loads(json.dumps(payload))
                candidate["reconciliation"]["first-member"][field] = value
                candidates[field] = candidate

            for name, candidate in candidates.items():
                with self.subTest(binding=name):
                    # write_journal deliberately recomputes the outer envelope hash.
                    workflow.write_journal(journal, candidate)
                    with self.assertRaisesRegex(
                        workflow.WorkflowError,
                        "reconciliation|payload binding",
                    ):
                        workflow.read_journal(journal, plan_hash)

    def test_journal_completion_ledger_rejects_tampering_and_old_schema(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            journal = run_dir / "workflow-state.json"
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())
            payload = workflow.read_journal(journal, plan_hash)

            old_schema = json.loads(json.dumps(payload))
            old_schema["version"] = workflow.JOURNAL_VERSION - 1
            workflow.write_journal(journal, old_schema)
            with self.assertRaisesRegex(workflow.WorkflowError, "unsupported"):
                workflow.read_journal(journal, plan_hash)

            tampered = json.loads(json.dumps(payload))
            tampered["phase_completions"]["validate"]["sequence"] = 2
            workflow.write_journal(journal, tampered)
            with self.assertRaisesRegex(workflow.WorkflowError, "invalid completion"):
                workflow.read_journal(journal, plan_hash)

            tampered = json.loads(json.dumps(payload))
            tampered["phase_completions"]["validate"]["completed_at"] = (
                "2026-07-31T20:00:00"
            )
            core = {
                key: tampered["phase_completions"]["validate"][key]
                for key in ("phase", "sequence", "plan_sha256", "run_id")
            }
            tampered["phase_completions"]["validate"]["completion_id"] = (
                workflow.sha256_bytes(workflow.canonical_json(core))
            )
            workflow.write_journal(journal, tampered)
            with self.assertRaisesRegex(workflow.WorkflowError, "UTC offset"):
                workflow.read_journal(journal, plan_hash)

            missing = json.loads(json.dumps(payload))
            missing["phase_completions"] = {}
            workflow.write_journal(journal, missing)
            with self.assertRaisesRegex(workflow.WorkflowError, "ledger"):
                workflow.read_journal(journal, plan_hash)

    def test_journal_completion_ledger_rejects_invalid_chronology(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            journal = run_dir / "workflow-state.json"
            plan_hash = workflow.sha256_bytes(plan_path.read_bytes())
            payload = workflow.read_journal(journal, plan_hash)
            started = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
            phases = ["validate", "capture-state", "baseline-capture"]
            self.set_completed_phases(payload, phases, started)
            ceiling = started + timedelta(seconds=10)

            def replace_timestamp(candidate: dict, phase: str, value: datetime) -> None:
                record = candidate["phase_completions"][phase]
                record["completed_at"] = workflow.iso8601_utc(value)
                core = {
                    key: record[key]
                    for key in ("phase", "sequence", "plan_sha256", "run_id")
                }
                record["completion_id"] = workflow.sha256_bytes(
                    workflow.canonical_json(core)
                )

            cases = {
                "equal": started + timedelta(seconds=1),
                "reversed": started,
                "future": ceiling + timedelta(microseconds=1),
            }
            for name, replacement in cases.items():
                with self.subTest(case=name):
                    candidate = json.loads(json.dumps(payload))
                    phase = "capture-state" if name != "future" else "baseline-capture"
                    replace_timestamp(candidate, phase, replacement)
                    workflow.write_journal(journal, candidate)
                    with (
                        mock.patch.object(workflow, "utc_now", return_value=ceiling) as clock,
                        self.assertRaises(workflow.WorkflowError),
                    ):
                        workflow.read_journal(journal, plan_hash)
                    clock.assert_called_once_with()

            workflow.write_journal(journal, payload)
            with mock.patch.object(
                workflow, "utc_now", return_value=ceiling
            ) as clock:
                accepted = workflow.read_journal(journal, plan_hash)
            clock.assert_called_once_with()
            self.assertEqual(accepted["completed_phases"], phases)

    def test_nonblocking_lock_rejects_second_process(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            fd = os.open(run_dir / "workflow.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(
                    workflow.main(
                        ["show-state", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
                    ),
                    2,
                )
            finally:
                os.close(fd)

    def test_post_capture_phase_rejects_modified_cluster_state(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )

            def capture_state(command, phase, current_run_dir, pass_fds=()):
                self.captured_roles(current_run_dir)
                return "captured\n"

            with mock.patch.object(
                workflow, "run_helper", side_effect=capture_state
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "capture-state",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    0,
                )

            state_path = (
                run_dir
                / "reports"
                / "cluster_initial_state_STANDALONE_TEST.json"
            )
            state_path.write_text(
                json.dumps(
                    {
                        "original_active_host": "192.0.2.21",
                        "original_standby_host": "192.0.2.20",
                    }
                )
            )
            with mock.patch.object(workflow, "run_helper") as helper:
                self.assertEqual(
                    workflow.main(
                        [
                            "baseline-capture",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
                helper.assert_not_called()

    def test_reconciliation_is_descriptor_bound_and_journal_bound(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            path = run_dir / "reconciliation" / "first-member.json"
            fd = workflow.create_reconciliation_output(path)
            try:
                value = {
                    "host": "192.0.2.21",
                    "result": "exact-target-confirmed",
                    "target_version": "R81.20",
                    "target_take": "76",
                    "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
                }
                value, operation, journal_path = self.bound_reconciliation(
                    payload, "first-member", value
                )
                raw = (json.dumps(value, sort_keys=True) + "\n").encode()
                os.write(fd, raw)
                os.fsync(fd)
                real_open = workflow.os.open

                def guarded_open(candidate, *open_args, **open_kwargs):
                    if Path(candidate) == path:
                        raise AssertionError(
                            "reconciliation must use inherited fd"
                        )
                    return real_open(candidate, *open_args, **open_kwargs)

                with mock.patch.object(
                    workflow.os, "open", side_effect=guarded_open
                ):
                    record = workflow.read_reconciliation(
                        path, fd, "first-member", context, payload, 0,
                        operation, journal_path,
                    )
                self.assertEqual(
                    record["_evidence"]["sha256"], workflow.sha256_bytes(raw)
                )
                self.assertEqual(record["_evidence"]["mode"], 0o600)
                self.assertEqual(record["_evidence"]["owner_uid"], os.geteuid())
                self.assertEqual(record["_evidence"]["inode"], os.fstat(fd).st_ino)

                path.chmod(0o640)
                with self.assertRaisesRegex(workflow.WorkflowError, "mode 0600"):
                    workflow.read_reconciliation(
                        path, fd, "first-member", context, payload, 0,
                        operation, journal_path,
                    )
                path.chmod(0o600)

                real_fstat = workflow.os.fstat

                def wrong_file_owner(candidate_fd):
                    metadata = real_fstat(candidate_fd)
                    changed = mock.Mock()
                    for field in (
                        "st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns",
                        "st_ctime_ns",
                    ):
                        setattr(changed, field, getattr(metadata, field))
                    changed.st_uid = metadata.st_uid + 1
                    return changed

                with (
                    mock.patch.object(
                        workflow.os, "fstat", side_effect=wrong_file_owner
                    ),
                    self.assertRaisesRegex(workflow.WorkflowError, "owned by"),
                ):
                    workflow.read_reconciliation(
                        path, fd, "first-member", context, payload, 0,
                        operation, journal_path,
                    )

                (run_dir / "reconciliation").chmod(0o750)
                try:
                    with self.assertRaisesRegex(
                        workflow.WorkflowError, "group.world accessible"
                    ):
                        workflow.read_reconciliation(
                            path, fd, "first-member", context, payload, 0,
                            operation, journal_path,
                        )
                finally:
                    (run_dir / "reconciliation").chmod(0o700)

                with self.assertRaisesRegex(workflow.WorkflowError, "predates"):
                    workflow.read_reconciliation(
                        path,
                        fd,
                        "first-member",
                        context,
                        payload,
                        time.time_ns() + 2_000_000_000,
                        operation,
                        journal_path,
                    )
            finally:
                os.close(fd)

    def test_reconciliation_rejects_path_replacement_and_in_place_mutation(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            path = run_dir / "reconciliation" / "first-member.json"
            value = {
                "host": "192.0.2.21",
                "result": "exact-target-confirmed",
                "target_version": "R81.20",
                "target_take": "76",
                "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
            }
            value, operation, journal_path = self.bound_reconciliation(
                payload, "first-member", value
            )
            raw = (json.dumps(value, sort_keys=True) + "\n").encode()
            original_lstat = Path.lstat

            for change in ("replacement", "in-place"):
                with self.subTest(change=change):
                    if path.exists():
                        path.unlink()
                    fd = workflow.create_reconciliation_output(path)
                    try:
                        os.write(fd, raw)
                        os.fsync(fd)
                        changed = False

                        def mutate_on_lstat(candidate: Path):
                            nonlocal changed
                            if (
                                not changed
                                and candidate == Path(os.path.abspath(path))
                            ):
                                changed = True
                                if change == "replacement":
                                    replacement = path.with_name("replacement.json")
                                    workflow.write_private(replacement, raw)
                                    replacement.replace(path)
                                else:
                                    writer = os.open(path, os.O_WRONLY)
                                    try:
                                        modified = bytearray(raw)
                                        modified[0] = ord(" ")
                                        os.pwrite(writer, modified, 0)
                                        os.fsync(writer)
                                    finally:
                                        os.close(writer)
                            return original_lstat(candidate)

                        with (
                            mock.patch.object(Path, "lstat", mutate_on_lstat),
                            self.assertRaisesRegex(
                                workflow.WorkflowError, "changed while"
                            ),
                        ):
                            workflow.read_reconciliation(
                                path, fd, "first-member", context, payload, 0,
                                operation, journal_path,
                            )
                        self.assertTrue(changed)
                    finally:
                        os.close(fd)

    def test_member_phase_rejects_stale_reconciliation_not_rewritten_by_helper(
        self,
    ) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            self.set_completed_phases(
                payload,
                list(context["order"][: context["order"].index("first-member")]),
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(run_dir / "workflow-state.json", payload)
            stale = run_dir / "reconciliation" / "first-member.json"
            workflow.write_private(
                stale,
                json.dumps(
                    {
                        "host": "192.0.2.21",
                        "result": "exact-target-confirmed",
                        "target_version": "R81.20",
                        "target_take": "76",
                        "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
                    }
                ).encode(),
            )
            with mock.patch.object(
                workflow, "run_helper", return_value="helper returned without evidence\n"
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
            self.assertTrue(stale.exists())
            self.assertEqual(stale.stat().st_size, 0)

    def test_changed_host_key_stops_and_requires_evidence(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            self.set_completed_phases(
                payload,
                list(context["order"][: context["order"].index("first-member")]),
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(run_dir / "workflow-state.json", payload)

            def host_key_failure(command, phase, current_run_dir, pass_fds=()):
                workflow.write_private(
                    current_run_dir / "logs" / f"{phase}.log",
                    b"REMOTE HOST IDENTIFICATION HAS CHANGED\n",
                )
                raise workflow.WorkflowError("first-member helper failed")

            with mock.patch.object(workflow, "run_helper", side_effect=host_key_failure):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            self.assertEqual(payload["host_key_stops"][-1]["phase"], "first-member")
            self.assertNotIn("first-member", payload["completed_phases"])

            with mock.patch.object(workflow, "run_helper") as helper:
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
                helper.assert_not_called()

            evidence = run_dir / "verified-fingerprint.txt"
            evidence.write_text("Fingerprint verified out of band; known_hosts replaced.\n")

            first_stop = payload["host_key_stops"][-1]
            with mock.patch.object(
                workflow, "run_helper", side_effect=host_key_failure
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--host-key-evidence",
                            str(evidence),
                            "--execute",
                        ]
                    ),
                    2,
                )
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            first_remediation = payload["host_key_remediation"][-1]
            self.assertEqual(first_remediation["stop_id"], first_stop["stop_id"])
            self.assertEqual(
                first_remediation["stop_log_sha256"], first_stop["log_sha256"]
            )
            self.assertNotEqual(
                payload["host_key_stops"][-1]["stop_id"], first_stop["stop_id"]
            )
            with mock.patch.object(workflow, "run_helper") as helper:
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
                helper.assert_not_called()

            def successful_member(command, phase, current_run_dir, pass_fds=()):
                self.write_reconciliation_command(
                    command,
                    pass_fds,
                    {
                        "host": "192.0.2.21",
                        "result": "exact-target-confirmed",
                        "target_version": "R81.20",
                        "target_take": "76",
                        "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
                    },
                )
                return "member reconciled\n"

            with mock.patch.object(workflow, "run_helper", side_effect=successful_member):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--host-key-evidence",
                            str(evidence),
                            "--execute",
                        ]
                    ),
                    0,
                )
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            remediation = payload["host_key_remediation"][-1]
            self.assertEqual(remediation["phase"], "first-member")
            self.assertEqual(
                remediation["stop_id"],
                payload["host_key_stops"][-1]["stop_id"],
            )
            self.assertEqual(
                remediation["stop_log_sha256"],
                payload["host_key_stops"][-1]["log_sha256"],
            )
            self.assertIn("first-member", payload["completed_phases"])

    def test_state_descriptor_survives_replacement_at_dispatch_boundary(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(run_dir / "workflow-state.json", plan_hash)
            self.set_completed_phases(
                payload,
                list(context["order"][: context["order"].index("first-member")]),
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(run_dir / "workflow-state.json", payload)
            state_path = Path(payload["initial_roles"]["state_file"]["path"])

            def replace_state(command, phase, current_run_dir, pass_fds=()):
                plan_arg = Path(command[command.index("--activity-plan-file") + 1])
                state_arg = Path(command[command.index("--state-file") + 1])
                self.assertTrue(str(plan_arg).startswith("/proc/self/fd/"))
                self.assertTrue(str(state_arg).startswith("/proc/self/fd/"))
                self.assertNotEqual(plan_arg, state_arg)
                self.assertIn(int(plan_arg.name), pass_fds)
                self.assertIn(int(state_arg.name), pass_fds)
                self.assertEqual(
                    workflow.sha256_bytes(plan_arg.read_bytes()),
                    payload["locked_plan"]["sha256"],
                )
                intent_arg = Path(
                    command[command.index("--mutation-intent-file") + 1]
                )
                self.assertEqual(
                    intent_arg,
                    run_dir / "mutation-intents" / "first-member.json",
                )
                replacement = state_path.with_name("replacement-state.json")
                workflow.write_private(
                    replacement,
                    json.dumps(
                        {
                            "original_active_host": "192.0.2.21",
                            "original_standby_host": "192.0.2.20",
                        }
                    ).encode(),
                )
                replacement.replace(state_path)
                inherited_state = json.loads(state_arg.read_text())
                self.assertEqual(inherited_state["original_standby_host"], "192.0.2.21")
                self.write_reconciliation_command(
                    command,
                    pass_fds,
                    {
                        "host": inherited_state["original_standby_host"],
                        "result": "exact-target-confirmed",
                        "target_version": "R81.20",
                        "target_take": "76",
                        "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
                    },
                )
                return "member reconciled from descriptor-bound state\n"

            with mock.patch.object(workflow, "run_helper", side_effect=replace_state):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    0,
                )

    def test_package_command_combines_bound_descriptors_and_intent(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            plan, _ = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.new_journal(
                "hash",
                context,
                {"path": str(plan_path), "sha256": "hash"},
                {
                    "path": str(run_dir / workflow.LOCKED_PLAN_NAME),
                    "sha256": "hash",
                },
            )
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            operation = workflow.create_pending_member_operation(
                payload, "first-member", context, event_nonce="c" * 64
            )
            plan_fd_path = "/proc/self/fd/41"
            state_fd_path = "/proc/self/fd/42"
            command, reconciliation = workflow.command_for_phase(
                "first-member",
                mock.Mock(run_dir=run_dir, username="admin"),
                context,
                payload,
                plan_fd_path,
                state_fd_path,
                operation,
            )
            self.assertEqual(
                command[command.index("--activity-plan-file") + 1],
                plan_fd_path,
            )
            self.assertEqual(
                command[command.index("--state-file") + 1],
                state_fd_path,
            )
            self.assertEqual(
                Path(command[command.index("--mutation-intent-file") + 1]),
                run_dir / "mutation-intents" / "first-member.json",
            )
            self.assertEqual(
                reconciliation,
                run_dir / "reconciliation" / "first-member.json",
            )

    def test_generated_package_command_is_accepted_by_helper_parser(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.new_journal(
                plan_hash,
                context,
                {"path": str(plan_path), "sha256": plan_hash},
                {
                    "path": str(run_dir / workflow.LOCKED_PLAN_NAME),
                    "sha256": plan_hash,
                },
            )
            state_path = (
                run_dir / "reports" / "cluster_initial_state_STANDALONE_TEST.json"
            )
            workflow.write_private(
                state_path,
                json.dumps(
                    {
                        "original_active_host": "192.0.2.20",
                        "original_standby_host": "192.0.2.21",
                    }
                ).encode(),
            )
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            operation = workflow.create_pending_member_operation(
                payload, "first-member", context, event_nonce="d" * 64
            )
            command, _ = workflow.command_for_phase(
                "first-member",
                mock.Mock(run_dir=run_dir, username="admin"),
                context,
                payload,
                plan_path,
                state_path,
                operation,
            )
            command.remove("--execute")
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("Execution disabled", result.stdout)
            self.assertNotIn("not allowed with argument", result.stderr)

    def test_major_phase_commands_are_python_only(self) -> None:
        temporary, plan_path, run_dir = self.setup_run("upgrade")
        with temporary:
            plan, _ = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            self.assertEqual(tuple(context["order"]), workflow.MAJOR_ORDER)
            payload = workflow.new_journal(
                "hash",
                context,
                {"path": str(plan_path), "sha256": "hash"},
                {"path": str(run_dir / workflow.LOCKED_PLAN_NAME), "sha256": "hash"},
            )
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            for phase in context["order"]:
                if phase in {"validate", "simulate-tester-gate"}:
                    continue
                operation = (
                    workflow.create_pending_member_operation(
                        payload,
                        phase,
                        context,
                        event_nonce=(
                            "e" * 64
                            if phase == "first-member"
                            else "f" * 64
                        ),
                    )
                    if phase in workflow.MEMBER_PHASES
                    else None
                )
                command, _ = workflow.command_for_phase(
                    phase,
                    mock.Mock(
                        run_dir=run_dir,
                        activity_plan_file=plan_path,
                        username="admin",
                    ),
                    context,
                    payload,
                    member_operation=operation,
                )
                self.assertNotIn("ansible-playbook", " ".join(command))
                if "--activity-plan-file" in command:
                    plan_argument = command[
                        command.index("--activity-plan-file") + 1
                    ]
                    self.assertEqual(
                        plan_argument,
                        str(run_dir / workflow.LOCKED_PLAN_NAME),
                    )
            first, _ = workflow.command_for_phase(
                "first-member",
                mock.Mock(
                    run_dir=run_dir,
                    activity_plan_file=plan_path,
                    username="admin",
                ),
                context,
                payload,
                member_operation=workflow.create_pending_member_operation(
                    payload,
                    "first-member",
                    context,
                    event_nonce="e" * 64,
                ),
            )
            self.assertIn("direct_package_step_from_activity.py", " ".join(first))
            self.assertIn("--mutation-intent-file", first)
            intent_path = Path(first[first.index("--mutation-intent-file") + 1])
            self.assertEqual(
                intent_path,
                run_dir / "mutation-intents" / "first-member.json",
            )
            with self.assertRaisesRegex(
                workflow.WorkflowError, "refuses to invoke"
            ):
                workflow.run_helper(
                    ["ansible-playbook", "unsafe.yml"], "unsafe", run_dir
                )

    def test_insecure_journal_mode_and_run_symlink_fail_closed(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                ["validate", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
            )
            journal = run_dir / "workflow-state.json"
            journal.chmod(0o644)
            self.assertEqual(
                workflow.main(
                    ["show-state", "--activity-plan-file", str(plan_path), "--run-dir", str(run_dir)]
                ),
                2,
            )
            linked = run_dir.parent / "linked-run"
            linked.symlink_to(run_dir, target_is_directory=True)
            self.assertEqual(
                workflow.main(
                    ["show-state", "--activity-plan-file", str(plan_path), "--run-dir", str(linked)]
                ),
                2,
            )


    def test_protected_intent_artifact_rejects_hostile_changes(self) -> None:
        def completed_first_member():
            temporary, plan_path, run_dir = self.setup_run()
            workflow.main(
                [
                    "validate",
                    "--activity-plan-file",
                    str(plan_path),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            journal = run_dir / "workflow-state.json"
            payload = workflow.read_journal(journal, plan_hash)
            payload["initial_roles"] = self.captured_roles(run_dir)
            first_index = context["order"].index("first-member")
            self.set_completed_phases(
                payload, list(context["order"][: first_index + 1])
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(journal, payload)
            workflow.read_journal(journal, plan_hash)
            return temporary, journal, plan_hash, payload

        for attack in (
            "missing",
            "extra",
            "replacement",
            "replacement-rebound",
            "in-place",
        ):
            with self.subTest(attack=attack):
                temporary, journal, plan_hash, payload = completed_first_member()
                with temporary:
                    intent = (
                        journal.parent
                        / "mutation-intents"
                        / "first-member.json"
                    )
                    if attack == "missing":
                        intent.unlink()
                    elif attack == "extra":
                        workflow.write_private(
                            intent.with_name("unknown.json"), b"{}\n"
                        )
                    elif attack in {"replacement", "replacement-rebound"}:
                        raw = intent.read_bytes()
                        intent.unlink()
                        workflow.write_private(intent, raw)
                        if attack == "replacement-rebound":
                            operation = payload["phase_completions"][
                                "first-member"
                            ]["member_operation"]
                            _, evidence = workflow.read_protected_intent(
                                intent,
                                not_before_ns=operation["created_at_ns"],
                            )
                            payload["reconciliation"]["first-member"][
                                "_intent_evidence"
                            ] = evidence
                            workflow.write_journal(journal, payload)
                    else:
                        raw = bytearray(intent.read_bytes())
                        raw[-2] = ord(" ")
                        with intent.open("r+b") as handle:
                            handle.write(raw)
                            handle.flush()
                            os.fsync(handle.fileno())
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.read_journal(journal, plan_hash)

        for attack in ("stale-schema", "wrong-host", "wrong-package"):
            with self.subTest(attack=attack):
                temporary, journal, plan_hash, payload = completed_first_member()
                with temporary:
                    intent = (
                        journal.parent
                        / "mutation-intents"
                        / "first-member.json"
                    )
                    document = json.loads(intent.read_text(encoding="utf-8"))
                    if attack == "stale-schema":
                        document["schema"] = workflow.MUTATION_INTENT_VERSION - 1
                    elif attack == "wrong-host":
                        document["host"] = "192.0.2.20"
                    else:
                        document["package_name"] = "hostile.tgz"
                    workflow.write_private(
                        intent,
                        (
                            json.dumps(document, indent=2, sort_keys=True)
                            + "\n"
                        ).encode(),
                    )
                    record = payload["reconciliation"]["first-member"]
                    operation = payload["phase_completions"]["first-member"][
                        "member_operation"
                    ]
                    _, evidence = workflow.read_protected_intent(
                        intent, not_before_ns=operation["created_at_ns"]
                    )
                    record["mutation_intent_sha256"] = workflow.sha256_bytes(
                        workflow.canonical_json(document)
                    )
                    record["_intent_evidence"] = evidence
                    bound = {
                        key: value
                        for key, value in record.items()
                        if key not in {"_evidence", "_intent_evidence"}
                    }
                    record["_evidence"]["payload_sha256"] = (
                        workflow.sha256_bytes(workflow.canonical_json(bound))
                    )
                    workflow.write_journal(journal, payload)
                    with self.assertRaises(workflow.WorkflowError):
                        workflow.read_journal(journal, plan_hash)

        temporary, journal, plan_hash, payload = completed_first_member()
        with temporary:
            record = payload["reconciliation"]["first-member"]
            record["mutation_intent_sha256"] = "0" * 64
            bound = {
                key: value
                for key, value in record.items()
                if key not in {"_evidence", "_intent_evidence"}
            }
            record["_evidence"]["payload_sha256"] = workflow.sha256_bytes(
                workflow.canonical_json(bound)
            )
            workflow.write_journal(journal, payload)
            with self.assertRaisesRegex(
                workflow.WorkflowError, "protected mutation-intent"
            ):
                workflow.read_journal(journal, plan_hash)

    def test_pending_member_operation_is_unique_strict_and_retry_bound(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                [
                    "validate",
                    "--activity-plan-file",
                    str(plan_path),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            journal = run_dir / "workflow-state.json"
            payload = workflow.read_journal(journal, plan_hash)
            payload["initial_roles"] = self.captured_roles(run_dir)
            first_index = context["order"].index("first-member")
            self.set_completed_phases(
                payload, list(context["order"][:first_index])
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            first_attempt = workflow.create_pending_member_operation(
                payload, "first-member", context
            )
            second_attempt = workflow.create_pending_member_operation(
                payload, "first-member", context
            )
            self.assertNotEqual(
                first_attempt["event_nonce"], second_attempt["event_nonce"]
            )
            self.assertNotEqual(
                first_attempt["operation_id"], second_attempt["operation_id"]
            )
            self.assertNotEqual(
                first_attempt["completion_id"], second_attempt["completion_id"]
            )
            payload["pending_member_operation"] = first_attempt
            self.write_intent(payload, first_attempt)
            workflow.write_journal(journal, payload)
            workflow.read_journal(journal, plan_hash)

            hostile = json.loads(json.dumps(payload))
            hostile["pending_member_operation"] = {}
            workflow.write_journal(journal, hostile)
            with self.assertRaises(workflow.WorkflowError):
                workflow.read_journal(journal, plan_hash)

            for field, value in (
                ("schema_version", 0),
                ("phase", "second-member"),
                ("event_nonce", "f" * 64),
            ):
                hostile = json.loads(json.dumps(payload))
                hostile["pending_member_operation"][field] = value
                body = {
                    key: item
                    for key, item in hostile[
                        "pending_member_operation"
                    ].items()
                    if key != "pending_proof"
                }
                hostile["pending_member_operation"]["pending_proof"] = (
                    workflow.sha256_bytes(workflow.canonical_json(body))
                )
                workflow.write_journal(journal, hostile)
                with self.assertRaises(workflow.WorkflowError):
                    workflow.read_journal(journal, plan_hash)

            hostile = json.loads(json.dumps(payload))
            hostile["pending_member_operation"]["unexpected"] = True
            workflow.write_journal(journal, hostile)
            with self.assertRaises(workflow.WorkflowError):
                workflow.read_journal(journal, plan_hash)

    def test_member_retry_reuses_event_and_is_reconciliation_only(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                [
                    "validate",
                    "--activity-plan-file",
                    str(plan_path),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            journal = run_dir / "workflow-state.json"
            payload = workflow.read_journal(journal, plan_hash)
            first_index = context["order"].index("first-member")
            self.set_completed_phases(
                payload, list(context["order"][:first_index])
            )
            payload["initial_roles"] = self.captured_roles(run_dir)
            workflow.write_journal(journal, payload)
            commands: list[list[str]] = []
            value = {
                "host": "192.0.2.21",
                "result": "exact-target-confirmed",
                "target_version": "R81.20",
                "target_take": "76",
                "package_name": (
                    "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
                ),
            }

            def uncertain_helper(command, phase, current_run_dir, pass_fds=()):
                commands.append(list(command))
                self.write_reconciliation_command(command, pass_fds, value)
                raise workflow.WorkflowError("synthetic uncertain outcome")

            with mock.patch.object(
                workflow, "run_helper", side_effect=uncertain_helper
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    2,
                )
            pending = workflow.read_journal(journal, plan_hash)[
                "pending_member_operation"
            ]

            def successful_retry(command, phase, current_run_dir, pass_fds=()):
                commands.append(list(command))
                self.write_reconciliation_command(command, pass_fds, value)
                return "reconciled\n"

            with mock.patch.object(
                workflow, "run_helper", side_effect=successful_retry
            ):
                self.assertEqual(
                    workflow.main(
                        [
                            "first-member",
                            "--activity-plan-file",
                            str(plan_path),
                            "--run-dir",
                            str(run_dir),
                            "--execute",
                        ]
                    ),
                    0,
                )
            self.assertNotIn("--standalone-reconciliation-only", commands[0])
            self.assertIn("--standalone-reconciliation-only", commands[1])
            for option in (
                "--standalone-event-nonce",
                "--standalone-operation-id",
                "--standalone-completion-id",
            ):
                self.assertEqual(
                    commands[0][commands[0].index(option) + 1],
                    commands[1][commands[1].index(option) + 1],
                )
            completed = workflow.read_journal(journal, plan_hash)
            self.assertEqual(completed["pending_member_operation"], {})
            self.assertEqual(
                completed["phase_completions"]["first-member"][
                    "event_nonce"
                ],
                pending["event_nonce"],
            )

    def test_initial_arbitrary_digest_and_other_attempt_replay_fail(self) -> None:
        temporary, plan_path, run_dir = self.setup_run()
        with temporary:
            workflow.main(
                [
                    "validate",
                    "--activity-plan-file",
                    str(plan_path),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            plan, plan_hash = workflow.load_plan(plan_path)
            context = workflow.plan_context(plan)
            payload = workflow.read_journal(
                run_dir / "workflow-state.json", plan_hash
            )
            payload["initial_roles"] = {
                "original_active_host": "192.0.2.20",
                "original_standby_host": "192.0.2.21",
            }
            path = run_dir / "reconciliation" / "first-member.json"
            value = {
                "host": "192.0.2.21",
                "result": "exact-target-confirmed",
                "target_version": "R81.20",
                "target_take": "76",
                "package_name": (
                    "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
                ),
            }
            value, first_operation, journal_path = self.bound_reconciliation(
                payload, "first-member", value
            )
            fd = workflow.create_reconciliation_output(path)
            try:
                hostile = {**value, "mutation_intent_sha256": "0" * 64}
                os.write(fd, (json.dumps(hostile) + "\n").encode())
                os.fsync(fd)
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "protected mutation-intent"
                ):
                    workflow.read_reconciliation(
                        path,
                        fd,
                        "first-member",
                        context,
                        payload,
                        0,
                        first_operation,
                        journal_path,
                    )
            finally:
                os.close(fd)

            intent_path = run_dir / "mutation-intents" / "first-member.json"
            intent_path.unlink()
            second_operation = workflow.create_pending_member_operation(
                payload,
                "first-member",
                context,
                event_nonce="c" * 64,
                created_at_ns=time.time_ns(),
            )
            self.write_intent(payload, second_operation)
            if path.exists():
                path.unlink()
            fd = workflow.create_reconciliation_output(path)
            try:
                os.write(fd, (json.dumps(value) + "\n").encode())
                os.fsync(fd)
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "operation_id|event_nonce"
                ):
                    workflow.read_reconciliation(
                        path,
                        fd,
                        "first-member",
                        context,
                        payload,
                        0,
                        second_operation,
                        journal_path,
                    )
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
