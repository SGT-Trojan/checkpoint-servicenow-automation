#!/usr/bin/env python3
"""Poll ServiceNow for governed Check Point CHGs and launch the runner.

The worker is the missing ServiceNow-first trigger layer.  ServiceNow remains
the system of record, while this process acts like the MID-side executor:

* find governed CHGs in Implement/approved state,
* verify the readiness SCTASK and implementation CTASK gates,
* run ``servicenow_checkpoint_runner.py`` once for the eligible CHG,
* remember whether the CHG completed, failed, or is waiting at a gate,
* resume from the second member only after a tester CTASK has been closed,
* create an engineer remediation CTASK on failure and resume from the failed
  phase only after that CTASK is closed with resume approval.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from servicenow_checkpoint_runner import (
    APPROVED_VALUES,
    AUTOMATION_MARKER,
    CHANGE_FIELDS,
    IMPLEMENT_STATE_VALUES,
    READINESS_CLOSED_STATES,
    ROOT,
    ServiceNowClient,
    display_value,
    has_automation_marker,
    implementation_task,
    readiness_tasks,
    validate_service_now_governance,
)

RUNNER = ROOT / "servicenow_checkpoint_runner.py"
DEFAULT_STATE_FILE = ROOT / "runs" / "worker_state.json"
DEFAULT_LOG_DIR = ROOT / "runs" / "worker_logs"
TESTER_TASK_TERMS = ("tester", "testing", "validation")
ENGINEER_REMEDIATION_PREFIX = "Engineer remediation required - Check Point automation"
FINAL_VALIDATION_SHORT_DESCRIPTION = "Final validation - Check Point post-implementation checks"
RESUME_APPROVED_VALUES = {"approved", "ready", "resume_approved"}
RESUME_REJECTED_VALUES = {"rejected", "not_viable", "blocked"}
COMPLETE_STATES = {"3", "7", "closed complete", "closed_complete", "closed skipped", "closed_skipped"}
TESTER_APPROVED_STATES = {"3", "closed complete", "closed_complete"}
PRE_PHASE_FAILURES = {"", "unknown", "initialization", "discover-targets"}
INCOMPLETE_STATES = {"4", "closed incomplete", "closed_incomplete", "canceled", "cancelled"}
OPERATION_ID_RE = re.compile(r"run_[0-9a-f]{64}")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"changes": {}}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise RuntimeError("worker state directory must be a private real directory")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp.exists():
            tmp.unlink()


def post_note(sn: ServiceNowClient, chg: dict[str, Any], text: str) -> None:
    sn.post_work_note("change_request", chg["sys_id"], text)


def ref_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def ref_display(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "")
    return str(value or "")


def redact_cmd(cmd: list[str]) -> list[str]:
    redacted = list(cmd)
    for idx, item in enumerate(redacted[:-1]):
        if item in {"--sn-password", "--password", "--cp-password", "--expert-password"}:
            redacted[idx + 1] = "***REDACTED***"
    return redacted


def latest_resume_state(chg_number: str, *, newer_than: float = 0.0) -> dict[str, Any]:
    candidates = sorted(
        ROOT.glob(f"runs/{chg_number}_*/resume_state.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.stat().st_mtime < newer_than:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data.setdefault("run_dir", str(path.parent))
        data.setdefault("resume_state", str(path))
        return data
    return {}


def latest_completed_run(chg_number: str) -> dict[str, Any]:
    candidates = sorted(
        ROOT.glob(f"runs/{chg_number}_*/summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == "completed":
            data.setdefault("run_dir", str(path.parent))
            data.setdefault("summary_file", str(path))
            return data
    return {}


def final_validation_task(sn: ServiceNowClient, chg_sys_id: str) -> dict[str, Any] | None:
    rows = sn.results(
        "change_task",
        f"change_request={chg_sys_id}^short_description={FINAL_VALIDATION_SHORT_DESCRIPTION}",
        "sys_id,number,short_description,state",
        1,
    )
    return rows[0] if rows else None


def complete_success_bookkeeping(sn: ServiceNowClient, chg: dict[str, Any], impl: dict[str, Any] | None, entry: dict[str, Any]) -> None:
    chg_number = str(chg.get("number") or chg["sys_id"])
    completed = latest_completed_run(chg_number)
    run_dir = str(completed.get("run_dir") or "")
    summary_file = str(completed.get("summary_file") or "")
    run_reference = Path(run_dir).name if run_dir else ""
    summary_reference = Path(summary_file).name if summary_file else ""
    evidence_lines = [
        line
        for line in [
            f"Evidence reference: {run_reference}" if run_reference else "",
            f"Summary artifact: {summary_reference}" if summary_reference else "",
        ]
        if line
    ]
    close_summary = (
        "Check Point automation completed successfully. Final post-implementation validation passed: "
        "target package/take state, cluster active/standby health, pnotes, monitored interfaces, and configured postcheck gates were validated."
    )
    evidence = "\n".join(evidence_lines)
    full_note = close_summary + ("\n" + evidence if evidence else "")

    final_task = final_validation_task(sn, chg["sys_id"])
    if not final_task:
        body: dict[str, Any] = {
            "change_request": chg["sys_id"],
            "short_description": FINAL_VALIDATION_SHORT_DESCRIPTION,
            "description": (
                "Records the automated final post-implementation validation for the Check Point firewall workflow. "
                "This CTASK is created and closed by the local automation worker after final postcheck succeeds. "
                "It confirms the requested software state, cluster health, pnotes, monitored interfaces, and evidence capture completed successfully."
            ),
            "state": "3",
            "close_notes": full_note,
            "work_notes": full_note,
        }
        if impl:
            if ref_value(impl.get("assignment_group")):
                body["assignment_group"] = ref_value(impl.get("assignment_group"))
            if ref_value(impl.get("assigned_to")):
                body["assigned_to"] = ref_value(impl.get("assigned_to"))
        final_task = sn.table("POST", "change_task", body=body).get("result", {})
    else:
        sn.patch("change_task", final_task["sys_id"], {"state": "3", "close_notes": full_note, "work_notes": full_note})

    if impl:
        sn.patch(
            "change_task",
            impl["sys_id"],
            {
                "state": "3",
                "close_notes": close_summary,
                "work_notes": "Implementation CTASK closed by automation after successful final validation. " + (evidence or ""),
            },
        )

    # Close the relabeled change-model default tasks so the change model can
    # progress its Implement phase naturally; the explicit Review move below is
    # the fallback if the model does not auto-advance.
    for row in sn.results(
        "change_task",
        f"change_request={chg['sys_id']}^short_descriptionSTARTSWITHChange-model default:^stateIN1,2,-5",
        "sys_id,number",
        10,
    ):
        sn.patch("change_task", row["sys_id"], {
            "state": "3",
            "close_notes": "Closed by automation after successful completion of the Check Point workflow.",
        })

    sn.patch(
        "change_request",
        chg["sys_id"],
        {
            "state": "0",
            "work_notes": close_summary + " Moving CHG to Review for normal post-implementation review and closure." + ("\n" + evidence if evidence else ""),
        },
    )
    entry["final_validation_task_number"] = final_task.get("number")
    entry["final_validation_task_sys_id"] = final_task.get("sys_id")
    entry["post_success_bookkeeping"] = "completed"


def remediation_tasks(sn: ServiceNowClient, chg_sys_id: str) -> list[dict[str, Any]]:
    tasks = sn.results(
        "change_task",
        f"change_request={chg_sys_id}",
        "sys_id,number,short_description,state,assignment_group,assigned_to,close_notes,"
        "u_checkpoint_resume_status,u_checkpoint_resume_phase,u_checkpoint_resume_summary,"
        "u_checkpoint_resume_evidence,sys_updated_on",
        100,
    )
    return [task for task in tasks if str(task.get("short_description") or "").startswith(ENGINEER_REMEDIATION_PREFIX)]


def open_remediation_task(sn: ServiceNowClient, chg_sys_id: str) -> dict[str, Any] | None:
    for task in remediation_tasks(sn, chg_sys_id):
        if display_value(task.get("state")).lower() not in READINESS_CLOSED_STATES:
            return task
    return None


def remediation_resume_decision(task: dict[str, Any]) -> str:
    state = display_value(task.get("state")).lower()
    resume_status = str(task.get("u_checkpoint_resume_status") or "").strip().lower()
    if state not in READINESS_CLOSED_STATES:
        return "open"
    if resume_status in RESUME_REJECTED_VALUES or state in INCOMPLETE_STATES:
        return "rejected"
    if state in COMPLETE_STATES and resume_status in RESUME_APPROVED_VALUES:
        return "approved"
    return "closed_without_approval"


def create_engineer_remediation_task(
    sn: ServiceNowClient,
    chg: dict[str, Any],
    impl: dict[str, Any] | None,
    entry: dict[str, Any],
    rc: int,
) -> dict[str, Any]:
    existing = open_remediation_task(sn, chg["sys_id"])
    if existing:
        return existing

    chg_number = chg.get("number") or chg["sys_id"]
    resume = latest_resume_state(
        str(chg_number), newer_than=float(entry.get("last_started_epoch") or 0.0)
    )
    failed_phase = str(resume.get("failed_phase") or entry.get("failed_phase") or "initialization")
    failed_playbook = str(resume.get("failed_playbook") or entry.get("failed_playbook") or "unknown")
    failed_step = str(resume.get("failed_step") or entry.get("failed_step") or "")
    failed_log = str(resume.get("failed_log") or entry.get("last_log") or "")
    run_dir = str(resume.get("run_dir") or "")
    run_reference = Path(run_dir).name if run_dir else ""
    log_reference = Path(failed_log).name if failed_log else ""
    summary = (
        f"Automation failed with rc={rc} at phase {failed_phase}"
        + (f" step {failed_step}" if failed_step else "")
        + f" ({failed_playbook}). Engineer remediation is required before automation can resume."
    )
    evidence = "\n".join(
        value
        for value in [
            f"evidence_reference={run_reference}" if run_reference else "",
            f"failed_log_artifact={log_reference}" if log_reference else "",
        ]
        if value
    )
    body: dict[str, Any] = {
        "change_request": chg["sys_id"],
        "short_description": f"{ENGINEER_REMEDIATION_PREFIX} at {failed_phase}",
        "description": (
            summary
            + "\n\nWhen remediation is complete, set Checkpoint Resume Status to approved and close this CTASK Complete. "
            + "The local worker will resume from the failed phase automatically."
        ),
        "state": "1",
        "u_checkpoint_resume_status": "pending",
        "u_checkpoint_resume_phase": failed_phase,
        "u_checkpoint_resume_summary": summary,
        "u_checkpoint_resume_evidence": evidence,
        "work_notes": summary + ("\n" + evidence if evidence else ""),
    }
    if impl:
        if ref_value(impl.get("assignment_group")):
            body["assignment_group"] = ref_value(impl.get("assignment_group"))
        if ref_value(impl.get("assigned_to")):
            body["assigned_to"] = ref_value(impl.get("assigned_to"))
    task = sn.table("POST", "change_task", body=body).get("result", {})
    entry.update(
        {
            "status": "waiting_engineer_remediation",
            "failed_phase": failed_phase,
            "failed_playbook": failed_playbook,
            "failed_step": failed_step,
            "failed_log": failed_log,
            "failed_run_dir": run_dir,
            "remediation_task_sys_id": task.get("sys_id"),
            "remediation_task_number": task.get("number"),
        }
    )
    return task


def change_candidates(sn: ServiceNowClient, limit: int) -> list[dict[str, Any]]:
    query = f"state=-1^approval=approved^descriptionLIKE{AUTOMATION_MARKER}"
    rows = sn.results(
        "change_request",
        query,
        CHANGE_FIELDS + ",assigned_to,assignment_group,sys_updated_on",
        limit,
    )
    out = []
    for chg in rows:
        if not has_automation_marker(chg):
            continue
        if display_value(chg.get("state")) not in IMPLEMENT_STATE_VALUES:
            continue
        if display_value(chg.get("approval")) not in APPROVED_VALUES:
            continue
        out.append(chg)
    return out


def context_for_change(sn: ServiceNowClient, chg: dict[str, Any]) -> dict[str, Any]:
    ritm_id = chg.get("parent", {}).get("value") if isinstance(chg.get("parent"), dict) else str(chg.get("parent") or "")
    return {
        "chg": chg,
        "ritm_id": ritm_id,
        "readiness_tasks": readiness_tasks(sn, ritm_id),
        "implementation_task": implementation_task(sn, chg["sys_id"]),
    }


def closed_tester_task_exists(sn: ServiceNowClient, chg_sys_id: str) -> bool:
    tasks = sn.results(
        "change_task",
        f"change_request={chg_sys_id}",
        "sys_id,number,short_description,state,close_notes",
        100,
    )
    for task in tasks:
        short = str(task.get("short_description") or "").lower()
        # Only the intentional BR-created gate task may approve the gate. Loose
        # term matching previously risked auto-approval from the automation-authored
        # final-validation CTASK or from relabeled/closed change-model default tasks
        # (all of which contain "testing"/"validation" and end up Closed Complete).
        if not short.startswith("tester validation gate"):
            continue
        # Only Closed Complete is an affirmative tester authorization. Skipped,
        # canceled, and incomplete tasks keep the workflow blocked.
        if display_value(task.get("state")).lower() in TESTER_APPROVED_STATES:
            return True
    return False


def remediation_start_at(entry: dict[str, Any], task: dict[str, Any]) -> str:
    requested = str(task.get("u_checkpoint_resume_phase") or "").strip()
    failed = requested or str(entry.get("failed_phase") or "").strip()
    return "" if failed.lower() in PRE_PHASE_FAILURES else failed


def implementation_task_is_open(task: dict[str, Any] | None) -> bool:
    if not task:
        return False
    return display_value(task.get("state")).lower() not in READINESS_CLOSED_STATES | {"4", "closed incomplete"}


def operation_id_for_entry(entry: dict[str, Any], *, start_at: str) -> str:
    operation_id = entry.get("operation_id")
    if operation_id is None:
        if start_at:
            raise RuntimeError("cannot resume governed automation without its persisted operation ID")
        operation_id = f"run_{secrets.token_hex(32)}"
        entry["operation_id"] = operation_id
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(operation_id):
        raise RuntimeError("worker state contains an invalid governed operation ID")
    return operation_id


def build_runner_cmd(
    args: argparse.Namespace,
    chg_sys_id: str,
    *,
    operation_id: str,
    start_at: str = "",
) -> list[str]:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--chg-sys-id",
        chg_sys_id,
        "--operation-id",
        operation_id,
    ]
    if start_at:
        cmd.extend(["--start-at", start_at])
    if args.simulate_gates:
        cmd.append("--simulate-gates")
    return cmd


def run_runner(args: argparse.Namespace, chg: dict[str, Any], *, start_at: str, state: dict[str, Any]) -> int:
    chg_number = chg.get("number") or chg["sys_id"]
    run_kind = f"resume_{start_at}" if start_at else "start"
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{chg_number}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}_{run_kind}.log"
    entry = state.setdefault("changes", {}).setdefault(chg["sys_id"], {})
    operation_id = operation_id_for_entry(entry, start_at=start_at)
    cmd = build_runner_cmd(
        args,
        chg["sys_id"],
        operation_id=operation_id,
        start_at=start_at,
    )
    safe_cmd = redact_cmd(cmd)
    entry.update(
        {
            "number": chg_number,
            "status": "running",
            "mode": run_kind,
            "last_started_at": utc_now(),
            "last_started_epoch": time.time(),
            "last_log": str(log_path),
            "command": safe_cmd,
            "operation_id": operation_id,
        }
    )
    save_state(Path(args.state_file), state)

    if args.dry_run:
        log_path.write_text("DRY RUN: " + " ".join(safe_cmd) + "\n")
        return 0

    with log_path.open("w") as log:
        log.write("$ " + " ".join(safe_cmd) + "\n")
        log.flush()
        child_env = os.environ.copy()
        child_env["SN_INSTANCE"] = args.instance
        child_env["SN_USERNAME"] = args.sn_username
        child_env["SN_PASSWORD"] = args.sn_password
        proc = subprocess.Popen(cmd, cwd=str(ROOT.parent), stdout=log, stderr=subprocess.STDOUT, env=child_env)
        entry["pid"] = proc.pid
        save_state(Path(args.state_file), state)
        return proc.wait()


def process_once(args: argparse.Namespace, sn: ServiceNowClient, state: dict[str, Any]) -> bool:
    for chg in change_candidates(sn, args.limit):
        context = context_for_change(sn, chg)
        try:
            validate_service_now_governance(context, allow_lab_override=False)
        except SystemExit as exc:
            print(f"Skipping {chg.get('number')}: {exc}", file=sys.stderr)
            continue
        impl = context.get("implementation_task")
        if not implementation_task_is_open(impl):
            continue

        entry = state.setdefault("changes", {}).setdefault(chg["sys_id"], {"number": chg.get("number")})
        status = entry.get("status")
        if status in {"completed", "running", "remediation_rejected", "stopped_by_operator"}:
            continue

        start_at = ""
        if status == "waiting_tester":
            if not closed_tester_task_exists(sn, chg["sys_id"]):
                continue
            start_at = "second-member"
            if args.dry_run:
                print(f"DRY RUN: would resume {chg.get('number')} from second-member after tester CTASK closure")
                return True
            post_note(sn, chg, "Check Point automation worker: tester CTASK closure detected; resuming from second-member phase.")
        elif status == "waiting_engineer_remediation":
            task_sys_id = str(entry.get("remediation_task_sys_id") or "")
            task = None
            if task_sys_id:
                task = sn.table("GET", f"change_task/{task_sys_id}").get("result", {})
            if not task or not task.get("sys_id"):
                task = open_remediation_task(sn, chg["sys_id"])
            if not task:
                task = create_engineer_remediation_task(sn, chg, impl, entry, int(entry.get("last_rc") or 1))
                save_state(Path(args.state_file), state)
                continue
            decision = remediation_resume_decision(task)
            if decision == "open":
                continue
            if decision != "approved":
                entry["status"] = "remediation_rejected"
                entry["last_finished_at"] = utc_now()
                save_state(Path(args.state_file), state)
                post_note(sn, chg, f"Check Point automation worker: remediation CTASK {task.get('number')} closed without resume approval; automation remains blocked.")
                return True
            start_at = remediation_start_at(entry, task)
            resume_label = start_at or "the beginning"
            if args.dry_run:
                print(f"DRY RUN: would resume {chg.get('number')} from {resume_label} after engineer remediation approval")
                return True
            post_note(sn, chg, f"Check Point automation worker: engineer remediation CTASK {task.get('number')} approved; resuming from {resume_label}.")
        else:
            if args.dry_run:
                print(f"DRY RUN: would start governed firewall automation for {chg.get('number')} ({chg['sys_id']})")
                return True
            post_note(sn, chg, "Check Point automation worker: governed CHG detected in Implement/approved state; starting firewall automation.")

        rc = run_runner(args, chg, start_at=start_at, state=state)
        entry = state["changes"][chg["sys_id"]]
        entry["last_finished_at"] = utc_now()
        entry["last_rc"] = rc
        entry.pop("pid", None)
        if rc == 0:
            entry["status"] = "completed"
            # Never let bookkeeping failures crash the worker after a successful
            # firewall run: the run outcome must be persisted as completed even if
            # the final ServiceNow updates fail, otherwise the CHG restarts stranded
            # in a stale "running" state after the service respawns.
            try:
                complete_success_bookkeeping(sn, chg, impl, entry)
                post_note(sn, chg, "Check Point automation worker: runner completed successfully and post-run bookkeeping is complete.")
            except Exception as exc:
                entry["bookkeeping_error"] = str(exc)
                try:
                    post_note(sn, chg, f"Check Point automation worker: runner completed successfully but post-run bookkeeping failed ({exc}). Close the implementation/final validation CTASKs and move the CHG to Review manually.")
                except Exception:
                    pass
        elif rc == 20:
            entry["status"] = "waiting_tester"
            post_note(sn, chg, "Check Point automation worker: runner stopped at tester gate; close the tester CTASK to resume.")
        elif rc == 21:
            entry["status"] = "stopped_by_operator"
            post_note(sn, chg, "Check Point automation worker: runner stopped at an explicit phase boundary; success bookkeeping was not performed.")
        else:
            for key in ("failed_phase", "failed_playbook", "failed_step", "failed_log", "failed_run_dir"):
                entry.pop(key, None)
            resume = latest_resume_state(
                str(chg.get("number") or chg["sys_id"]),
                newer_than=float(entry.get("last_started_epoch") or 0.0),
            )
            entry.update({k: v for k, v in {
                "failed_phase": resume.get("failed_phase"),
                "failed_playbook": resume.get("failed_playbook"),
                "failed_step": resume.get("failed_step"),
                "failed_log": resume.get("failed_log"),
                "failed_run_dir": resume.get("run_dir"),
            }.items() if v})
            task = create_engineer_remediation_task(sn, chg, impl, entry, rc)
            entry["status"] = "waiting_engineer_remediation"
            post_note(sn, chg, f"Check Point automation worker: runner failed with rc={rc}; engineer remediation CTASK {task.get('number')} created/waiting before retry.")
        save_state(Path(args.state_file), state)
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=os.environ.get("SN_INSTANCE", ""))
    ap.add_argument("--sn-username", default=os.environ.get("SN_USERNAME", ""))
    ap.add_argument("--sn-password", default=os.environ.get("SN_PASSWORD", ""))
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--poll-interval", type=int, default=60)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--simulate-gates", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    missing = [name for name in ("instance", "sn_username", "sn_password") if not getattr(args, name)]
    if missing:
        raise SystemExit(f"ERROR: missing required ServiceNow settings: {', '.join(missing)}")
    if not os.environ.get("CP_PASSWORD") or not os.environ.get("CP_EXPERT_PASSWORD"):
        raise SystemExit("ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD must be set in the worker environment")

    lock_path = Path(args.state_file).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sn = ServiceNowClient(args.instance, args.sn_username, args.sn_password)
        while True:
            state = load_state(Path(args.state_file))
            did_work = process_once(args, sn, state)
            if args.once:
                return 0 if did_work else 2
            time.sleep(max(args.poll_interval, 5))


if __name__ == "__main__":
    raise SystemExit(main())
