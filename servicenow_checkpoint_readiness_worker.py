#!/usr/bin/env python3
"""Automated ServiceNow readiness validator for Check Point firewall requests.

This worker owns the pre-CHG automated validation SCTASK.  It validates the RITM
request and resources without installing/removing packages.  On success it writes
u_checkpoint_readiness_status=ready/source=automated on the SCTASK and RITM,
then closes the automated SCTASK.  On failure it writes failed readiness fields,
closes the automated SCTASK, and creates a manual Firewall Deploy remediation
SCTASK; the CHG is created only when readiness fields explicitly say ready.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

from servicenow_checkpoint_runner import (
    DEFAULT_ANSIBLE_PLAYBOOK,
    DEFAULT_SUPPORT_CAPTURE,
    ROOT,
    ServiceNowClient,
    apply_dependency_rows,
    build_base_plan,
    choose_attachment,
    discover_targets,
    package_steps_from_rows,
    parse_key_values,
    parse_tabular_file,
    runner_vars,
    run_playbook,
)

READY_MARKER = "[CHECKPOINT_READINESS_READY]"
FAILED_MARKER = "[CHECKPOINT_READINESS_FAILED]"
AUTO_PREFIX = "Automated Check Point readiness validation"
MANUAL_PREFIX = "Firewall Deploy manual readiness remediation"
FIREWALL_DEPLOY_GROUP = os.environ.get("SN_FIREWALL_DEPLOY_GROUP_SYS_ID", "")
RUNS_DIR = ROOT / "runs" / "readiness"
DEFAULT_LOCK_FILE = RUNS_DIR / "readiness-worker.lock"
ATTACHMENT_WAIT_MARKER = "[CHECKPOINT_ATTACHMENT_WAIT]"
ATTACHMENT_VARIABLE_NAMES = {"cpuse_package_upload", "cpuse_dependency_upload"}


class AttachmentPending(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ref_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def display(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "")
    return str(value or "")


def split_values(raw: str) -> list[str]:
    import re
    return [x.strip() for x in re.split(r"[,;\n\r\t ]+", raw or "") if x.strip()]


def task_open(task: dict[str, Any]) -> bool:
    return display(task.get("state")).lower() not in {"3", "4", "7", "closed complete", "closed incomplete", "closed skipped", "canceled"}


def query_auto_tasks(sn: ServiceNowClient, limit: int) -> list[dict[str, Any]]:
    return [
        t for t in sn.results(
            "sc_task",
            f"short_descriptionSTARTSWITH{AUTO_PREFIX}^stateNOT IN3,4,7",
            "sys_id,number,short_description,state,request_item,assignment_group,assigned_to,work_notes,close_notes,sys_created_on",
            limit,
        )
        if task_open(t)
    ]


def attachment_rows(sn: ServiceNowClient, ritm_id: str) -> list[dict[str, Any]]:
    attachments = sn.results("sys_attachment", f"table_sys_id={ritm_id}", "sys_id,file_name,content_type,size_bytes", 100)
    seen = {str(row.get("sys_id") or "") for row in attachments}

    mappings = sn.results("sc_item_option_mtom", f"request_item={ritm_id}", "sc_item_option", 100)
    option_ids = [ref_value(row.get("sc_item_option")) for row in mappings if ref_value(row.get("sc_item_option"))]
    if not option_ids:
        return attachments
    options = sn.results("sc_item_option", "sys_idIN" + ",".join(option_ids), "sys_id,value,item_option_new", len(option_ids))
    variable_ids = [ref_value(row.get("item_option_new")) for row in options if ref_value(row.get("item_option_new"))]
    variables = sn.results("item_option_new", "sys_idIN" + ",".join(variable_ids), "sys_id,name", len(variable_ids)) if variable_ids else []
    variable_names = {str(row.get("sys_id") or ""): str(row.get("name") or "") for row in variables}
    for option in options:
        if variable_names.get(ref_value(option.get("item_option_new"))) not in ATTACHMENT_VARIABLE_NAMES:
            continue
        attachment_id = str(option.get("value") or "").strip()
        if not attachment_id or attachment_id in seen:
            continue
        attachment = sn.first("sys_attachment", f"sys_id={attachment_id}", "sys_id,file_name,content_type,size_bytes")
        if attachment:
            attachments.append(attachment)
            seen.add(attachment_id)
    return attachments


def download_attachments(sn: ServiceNowClient, attachments: list[dict[str, Any]], run_dir: Path) -> None:
    out_dir = run_dir / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)
    for att in attachments:
        out = out_dir / str(att.get("file_name") or att["sys_id"])
        out.write_bytes(sn.attachment_bytes(att["sys_id"]))
        att["local_path"] = str(out)


def namespace_from_ritm(ritm: dict[str, Any], values: dict[str, str], chg_number: str, args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        chg_number=chg_number,
        change_number=chg_number,
        chg_sys_id="",
        instance="",
        sn_username="",
        sn_password="",
        package_file=None,
        dependency_file=None,
        target_ips=values.get("target_ips") or values.get("firewall_ips") or values.get("firewall_ips_target_ips") or "",
        mds_host=values.get("mds_host") or values.get("mds_host_ip") or "",
        cma_name="",
        cma_ip="",
        cluster_name="",
        policy_package="",
        activity_type=values.get("activity_type") or "Software Patch Activity",
        environment=values.get("environment") or "production",
        current_version=values.get("current_version") or "",
        target_version=values.get("target_version") or values.get("current_version") or "",
        target_take="",
        icap_mode=(values.get("icap_mode") or "disabled").lower(),
        package_source_dir="/var/log/tmp",
        support_capture_script=DEFAULT_SUPPORT_CAPTURE,
        preserve_original_active=values.get("preserve_original_active") or "true",
        tester_gate=values.get("tester_gate") or "true",
        ansible_playbook=Path(args.ansible_playbook),
        skip_discovery=False,
        simulate_gates=False,
        lab_override_governance=False,
        start_at="",
        stop_after="",
        dry_run=False,
    )


def write_vars(plan: dict[str, Any], plan_path: Path, vars_path: Path, phase: str, step: str = "") -> None:
    vars_path.write_text(json.dumps(runner_vars(plan, plan_path, phase, step), indent=2) + "\n")


def run_readiness_playbook(ansible: Path, playbook: str, vars_path: Path, env: dict[str, str], log_dir: Path, phase: str, step: str = "", extra: dict[str, Any] | None = None) -> int:
    log_path = log_dir / f"{phase}_{step or 'none'}_{playbook}.log"
    return run_playbook(ansible, playbook, vars_path, env, log_path, extra or {})


def validate_request(sn: ServiceNowClient, task: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str, Path]:
    ritm_id = ref_value(task.get("request_item"))
    if not ritm_id:
        raise RuntimeError("automated SCTASK has no parent RITM")
    ritm = sn.table("GET", f"sc_req_item/{ritm_id}").get("result", {})
    if not ritm:
        raise RuntimeError(f"RITM not found: {ritm_id}")

    chg_number = f"READINESS_{ritm.get('number') or task.get('number')}"
    run_dir = RUNS_DIR / f"{task.get('number')}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir = run_dir / "logs"
    log_dir.mkdir()

    values = parse_key_values((ritm.get("description") or "") + "\n" + (task.get("description") or ""))
    attachments = attachment_rows(sn, ritm_id)
    download_attachments(sn, attachments, run_dir)
    ctx = {"attachments": attachments}
    package_file = choose_attachment(ctx, "package")
    dependency_file = choose_attachment(ctx, "dependency")
    if not package_file or not package_file.exists():
        raise AttachmentPending("CPUSE package CSV/XLSX attachment is not yet available from the RITM or attachment variable")

    rows = parse_tabular_file(package_file)
    steps = package_steps_from_rows(rows, "/var/log/tmp")
    if dependency_file and dependency_file.exists():
        apply_dependency_rows(steps, parse_tabular_file(dependency_file))
    if not steps:
        raise RuntimeError("no package steps parsed from CPUSE package attachment")

    ns = namespace_from_ritm(ritm, values, chg_number, args)
    target_ips = split_values(ns.target_ips)
    if not target_ips:
        raise RuntimeError("no target firewall IPs were found on the RITM")
    if not ns.mds_host:
        raise RuntimeError("no MDS host/IP was found on the RITM")

    env = os.environ.copy()
    if not env.get("CP_PASSWORD") or not env.get("CP_EXPERT_PASSWORD"):
        raise RuntimeError("CP_PASSWORD and CP_EXPERT_PASSWORD are required for automated readiness")
    ansible = Path(args.ansible_playbook)
    if not ansible.exists():
        raise RuntimeError(f"ansible-playbook not found: {ansible}")

    (run_dir / "readiness_input.json").write_text(json.dumps({"ritm": ritm.get("number"), "task": task.get("number"), "values": values, "attachments": [a.get("file_name") for a in attachments]}, indent=2) + "\n")

    discovered = discover_targets(ns, ansible, env, run_dir, values)
    plan = build_base_plan(ns, steps, discovered, values)
    plan_path = run_dir / f"{chg_number}_activity_plan.json"
    vars_path = run_dir / f"{chg_number}_vars.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")

    checks: list[tuple[str, str, str, dict[str, Any]]] = [
        ("validate-plan", "01_validate_activity_plan.yml", "", {}),
        ("init", "00_precheck.yml", "", {}),
        ("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}),
        ("stage-files", "06_validate_mds_package.yml", "", {}),
    ]
    for step in plan.get("package_steps", []):
        checks.append(("first-member", "08_validate_package_prerequisites.yml", step["name"], {}))

    for phase, playbook, step, extra in checks:
        write_vars(plan, plan_path, vars_path, phase, step)
        rc = run_readiness_playbook(ansible, playbook, vars_path, env, log_dir, phase, step, extra)
        if rc != 0:
            raise RuntimeError(
                f"readiness phase {phase} ({playbook}{' step '+step if step else ''}) failed with rc={rc}; "
                f"evidence reference {run_dir.name}"
            )

    summary = {
        "status": "ready",
        "task": task.get("number"),
        "ritm": ritm.get("number"),
        "run_dir": str(run_dir),
        "discovered": discovered,
        "package_steps": steps,
        "finished_at": utc_now(),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return True, f"Automated readiness passed. Resolved domain={discovered.get('domain')}, cluster={discovered.get('cluster_name')}, policy={discovered.get('policy_package')}. Evidence reference: {run_dir.name}", run_dir


def manual_task_exists(sn: ServiceNowClient, ritm_id: str) -> bool:
    rows = sn.results("sc_task", f"request_item={ritm_id}^short_descriptionSTARTSWITH{MANUAL_PREFIX}^stateNOT IN3,4,7", "sys_id,number", 10)
    return bool(rows)


def create_manual_task(sn: ServiceNowClient, task: dict[str, Any], failure: str, run_dir: Path | None) -> None:
    ritm_id = ref_value(task.get("request_item"))
    if manual_task_exists(sn, ritm_id):
        return
    evidence_reference = run_dir.name if run_dir else "-"
    desc = (
        "Automated Check Point readiness validation failed. Firewall Deploy must review/remediate the issue, "
        "then close this SCTASK Complete only when the request is ready for CHG creation.\n\n"
        f"Automated task: {task.get('number')}\n"
        f"Failure: {failure}\n"
        f"Evidence reference: {evidence_reference}\n\n"
        "When ready, close this task Complete. Add remediation details in close notes."
    )
    body = {
        "request_item": ritm_id,
        "assignment_group": FIREWALL_DEPLOY_GROUP,
        "assigned_to": "",
        "short_description": f"{MANUAL_PREFIX} - {task.get('number')}",
        "description": desc,
        "u_checkpoint_readiness_status": "pending",
        "u_checkpoint_readiness_source": "manual",
        "u_checkpoint_readiness_summary": f"Manual remediation required after automated readiness failure: {failure}",
        "u_checkpoint_readiness_evidence": evidence_reference if run_dir else "",
        "work_notes": f"{FAILED_MARKER} Created after automated readiness failure on {task.get('number')}. Failure: {failure}",
    }
    sn.table("POST", "sc_task", body=body)


def close_task(sn: ServiceNowClient, task: dict[str, Any], *, success: bool, message: str) -> None:
    ritm_id = ref_value(task.get("request_item"))
    if success:
        body = {
            "state": "3",
            "u_checkpoint_readiness_status": "ready",
            "u_checkpoint_readiness_source": "automated",
            "u_checkpoint_readiness_summary": message,
            "u_checkpoint_readiness_evidence": message.split("Evidence reference: ", 1)[1] if "Evidence reference: " in message else "",
            "work_notes": f"{READY_MARKER} {message}",
            "close_notes": f"{READY_MARKER} Automated readiness passed. {message}",
        }
        if ritm_id:
            sn.patch("sc_req_item", ritm_id, {
                "u_checkpoint_readiness_status": "ready",
                "u_checkpoint_readiness_source": "automated",
                "u_checkpoint_readiness_summary": message,
                "u_checkpoint_readiness_evidence": body["u_checkpoint_readiness_evidence"],
            })
    else:
        body = {
            "state": "4",
            "u_checkpoint_readiness_status": "failed",
            "u_checkpoint_readiness_source": "automated",
            "u_checkpoint_readiness_summary": message,
            "u_checkpoint_readiness_evidence": "",
            "work_notes": f"{FAILED_MARKER} {message}",
            "close_notes": f"{FAILED_MARKER} Automated readiness failed. Manual Firewall Deploy validation/remediation SCTASK created. {message}",
        }
        if ritm_id:
            sn.patch("sc_req_item", ritm_id, {
                "u_checkpoint_readiness_status": "failed",
                "u_checkpoint_readiness_source": "automated",
                "u_checkpoint_readiness_summary": message,
                "u_checkpoint_readiness_evidence": "",
            })
    sn.patch("sc_task", task["sys_id"], body)


def process_once(args: argparse.Namespace, sn: ServiceNowClient) -> bool:
    tasks = query_auto_tasks(sn, args.limit)
    if not tasks:
        return False
    for task in tasks:
        if args.dry_run:
            print(f"DRY RUN: would validate {task.get('number')} {task.get('short_description')}")
            return True
        run_dir: Path | None = None
        try:
            ok, message, run_dir = validate_request(sn, task, args)
            close_task(sn, task, success=ok, message=message)
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, AttachmentPending):
                created_raw = str(task.get("sys_created_on") or "")
                try:
                    created = dt.datetime.strptime(created_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                    age_seconds = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
                except ValueError:
                    age_seconds = args.attachment_grace + 1
                if age_seconds <= args.attachment_grace:
                    if ATTACHMENT_WAIT_MARKER not in str(task.get("work_notes") or ""):
                        sn.post_work_note("sc_task", task["sys_id"], f"{ATTACHMENT_WAIT_MARKER} Waiting up to {args.attachment_grace} seconds for the CPUSE attachment transaction to complete.")
                    return True
            try:
                create_manual_task(sn, task, message, run_dir)
            finally:
                close_task(sn, task, success=False, message=message)
        return True
    return False


def acquire_worker_lock(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError(f"readiness worker is already running: {path}") from exc
    return lock


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=os.environ.get("SN_INSTANCE", ""))
    ap.add_argument("--sn-username", default=os.environ.get("SN_USERNAME", ""))
    ap.add_argument("--sn-password", default=os.environ.get("SN_PASSWORD", ""))
    ap.add_argument("--ansible-playbook", default=str(DEFAULT_ANSIBLE_PLAYBOOK))
    ap.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    ap.add_argument("--poll-interval", type=int, default=60)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--attachment-grace", type=int, default=180)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.instance or not args.sn_username or not args.sn_password:
        raise SystemExit("ERROR: SN_INSTANCE, SN_USERNAME, and SN_PASSWORD are required")
    if not FIREWALL_DEPLOY_GROUP:
        raise SystemExit("ERROR: SN_FIREWALL_DEPLOY_GROUP_SYS_ID is required")
    sn = ServiceNowClient(args.instance, args.sn_username, args.sn_password)
    try:
        lock = acquire_worker_lock(Path(args.lock_file))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    with lock:
        while True:
            did = process_once(args, sn)
            if args.once:
                return 0 if did else 2
            time.sleep(max(args.poll_interval, 5))


if __name__ == "__main__":
    raise SystemExit(main())
