from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


artifacts = load("governed_cdt_artifacts")
record = load("record_cdt_mutation")
reconcile = load("reconcile_cdt_member")


class ArtifactBindingTests(unittest.TestCase):
    operation_id = "run_" + "a" * 64

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.context_dir = self.root / "contexts"
        self.receipt_dir = self.root / "receipts"
        self.evidence_dir = self.root / "evidence"
        for path in (self.context_dir, self.receipt_dir, self.evidence_dir):
            path.mkdir(mode=0o700)
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "target_version": "R82",
                "target_take": "91",
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            },
            "package_steps": [{
                "name": "install_take_91",
                "action": "install",
                "package_type": "jhf",
                "package_name": "Check_Point_R82_T91_FULL.tgz",
                "source_path": "/var/log/tmp/Check_Point_R82_T91_FULL.tgz",
                "target_build": "",
            }],
        }) + "\n")
        os.chmod(self.plan, 0o600)
        self.context_path = self.context_dir / "first.json"
        self.receipt_path = self.receipt_dir / "first.json"
        self.evidence_path = self.evidence_dir / "first.json"
        self.now = time.time_ns()
        self.context = {
            "schema": 1,
            "operation_id": self.operation_id,
            "change_identity": "CHG_TEST",
            "activity_plan_sha256": artifacts.plan_sha256(self.plan),
            "phase": "first-member",
            "step_name": "install_take_91",
            "action": "install",
            "target_host": "192.0.2.20",
            "selected_candidate_ip": "192.0.2.20",
            "package_name": "Check_Point_R82_T91_FULL.tgz",
            "package_type": "jhf",
            "target_version": "R82",
            "target_take": "91",
            "target_build": "",
            "identity_source": "immutable-activity-plan",
            "context_id": "b" * 64,
            "created_at_ns": self.now - 1_000,
        }
        context_bytes = artifacts.atomic_write_private_json(
            self.context_path, self.context
        )
        receipt = record.validate_context(
            self.context,
            context_bytes,
            self.plan,
            self.operation_id,
            "first-member",
            "install_take_91",
        )
        self.now = receipt["mutation_completed_at_ns"]
        artifacts.atomic_write_private_json(self.receipt_path, receipt)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def validate(self, **overrides):
        values = {
            "plan_path": self.plan,
            "context_path": self.context_path,
            "receipt_path": self.receipt_path,
            "operation_id": self.operation_id,
            "phase": "first-member",
            "step": "install_take_91",
            "now_ns": self.now + 1_000,
        }
        values.update(overrides)
        return reconcile.validate_artifacts(**values)

    @staticmethod
    def rewrite(path: Path, payload: dict) -> bytes:
        path.unlink(missing_ok=True)
        return artifacts.atomic_write_private_json(path, payload)

    def evidence(self, **overrides) -> dict:
        context, context_bytes, _ = artifacts.read_private_json(
            self.context_path, "CDT context"
        )
        receipt, receipt_bytes, _ = artifacts.read_private_json(
            self.receipt_path, "CDT mutation receipt"
        )
        payload = {
            "schema": 1,
            "operation_id": self.operation_id,
            "change_identity": context["change_identity"],
            "activity_plan_sha256": artifacts.plan_sha256(self.plan),
            "phase": "first-member",
            "step_name": "install_take_91",
            "action": context["action"],
            "target_host": context["target_host"],
            "selected_candidate_ip": context["selected_candidate_ip"],
            "package_name": context["package_name"],
            "package_type": context["package_type"],
            "target_version": context["target_version"],
            "target_take": context["target_take"],
            "target_build": context["target_build"],
            "identity_source": context["identity_source"],
            "context_id": context["context_id"],
            "context_sha256": artifacts.sha256_bytes(context_bytes),
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": artifacts.sha256_bytes(receipt_bytes),
            "mutation_completed_at_ns": receipt["mutation_completed_at_ns"],
            "reconciled_at_ns": receipt["mutation_completed_at_ns"] + 1,
            "observed": {
                "host": context["target_host"],
                "target_version": context["target_version"],
                "target_take": context["target_take"],
                "target_build": context["target_build"],
                "package_name": context["package_name"],
                "result": "exact-target-confirmed",
            },
        }
        payload.update(overrides)
        return payload

    def test_bound_context_and_receipt_are_accepted(self) -> None:
        self.assertEqual(self.validate()["target_host"], "192.0.2.20")

    def test_exact_evidence_chain_is_accepted(self) -> None:
        artifacts.atomic_write_private_json(self.evidence_path, self.evidence())
        evidence = reconcile.validate_evidence_chain(
            self.plan,
            self.context_path,
            self.receipt_path,
            self.evidence_path,
            self.operation_id,
            "first-member",
            "install_take_91",
            now_ns=self.now + 2,
        )
        self.assertEqual(evidence["receipt_id"], json.loads(self.receipt_path.read_text())["receipt_id"])

    def test_artifacts_are_create_once_and_symlinks_cannot_replace_them(self) -> None:
        inode = self.context_path.stat().st_ino
        original = self.context_path.read_bytes()
        with self.assertRaises(FileExistsError):
            artifacts.atomic_write_private_json(self.context_path, self.context)
        self.assertEqual(self.context_path.stat().st_ino, inode)
        self.assertEqual(self.context_path.read_bytes(), original)

        link = self.evidence_dir / "linked.json"
        target = self.evidence_dir / "target.json"
        target.write_text("{}\n")
        os.chmod(target, 0o600)
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            artifacts.atomic_write_private_json(link, self.evidence())

    def test_unknown_missing_and_replayed_evidence_fail_closed(self) -> None:
        unknown = self.evidence(unexpected=True)
        artifacts.atomic_write_private_json(self.evidence_path, unknown)
        with self.assertRaisesRegex(RuntimeError, "unknown fields"):
            reconcile.validate_evidence_chain(
                self.plan, self.context_path, self.receipt_path, self.evidence_path,
                self.operation_id, "first-member", "install_take_91",
                now_ns=self.now + 2,
            )

        missing = self.evidence()
        missing.pop("receipt_sha256")
        self.rewrite(self.evidence_path, missing)
        with self.assertRaisesRegex(RuntimeError, "missing fields"):
            reconcile.validate_evidence_chain(
                self.plan, self.context_path, self.receipt_path, self.evidence_path,
                self.operation_id, "first-member", "install_take_91",
                now_ns=self.now + 2,
            )

        replay = self.evidence(phase="second-member")
        self.rewrite(self.evidence_path, replay)
        with self.assertRaisesRegex(RuntimeError, "phase"):
            reconcile.validate_evidence_chain(
                self.plan, self.context_path, self.receipt_path, self.evidence_path,
                self.operation_id, "first-member", "install_take_91",
                now_ns=self.now + 2,
            )

    def test_context_and_receipt_unknown_or_missing_fields_fail_closed(self) -> None:
        original_receipt = json.loads(self.receipt_path.read_text())
        cases = (
            (self.context_path, {**self.context, "unexpected": True}, "unknown fields"),
            (
                self.context_path,
                {key: value for key, value in self.context.items() if key != "action"},
                "missing fields",
            ),
            (
                self.receipt_path,
                {**original_receipt, "unexpected": True},
                "unknown fields",
            ),
            (
                self.receipt_path,
                {
                    key: value
                    for key, value in original_receipt.items()
                    if key != "receipt_id"
                },
                "missing fields",
            ),
        )
        for path, payload, message in cases:
            with self.subTest(path=path, message=message):
                self.rewrite(path, payload)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.validate()
                self.rewrite(self.context_path, self.context)
                self.rewrite(self.receipt_path, original_receipt)

    def test_receipt_replacement_invalidates_existing_evidence_digest(self) -> None:
        artifacts.atomic_write_private_json(self.evidence_path, self.evidence())
        receipt = json.loads(self.receipt_path.read_text())
        receipt["receipt_id"] = "c" * 64
        self.rewrite(self.receipt_path, receipt)
        with self.assertRaisesRegex(RuntimeError, "receipt_id|receipt_sha256"):
            reconcile.validate_evidence_chain(
                self.plan, self.context_path, self.receipt_path, self.evidence_path,
                self.operation_id, "first-member", "install_take_91",
                now_ns=self.now + 2,
            )

    def test_management_candidate_is_bound_to_its_access_address(self) -> None:
        plan = json.loads(self.plan.read_text())
        plan["checkpoint"]["members"] = [
            {"management_ip": "192.0.2.20", "access_ip": "198.51.100.20"},
            {"management_ip": "192.0.2.21", "access_ip": "198.51.100.21"},
        ]
        self.plan.write_text(json.dumps(plan) + "\n")
        context = dict(
            self.context,
            activity_plan_sha256=artifacts.plan_sha256(self.plan),
            target_host="198.51.100.20",
        )
        context_bytes = self.rewrite(self.context_path, context)
        receipt = record.validate_context(
            context,
            context_bytes,
            self.plan,
            self.operation_id,
            "first-member",
            "install_take_91",
        )
        self.assertEqual(receipt["target_host"], "198.51.100.20")

        context["target_host"] = "198.51.100.21"
        context_bytes = self.rewrite(self.context_path, context)
        with self.assertRaisesRegex(RuntimeError, "selected plan member"):
            record.validate_context(
                context,
                context_bytes,
                self.plan,
                self.operation_id,
                "first-member",
                "install_take_91",
            )

    def test_wrong_member_plan_phase_or_step_is_rejected(self) -> None:
        mutations = (
            ("target_host", "192.0.2.99", "selected plan member"),
            ("phase", "second-member", "phase"),
            ("step_name", "other", "step_name"),
        )
        for key, value, message in mutations:
            with self.subTest(key=key):
                changed = dict(self.context)
                changed[key] = value
                self.rewrite(self.context_path, changed)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.validate()
                self.rewrite(self.context_path, self.context)
        self.plan.write_text(
            '{"change":{"number":"CHG_TEST"},'
            '"checkpoint":{"target_version":"R81.20"}}\n'
        )
        os.chmod(self.plan, 0o600)
        with self.assertRaisesRegex(RuntimeError, "activity_plan_sha256"):
            self.validate()

    def test_missing_tampered_and_stale_artifacts_fail_closed(self) -> None:
        self.receipt_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.validate()

        context_bytes = self.context_path.read_bytes()
        receipt = record.validate_context(
            self.context,
            context_bytes,
            self.plan,
            self.operation_id,
            "first-member",
            "install_take_91",
        )
        receipt["context_sha256"] = "0" * 64
        receipt["mutation_completed_at_ns"] = self.now
        self.rewrite(self.receipt_path, receipt)
        with self.assertRaisesRegex(RuntimeError, "context_sha256"):
            self.validate()

        stale_completed = self.now - reconcile.MAX_CONTEXT_AGE_NS - 1
        stale_context = dict(self.context, created_at_ns=stale_completed - 1)
        context_bytes = self.rewrite(
            self.context_path, stale_context
        )
        receipt["context_sha256"] = artifacts.sha256_bytes(context_bytes)
        receipt["context_created_at_ns"] = stale_context["created_at_ns"]
        receipt["mutation_completed_at_ns"] = stale_completed
        self.rewrite(self.receipt_path, receipt)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.validate(now_ns=self.now)

    def test_removal_context_requires_cpinstlog_identity_source(self) -> None:
        plan = json.loads(self.plan.read_text())
        plan["package_steps"][0]["action"] = "remove"
        self.plan.write_text(json.dumps(plan) + "\n")
        removal = dict(
            self.context,
            activity_plan_sha256=artifacts.plan_sha256(self.plan),
            action="remove",
            identity_source="immutable-activity-plan",
        )
        context_bytes = self.rewrite(self.context_path, removal)
        with self.assertRaisesRegex(RuntimeError, "CPInstLog/CPRID"):
            record.validate_context(
                removal,
                context_bytes,
                self.plan,
                self.operation_id,
                "first-member",
                "install_take_91",
            )

    def test_symlink_or_public_artifact_is_rejected(self) -> None:
        self.context_path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "0600"):
            self.validate()
        self.context_path.chmod(0o600)
        target = self.context_dir / "target.json"
        self.context_path.rename(target)
        self.context_path.symlink_to(target)
        with self.assertRaises(OSError):
            self.validate()


