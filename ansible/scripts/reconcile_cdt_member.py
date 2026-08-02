#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402
import direct_package_step_from_activity as direct  # noqa: E402
import record_cdt_mutation as mutation_record  # noqa: E402
from governed_cdt_artifacts import (  # noqa: E402
    CONTEXT_FIELDS,
    EVIDENCE_FIELDS,
    RECEIPT_FIELDS,
    atomic_write_private_json,
    plan_sha256,
    read_private_json,
    require_exact_schema,
    sha256_bytes,
)

MAX_CONTEXT_AGE_NS = 6 * 60 * 60 * 1_000_000_000


def os_build(output: str) -> str | None:
    matches = re.findall(r"\bOS\s+build\s*[:=]?\s*(\d+)\b", output, re.IGNORECASE)
    return matches[-1] if matches else None


def validate_artifacts(
    plan_path: Path,
    context_path: Path,
    receipt_path: Path,
    operation_id: str,
    phase: str,
    step: str,
    now_ns: int | None = None,
) -> dict:
    context, context_bytes, _ = read_private_json(context_path, "CDT context")
    receipt, _, _ = read_private_json(receipt_path, "CDT mutation receipt")
    require_exact_schema(context, CONTEXT_FIELDS, "CDT context")
    require_exact_schema(receipt, RECEIPT_FIELDS, "CDT mutation receipt")
    mutation_record.validate_context(
        context,
        context_bytes,
        plan_path,
        operation_id,
        phase,
        step,
    )
    digest = plan_sha256(plan_path)
    plan = json.loads(plan_path.read_text())
    expected_context = {
        "schema": 1,
        "operation_id": operation_id,
        "change_identity": str(plan.get("change", {}).get("number") or ""),
        "activity_plan_sha256": digest,
        "phase": phase,
        "step_name": step,
    }
    for key, value in expected_context.items():
        if context.get(key) != value:
            raise RuntimeError(f"CDT context does not match {key}")
    expected_receipt = {
        **expected_context,
        "target_host": context.get("target_host"),
        "selected_candidate_ip": context.get("selected_candidate_ip"),
        "context_id": context.get("context_id"),
        "context_sha256": sha256_bytes(context_bytes),
        "context_created_at_ns": context.get("created_at_ns"),
    }
    for key in (
        "change_identity",
        "action",
        "package_name",
        "package_type",
        "target_version",
        "target_take",
        "target_build",
        "identity_source",
    ):
        expected_receipt[key] = context.get(key)
    for key, value in expected_receipt.items():
        if receipt.get(key) != value:
            raise RuntimeError(f"CDT mutation receipt does not match {key}")
    created = context.get("created_at_ns")
    completed = receipt.get("mutation_completed_at_ns")
    current = time.time_ns() if now_ns is None else now_ns
    if not isinstance(created, int) or not isinstance(completed, int):
        raise RuntimeError("CDT context or mutation receipt has no valid timestamp")
    if completed < created:
        raise RuntimeError("CDT mutation receipt predates its context")
    if created > current or completed > current:
        raise RuntimeError("CDT context or mutation receipt is from the future")
    if current - completed > MAX_CONTEXT_AGE_NS:
        raise RuntimeError("CDT mutation receipt is stale")
    if context.get("action") not in {"install", "upgrade", "remove"}:
        raise RuntimeError("CDT context has an unsupported action")
    if (
        context.get("action") == "remove"
        and context.get("identity_source") != "gateway-cpinstlog-via-cprid"
    ):
        raise RuntimeError("removal context is not bound to CPInstLog/CPRID identity")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", str(context.get("package_name", ""))):
        raise RuntimeError("CDT context has an unsafe package identity")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(context.get("target_host", ""))):
        raise RuntimeError("CDT context has an invalid target host")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("receipt_id", ""))):
        raise RuntimeError("CDT mutation receipt has an invalid receipt ID")
    return context


