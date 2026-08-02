#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from governed_cdt_artifacts import (
    CONTEXT_FIELDS,
    atomic_write_private_json,
    plan_sha256,
    read_private_json,
    require_exact_schema,
    sha256_bytes,
)

OPERATION_ID_RE = re.compile(r"run_[0-9a-f]{64}")
MAX_CONTEXT_AGE_NS = 6 * 60 * 60 * 1_000_000_000


def validate_context(
    context: dict,
    context_bytes: bytes,
    plan_path: Path,
    operation_id: str,
    phase: str,
    step: str,
) -> dict:
    require_exact_schema(context, CONTEXT_FIELDS, "CDT context")
    expected = {
        "schema": 1,
        "operation_id": operation_id,
        "change_identity": str(
            json.loads(plan_path.read_text()).get("change", {}).get("number") or ""
        ),
        "activity_plan_sha256": plan_sha256(plan_path),
        "phase": phase,
        "step_name": step,
    }
    for key, value in expected.items():
        if context.get(key) != value:
            raise RuntimeError(f"CDT context does not match {key}")
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise RuntimeError("invalid governed operation ID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(context.get("context_id", ""))):
        raise RuntimeError("CDT context has an invalid context ID")
    created_at_ns = context.get("created_at_ns")
    now_ns = time.time_ns()
    if not isinstance(created_at_ns, int):
        raise RuntimeError("CDT context has no creation timestamp")
    if created_at_ns > now_ns or now_ns - created_at_ns > MAX_CONTEXT_AGE_NS:
        raise RuntimeError("CDT context is stale or from the future")
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("immutable activity plan is unreadable") from exc
    package_steps = [
        item
        for item in plan.get("package_steps", [])
        if isinstance(item, dict) and item.get("name") == step
    ]
    if len(package_steps) != 1:
        raise RuntimeError("CDT context step is not unique in the activity plan")
    package = package_steps[0]
    members = plan.get("checkpoint", {}).get("members") or []
    candidate_ip = str(context.get("selected_candidate_ip") or "")
    selected_members = [
        member
        for member in members
        if isinstance(member, dict)
        and str(member.get("management_ip") or member.get("ip") or "") == candidate_ip
    ]
    if len(selected_members) != 1:
        raise RuntimeError("CDT candidate address does not identify one activity-plan member")
    selected_member = selected_members[0]
    expected_target_host = str(
        selected_member.get("access_ip")
        or selected_member.get("ip")
        or selected_member.get("management_ip")
        or ""
    )
    if context.get("target_host") != expected_target_host:
        raise RuntimeError("CDT reconciliation host does not match the selected plan member")
    action = str(context.get("action") or "")
    package_type = str(context.get("package_type") or "")
    package_name = str(context.get("package_name") or "")
    if action != package.get("action") or action not in {"install", "upgrade", "remove"}:
        raise RuntimeError("CDT context action does not match the activity plan")
    if package_type != str(package.get("package_type") or ""):
        raise RuntimeError("CDT context package type does not match the activity plan")
    if re.fullmatch(r"[A-Za-z0-9_.+-]+", package_name) is None:
        raise RuntimeError("CDT context package identity is unsafe")
    checkpoint = plan.get("checkpoint", {})
    expected_version = str(package.get("target_version") or checkpoint.get("target_version") or "")
    expected_take = str(package.get("target_take") or checkpoint.get("target_take") or "")
    expected_build = str(package.get("target_build") or "")
    for key, value in (
        ("target_version", expected_version),
        ("target_take", expected_take),
        ("target_build", expected_build),
    ):
        if str(context.get(key) or "") != value:
            raise RuntimeError(f"CDT context {key} does not match the activity plan")
    if action == "remove":
        if context.get("identity_source") != "gateway-cpinstlog-via-cprid":
            raise RuntimeError("removal context is not bound to CPInstLog/CPRID identity")
    else:
        expected_name = Path(str(package.get("source_path") or package.get("package_name") or "")).name
        if package_name != expected_name:
            raise RuntimeError("CDT context package identity does not match the activity plan")
    return {
        "schema": 1,
        "operation_id": operation_id,
        "change_identity": context["change_identity"],
        "activity_plan_sha256": expected["activity_plan_sha256"],
        "phase": phase,
        "step_name": step,
        "action": action,
        "target_host": context.get("target_host"),
        "selected_candidate_ip": context.get("selected_candidate_ip"),
        "package_name": package_name,
        "package_type": package_type,
        "target_version": context["target_version"],
        "target_take": context["target_take"],
        "target_build": context["target_build"],
        "identity_source": context["identity_source"],
        "context_id": context["context_id"],
        "context_sha256": sha256_bytes(context_bytes),
        "context_created_at_ns": context["created_at_ns"],
        "receipt_id": secrets.token_hex(32),
        "mutation_completed_at_ns": time.time_ns(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activity-plan-file", type=Path, required=True)
    parser.add_argument("--context-file", type=Path, required=True)
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--phase", choices=("first-member", "second-member"), required=True)
    parser.add_argument("--step", required=True)
    args = parser.parse_args()
    context, context_bytes, _ = read_private_json(args.context_file, "CDT context")
    receipt = validate_context(
        context,
        context_bytes,
        args.activity_plan_file,
        args.operation_id,
        args.phase,
        args.step,
    )
    if args.validate_only:
        if args.receipt_file is not None:
            raise SystemExit("ERROR: --validate-only cannot be combined with --receipt-file")
        print("CDT candidate context validated")
        return 0
    if args.receipt_file is None:
        raise SystemExit("ERROR: --receipt-file is required unless --validate-only is used")
    try:
        atomic_write_private_json(args.receipt_file, receipt)
    except FileExistsError as exc:
        raise SystemExit(
            "ERROR: CDT mutation receipt already exists; do not overwrite or "
            "redispatch this phase. Validate the existing reconciliation chain "
            "or start a new governed operation."
        ) from exc
    print(f"CDT mutation receipt: {args.receipt_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
