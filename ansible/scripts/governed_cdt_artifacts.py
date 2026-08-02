#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


CONTEXT_FIELDS = frozenset({
    "schema", "operation_id", "change_identity", "activity_plan_sha256",
    "phase", "step_name", "action", "target_host", "selected_candidate_ip",
    "package_name", "package_type", "target_version", "target_take",
    "target_build", "identity_source", "context_id", "created_at_ns",
})
RECEIPT_FIELDS = frozenset({
    "schema", "operation_id", "change_identity", "activity_plan_sha256",
    "phase", "step_name", "action", "target_host", "selected_candidate_ip",
    "package_name", "package_type", "target_version", "target_take",
    "target_build", "identity_source", "context_id", "context_sha256",
    "context_created_at_ns", "receipt_id", "mutation_completed_at_ns",
})
EVIDENCE_FIELDS = frozenset({
    "schema", "operation_id", "change_identity", "activity_plan_sha256",
    "phase", "step_name", "action", "target_host", "selected_candidate_ip",
    "package_name", "package_type", "target_version", "target_take",
    "target_build", "identity_source", "context_id", "context_sha256",
    "receipt_id", "receipt_sha256", "mutation_completed_at_ns",
    "reconciled_at_ns", "observed",
})


def require_private_directory(path: Path, label: str) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"{label} must be owned by the effective user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"{label} must have mode 0700")


def ensure_private_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    require_private_directory(path, label)


def require_exact_schema(
    payload: dict[str, Any], expected_fields: frozenset[str], label: str
) -> None:
    actual = frozenset(payload)
    missing = sorted(expected_fields - actual)
    unknown = sorted(actual - expected_fields)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise RuntimeError(f"{label} schema mismatch ({'; '.join(details)})")


def atomic_write_private_json(path: Path, payload: dict[str, Any]) -> bytes:
    """Create an immutable private JSON artifact without replacing any pathname."""
    ensure_private_directory(path.parent, "CDT artifact directory")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = -1
    created_identity: tuple[int, int] | None = None
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w+b") as handle:
            fd = -1
            os.fchmod(handle.fileno(), 0o600)
            before = os.fstat(handle.fileno())
            created_identity = (before.st_dev, before.st_ino)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            persisted = handle.read()
            after = os.fstat(handle.fileno())
            if persisted != data or (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise RuntimeError("CDT artifact changed while it was created")
            if after.st_uid != os.geteuid() or stat.S_IMODE(after.st_mode) != 0o600:
                raise RuntimeError("new CDT artifact is not owner-owned mode 0600")
        path_meta = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(path_meta.st_mode):
            raise RuntimeError("new CDT artifact pathname is not a regular file")
        if (path_meta.st_dev, path_meta.st_ino) != created_identity:
            raise RuntimeError("new CDT artifact pathname was replaced")
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if created_identity is not None:
            try:
                metadata = path.lstat()
                if not path.is_symlink() and (
                    metadata.st_dev,
                    metadata.st_ino,
                ) == created_identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise
    return data


def read_private_json(path: Path, label: str) -> tuple[dict[str, Any], bytes, os.stat_result]:
    require_private_directory(path.parent, f"{label} directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"{label} must be a regular file")
        if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise RuntimeError(f"{label} must be owner-owned mode 0600")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read()
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"{label} changed while it was being read")
        path_meta = path.lstat()
        if path.is_symlink() or (path_meta.st_dev, path_meta.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise RuntimeError(f"{label} pathname was replaced")
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return payload, data, before


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