class MemberStateTests(unittest.TestCase):
    def test_removal_reconciliation_uses_integrated_installed_table_gate(self) -> None:
        context = {
            "action": "remove",
            "target_host": "192.0.2.20",
            "package_name": "Check_Point_R82_T91_FULL.tgz",
            "package_type": "jhf",
            "target_version": "R82",
            "target_take": "91",
            "target_build": "",
        }
        fake_session = mock.MagicMock()
        fake_session.run_interactive_clish.return_value = SimpleNamespace(
            output="No installed packages match\n__RC=0\n"
        )
        with mock.patch.dict(
            os.environ, {"CP_PASSWORD": "pw", "CP_EXPERT_PASSWORD": "expert"}
        ), mock.patch.object(reconcile.c, "SshPty", return_value=fake_session):
            result = reconcile.verify_member(context, "admin", 180)
        self.assertEqual(result["result"], "exact-package-absence-confirmed")
        self.assertIn(
            "show installer packages installed",
            fake_session.run_interactive_clish.call_args.args[0],
        )

    def test_os_build_parser_is_exact(self) -> None:
        self.assertEqual(reconcile.os_build("Product R82\nOS build 777\n"), "777")
        self.assertIsNone(reconcile.os_build("Product R82\nBuild unknown\n"))

    def test_wrong_release_take_or_build_fails_closed(self) -> None:
        context = {
            "action": "upgrade",
            "target_host": "192.0.2.20",
            "package_name": "R82_Blink.tgz",
            "package_type": "blink",
            "target_version": "R82",
            "target_take": "60",
            "target_build": "777",
        }
        fake_session = mock.MagicMock()
        cases = (
            ("Product version R81.20\nOS build 777", "Take: 60", "wrong release"),
            ("Product version R82\nOS build 777", "BUNDLE_R82_JUMBO_HF_MAIN Take: 91", "wrong Take"),
            ("Product version R82\nOS build 778", "BUNDLE_R82_JUMBO_HF_MAIN Take: 60", "wrong OS build"),
        )
        with mock.patch.dict(
            os.environ, {"CP_PASSWORD": "pw", "CP_EXPERT_PASSWORD": "expert"}
        ), mock.patch.object(reconcile.c, "SshPty", return_value=fake_session):
            for version, take, message in cases:
                with self.subTest(message=message), mock.patch.object(
                    reconcile.direct, "run_checked", return_value=version
                ), mock.patch.object(
                    reconcile.direct, "run_expert_checked", return_value=take
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        reconcile.verify_member(context, "admin", 30)

    def test_major_requires_declared_build(self) -> None:
        context = {
            "action": "upgrade",
            "target_host": "192.0.2.20",
            "package_name": "R82_Blink.tgz",
            "package_type": "blink",
            "target_version": "R82",
            "target_take": "60",
            "target_build": "",
        }
        with mock.patch.dict(
            os.environ, {"CP_PASSWORD": "pw", "CP_EXPERT_PASSWORD": "expert"}
        ), mock.patch.object(reconcile.c, "SshPty"), mock.patch.object(
            reconcile.direct,
            "run_checked",
            return_value="Product version R82\nOS build 777",
        ), mock.patch.object(
            reconcile.direct,
            "run_expert_checked",
            return_value="BUNDLE_R82_JUMBO_HF_MAIN Take: 60",
        ):
            with self.assertRaisesRegex(RuntimeError, "exact declared OS build"):
                reconcile.verify_member(context, "admin", 30)

    def test_package_still_installed_fails_removal(self) -> None:
        context = {
            "action": "remove",
            "target_host": "192.0.2.20",
            "package_name": "Take91.tgz",
        }
        with mock.patch.dict(
            os.environ, {"CP_PASSWORD": "pw", "CP_EXPERT_PASSWORD": "expert"}
        ), mock.patch.object(reconcile.c, "SshPty"), mock.patch.object(
            reconcile.direct,
            "verify_package_absent",
            side_effect=RuntimeError("package still installed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "still installed"):
                reconcile.verify_member(context, "admin", 30)


if __name__ == "__main__":
    unittest.main()
