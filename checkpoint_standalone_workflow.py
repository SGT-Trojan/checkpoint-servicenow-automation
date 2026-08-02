#!/usr/bin/env python3
"""Journaled Check Point workflow using Python helpers without Ansible."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import stat
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "ansible" / "scripts"
JOURNAL_VERSION = 9
RECONCILIATION_VERSION = 3
MUTATION_INTENT_VERSION = 3
PENDING_OPERATION_VERSION = 1
RUN_ID_PATTERN = re.compile(r"run_[0-9a-f]{64}")
OPERATION_ID_PATTERN = re.compile(r"operation_[0-9a-f]{64}")
EVENT_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}")
LOCKED_PLAN_NAME = "activity-plan.locked.json"
STATE_CONSUMING_PHASES = {
    "first-member",
    "mvc-on",
    "failover-to-first",
    "second-member",
    "mvc-off",
    "restore-original-active",
    "postcheck",
}
MEMBER_PHASES = {"first-member", "second-member"}
PHASES = (
    "validate",
    "capture-state",
    "baseline-capture",
    "stage-files",
    "first-member",
    "mixed-version-policy",
    "mvc-on",
    "failover-to-first",
    "simulate-tester-gate",
    "second-member",
    "final-policy",
    "mvc-off",
    "restore-original-active",
    "final-capture",
    "postcheck",
    "show-state",
)
PATCH_INSTALL_ORDER = (
    "validate",
    "capture-state",
    "baseline-capture",
    "stage-files",
    "first-member",
    "failover-to-first",
    "simulate-tester-gate",
    "second-member",
    "restore-original-active",
    "final-capture",
    "postcheck",
)
PATCH_REMOVE_ORDER = tuple(
    phase for phase in PATCH_INSTALL_ORDER if phase != "stage-files"
)
MAJOR_ORDER = (
    "validate",
    "capture-state",
    "baseline-capture",
    "stage-files",
    "first-member",
    "mixed-version-policy",
    "mvc-on",
    "failover-to-first",
    "simulate-tester-gate",
    "second-member",
    "final-policy",
    "mvc-off",
    "restore-original-active",
    "final-capture",
    "postcheck",
)
HOST_KEY_MARKERS = (
    "remote host identification has changed",
    "host key mismatch",
    "host key verification failed",
    "offending key",
)


class WorkflowError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def nofollow_flag() -> int:
    try:
        flag = os.O_NOFOLLOW
    except AttributeError as exc:
        raise WorkflowError("O_NOFOLLOW is required for protected file access") from exc
    if type(flag) is not int:
        raise WorkflowError("O_NOFOLLOW is required for protected file access")
    return flag


def protected_snapshot(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mode,
        value.st_uid,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso8601_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkflowError("workflow timestamps must include a UTC offset")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} requires a timestamp with a UTC offset")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkflowError(f"{label} has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowError(f"{label} timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_plan(raw: bytes) -> dict[str, Any]:
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"activity plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise WorkflowError("activity plan must be a JSON object")
    return plan


def load_plan(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return parse_plan(raw), sha256_bytes(raw)


def read_source_plan(path: Path) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Read one regular source-plan object and bind the bytes to its identity."""
    flags = os.O_RDONLY | nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorkflowError(f"cannot open activity plan source: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WorkflowError("activity plan source must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = path.lstat()
    except OSError as exc:
        raise WorkflowError(f"activity plan source changed while it was read: {exc}") from exc
    identity = (before.st_dev, before.st_ino)
    if identity != (after.st_dev, after.st_ino) or identity != (
        current.st_dev,
        current.st_ino,
    ):
        raise WorkflowError("activity plan source was replaced while it was read")
    if after.st_size != len(raw):
        raise WorkflowError("activity plan source changed while it was read")
    record = {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "size": len(raw),
        "mode": stat.S_IMODE(after.st_mode),
        "owner_uid": after.st_uid,
        "device": after.st_dev,
        "inode": after.st_ino,
    }
    return parse_plan(raw), raw, record


def plan_context(plan: dict[str, Any]) -> dict[str, Any]:
    checkpoint = plan.get("checkpoint") or {}
    members = checkpoint.get("members") or []
    steps = plan.get("package_steps") or []
    if len(members) != 2:
        raise WorkflowError("standalone workflow requires exactly two checkpoint.members")
    member_ips: list[str] = []
    member_identities: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for member in members:
        if not isinstance(member, dict):
            raise WorkflowError("checkpoint.members entries must be objects")
        value = member.get("ip")
        if not isinstance(value, str) or not value:
            raise WorkflowError("cluster member IP addresses must be non-empty strings")
        try:
            identity = ipaddress.ip_address(value)
        except ValueError as exc:
            raise WorkflowError(f"invalid cluster member IP address: {value!r}") from exc
        member_ips.append(value)
        member_identities.append(identity)
    if len(set(member_identities)) != 2:
        raise WorkflowError("cluster member IP addresses must be distinct")
    if len(steps) != 1:
        raise WorkflowError("standalone workflow requires exactly one package step")
    step = steps[0]
    action = str(step.get("action") or "").lower()
    if action not in {"install", "remove", "upgrade"}:
        raise WorkflowError(f"unsupported package action: {action!r}")
    step_name = str(step.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", step_name):
        raise WorkflowError("package step name contains unsafe characters")
    current_version = str(checkpoint.get("current_version") or "")
    target_version = str(checkpoint.get("target_version") or "")
    target_take = str(checkpoint.get("target_take") or "")
    if not current_version or not target_version:
        raise WorkflowError("checkpoint.current_version and target_version are required")
    if not re.fullmatch(r"\d{1,4}", target_take):
        raise WorkflowError("checkpoint.target_take must be one to four digits")
    package_type = str(step.get("package_type") or "").lower()
    required_package_type = {
        "install": "jhf",
        "remove": "jhf",
        "upgrade": "blink",
    }[action]
    if package_type != required_package_type:
        raise WorkflowError(
            f"standalone {action} requires package_type {required_package_type}"
        )
    execution = plan.get("execution") or {}
    if execution.get("deployment_backend") != "standalone":
        raise WorkflowError("execution.deployment_backend must be standalone")
    if execution.get("tester_pause") is not True:
        raise WorkflowError("execution.tester_pause must be true")
    if checkpoint.get("preserve_original_active") is not True:
        raise WorkflowError("checkpoint.preserve_original_active must be true")
    icap_mode = str(checkpoint.get("icap_mode") or "optional")
    if icap_mode not in {"required", "optional", "disabled"}:
        raise WorkflowError(f"unsupported checkpoint.icap_mode: {icap_mode!r}")
    package_name = str(step.get("package_name") or "")
    source_path = str(step.get("source_path") or "")
    if package_name and not re.fullmatch(r"[A-Za-z0-9_.+-]+", package_name):
        raise WorkflowError("package_name contains unsafe characters")
    if source_path:
        if not source_path.startswith("/") or "//" in source_path:
            raise WorkflowError("source_path must be an absolute normalized path")
        if any(part in {"", ".", ".."} for part in Path(source_path).parts[1:]):
            raise WorkflowError("source_path contains an unsafe path component")
        if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", source_path):
            raise WorkflowError("source_path contains unsafe characters")
    if action in {"install", "upgrade"}:
        if not source_path or not package_name:
            raise WorkflowError("install/upgrade requires source_path and package_name")
        checksum = str(step.get("checksum_sha256") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise WorkflowError("install/upgrade requires a valid published SHA256")
        if execution.get("staging_method") != "cprid_from_mds":
            raise WorkflowError(
                "install/upgrade requires execution.staging_method cprid_from_mds"
            )
        mds_host = str(checkpoint.get("mds_host") or "")
        if not mds_host:
            raise WorkflowError("install/upgrade requires checkpoint.mds_host")
        try:
            ipaddress.ip_address(mds_host)
        except ValueError:
            if (
                len(mds_host) > 253
                or ".." in mds_host
                or not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", mds_host
                )
            ):
                raise WorkflowError(
                    "checkpoint.mds_host must be an IP address or safe hostname"
                )
    change_number = str((plan.get("change") or {}).get("number") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", change_number):
        raise WorkflowError("change.number is required and must be path-safe")
    if action == "upgrade":
        if package_type != "blink":
            raise WorkflowError("major upgrade requires package_type blink")
        if current_version == target_version:
            raise WorkflowError("major upgrade requires different current and target versions")
        for field in ("cma_name", "domain", "cluster_name", "policy_package"):
            if not str(checkpoint.get(field) or "").strip():
                raise WorkflowError(f"major upgrade requires checkpoint.{field}")
        order = MAJOR_ORDER
        kind = "major-upgrade"
    elif action == "remove":
        if current_version != target_version:
            raise WorkflowError("package removal cannot change the target version")
        order = PATCH_REMOVE_ORDER
        kind = "patch-remove"
    else:
        if current_version != target_version:
            raise WorkflowError("package install cannot change the target version; use upgrade")
        order = PATCH_INSTALL_ORDER
        kind = "patch-install"
    return {
        "checkpoint": checkpoint,
        "members": member_ips,
        "member_identities": member_identities,
        "step": step,
        "step_name": step_name,
        "action": action,
        "target_take": target_take,
        "target_version": target_version,
        "icap_mode": icap_mode,
        "order": order,
        "kind": kind,
        "change_number": change_number,
    }


def protected_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise WorkflowError(f"run path is not a real directory: {path}")
    path.chmod(0o700)


def write_private(path: Path, data: bytes) -> None:
    protected_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def locked_plan_record(path: Path, raw: bytes) -> dict[str, Any]:
    info = path.lstat()
    return {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "size": len(raw),
        "owner_uid": info.st_uid,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def verify_source_binding(actual: dict[str, Any], payload: dict[str, Any]) -> None:
    expected = payload.get("source_plan")
    if not isinstance(expected, dict) or actual != expected:
        raise WorkflowError("activity plan source binding does not match the workflow journal")


def open_verified_locked_plan(
    path: Path, payload: dict[str, Any]
) -> tuple[dict[str, Any], str, int]:
    expected = payload.get("locked_plan")
    if not isinstance(expected, dict) or expected.get("path") != str(path):
        raise WorkflowError("locked plan path does not match the workflow journal")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        expected_identity = (expected.get("device"), expected.get("inode"))
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowError("locked plan snapshot must be a regular file")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise WorkflowError("locked plan snapshot must have mode 0600")
        if opened.st_uid != os.geteuid() or opened.st_uid != expected.get("owner_uid"):
            raise WorkflowError(
                "locked plan snapshot owner does not match the workflow journal"
            )
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise WorkflowError(
                "locked plan snapshot file identity does not match the workflow journal"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(fd)
        current = path.lstat()
    except WorkflowError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise WorkflowError(f"cannot open or read locked plan snapshot: {exc}") from exc
    try:
        if (after.st_dev, after.st_ino) != expected_identity or (
            current.st_dev, current.st_ino
        ) != expected_identity:
            raise WorkflowError("locked plan snapshot changed while it was read")
        if after.st_size != len(raw):
            raise WorkflowError("locked plan snapshot size changed while it was read")
        digest = sha256_bytes(raw)
        if len(raw) != expected.get("size") or digest != expected.get("sha256"):
            raise WorkflowError(
                "locked plan snapshot content hash does not match the journal"
            )
        os.lseek(fd, 0, os.SEEK_SET)
        return parse_plan(raw), digest, fd
    except Exception:
        os.close(fd)
        raise


def verify_locked_plan(path: Path, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    plan, digest, fd = open_verified_locked_plan(path, payload)
    os.close(fd)
    return plan, digest


def show_state_artifact_warnings(
    requested_source: Path,
    locked_plan_path: Path,
    payload: dict[str, Any],
) -> None:
    expected_source = payload.get("source_plan")
    if not isinstance(expected_source, dict) or not expected_source.get("path"):
        print("WARNING: journal has no source-plan binding to diagnose", file=sys.stderr)
    else:
        recorded_source = Path(str(expected_source["path"]))
        if os.path.abspath(requested_source) != os.path.abspath(recorded_source):
            print(
                "WARNING: requested source plan path differs from the journal binding",
                file=sys.stderr,
            )
        try:
            _, _, actual_source = read_source_plan(recorded_source)
            if actual_source != expected_source:
                print(
                    "WARNING: source plan does not match the journal binding",
                    file=sys.stderr,
                )
        except WorkflowError as exc:
            print(f"WARNING: source plan is unavailable: {exc}", file=sys.stderr)

    try:
        _, _, fd = open_verified_locked_plan(locked_plan_path, payload)
    except WorkflowError as exc:
        print(
            f"WARNING: locked plan integrity mismatch or unavailable: {exc}",
            file=sys.stderr,
        )
    else:
        os.close(fd)

def journal_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload": payload,
        "integrity_sha256": sha256_bytes(canonical_json(payload)),
    }


def write_journal(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(journal_envelope(payload), indent=2, sort_keys=True) + "\n"
    write_private(path, encoded.encode("utf-8"))


def member_operation_identity(
    payload: dict[str, Any], phase: str, event_nonce: str
) -> dict[str, Any]:
    return {
        "phase": phase,
        "sequence": len(payload["completed_phases"]) + 1,
        "plan_sha256": payload["plan_sha256"],
        "run_id": payload["run_id"],
        "event_nonce": event_nonce,
    }


def operation_id_for(payload: dict[str, Any], phase: str, event_nonce: str) -> str:
    identity = member_operation_identity(payload, phase, event_nonce)
    completion_id = sha256_bytes(canonical_json(identity))
    operation = {**identity, "completion_id": completion_id}
    return f"operation_{sha256_bytes(canonical_json(operation))}"


def mutation_intent_artifact_binding(
    operation: dict[str, Any], reconciliation: dict[str, Any]
) -> str:
    evidence = reconciliation.get("_intent_evidence")
    expected_evidence_keys = {
        "path",
        "sha256",
        "size",
        "mode",
        "owner_uid",
        "device",
        "inode",
        "mtime_ns",
        "ctime_ns",
    }
    digest = reconciliation.get("mutation_intent_sha256")
    if (
        not isinstance(evidence, dict)
        or set(evidence) != expected_evidence_keys
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(evidence.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None
    ):
        raise WorkflowError("member completion has invalid mutation-intent evidence")
    for key in (
        "size",
        "mode",
        "owner_uid",
        "device",
        "inode",
        "mtime_ns",
        "ctime_ns",
    ):
        if type(evidence.get(key)) is not int or evidence[key] < 0:
            raise WorkflowError(
                f"member completion has invalid mutation-intent {key}"
            )
    if (
        not isinstance(evidence.get("path"), str)
        or not evidence["path"]
        or evidence["size"] <= 0
        or evidence["mode"] != 0o600
    ):
        raise WorkflowError("member completion has invalid mutation-intent metadata")
    binding = {
        "run_id": operation["run_id"],
        "plan_sha256": operation["plan_sha256"],
        "phase": operation["phase"],
        "operation_id": operation["operation_id"],
        "completion_id": operation["completion_id"],
        "event_nonce": operation["event_nonce"],
        "mutation_intent_sha256": digest,
        "artifact_evidence": evidence,
    }
    return sha256_bytes(canonical_json(binding))


def create_pending_member_operation(
    payload: dict[str, Any],
    phase: str,
    context: dict[str, Any],
    *,
    event_nonce: str | None = None,
    created_at: datetime | None = None,
    created_at_ns: int | None = None,
) -> dict[str, Any]:
    nonce = event_nonce or secrets.token_hex(32)
    if EVENT_NONCE_PATTERN.fullmatch(nonce) is None:
        raise WorkflowError("pending member operation has an invalid event nonce")
    role = (
        "original_standby_host"
        if phase == "first-member"
        else "original_active_host"
    )
    host = payload.get("initial_roles", {}).get(role)
    try:
        ipaddress.ip_address(host)
    except (ValueError, TypeError) as exc:
        raise WorkflowError("pending member operation has no valid target host") from exc
    identity = member_operation_identity(payload, phase, nonce)
    completion_id = sha256_bytes(canonical_json(identity))
    record = {
        "schema_version": PENDING_OPERATION_VERSION,
        **identity,
        "operation_id": operation_id_for(payload, phase, nonce),
        "completion_id": completion_id,
        "created_at": iso8601_utc(created_at or utc_now()),
        "created_at_ns": (
            created_at_ns if created_at_ns is not None else time.time_ns()
        ),
        "host": str(host),
        "action": context["action"],
        "step_name": context["step_name"],
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
    return {**record, "pending_proof": sha256_bytes(canonical_json(record))}


def completion_record(
    payload: dict[str, Any],
    phase: str,
    completed_at: datetime | None = None,
    member_operation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = {
        "phase": phase,
        "sequence": len(payload["completed_phases"]) + 1,
        "plan_sha256": payload["plan_sha256"],
        "run_id": payload["run_id"],
    }
    timestamp = iso8601_utc(completed_at or utc_now())
    if phase not in MEMBER_PHASES:
        return {
            **identity,
            "completed_at": timestamp,
            "completion_id": sha256_bytes(canonical_json(identity)),
        }
    if not isinstance(member_operation, dict):
        raise WorkflowError("member completion requires its pending operation")
    reconciliation = payload.get("reconciliation", {}).get(phase)
    if not isinstance(reconciliation, dict):
        raise WorkflowError("member completion requires reconciliation evidence")
    member_identity = member_operation_identity(
        payload, phase, str(member_operation.get("event_nonce") or "")
    )
    completion_id = sha256_bytes(canonical_json(member_identity))
    if (
        member_operation.get("completion_id") != completion_id
        or member_operation.get("operation_id")
        != operation_id_for(payload, phase, member_identity["event_nonce"])
    ):
        raise WorkflowError("member completion does not match its pending operation")
    core = {
        **member_identity,
        "operation_id": member_operation["operation_id"],
        "completion_id": completion_id,
        "mutation_intent_binding_sha256": mutation_intent_artifact_binding(
            member_operation, reconciliation
        ),
        "completed_at": timestamp,
        "member_operation": member_operation,
    }
    return {**core, "record_proof": sha256_bytes(canonical_json(core))}


def validate_member_operation_record(
    payload: dict[str, Any],
    record: object,
    phase: str,
    sequence: int,
    read_ceiling: datetime | None = None,
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "phase",
        "sequence",
        "plan_sha256",
        "run_id",
        "event_nonce",
        "operation_id",
        "completion_id",
        "created_at",
        "created_at_ns",
        "host",
        "action",
        "step_name",
        "requested_package_name",
        "requested_source_path",
        "requested_package_type",
        "target_version",
        "target_take",
        "pending_proof",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise WorkflowError("workflow journal has an invalid pending member operation")
    nonce = record.get("event_nonce")
    if (
        record.get("schema_version") != PENDING_OPERATION_VERSION
        or record.get("phase") != phase
        or record.get("sequence") != sequence
        or record.get("plan_sha256") != payload.get("plan_sha256")
        or record.get("run_id") != payload.get("run_id")
        or not isinstance(nonce, str)
        or EVENT_NONCE_PATTERN.fullmatch(nonce) is None
        or not isinstance(record.get("created_at_ns"), int)
        or isinstance(record.get("created_at_ns"), bool)
        or record["created_at_ns"] <= 0
    ):
        raise WorkflowError("workflow journal has an invalid pending member operation")
    identity = {
        "phase": phase,
        "sequence": sequence,
        "plan_sha256": payload["plan_sha256"],
        "run_id": payload["run_id"],
        "event_nonce": nonce,
    }
    completion_id = sha256_bytes(canonical_json(identity))
    operation_id = (
        f"operation_{sha256_bytes(canonical_json({**identity, 'completion_id': completion_id}))}"
    )
    proof_body = {
        key: value for key, value in record.items() if key != "pending_proof"
    }
    if (
        record.get("completion_id") != completion_id
        or record.get("operation_id") != operation_id
        or record.get("pending_proof") != sha256_bytes(canonical_json(proof_body))
    ):
        raise WorkflowError("workflow journal has an invalid pending member operation")
    created_at = parse_timestamp(
        record.get("created_at"), f"{phase} pending member operation"
    )
    if read_ceiling is not None and created_at > read_ceiling.astimezone(timezone.utc):
        raise WorkflowError("pending member operation was created in the future")
    role = (
        "original_standby_host"
        if phase == "first-member"
        else "original_active_host"
    )
    expected_host = payload.get("initial_roles", {}).get(role)
    try:
        actual_identity = ipaddress.ip_address(record.get("host"))
        expected_identity = ipaddress.ip_address(expected_host)
    except (ValueError, TypeError) as exc:
        raise WorkflowError("pending member operation has an invalid host") from exc
    if actual_identity != expected_identity:
        raise WorkflowError("pending member operation has the wrong host")
    for key in (
        "action",
        "step_name",
        "requested_package_name",
        "requested_source_path",
        "requested_package_type",
        "target_version",
        "target_take",
    ):
        if not isinstance(record.get(key), str):
            raise WorkflowError(
                f"pending member operation has an invalid {key} context"
            )
    if record["action"] not in {"install", "upgrade", "remove"}:
        raise WorkflowError("pending member operation has an invalid action")
    if not record["step_name"] or not record["requested_package_type"]:
        raise WorkflowError("pending member operation has incomplete package context")
    return record


def verify_pending_member_operation(
    payload: dict[str, Any], read_ceiling: datetime
) -> dict[str, Any] | None:
    pending = payload.get("pending_member_operation")
    if not isinstance(pending, dict):
        raise WorkflowError("workflow journal pending member operation is invalid")
    if not pending:
        return None
    completed = payload["completed_phases"]
    phase_order = payload["phase_order"]
    if len(completed) >= len(phase_order):
        raise WorkflowError("workflow journal has a pending operation after completion")
    phase = phase_order[len(completed)]
    if phase not in MEMBER_PHASES:
        raise WorkflowError(
            "pending member operation is not for the next incomplete member phase"
        )
    return validate_member_operation_record(
        payload, pending, phase, len(completed) + 1, read_ceiling
    )



def verify_phase_completion_ledger(
    payload: dict[str, Any],
    read_ceiling: datetime | None = None,
) -> None:
    ceiling = read_ceiling if read_ceiling is not None else utc_now()
    if ceiling.tzinfo is None or ceiling.utcoffset() is None:
        raise WorkflowError("journal validation ceiling must include a UTC offset")
    ceiling = ceiling.astimezone(timezone.utc)
    completed = payload.get("completed_phases")
    phase_order = payload.get("phase_order")
    records = payload.get("phase_completions")
    plan_hash = payload.get("plan_sha256")
    run_id = payload.get("run_id")
    if (
        not isinstance(completed, list)
        or any(not isinstance(phase, str) for phase in completed)
        or len(completed) != len(set(completed))
    ):
        raise WorkflowError("workflow journal has an invalid completed phase list")
    if not isinstance(phase_order, list) or completed != phase_order[: len(completed)]:
        raise WorkflowError("workflow journal has out-of-order completed phases")
    if not isinstance(records, dict) or set(records) != set(completed):
        raise WorkflowError(
            "workflow journal phase-completion ledger does not match completed phases"
        )
    if not isinstance(plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
        raise WorkflowError("workflow journal has an invalid activity-plan hash")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise WorkflowError("workflow journal has an invalid run identity")

    previous_completed_at: datetime | None = None
    for sequence, phase in enumerate(completed, start=1):
        record = records.get(phase)
        if not isinstance(record, dict):
            raise WorkflowError(
                f"workflow journal has no completion record for {phase}"
            )
        expected_keys = {
            "phase",
            "sequence",
            "completed_at",
            "plan_sha256",
            "run_id",
            "completion_id",
        }
        identity = {
            "phase": phase,
            "sequence": sequence,
            "plan_sha256": plan_hash,
            "run_id": run_id,
        }
        if phase in MEMBER_PHASES:
            expected_keys.update(
                {
                    "event_nonce",
                    "operation_id",
                    "member_operation",
                    "mutation_intent_binding_sha256",
                    "record_proof",
                }
            )
            event_nonce = record.get("event_nonce")
            if (
                not isinstance(event_nonce, str)
                or EVENT_NONCE_PATTERN.fullmatch(event_nonce) is None
            ):
                raise WorkflowError(
                    f"workflow journal has an invalid completion record for {phase}"
                )
            identity["event_nonce"] = event_nonce
        if set(record) != expected_keys:
            raise WorkflowError(
                f"workflow journal has an invalid completion record for {phase}"
            )
        expected_completion_id = sha256_bytes(canonical_json(identity))
        if (
            record.get("phase") != phase
            or record.get("sequence") != sequence
            or record.get("plan_sha256") != plan_hash
            or record.get("run_id") != run_id
            or record.get("completion_id") != expected_completion_id
        ):
            raise WorkflowError(
                f"workflow journal has an invalid completion record for {phase}"
            )
        if phase in MEMBER_PHASES:
            expected_operation_id = operation_id_for(
                {
                    "completed_phases": completed[: sequence - 1],
                    "plan_sha256": plan_hash,
                    "run_id": run_id,
                },
                phase,
                identity["event_nonce"],
            )
            member_operation = validate_member_operation_record(
                payload,
                record.get("member_operation"),
                phase,
                sequence,
                ceiling,
            )
            core = {key: record[key] for key in expected_keys - {"record_proof"}}
            if (
                record.get("operation_id") != expected_operation_id
                or not isinstance(
                    record.get("mutation_intent_binding_sha256"), str
                )
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    record["mutation_intent_binding_sha256"],
                )
                is None
                or member_operation.get("event_nonce") != identity["event_nonce"]
                or member_operation.get("operation_id") != expected_operation_id
                or member_operation.get("completion_id") != expected_completion_id
                or record.get("record_proof")
                != sha256_bytes(canonical_json(core))
            ):
                raise WorkflowError(
                    f"workflow journal has an invalid completion record for {phase}"
                )
        completed_at = parse_timestamp(
            record.get("completed_at"), f"{phase} completion record"
        )
        if completed_at > ceiling:
            raise WorkflowError(
                f"workflow journal completion record for {phase} is in the future"
            )
        if previous_completed_at is not None and completed_at <= previous_completed_at:
            raise WorkflowError(
                "workflow journal completion timestamps are not strictly increasing"
            )
        previous_completed_at = completed_at


def verified_completion_record(
    payload: dict[str, Any],
    phase: str,
    read_ceiling: datetime | None = None,
) -> tuple[dict[str, Any], datetime]:
    verify_phase_completion_ledger(payload, read_ceiling)
    record = payload["phase_completions"].get(phase)
    if not isinstance(record, dict):
        raise WorkflowError(f"workflow journal has no completion record for {phase}")
    return record, parse_timestamp(
        record["completed_at"], f"{phase} completion record"
    )


def verify_tester_gate_binding(
    payload: dict[str, Any],
    read_ceiling: datetime | None = None,
) -> None:
    gate = payload.get("tester_gate")
    completed = payload.get("completed_phases", [])
    gate_completed = "simulate-tester-gate" in completed
    if not gate_completed:
        if gate not in ({}, None):
            raise WorkflowError(
                "workflow journal has tester-gate evidence before gate completion"
            )
        return
    if not isinstance(gate, dict) or not gate:
        raise WorkflowError("workflow journal has no completed tester-gate record")

    run_id = payload["run_id"]
    if gate.get("run_id") != run_id:
        raise WorkflowError("workflow journal tester-gate run identity does not match")
    failover, _ = verified_completion_record(
        payload, "failover-to-first", read_ceiling
    )
    expected = {
        "run_id": run_id,
        "completion_id": failover["completion_id"],
        "completed_at": failover["completed_at"],
    }
    if gate.get("failover_completion") != expected:
        raise WorkflowError(
            "workflow journal tester-gate failover binding does not match"
        )



def read_protected_intent(
    path: Path,
    *,
    expected_evidence: dict[str, Any] | None = None,
    not_before_ns: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent
    parent_fd: int | None = None
    fd: int | None = None
    try:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if type(directory_flag) is not int:
            raise WorkflowError(
                "O_DIRECTORY is required for protected intent access"
            )
        parent_fd = os.open(
            parent, os.O_RDONLY | directory_flag | nofollow_flag()
        )
        parent_opened = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or parent_opened.st_uid != os.geteuid()
            or stat.S_IMODE(parent_opened.st_mode) != 0o700
        ):
            raise WorkflowError(
                "mutation-intent directory must be owner-owned mode 0700"
            )
        fd = os.open(
            absolute.name,
            os.O_RDONLY | nofollow_flag(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise WorkflowError(
                "mutation-intent artifact must be an owner-owned mode-0600 regular file"
            )
        if (
            opened.st_mtime_ns < not_before_ns - 1_000_000_000
            or opened.st_ctime_ns < not_before_ns - 1_000_000_000
        ):
            raise WorkflowError("mutation-intent artifact predates its pending operation")
        if opened.st_size <= 0 or opened.st_size > 1_048_576:
            raise WorkflowError("mutation-intent artifact has an invalid size")

        def descriptor_bytes() -> bytes:
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if total > 1_048_576:
                    raise WorkflowError("mutation-intent artifact is too large")
                chunks.append(chunk)

        raw = descriptor_bytes()
        after_first = os.fstat(fd)
        confirmation = descriptor_bytes()
        after_second = os.fstat(fd)
        current = absolute.lstat()
        parent_current = parent.lstat()
        final_confirmation = descriptor_bytes()
        after_path_check = os.fstat(fd)
        parent_after = os.fstat(parent_fd)
        opened_snapshot = protected_snapshot(opened)
        parent_snapshot = protected_snapshot(parent_opened)
        if (
            len(raw) != opened.st_size
            or confirmation != raw
            or final_confirmation != raw
            or protected_snapshot(after_first) != opened_snapshot
            or protected_snapshot(after_second) != opened_snapshot
            or protected_snapshot(after_path_check) != opened_snapshot
            or protected_snapshot(current) != opened_snapshot
            or protected_snapshot(parent_after) != parent_snapshot
            or protected_snapshot(parent_current) != parent_snapshot
        ):
            raise WorkflowError(
                "mutation-intent artifact changed while it was read"
            )
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(
            f"mutation-intent artifact cannot be opened safely: {exc}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"mutation-intent artifact is invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise WorkflowError("mutation-intent artifact must contain a JSON object")
    evidence = {
        "path": str(absolute),
        "sha256": sha256_bytes(raw),
        "size": len(raw),
        "mode": stat.S_IMODE(opened.st_mode),
        "owner_uid": opened.st_uid,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "mtime_ns": opened.st_mtime_ns,
        "ctime_ns": opened.st_ctime_ns,
    }
    if expected_evidence is not None and evidence != expected_evidence:
        raise WorkflowError("mutation-intent artifact no longer matches journal evidence")
    return document, evidence


def validate_intent_document(
    document: dict[str, Any],
    operation: dict[str, Any],
    reconciliation: dict[str, Any] | None = None,
) -> str:
    package_name = (
        str((reconciliation or {}).get("package_name") or "")
        if operation["action"] == "remove"
        else operation["requested_package_name"]
    )
    if operation["action"] == "remove" and not package_name:
        package_name = str(document.get("package_name") or "")
    if (
        not package_name
        or Path(package_name).name != package_name
        or re.fullmatch(r"[A-Za-z0-9_.+-]+", package_name) is None
    ):
        raise WorkflowError("mutation-intent artifact has an invalid package identity")
    expected = {
        "schema": MUTATION_INTENT_VERSION,
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
        "package_name": package_name,
    }
    if operation["action"] != "remove":
        expected.update(
            {
                "target_version": operation["target_version"],
                "target_take": operation["target_take"],
            }
        )
    if set(document) != set(expected):
        raise WorkflowError("mutation-intent artifact has an unsupported schema")
    try:
        document_host = ipaddress.ip_address(document.get("host"))
        expected_host = ipaddress.ip_address(operation["host"])
    except (ValueError, TypeError) as exc:
        raise WorkflowError("mutation-intent artifact has an invalid host") from exc
    if document_host != expected_host:
        raise WorkflowError("mutation-intent artifact has the wrong host")
    for key, value in expected.items():
        if key != "host" and document.get(key) != value:
            raise WorkflowError(
                f"mutation-intent artifact has invalid {key} binding"
            )
    return sha256_bytes(canonical_json(document))


def validate_intent_artifact(
    journal_path: Path,
    operation: dict[str, Any],
    reconciliation: dict[str, Any] | None = None,
    expected_evidence: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    path = journal_path.parent / "mutation-intents" / f"{operation['phase']}.json"
    document, evidence = read_protected_intent(
        path,
        expected_evidence=expected_evidence,
        not_before_ns=operation["created_at_ns"],
    )
    digest = validate_intent_document(document, operation, reconciliation)
    if (
        reconciliation is not None
        and reconciliation.get("mutation_intent_sha256") != digest
    ):
        raise WorkflowError(
            f"workflow journal reconciliation {operation['phase']} "
            "does not match the protected mutation-intent artifact"
        )
    return digest, evidence


def intent_directory_entries(journal_path: Path) -> set[str]:
    directory = journal_path.parent / "mutation-intents"
    fd: int | None = None
    try:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if type(directory_flag) is not int:
            raise WorkflowError(
                "O_DIRECTORY is required for protected intent access"
            )
        fd = os.open(
            directory, os.O_RDONLY | directory_flag | nofollow_flag()
        )
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise WorkflowError(
                "mutation-intent directory must be owner-owned mode 0700"
            )
        entries = set(os.listdir(fd))
        after = os.fstat(fd)
        current = directory.lstat()
        if (
            protected_snapshot(after) != protected_snapshot(opened)
            or protected_snapshot(current) != protected_snapshot(opened)
        ):
            raise WorkflowError(
                "mutation-intent directory changed while it was inspected"
            )
        return entries
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(
            f"mutation-intent directory cannot be inspected safely: {exc}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)


def verify_reconciliation_ledger(
    payload: dict[str, Any],
    journal_path: Path,
    read_ceiling: datetime,
) -> None:
    completed = payload.get("completed_phases", [])
    reconciliation = payload.get("reconciliation")
    expected_phases = {phase for phase in completed if phase in MEMBER_PHASES}
    if not isinstance(reconciliation, dict) or set(reconciliation) != expected_phases:
        raise WorkflowError(
            "workflow journal reconciliation ledger does not match completed member phases"
        )
    pending = verify_pending_member_operation(payload, read_ceiling)
    expected_intent_names = {f"{phase}.json" for phase in expected_phases}
    if pending is not None:
        pending_name = f"{pending['phase']}.json"
        pending_path = journal_path.parent / "mutation-intents" / pending_name
        if pending_path.exists() or pending_path.is_symlink():
            expected_intent_names.add(pending_name)
    actual_intent_names = intent_directory_entries(journal_path)
    if actual_intent_names != expected_intent_names:
        raise WorkflowError(
            "mutation-intent artifacts do not match completed or pending member phases"
        )

    seen_nonces: set[str] = set()
    for phase in expected_phases:
        record = reconciliation.get(phase)
        completion = payload["phase_completions"].get(phase)
        if not isinstance(record, dict) or not isinstance(completion, dict):
            raise WorkflowError(f"workflow journal has invalid reconciliation for {phase}")
        operation = completion.get("member_operation")
        if not isinstance(operation, dict):
            raise WorkflowError(f"workflow journal has no member operation for {phase}")
        event_nonce = completion["event_nonce"]
        if event_nonce in seen_nonces:
            raise WorkflowError("workflow journal reuses a member operation nonce")
        seen_nonces.add(event_nonce)
        expected = {
            "schema_version": RECONCILIATION_VERSION,
            "run_id": payload["run_id"],
            "plan_sha256": payload["plan_sha256"],
            "phase": phase,
            "operation_id": completion["operation_id"],
            "completion_id": completion["completion_id"],
            "event_nonce": event_nonce,
        }
        for key, value in expected.items():
            if record.get(key) != value:
                raise WorkflowError(
                    f"workflow journal reconciliation {phase} has invalid {key} binding"
                )
        try:
            actual_identity = ipaddress.ip_address(record.get("host"))
            expected_identity = ipaddress.ip_address(operation["host"])
        except (ValueError, TypeError) as exc:
            raise WorkflowError(
                f"workflow journal reconciliation {phase} has an invalid host binding"
            ) from exc
        if actual_identity != expected_identity:
            raise WorkflowError(
                f"workflow journal reconciliation {phase} has the wrong host binding"
            )
        if operation["action"] == "remove":
            if record.get("result") != "exact-package-absence-confirmed":
                raise WorkflowError(
                    f"workflow journal reconciliation {phase} has invalid outcome"
                )
        else:
            outcome = {
                "result": "exact-target-confirmed",
                "target_version": operation["target_version"],
                "target_take": operation["target_take"],
                "package_name": operation["requested_package_name"],
            }
            for key, value in outcome.items():
                if record.get(key) != value:
                    raise WorkflowError(
                        f"workflow journal reconciliation {phase} has invalid {key}"
                    )
        evidence = record.get("_evidence")
        intent_evidence = record.get("_intent_evidence")
        bound_payload = {
            key: value
            for key, value in record.items()
            if key not in {"_evidence", "_intent_evidence"}
        }
        if (
            not isinstance(evidence, dict)
            or evidence.get("payload_sha256")
            != sha256_bytes(canonical_json(bound_payload))
            or not isinstance(intent_evidence, dict)
        ):
            raise WorkflowError(
                f"workflow journal reconciliation {phase} payload binding is invalid"
            )
        validate_intent_artifact(
            journal_path,
            operation,
            record,
            expected_evidence=intent_evidence,
        )
        if completion.get(
            "mutation_intent_binding_sha256"
        ) != mutation_intent_artifact_binding(operation, record):
            raise WorkflowError(
                f"workflow journal reconciliation {phase} does not match "
                "the completed mutation-intent artifact binding"
            )
    if pending is not None:
        if pending["event_nonce"] in seen_nonces:
            raise WorkflowError("pending member operation reuses a completed nonce")
        pending_path = (
            journal_path.parent
            / "mutation-intents"
            / f"{pending['phase']}.json"
        )
        if pending_path.exists() or pending_path.is_symlink():
            validate_intent_artifact(journal_path, pending)
def read_journal(path: Path, plan_hash: str | None) -> dict[str, Any]:
    read_ceiling = utc_now()
    if path.is_symlink():
        raise WorkflowError("journal cannot be a symbolic link")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise WorkflowError("workflow journal must have mode 0600")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read workflow journal: {exc}") from exc
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise WorkflowError("workflow journal payload is missing")
    expected = sha256_bytes(canonical_json(payload))
    if envelope.get("integrity_sha256") != expected:
        raise WorkflowError("workflow journal integrity check failed")
    if payload.get("version") != JOURNAL_VERSION:
        raise WorkflowError("unsupported workflow journal version")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise WorkflowError("workflow journal has an invalid run identity")
    if plan_hash is not None and payload.get("plan_sha256") != plan_hash:
        raise WorkflowError("activity plan does not match the workflow journal")
    verify_phase_completion_ledger(payload, read_ceiling)
    verify_tester_gate_binding(payload, read_ceiling)
    verify_reconciliation_ledger(payload, path, read_ceiling)
    return payload


def new_journal(
    plan_hash: str,
    context: dict[str, Any],
    source_plan: dict[str, Any],
    locked_plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": JOURNAL_VERSION,
        "run_id": f"run_{secrets.token_hex(32)}",
        "plan_sha256": plan_hash,
        "source_plan": source_plan,
        "locked_plan": locked_plan,
        "workflow_kind": context["kind"],
        "phase_order": list(context["order"]),
        "completed_phases": [],
        "phase_completions": {},
        "initial_roles": {},
        "reconciliation": {},
        "pending_member_operation": {},
        "tester_gate": {},
        "host_key_stops": [],
        "host_key_remediation": [],
        "final_ownership": {},
    }


def verify_order(payload: dict[str, Any], phase: str, expected_order: tuple[str, ...]) -> None:
    if payload.get("phase_order") != list(expected_order):
        raise WorkflowError("workflow journal phase order does not match the activity plan")
    completed = payload["completed_phases"]
    if completed != list(expected_order[: len(completed)]):
        raise WorkflowError("workflow journal contains out-of-order completed phases")
    if phase in completed:
        raise WorkflowError(f"phase is already complete: {phase}")
    if len(completed) >= len(expected_order) or phase != expected_order[len(completed)]:
        next_phase = expected_order[len(completed)] if len(completed) < len(expected_order) else "none"
        raise WorkflowError(f"phase {phase} is out of order; next required phase is {next_phase}")


def command_for_phase(
    phase: str,
    args: argparse.Namespace,
    context: dict[str, Any],
    payload: dict[str, Any],
    locked_plan_path: Path | str | None = None,
    state_file_path: Path | str | None = None,
    member_operation: dict[str, Any] | None = None,
    reconciliation_only: bool = False,
) -> tuple[list[str], Path | None]:
    members = context["members"]
    common_cluster = [
        "--members", *members, "--username", args.username,
        "--icap-mode", context["icap_mode"],
    ]
    state_file = str(
        state_file_path
        or (
            args.run_dir
            / "reports"
            / f"cluster_initial_state_{context['change_number']}.json"
        )
    )
    plan = str(locked_plan_path or (args.run_dir / LOCKED_PLAN_NAME))
    reports = str(args.run_dir / "reports")
    python = sys.executable
    reconciliation_file: Path | None = None
    if phase == "capture-state":
        return [python, str(SCRIPTS / "cluster_phase_control.py"), "capture-state", *common_cluster, "--state-file", str(state_file)], None
    if phase in {"baseline-capture", "final-capture"}:
        label = "baseline" if phase == "baseline-capture" else "final"
        return [python, str(ROOT / "checkpoint_cluster_upgrade.py"), "--members", *members, "--username", args.username, "--phase", "support-capture", "--support-label", label, "--support-output-dir", reports, "--icap-mode", context["icap_mode"]], None
    if phase == "stage-files":
        return [python, str(SCRIPTS / "stage_packages_cprid.py"), "--activity-plan-file", plan, "--username", args.username], None
    if phase in {"first-member", "second-member"}:
        if not isinstance(member_operation, dict):
            raise WorkflowError("member phase has no pending operation")
        reconciliation_file = args.run_dir / "reconciliation" / f"{phase}.json"
        mutation_intent_file = args.run_dir / "mutation-intents" / f"{phase}.json"
        command = [
            python,
            str(SCRIPTS / "direct_package_step_from_activity.py"),
            "--activity-plan-file",
            plan,
            "--state-file",
            state_file,
            "--phase",
            phase,
            "--step",
            context["step_name"],
            "--username",
            args.username,
            "--reconciliation-file",
            str(reconciliation_file),
            "--mutation-intent-file",
            str(mutation_intent_file),
            "--standalone-run-id",
            payload["run_id"],
            "--standalone-plan-sha256",
            payload["plan_sha256"],
            "--standalone-phase",
            phase,
            "--standalone-operation-id",
            member_operation["operation_id"],
            "--standalone-completion-id",
            member_operation["completion_id"],
            "--standalone-event-nonce",
            member_operation["event_nonce"],
        ]
        if reconciliation_only:
            command.append("--standalone-reconciliation-only")
        command.append("--execute")
        return command, reconciliation_file
    if phase in {"mixed-version-policy", "final-policy"}:
        helper_phase = "mixed-version-policy-gate" if phase == "mixed-version-policy" else "final-policy-install"
        return [python, str(SCRIPTS / "major_policy_gate_from_activity.py"), "--activity-plan-file", plan, "--phase", helper_phase, "--username", args.username], None
    if phase in {"mvc-on", "mvc-off"}:
        return [python, str(SCRIPTS / "major_mvc_from_activity.py"), "--activity-plan-file", plan, "--state-file", state_file, "--phase", phase, "--username", args.username], None
    if phase == "failover-to-first":
        target = str(payload.get("initial_roles", {}).get("original_standby_host") or "")
        if not target:
            raise WorkflowError("captured original standby member is missing from the journal")
        return [python, str(SCRIPTS / "cluster_phase_control.py"), "failover-to", *common_cluster, "--state-file", str(state_file), "--target-host", target], None
    if phase == "restore-original-active":
        return [python, str(SCRIPTS / "cluster_phase_control.py"), "restore-original-active", *common_cluster, "--state-file", str(state_file)], None
    if phase == "postcheck":
        command = [python, str(SCRIPTS / "postcheck_gateways.py"), "--members", *members, "--username", args.username, "--target-take", context["target_take"], "--icap-mode", context["icap_mode"], "--state-file", str(state_file), "--activity-plan-file", plan]
        if context["action"] == "remove":
            command.extend(["--absent-take", context["target_take"]])
        return command, None
    raise WorkflowError(f"phase has no helper command: {phase}")


def run_helper(
    command: list[str],
    phase: str,
    run_dir: Path,
    pass_fds: tuple[int, ...] = (),
) -> str:
    if any(Path(part).name == "ansible-playbook" for part in command):
        raise WorkflowError("standalone workflow refuses to invoke ansible-playbook")
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )
    output = result.stdout + result.stderr
    write_private((run_dir / "logs" / f"{phase}.log"), output.encode("utf-8", errors="replace"))
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if result.returncode:
        raise WorkflowError(f"{phase} helper failed with return code {result.returncode}")
    return output


def read_protected_evidence(path: Path) -> tuple[bytes, dict[str, Any]]:
    absolute = Path(os.path.abspath(path))
    fd: int | None = None
    try:
        fd = os.open(absolute, os.O_RDONLY | nofollow_flag())
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowError(f"evidence must be a regular file: {path}")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise WorkflowError(f"evidence file must have mode 0600: {path}")
        if opened.st_uid != os.geteuid():
            raise WorkflowError(
                f"evidence file must be owned by the executing user: {path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after_read = os.fstat(fd)
        current_path = absolute.lstat()
        final = os.fstat(fd)
        opened_snapshot = protected_snapshot(opened)
        if (
            len(raw) != opened.st_size
            or protected_snapshot(after_read) != opened_snapshot
            or protected_snapshot(current_path) != opened_snapshot
            or protected_snapshot(final) != opened_snapshot
        ):
            raise WorkflowError(f"evidence file changed while it was read: {path}")
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(f"cannot open protected evidence file {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not raw.strip():
        raise WorkflowError(f"evidence file is empty: {path}")
    return raw, {
        "path": str(absolute),
        "sha256": sha256_bytes(raw),
        "size": len(raw),
        "mode": stat.S_IMODE(opened.st_mode),
        "owner_uid": opened.st_uid,
        "device": opened.st_dev,
        "inode": opened.st_ino,
    }


def evidence_record(path: Path) -> dict[str, Any]:
    _, record = read_protected_evidence(path)
    return record


def validate_gate_evidence(
    path: Path,
    context: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    validation_ceiling = utc_now()
    raw, record = read_protected_evidence(path)
    rows = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row is not an object")
                rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkflowError(f"tester-gate evidence is not valid JSONL: {exc}") from exc
    if len(rows) < 3:
        raise WorkflowError("tester-gate evidence requires at least three monitoring samples")

    failover, failover_completed_at = verified_completion_record(
        payload, "failover-to-first", validation_ceiling
    )
    expected_members = set(context["member_identities"])
    first_host = str(payload.get("initial_roles", {}).get("original_standby_host") or "")
    previous_sample_id: int | None = None
    previous_timestamp: datetime | None = None
    for index, row in enumerate(rows, start=1):
        sample_id = row.get("sample")
        if (
            not isinstance(sample_id, int)
            or isinstance(sample_id, bool)
            or sample_id < 1
            or (previous_sample_id is not None and sample_id <= previous_sample_id)
        ):
            raise WorkflowError(
                f"tester-gate sample {index} has a missing, duplicate, or out-of-order sample identifier"
            )
        sample_timestamp = parse_timestamp(
            row.get("timestamp"), f"tester-gate sample {index}"
        )
        if sample_timestamp <= failover_completed_at:
            raise WorkflowError(
                f"tester-gate sample {index} is not newer than the completed failover"
            )
        if sample_timestamp > validation_ceiling:
            raise WorkflowError(
                f"tester-gate sample {index} timestamp is in the future"
            )
        if previous_timestamp is not None and sample_timestamp <= previous_timestamp:
            raise WorkflowError(
                f"tester-gate sample {index} timestamp is not strictly increasing"
            )
        previous_sample_id = sample_id
        previous_timestamp = sample_timestamp

        members = row.get("members")
        active_count = row.get("active_count")
        standby_count = row.get("standby_count")
        if (
            row.get("cluster_shape_ok") is not True
            or type(active_count) is not int
            or active_count != 1
            or type(standby_count) is not int
            or standby_count != 1
            or not isinstance(members, list)
        ):
            raise WorkflowError(f"tester-gate sample {index} has an unhealthy cluster shape")
        if len(members) != 2:
            raise WorkflowError(
                f"tester-gate sample {index} must contain exactly two member objects"
            )
        by_host: dict[
            ipaddress.IPv4Address | ipaddress.IPv6Address, dict[str, Any]
        ] = {}
        for member in members:
            if not isinstance(member, dict):
                raise WorkflowError(
                    f"tester-gate sample {index} contains a non-object member"
                )
            host = member.get("host")
            if not isinstance(host, str) or not host:
                raise WorkflowError(
                    f"tester-gate sample {index} contains an invalid member host"
                )
            try:
                host_identity = ipaddress.ip_address(host)
            except ValueError as exc:
                raise WorkflowError(
                    f"tester-gate sample {index} contains an invalid member host"
                ) from exc
            if host_identity in by_host:
                raise WorkflowError(
                    f"tester-gate sample {index} contains duplicate member host {host}"
                )
            by_host[host_identity] = member
        if set(by_host) != expected_members:
            raise WorkflowError(f"tester-gate sample {index} does not cover both plan members")
        for member in by_host.values():
            host = str(member["host"])
            if member.get("error"):
                raise WorkflowError(f"tester-gate sample {index} reports an error for {host}")
            if member.get("pnotes_ok") is not True or member.get("interfaces_ok") is not True:
                raise WorkflowError(f"tester-gate sample {index} is unhealthy for {host}")
            if context["icap_mode"] == "required" and member.get("icap_ok") is not True:
                raise WorkflowError(f"tester-gate sample {index} has unhealthy ICAP for {host}")
        first = by_host.get(ipaddress.ip_address(first_host), {})
        original_active_host = str(
            payload.get("initial_roles", {}).get("original_active_host") or ""
        )
        original_active = by_host.get(ipaddress.ip_address(original_active_host), {})
        if not str(first.get("cluster_state") or "").upper().startswith("ACTIVE"):
            raise WorkflowError(
                f"tester-gate sample {index} does not show the upgraded first member ACTIVE"
            )
        if not str(original_active.get("cluster_state") or "").upper().startswith(
            "STANDBY"
        ):
            raise WorkflowError(
                f"tester-gate sample {index} does not show the original active member STANDBY"
            )
        if context["action"] == "remove":
            if str(first.get("take")) == context["target_take"]:
                raise WorkflowError(f"tester-gate sample {index} still reports the removed Take")
        elif str(first.get("take")) != context["target_take"]:
            raise WorkflowError(f"tester-gate sample {index} does not report the target Take")
    record["samples"] = len(rows)
    record["first_sample_id"] = rows[0]["sample"]
    record["last_sample_id"] = rows[-1]["sample"]
    record["first_sample_at"] = rows[0]["timestamp"]
    record["last_sample_at"] = rows[-1]["timestamp"]
    record["failover_completion"] = {
        "run_id": payload["run_id"],
        "completion_id": failover["completion_id"],
        "completed_at": failover["completed_at"],
    }
    record["run_id"] = payload["run_id"]
    record["authorization"] = "simulated-after-technical-validation"
    return record


def open_captured_state(
    path: Path,
    expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowError("captured cluster-state evidence must be a regular file")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise WorkflowError("captured cluster-state evidence must have mode 0600")
        if opened.st_uid != os.geteuid():
            raise WorkflowError(
                "captured cluster-state evidence must be owned by the executing user"
            )
        if expected:
            if expected.get("path") != str(path):
                raise WorkflowError(
                    "captured cluster-state path does not match the workflow journal"
                )
            if (opened.st_dev, opened.st_ino) != (
                expected.get("device"),
                expected.get("inode"),
            ):
                raise WorkflowError(
                    "captured cluster-state file identity does not match the journal"
                )
            if opened.st_uid != expected.get("owner_uid"):
                raise WorkflowError(
                    "captured cluster-state owner does not match the journal"
                )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(fd)
        current = path.lstat()
    except WorkflowError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise WorkflowError(
            f"cannot open or read captured cluster-state evidence: {exc}"
        ) from exc
    try:
        identity = (opened.st_dev, opened.st_ino)
        if (after.st_dev, after.st_ino) != identity or (
            current.st_dev, current.st_ino
        ) != identity:
            raise WorkflowError(
                "captured cluster-state evidence changed while it was read"
            )
        if after.st_size != len(raw):
            raise WorkflowError(
                "captured cluster-state evidence size changed while it was read"
            )
        digest = sha256_bytes(raw)
        if expected and (
            len(raw) != expected.get("size") or digest != expected.get("sha256")
        ):
            raise WorkflowError("captured cluster-state integrity check failed")
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"captured cluster state is not valid JSON: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise WorkflowError("captured cluster state must be a JSON object")
        record = {
            "path": str(path),
            "sha256": digest,
            "size": len(raw),
            "mode": stat.S_IMODE(opened.st_mode),
            "owner_uid": opened.st_uid,
            "device": opened.st_dev,
            "inode": opened.st_ino,
        }
        os.lseek(fd, 0, os.SEEK_SET)
        return state, record, fd
    except Exception:
        os.close(fd)
        raise


def load_captured_roles(run_dir: Path, members: list[str]) -> dict[str, Any]:
    candidates = list((run_dir / "reports").glob("cluster_initial_state_*.json"))
    if len(candidates) != 1:
        raise WorkflowError("expected exactly one captured cluster-state file")
    state, record, fd = open_captured_state(candidates[0])
    os.close(fd)
    active = state.get("original_active_host")
    standby = state.get("original_standby_host")
    if active == standby or {active, standby} != set(members):
        raise WorkflowError("captured active/standby roles do not match the plan members")
    return {
        "original_active_host": active,
        "original_standby_host": standby,
        "state_sha256": record["sha256"],
        "state_file": record,
    }


def open_verified_captured_state(
    run_dir: Path,
    context: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    path = (
        run_dir
        / "reports"
        / f"cluster_initial_state_{context['change_number']}.json"
    )
    roles = payload.get("initial_roles") or {}
    expected = roles.get("state_file")
    if not isinstance(expected, dict):
        raise WorkflowError(
            "captured cluster-state file binding is missing from the journal"
        )
    if expected.get("sha256") != roles.get("state_sha256"):
        raise WorkflowError(
            "captured cluster-state hash binding is inconsistent in the journal"
        )
    state, _, fd = open_captured_state(path, expected)
    active = state.get("original_active_host")
    standby = state.get("original_standby_host")
    if (
        active != roles.get("original_active_host")
        or standby != roles.get("original_standby_host")
        or active == standby
        or {active, standby} != set(context["members"])
    ):
        os.close(fd)
        raise WorkflowError(
            "captured cluster-state roles do not match the workflow journal"
        )
    return state, fd


def verify_captured_state(
    run_dir: Path,
    context: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    _, fd = open_verified_captured_state(run_dir, context, payload)
    os.close(fd)

def create_reconciliation_output(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent
    parent_meta = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_meta.st_mode)
        or parent_meta.st_uid != os.geteuid()
        or stat.S_IMODE(parent_meta.st_mode) & 0o077
    ):
        raise WorkflowError(
            "member reconciliation directory must be a private real directory "
            "owned by the executing user"
        )
    try:
        fd = os.open(
            absolute,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow_flag(),
            0o600,
        )
    except OSError as exc:
        raise WorkflowError(
            f"cannot create protected member reconciliation output: {exc}"
        ) from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.geteuid()
    ):
        os.close(fd)
        raise WorkflowError("member reconciliation output identity is invalid")
    return fd

def read_reconciliation(
    path: Path,
    fd: int,
    phase: str,
    context: dict[str, Any],
    payload: dict[str, Any],
    not_before_ns: int,
    member_operation: dict[str, Any],
    journal_path: Path,
) -> dict[str, Any]:
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent
    try:
        parent_meta = parent.lstat()
    except OSError as exc:
        raise WorkflowError(
            f"member reconciliation directory is unavailable: {exc}"
        ) from exc
    if not stat.S_ISDIR(parent_meta.st_mode) or parent.is_symlink():
        raise WorkflowError("member reconciliation directory must be a real directory")
    if parent_meta.st_uid != os.geteuid():
        raise WorkflowError(
            "member reconciliation directory must be owned by the executing user"
        )
    if stat.S_IMODE(parent_meta.st_mode) & 0o077:
        raise WorkflowError(
            "member reconciliation directory must not be group/world accessible"
        )

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowError(
                "member helper did not produce regular reconciliation evidence"
            )
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise WorkflowError("member reconciliation evidence must have mode 0600")
        if opened.st_uid != os.geteuid():
            raise WorkflowError(
                "member reconciliation evidence must be owned by the executing user"
            )
        if (
            opened.st_mtime_ns < not_before_ns - 1_000_000_000
            or opened.st_ctime_ns < not_before_ns - 1_000_000_000
        ):
            raise WorkflowError(
                "member reconciliation evidence predates the current helper dispatch"
            )
        if opened.st_size <= 0:
            raise WorkflowError("member helper produced empty reconciliation evidence")

        def descriptor_bytes() -> bytes:
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        raw = descriptor_bytes()
        after_first = os.fstat(fd)
        confirmation = descriptor_bytes()
        after_second = os.fstat(fd)
        current = absolute.lstat()
        final_confirmation = descriptor_bytes()
        after_path_check = os.fstat(fd)
        opened_snapshot = protected_snapshot(opened)
        if (
            len(raw) != opened.st_size
            or confirmation != raw
            or final_confirmation != raw
            or protected_snapshot(after_first) != opened_snapshot
            or protected_snapshot(after_second) != opened_snapshot
            or protected_snapshot(after_path_check) != opened_snapshot
            or protected_snapshot(current) != opened_snapshot
        ):
            raise WorkflowError(
                "member reconciliation evidence changed while it was read"
            )
    except WorkflowError:
        raise
    except OSError as exc:
        raise WorkflowError(
            f"member reconciliation evidence cannot be read safely: {exc}"
        ) from exc

    try:
        reconciliation = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            f"member helper did not produce valid reconciliation evidence: {exc}"
        ) from exc
    if not isinstance(reconciliation, dict):
        raise WorkflowError("member reconciliation evidence must be a JSON object")
    expected_bindings = {
        "schema_version": RECONCILIATION_VERSION,
        "run_id": payload["run_id"],
        "plan_sha256": payload["plan_sha256"],
        "phase": phase,
        "operation_id": member_operation["operation_id"],
        "completion_id": member_operation["completion_id"],
        "event_nonce": member_operation["event_nonce"],
    }
    for key, value in expected_bindings.items():
        if reconciliation.get(key) != value:
            raise WorkflowError(
                f"member reconciliation does not match expected {key} binding"
            )
    try:
        reconciliation_host = ipaddress.ip_address(reconciliation.get("host"))
        expected_host_identity = ipaddress.ip_address(member_operation["host"])
    except (ValueError, TypeError) as exc:
        raise WorkflowError("member reconciliation has an invalid host binding") from exc
    if reconciliation_host != expected_host_identity:
        raise WorkflowError("member reconciliation evidence targets the wrong host")
    if context["action"] == "remove":
        if reconciliation.get("result") != "exact-package-absence-confirmed":
            raise WorkflowError("member reconciliation does not prove exact package absence")
        package_name = str(reconciliation.get("package_name") or "")
        if (
            Path(package_name).name != package_name
            or not re.fullmatch(r"[A-Za-z0-9_.+-]+", package_name)
        ):
            raise WorkflowError(
                "member reconciliation has an invalid resolved package identity"
            )
    else:
        expected = {
            "result": "exact-target-confirmed",
            "target_version": context["target_version"],
            "target_take": context["target_take"],
            "package_name": str(context["step"].get("package_name") or ""),
        }
        for key, value in expected.items():
            if reconciliation.get(key) != value:
                raise WorkflowError(
                    f"member reconciliation does not match expected {key}"
                )
    if "_evidence" in reconciliation or "_intent_evidence" in reconciliation:
        raise WorkflowError("member reconciliation contains a reserved evidence field")
    _, intent_evidence = validate_intent_artifact(
        journal_path, member_operation, reconciliation
    )
    reconciliation_evidence = {
        "path": str(absolute),
        "sha256": sha256_bytes(raw),
        "size": len(raw),
        "mode": stat.S_IMODE(opened.st_mode),
        "owner_uid": opened.st_uid,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "mtime_ns": opened.st_mtime_ns,
        "ctime_ns": opened.st_ctime_ns,
        "payload_sha256": sha256_bytes(canonical_json(reconciliation)),
    }
    reconciliation["_evidence"] = reconciliation_evidence
    reconciliation["_intent_evidence"] = intent_evidence
    return reconciliation



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--activity-plan-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--host-key-evidence", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.run_dir.is_symlink():
        print("ERROR: run directory cannot be a symbolic link", file=sys.stderr)
        return 2
    args.run_dir = args.run_dir.resolve()
    protected_directory(args.run_dir)
    protected_directory(args.run_dir / "logs")
    protected_directory(args.run_dir / "reports")
    protected_directory(args.run_dir / "reconciliation")
    protected_directory(args.run_dir / "mutation-intents")

    lock_path = args.run_dir / "workflow.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        print(f"ERROR: cannot open protected workflow lock: {exc}", file=sys.stderr)
        return 2

    plan_fd: int | None = None
    state_fd: int | None = None
    reconciliation_fd: int | None = None
    member_operation: dict[str, Any] | None = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowError(
                "another standalone workflow process holds the run lock"
            ) from exc

        journal_path = args.run_dir / "workflow-state.json"
        locked_plan_path = args.run_dir / LOCKED_PLAN_NAME

        if args.phase == "show-state":
            if not journal_path.exists():
                raise WorkflowError("run validate before showing workflow state")
            payload = read_journal(journal_path, None)
            print(json.dumps(payload, indent=2, sort_keys=True))
            show_state_artifact_warnings(
                args.activity_plan_file, locked_plan_path, payload
            )
            return 0

        if args.phase != "validate" and not args.execute:
            raise WorkflowError("phase execution requires --execute")

        try:
            args.activity_plan_file = args.activity_plan_file.resolve(strict=True)
        except OSError as exc:
            raise WorkflowError(f"activity plan source is unavailable: {exc}") from exc
        source_plan, source_raw, source_record = read_source_plan(
            args.activity_plan_file
        )
        source_hash = source_record["sha256"]

        if args.phase == "validate" and not journal_path.exists():
            if locked_plan_path.exists() or locked_plan_path.is_symlink():
                raise WorkflowError(
                    "locked plan snapshot already exists without a journal"
                )
            context = plan_context(source_plan)
            write_private(locked_plan_path, source_raw)
            locked_record = locked_plan_record(locked_plan_path, source_raw)
            payload = new_journal(
                source_hash, context, source_record, locked_record
            )
            verify_locked_plan(locked_plan_path, payload)
        else:
            if not journal_path.exists():
                raise WorkflowError("run validate before any other phase")
            payload = read_journal(journal_path, None)
            plan, locked_hash, plan_fd = open_verified_locked_plan(
                locked_plan_path, payload
            )
            verify_source_binding(source_record, payload)
            if (
                locked_hash != payload.get("plan_sha256")
                or locked_hash != source_hash
            ):
                raise WorkflowError(
                    "locked plan snapshot is not bound to the source plan"
                )
            context = plan_context(plan)

        verify_order(payload, args.phase, context["order"])
        if (
            args.phase not in {"validate", "capture-state"}
            and "capture-state" in payload["completed_phases"]
        ):
            _, verified_state_fd = open_verified_captured_state(
                args.run_dir, context, payload
            )
            if args.phase in STATE_CONSUMING_PHASES:
                state_fd = verified_state_fd
            else:
                os.close(verified_state_fd)

        latest_stop = None
        if payload.get("host_key_stops"):
            candidate = payload["host_key_stops"][-1]
            if candidate.get("phase") == args.phase:
                latest_stop = candidate
        if latest_stop and not isinstance(latest_stop.get("stop_id"), int):
            raise WorkflowError("latest host-key stop has no durable stop identity")
        stop_is_remediated = bool(
            latest_stop
            and any(
                remediation.get("phase") == args.phase
                and remediation.get("stop_id") == latest_stop.get("stop_id")
                and remediation.get("stop_log_sha256")
                == latest_stop.get("log_sha256")
                for remediation in payload.get("host_key_remediation", [])
            )
        )
        if latest_stop and not stop_is_remediated and not args.host_key_evidence:
            raise WorkflowError(
                f"{args.phase} retry requires --host-key-evidence for the latest host-key stop"
            )
        if args.host_key_evidence:
            if args.phase not in {"first-member", "second-member"}:
                raise WorkflowError(
                    "host-key evidence is accepted only for member execution phases"
                )
            if not latest_stop:
                raise WorkflowError("no host-key stop is recorded for this phase")
            remediation = evidence_record(args.host_key_evidence)
            remediation["phase"] = args.phase
            remediation["stop_id"] = latest_stop["stop_id"]
            remediation["stop_log_sha256"] = latest_stop["log_sha256"]
            payload["host_key_remediation"].append(remediation)
            write_journal(journal_path, payload)

        if args.phase == "validate":
            print(f"Validated {context['kind']} plan; no live command was run")
        elif args.phase == "simulate-tester-gate":
            if not args.evidence:
                raise WorkflowError("simulate-tester-gate requires --evidence")
            payload["tester_gate"] = validate_gate_evidence(
                args.evidence, context, payload
            )
        else:
            plan_argument = (
                f"/proc/self/fd/{plan_fd}" if plan_fd is not None else locked_plan_path
            )
            state_argument = (
                f"/proc/self/fd/{state_fd}" if state_fd is not None else None
            )
            reconciliation_only = False
            if args.phase in MEMBER_PHASES:
                pending = payload["pending_member_operation"]
                if pending:
                    member_operation = validate_member_operation_record(
                        payload,
                        pending,
                        args.phase,
                        len(payload["completed_phases"]) + 1,
                        utc_now(),
                    )
                    reconciliation_only = True
                else:
                    member_operation = create_pending_member_operation(
                        payload, args.phase, context
                    )
                    payload["pending_member_operation"] = member_operation
                    write_journal(journal_path, payload)
            command, reconciliation_file = command_for_phase(
                args.phase,
                args,
                context,
                payload,
                plan_argument,
                state_argument,
                member_operation,
                reconciliation_only,
            )
            if reconciliation_file and (
                reconciliation_file.exists() or reconciliation_file.is_symlink()
            ):
                reconciliation_file.unlink()
            if reconciliation_file:
                reconciliation_fd = create_reconciliation_output(
                    reconciliation_file
                )
                argument_index = command.index("--reconciliation-file")
                command[argument_index : argument_index + 2] = [
                    "--reconciliation-fd",
                    str(reconciliation_fd),
                ]

            inherited: list[int] = []
            if plan_fd is not None and str(plan_argument) in command:
                inherited.append(plan_fd)
            if (
                state_fd is not None
                and state_argument is not None
                and state_argument in command
            ):
                inherited.append(state_fd)
            if reconciliation_fd is not None:
                inherited.append(reconciliation_fd)
            helper_started_ns = time.time_ns()
            try:
                if inherited:
                    output = run_helper(
                        command,
                        args.phase,
                        args.run_dir,
                        pass_fds=tuple(inherited),
                    )
                else:
                    output = run_helper(command, args.phase, args.run_dir)
            except WorkflowError as exc:
                log_path = args.run_dir / "logs" / f"{args.phase}.log"
                log_text = (
                    log_path.read_text(errors="replace")
                    if log_path.exists()
                    else str(exc)
                )
                if args.phase in {"first-member", "second-member"} and any(
                    marker in log_text.lower() for marker in HOST_KEY_MARKERS
                ):
                    payload["host_key_stops"].append(
                        {
                            "stop_id": len(payload["host_key_stops"]) + 1,
                            "phase": args.phase,
                            "log_sha256": sha256_bytes(log_text.encode()),
                        }
                    )
                    write_journal(journal_path, payload)
                    raise WorkflowError(
                        f"{args.phase} stopped on changed host key; verify the new "
                        "fingerprint and retry with --host-key-evidence"
                    ) from exc
                raise
            if args.phase == "capture-state":
                payload["initial_roles"] = load_captured_roles(
                    args.run_dir, context["members"]
                )
            if reconciliation_file:
                if not isinstance(member_operation, dict):
                    raise WorkflowError("member phase lost its pending operation")
                reconciliation = read_reconciliation(
                    reconciliation_file,
                    reconciliation_fd,
                    args.phase,
                    context,
                    payload,
                    helper_started_ns,
                    member_operation,
                    journal_path,
                )
                payload["reconciliation"][args.phase] = reconciliation
            if args.phase == "restore-original-active":
                payload["final_ownership"] = {
                    "active_host": payload["initial_roles"]["original_active_host"],
                    "helper_output_sha256": sha256_bytes(output.encode()),
                }

        completed = completion_record(
            payload,
            args.phase,
            member_operation=(
                member_operation if args.phase in MEMBER_PHASES else None
            ),
        )
        payload["completed_phases"].append(args.phase)
        payload["phase_completions"][args.phase] = completed
        if args.phase in MEMBER_PHASES:
            payload["pending_member_operation"] = {}
        write_journal(journal_path, payload)
        print(f"Standalone phase complete: {args.phase}")
        return 0
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if state_fd is not None:
            os.close(state_fd)
        if plan_fd is not None:
            os.close(plan_fd)
        if reconciliation_fd is not None:
            os.close(reconciliation_fd)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