def validate_evidence_chain(
    plan_path: Path,
    context_path: Path,
    receipt_path: Path,
    evidence_path: Path,
    operation_id: str,
    phase: str,
    step: str,
    now_ns: int | None = None,
) -> dict:
    context = validate_artifacts(
        plan_path,
        context_path,
        receipt_path,
        operation_id,
        phase,
        step,
        now_ns=now_ns,
    )
    context_payload, context_bytes, _ = read_private_json(context_path, "CDT context")
    receipt, receipt_bytes, _ = read_private_json(receipt_path, "CDT mutation receipt")
    evidence, _, _ = read_private_json(evidence_path, "CDT reconciliation evidence")
    require_exact_schema(context_payload, CONTEXT_FIELDS, "CDT context")
    require_exact_schema(receipt, RECEIPT_FIELDS, "CDT mutation receipt")
    require_exact_schema(evidence, EVIDENCE_FIELDS, "CDT reconciliation evidence")
    expected = {
        "schema": 1,
        "operation_id": operation_id,
        "change_identity": context["change_identity"],
        "activity_plan_sha256": plan_sha256(plan_path),
        "phase": phase,
        "step_name": step,
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
        "context_sha256": sha256_bytes(context_bytes),
        "receipt_id": receipt["receipt_id"],
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "mutation_completed_at_ns": receipt["mutation_completed_at_ns"],
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            raise RuntimeError(f"CDT reconciliation evidence does not match {key}")
    reconciled = evidence.get("reconciled_at_ns")
    current = time.time_ns() if now_ns is None else now_ns
    if not isinstance(reconciled, int):
        raise RuntimeError("CDT reconciliation evidence has no valid timestamp")
    if reconciled < receipt["mutation_completed_at_ns"] or reconciled > current:
        raise RuntimeError("CDT reconciliation evidence timestamp is invalid")
    observed = evidence.get("observed")
    if not isinstance(observed, dict):
        raise RuntimeError("CDT reconciliation evidence has no observed result")
    observed_fields = (
        frozenset({"host", "package_name", "result"})
        if context["action"] == "remove"
        else frozenset({
            "host", "target_version", "target_take", "target_build",
            "package_name", "result",
        })
    )
    require_exact_schema(observed, observed_fields, "CDT observed reconciliation")
    expected_result = (
        "exact-package-absence-confirmed"
        if context["action"] == "remove"
        else "exact-target-confirmed"
    )
    if observed.get("result") != expected_result:
        raise RuntimeError("CDT reconciliation evidence is not successful")
    if observed.get("host") != context["target_host"]:
        raise RuntimeError("CDT reconciliation evidence observed the wrong member")
    if observed.get("package_name") != context["package_name"]:
        raise RuntimeError("CDT reconciliation evidence observed the wrong package")
    if context["action"] != "remove":
        for key in ("target_version", "target_take", "target_build"):
            if observed.get(key) != context[key]:
                raise RuntimeError(
                    f"CDT reconciliation evidence observed the wrong {key}"
                )
    return evidence


def verify_member(context: dict, username: str, timeout: int) -> dict[str, str]:
    host = str(context["target_host"])
    password = os.environ.get("CP_PASSWORD", "")
    expert_password = os.environ.get("CP_EXPERT_PASSWORD", password)
    if not password or not expert_password:
        raise RuntimeError("CP_PASSWORD and CP_EXPERT_PASSWORD are required")
    action = str(context["action"])
    package_name = str(context["package_name"])
    if action == "remove":
        result = direct.verify_package_absent(
            host, username, password, expert_password, package_name
        )
        return {str(key): str(value) for key, value in result.items()}
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        if action in {"install", "upgrade"}:
            target_version = str(context.get("target_version", ""))
            target_take = str(context.get("target_take", ""))
            if not target_version or not re.fullmatch(r"\d{1,4}", target_take):
                raise RuntimeError("install reconciliation requires exact target release and Take")
            session.enter_expert(expert_password)
            version_output = direct.run_checked(session, host, "show version all", timeout)
            if not c.version_output_matches_target(version_output, target_version):
                raise RuntimeError(f"{host}: wrong release; expected {target_version}")
            take_output = direct.run_expert_checked(
                session,
                host,
                "cpinfo -y all | egrep -i '(HOTFIX|BUNDLE)_R[0-9_]+_JUMBO_HF_MAIN|No hotfixes' | head -120",
                timeout,
            )
            observed_take = direct.installed_jhf_take(take_output, target_version)
            if observed_take != target_take:
                raise RuntimeError(
                    f"{host}: wrong Take; expected {target_take}, observed {observed_take or 'missing'}"
                )
            target_build = str(context.get("target_build", ""))
            if context.get("package_type") == "blink":
                if not re.fullmatch(r"\d+", target_build):
                    raise RuntimeError("major reconciliation requires an exact declared OS build")
                observed_build = os_build(version_output)
                if observed_build != target_build:
                    raise RuntimeError(
                        f"{host}: wrong OS build; expected {target_build}, observed {observed_build or 'missing'}"
                    )
            result = {
                "host": host,
                "target_version": target_version,
                "target_take": target_take,
                "target_build": target_build,
                "package_name": package_name,
                "result": "exact-target-confirmed",
            }
        return {str(key): str(value) for key, value in result.items()}
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activity-plan-file", type=Path, required=True)
    parser.add_argument("--context-file", type=Path, required=True)
    parser.add_argument("--receipt-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--phase", choices=("first-member", "second-member"), required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    context = validate_artifacts(
        args.activity_plan_file,
        args.context_file,
        args.receipt_file,
        args.operation_id,
        args.phase,
        args.step,
    )
    observed = verify_member(context, args.username, args.timeout)
    _, context_bytes, _ = read_private_json(args.context_file, "CDT context")
    receipt, receipt_bytes, _ = read_private_json(
        args.receipt_file, "CDT mutation receipt"
    )
    evidence = {
        "schema": 1,
        "operation_id": args.operation_id,
        "change_identity": context["change_identity"],
        "activity_plan_sha256": plan_sha256(args.activity_plan_file),
        "phase": args.phase,
        "step_name": args.step,
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
        "context_sha256": sha256_bytes(context_bytes),
        "receipt_id": receipt["receipt_id"],
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "mutation_completed_at_ns": receipt["mutation_completed_at_ns"],
        "reconciled_at_ns": time.time_ns(),
        "observed": observed,
    }
    try:
        atomic_write_private_json(args.evidence_file, evidence)
    except FileExistsError as exc:
        raise SystemExit(
            "ERROR: CDT reconciliation evidence already exists; completed "
            "evidence is immutable and cannot be replaced during retry."
        ) from exc
    print("CDT_RECONCILIATION=" + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
