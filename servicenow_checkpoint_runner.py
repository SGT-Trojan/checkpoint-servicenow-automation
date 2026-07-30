#!/usr/bin/env python3
"""ServiceNow-first Check Point firewall maintenance runner.

This runner is independent of the legacy Flask prototype. It uses
ServiceNow as the system of record, generates the same activity-plan contract the
Ansible/CDT playbooks expect, runs the playbooks in guarded phases, and writes
phase status back to ServiceNow CTASK/CHG records.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent
ANSIBLE_DIR = ROOT / "ansible"
RUNS_DIR = ROOT / "runs"
ALLOWED_TICKET_ATTACHMENT_SUFFIXES = {".csv", ".xlsx"}
SERVICENOW_SYS_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


def default_ansible_playbook() -> Path:
    configured = os.environ.get("CHECKPOINT_ANSIBLE_PLAYBOOK", "").strip()
    if configured:
        return Path(configured).expanduser()
    bundled = ROOT / ".venv" / "bin" / "ansible-playbook"
    if bundled.exists():
        return bundled
    sibling = ROOT.parent / ".venv-ansible" / "bin" / "ansible-playbook"
    if sibling.exists():
        return sibling
    discovered = shutil.which("ansible-playbook")
    return Path(discovered) if discovered else bundled


DEFAULT_ANSIBLE_PLAYBOOK = default_ansible_playbook()
DEFAULT_SUPPORT_CAPTURE = str(ANSIBLE_DIR / "scripts" / "gateway_support_commands.example.sh")
AUTOMATION_MARKER = "[CHECKPOINT_AUTOMATION]"
IMPLEMENT_STATE_VALUES = {"-1", "implement", "Implement"}
APPROVED_VALUES = {"approved", "Approved"}
READINESS_CLOSED_STATES = {"3", "4", "7", "closed complete", "closed_complete", "closed skipped", "closed_skipped", "closed incomplete", "closed_incomplete", "canceled", "cancelled"}
CONTROLLED_STOP_RC = 21
CLOSED_COMPLETE_STATES = {"3", "closed complete", "closed_complete"}
IMPLEMENTATION_TASK_PREFIX = "Implementation - Check Point firewall automation workflow"
ATTACHMENT_VARIABLE_NAMES = {"cpuse_package_upload", "cpuse_dependency_upload"}
CHANGE_FIELDS = "sys_id,number,parent,short_description,description,work_notes,state,approval,cmdb_ci,implementation_plan,backout_plan,test_plan"

ACTIVITY_MAP = {
    "version_upgrade_activity": "Major Version Upgrade",
    "Version Upgrade Activity": "Major Version Upgrade",
    "software_patch_activity": "Software Patch Activity",
    "Software Patch Activity": "Software Patch Activity",
    "deployment_agent_install": "Deployment Agent Update",
    "Deployment Agent Install": "Deployment Agent Update",
}

PHASE_LABELS = {
    "discover-targets": "Discover Check Point targets",
    "validate-plan": "Validate activity plan",
    "init": "Precheck gateway and cluster health",
    "deployment-agent-readiness": "Validate Deployment Agent readiness",
    "cluster-state-capture": "Capture original cluster state",
    "baseline-capture": "Run baseline support capture",
    "stage-files": "Validate MDS package and air-gap staging",
    "mixed-version-policy-gate": "Validate mixed-version policy compatibility",
    "mvc-on": "Enable Multi-Version Cluster mode",
    "first-member": "Execute package step on first member",
    "failover-to-first": "Fail over to updated first member",
    "approve-testers": "Tester validation gate",
    "second-member": "Execute package step on second member",
    "final-policy-install": "Install final target-version policy",
    "mvc-off": "Disable Multi-Version Cluster mode",
    "restore-original-active": "Restore original active member",
    "final-support-capture": "Run final support capture",
    "support-diff": "Generate support capture diff",
    "postcheck": "Run final postcheck",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "")
    return value.strip("_") or "step"


def split_values(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;\n\r\t]+", raw or "") if x.strip()]


def resolve_icap_mode(cli_value: str | None, catalog_value: str | None) -> str:
    mode = (cli_value or catalog_value or "disabled").strip().lower()
    if mode not in {"required", "optional", "disabled"}:
        raise ValueError(f"invalid ICAP mode: {mode}")
    return mode


def resolve_deployment_backend(value: str | None) -> str:
    backend = (value or "cdt").strip().lower().replace("-", "_")
    aliases = {"management_api": "api", "web_api": "api"}
    backend = aliases.get(backend, backend)
    if backend not in {"cdt", "api"}:
        raise ValueError(f"invalid deployment backend: {backend}")
    return backend


class ServiceNowClient:
    def __init__(self, instance: str, username: str, password: str):
        self.base = instance.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def table(self, method: str, table_path: str, query: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> Any:
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        url = f"{self.base}/api/now/table/{table_path}{qs}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self.headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode())

    def results(self, table: str, query: str, fields: str = "", limit: int = 100) -> list[dict[str, Any]]:
        params = {"sysparm_query": query, "sysparm_limit": str(limit)}
        if fields:
            params["sysparm_fields"] = fields
        return self.table("GET", table, params).get("result", [])

    def first(self, table: str, query: str, fields: str = "") -> dict[str, Any] | None:
        rows = self.results(table, query, fields, 1)
        return rows[0] if rows else None

    def patch(self, table: str, sys_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.table("PATCH", f"{table}/{sys_id}", body=body).get("result", {})

    def post_work_note(self, table: str, sys_id: str, note: str) -> None:
        self.patch(table, sys_id, {"work_notes": note})

    def attachment_bytes(self, sys_id: str) -> bytes:
        url = f"{self.base}/api/now/attachment/{sys_id}/file"
        req = urllib.request.Request(url)
        for k, v in self.headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    def upload_attachment(self, table: str, sys_id: str, filename: str, data: bytes, content_type: str = "text/plain") -> None:
        qs = urllib.parse.urlencode({"table_name": table, "table_sys_id": sys_id, "file_name": filename})
        url = f"{self.base}/api/now/attachment/file?{qs}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", self.headers["Authorization"])
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()


def ref_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def display_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "")
    return str(value or "")


def ritm_attachment_rows(sn: ServiceNowClient, ritm_id: str) -> list[dict[str, Any]]:
    """Return physical RITM attachments plus files referenced by attachment variables."""
    attachments = sn.results(
        "sys_attachment",
        f"table_sys_id={ritm_id}",
        "sys_id,file_name,content_type,size_bytes",
        100,
    )
    seen = {str(row.get("sys_id") or "") for row in attachments}
    mappings = sn.results("sc_item_option_mtom", f"request_item={ritm_id}", "sc_item_option", 100)
    option_ids = [ref_value(row.get("sc_item_option")) for row in mappings if ref_value(row.get("sc_item_option"))]
    if not option_ids:
        return attachments

    options = sn.results(
        "sc_item_option",
        "sys_idIN" + ",".join(option_ids),
        "sys_id,value,item_option_new",
        len(option_ids),
    )
    variable_ids = [ref_value(row.get("item_option_new")) for row in options if ref_value(row.get("item_option_new"))]
    variables = (
        sn.results("item_option_new", "sys_idIN" + ",".join(variable_ids), "sys_id,name", len(variable_ids))
        if variable_ids
        else []
    )
    variable_names = {str(row.get("sys_id") or ""): str(row.get("name") or "") for row in variables}
    for option in options:
        if variable_names.get(ref_value(option.get("item_option_new"))) not in ATTACHMENT_VARIABLE_NAMES:
            continue
        attachment_id = str(option.get("value") or "").strip()
        if not attachment_id or attachment_id in seen:
            continue
        attachment = sn.first(
            "sys_attachment",
            f"sys_id={attachment_id}",
            "sys_id,file_name,content_type,size_bytes",
        )
        if attachment:
            attachments.append(attachment)
            seen.add(attachment_id)
    return attachments


def parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_").replace("/", "_")
        values[key] = value.strip()
    return values


def has_automation_marker(record: dict[str, Any]) -> bool:
    text = "\n".join(
        str(record.get(field) or "")
        for field in (
            "short_description",
            "description",
            "implementation_plan",
            "backout_plan",
            "test_plan",
            "work_notes",
        )
    )
    return AUTOMATION_MARKER in text


def readiness_tasks(sn: ServiceNowClient, ritm_id: str) -> list[dict[str, Any]]:
    if not ritm_id:
        return []
    tasks = sn.results(
        "sc_task",
        f"request_item={ritm_id}",
        "sys_id,number,short_description,state,assignment_group,assigned_to,"
        "u_checkpoint_readiness_status,u_checkpoint_readiness_source",
        100,
    )
    out = []
    for task in tasks:
        short = str(task.get("short_description") or "").lower()
        if "readiness" in short or "firewall deploy" in short:
            out.append(task)
    return out


def implementation_task(sn: ServiceNowClient, chg_sys_id: str) -> dict[str, Any] | None:
    tasks = sn.results(
        "change_task",
        f"change_request={chg_sys_id}",
        "sys_id,number,short_description,state,assignment_group,assigned_to",
        100,
    )
    matches = []
    for task in tasks:
        short = str(task.get("short_description") or "")
        if short.startswith(IMPLEMENTATION_TASK_PREFIX) or AUTOMATION_MARKER in short:
            matches.append(task)
    if len(matches) > 1:
        details = ", ".join(f"{t.get('number')}={t.get('short_description')!r}" for t in matches)
        raise SystemExit(f"ERROR: ambiguous implementation CTASKs for governed CHG: {details}")
    return matches[0] if matches else None


def validate_service_now_governance(context: dict[str, Any], *, allow_lab_override: bool = False) -> None:
    chg = context["chg"]
    ritm_id = context.get("ritm_id", "")
    errors: list[str] = []

    if not has_automation_marker(chg):
        errors.append(f"CHG does not contain the {AUTOMATION_MARKER} marker from the governed creation rule")

    state = display_value(chg.get("state"))
    if state not in IMPLEMENT_STATE_VALUES:
        errors.append(f"CHG state must be Implement before execution; current state value is {state!r}")

    approval = display_value(chg.get("approval"))
    if approval not in APPROVED_VALUES:
        errors.append(f"CHG approval must be approved before execution; current approval is {approval!r}")

    readiness = context.get("readiness_tasks") or []
    if not ritm_id:
        errors.append("CHG parent RITM is missing")
    elif not readiness:
        errors.append("no Firewall Deploy readiness SCTASK was found for the parent RITM")
    else:
        open_tasks = []
        ready_tasks = []
        for task in readiness:
            task_state = display_value(task.get("state")).lower()
            readiness_status = str(task.get("u_checkpoint_readiness_status") or "").strip().lower()
            if task_state not in READINESS_CLOSED_STATES:
                open_tasks.append(f"{task.get('number')} state={display_value(task.get('state'))!r}")
            elif task_state in CLOSED_COMPLETE_STATES and readiness_status == "ready":
                ready_tasks.append(task)
        if open_tasks:
            errors.append("readiness SCTASK is not closed: " + "; ".join(open_tasks))
        if not ready_tasks:
            errors.append("no readiness SCTASK is Closed Complete with u_checkpoint_readiness_status=ready")

    if not context.get("implementation_task"):
        errors.append(f"BR-created implementation CTASK was not found using prefix {IMPLEMENTATION_TASK_PREFIX!r}")

    if errors and not allow_lab_override:
        joined = "\n  - ".join(errors)
        raise SystemExit(
            "ERROR: ServiceNow governance gate failed. Refusing to execute firewall automation.\n"
            f"  - {joined}\n"
            "Use --lab-override-governance only for an explicitly controlled lab validation."
        )

    if errors and allow_lab_override:
        print("WARNING: ServiceNow governance gate bypassed by --lab-override-governance:")
        for error in errors:
            print(f"  - {error}")


def resolve_change_record(sn: ServiceNowClient, chg_number: str, chg_sys_id: str = "") -> dict[str, Any]:
    if chg_sys_id:
        chg = sn.table("GET", f"change_request/{chg_sys_id}", {"sysparm_fields": CHANGE_FIELDS}).get("result", {})
        if not chg or not chg.get("sys_id"):
            raise SystemExit(f"ERROR: CHG sys_id not found: {chg_sys_id}")
        if chg_number and str(chg.get("number") or "") != chg_number:
            raise SystemExit(f"ERROR: CHG sys_id {chg_sys_id} is {chg.get('number')}, not requested number {chg_number}")
        return chg

    rows = sn.results("change_request", f"number={chg_number}", CHANGE_FIELDS, 20)
    if not rows:
        raise SystemExit(f"ERROR: CHG not found: {chg_number}")
    if len(rows) == 1:
        return rows[0]

    marked = [row for row in rows if has_automation_marker(row)]
    if len(marked) == 1:
        return marked[0]

    details = ", ".join(f"{row.get('number')}:{row.get('sys_id')} marker={has_automation_marker(row)} state={display_value(row.get('state'))!r}" for row in rows)
    if marked:
        raise SystemExit(f"ERROR: multiple governed CHGs match {chg_number}; rerun with --chg-sys-id. Matches: {details}")
    raise SystemExit(f"ERROR: duplicate CHG number {chg_number} has no unique governed automation record; rerun with --chg-sys-id. Matches: {details}")


def parse_csv_bytes(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for si in root.findall("a:si", ns):
        parts = [t.text or "" for t in si.findall(".//a:t", ns)]
        strings.append("".join(parts))
    return strings


def xlsx_first_sheet_path(zf: zipfile.ZipFile) -> str:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    document_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    first_sheet = workbook.find(f".//{{{spreadsheet_ns}}}sheets/{{{spreadsheet_ns}}}sheet")
    if first_sheet is None:
        raise ValueError("XLSX workbook has no worksheets")
    relationship_id = first_sheet.attrib.get(f"{{{document_rel_ns}}}id", "")
    if not relationship_id:
        raise ValueError("XLSX first worksheet has no relationship id")

    relationships = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relationship = next(
        (
            row
            for row in relationships.findall(f"{{{package_rel_ns}}}Relationship")
            if row.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None:
        raise ValueError(f"XLSX worksheet relationship {relationship_id!r} was not found")
    if relationship.attrib.get("TargetMode", "").lower() == "external":
        raise ValueError("XLSX worksheet relationship must not be external")
    if not relationship.attrib.get("Type", "").endswith("/worksheet"):
        raise ValueError("XLSX first sheet relationship is not a worksheet")

    target = relationship.attrib.get("Target", "").replace("\\", "/").lstrip("/")
    if not target:
        raise ValueError("XLSX worksheet relationship has no target")
    relative = PurePosixPath(target)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe XLSX worksheet target: {target!r}")
    path = relative if relative.parts[:1] == ("xl",) else PurePosixPath("xl") / relative
    return path.as_posix()


def xlsx_column_index(reference: str) -> int:
    match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", reference.upper())
    if not match:
        raise ValueError(f"invalid XLSX cell reference: {reference!r}")
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    index -= 1
    if index >= 16384:
        raise ValueError(f"XLSX cell reference exceeds column XFD: {reference!r}")
    return index


def xlsx_cell_value(cell: ET.Element, shared: list[str], namespace: str) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{namespace}}}t"))
    value = cell.find(f"{{{namespace}}}v")
    raw = value.text or "" if value is not None else ""
    if cell_type == "s":
        if not raw.isdigit() or int(raw) >= len(shared):
            raise ValueError(f"invalid XLSX shared-string index: {raw!r}")
        return shared[int(raw)]
    return raw


def parse_xlsx_bytes(data: bytes) -> list[dict[str, str]]:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared = xlsx_shared_strings(zf)
        root = ET.fromstring(zf.read(xlsx_first_sheet_path(zf)))
    rows: list[dict[int, str]] = []
    for row in root.findall(f".//{{{namespace}}}sheetData/{{{namespace}}}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{{{namespace}}}c"):
            column = xlsx_column_index(cell.attrib.get("r", ""))
            if column in values:
                raise ValueError(f"duplicate XLSX cell column in one row: {cell.attrib.get('r')!r}")
            values[column] = xlsx_cell_value(cell, shared, namespace)
        rows.append(values)
    if not rows:
        return []
    headers = {column: value.strip() for column, value in rows[0].items() if value.strip()}
    out = []
    for row in rows[1:]:
        item = {header: row.get(column, "").strip() for column, header in headers.items()}
        if any(item.values()):
            out.append(item)
    return out


def parse_tabular_file(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    if path.suffix.lower() == ".xlsx":
        return parse_xlsx_bytes(data)
    return parse_csv_bytes(data)


def infer_package_type(name: str, explicit: str = "") -> str:
    if explicit:
        value = explicit.strip().lower().replace(" ", "_")
        if value in {"jhf", "wrapper", "blink", "deployment_agent", "other"}:
            return value
    low = name.lower()
    if "deployment" in low and "agent" in low:
        return "deployment_agent"
    if "blink" in low:
        return "blink"
    if "wrapper" in low or "hotfix" in low and "bundle" not in low:
        return "wrapper"
    if "jumbo" in low or "jhf" in low or re.search(r"\bt\d+\b", low):
        return "jhf"
    return "other"


def normalize_action(value: str) -> str:
    v = (value or "").strip().lower()
    if not v:
        return "install"
    if v in {"install", "installation"}:
        return "install"
    if v in {"remove", "removal", "uninstall", "delete"}:
        return "remove"
    if v in {"upgrade", "update"}:
        return "upgrade"
    raise ValueError(f"unsupported package action {value!r}")


def validated_package_hashes(
    action: str, package_name: str, sha1: str, sha256: str
) -> tuple[str, str]:
    sha1 = sha1.strip().lower()
    sha256 = sha256.strip().lower()
    if sha1 and not re.fullmatch(r"[0-9a-f]{40}", sha1):
        raise ValueError(f"package {package_name!r} has an invalid SHA1 value")
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"package {package_name!r} has an invalid SHA256 value")
    if action in {"install", "upgrade"} and not (sha1 or sha256):
        raise ValueError(
            f"package {package_name!r} requires a published SHA1 or SHA256 checksum for {action}"
        )
    return sha1, sha256


def validated_package_name(value: str) -> str:
    stripped = value.strip()
    if value != stripped or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", value):
        raise ValueError(
            f"package_name {value!r} contains whitespace, path separators, or unsupported characters"
        )
    return value


def validated_package_source_path(value: str) -> str:
    stripped = value.strip()
    parts = value.split("/")
    if (
        value != stripped
        or not value.startswith("/")
        or value.endswith("/")
        or not re.fullmatch(r"/[A-Za-z0-9_.+/-]+", value)
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise ValueError(
            f"source_path {value!r} must be an absolute package path without traversal or unsupported characters"
        )
    return value


def package_steps_from_rows(rows: list[dict[str, str]], package_source_dir: str = "/var/log/tmp") -> list[dict[str, Any]]:
    steps = []
    for i, row in enumerate(rows, 1):
        lower = {k.lower().strip().replace(" ", "_"): v for k, v in row.items()}
        package_name = lower.get("package_name") or lower.get("package") or lower.get("filename") or lower.get("file_name") or ""
        if not package_name:
            continue
        package_name = validated_package_name(package_name)
        order_s = lower.get("sequence_number") or lower.get("sequence") or lower.get("order") or str(i)
        try:
            order = int(float(order_s))
        except ValueError:
            order = i
        action = normalize_action(lower.get("action"))
        package_type = infer_package_type(package_name, lower.get("package_type", ""))
        name_base = re.sub(r"\.(tar|tgz|gz)$", "", Path(package_name).name, flags=re.I)
        step_name = lower.get("step_name") or f"{action}_{slug(name_base)}"
        source_path = lower.get("source_path") or lower.get("path") or f"{package_source_dir.rstrip('/')}/{package_name}"
        source_path = validated_package_source_path(source_path)
        checksum_sha1, checksum_sha256 = validated_package_hashes(
            action,
            package_name,
            lower.get("sha1") or lower.get("checksum_sha1") or "",
            lower.get("sha256") or lower.get("checksum_sha256") or "",
        )
        steps.append({
            "order": order,
            "name": slug(step_name),
            "action": action,
            "package_name": package_name,
            "package_type": package_type,
            "source_path": source_path,
            "dest_path": lower.get("dest_path") or package_source_dir,
            "checksum_sha1": checksum_sha1,
            "checksum_sha256": checksum_sha256,
            "requires_present": [],
            "requires_absent": [],
            "reboot_expected": (lower.get("reboot_expected", "true").lower() not in {"false", "no", "0", "n"}),
            "notes": lower.get("notes") or "",
        })
    return sorted(steps, key=lambda s: s["order"])


def apply_dependency_rows(steps: list[dict[str, Any]], rows: list[dict[str, str]]) -> None:
    present: list[str] = []
    absent: list[str] = []
    for row in rows:
        lower = {k.lower().strip().replace(" ", "_"): v for k, v in row.items()}
        state = (lower.get("expected_state") or lower.get("state") or lower.get("check_type") or "").strip().lower()
        pkg = lower.get("package_name") or lower.get("package") or lower.get("name") or ""
        if not pkg:
            continue
        if state in {"present", "installed", "requires_present", "required"}:
            present.append(pkg)
        elif state in {"not_present", "not present", "absent", "not_installed", "not installed", "requires_absent"}:
            absent.append(pkg)
    for step in steps:
        step["requires_present"] = list(dict.fromkeys(step.get("requires_present", []) + present))
        step["requires_absent"] = list(dict.fromkeys(step.get("requires_absent", []) + absent))


def service_now_context(sn: ServiceNowClient, chg_number: str, chg_sys_id: str = "") -> dict[str, Any]:
    chg = resolve_change_record(sn, chg_number, chg_sys_id)
    ritm_id = ref_value(chg.get("parent"))
    ritm = sn.table("GET", f"sc_req_item/{ritm_id}").get("result", {}) if ritm_id else {}
    values = parse_key_values((ritm.get("description") or "") + "\n" + (chg.get("description") or ""))

    attachments = ritm_attachment_rows(sn, ritm_id) if ritm_id else []
    attachments.extend(
        sn.results(
            "sys_attachment",
            f"table_sys_id={chg['sys_id']}",
            "sys_id,file_name,content_type,size_bytes",
            100,
        )
    )

    impl = implementation_task(sn, chg["sys_id"])
    return {
        "chg": chg,
        "ritm": ritm,
        "ritm_id": ritm_id,
        "values": values,
        "attachments": attachments,
        "readiness_tasks": readiness_tasks(sn, ritm_id),
        "implementation_task": impl,
    }


def attachment_destination(out_dir: Path, attachment: dict[str, Any]) -> Path:
    original_name = str(attachment.get("file_name") or "")
    if (
        not original_name
        or original_name != original_name.strip()
        or original_name != Path(original_name).name
        or "/" in original_name
        or "\\" in original_name
        or any(ord(char) < 32 for char in original_name)
    ):
        raise ValueError(f"unsafe or empty ServiceNow attachment filename: {original_name!r}")

    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_TICKET_ATTACHMENT_SUFFIXES:
        raise ValueError(
            f"unsupported ServiceNow attachment extension {suffix or '<none>'!r}; "
            "only .csv and .xlsx are accepted"
        )

    sys_id = str(attachment.get("sys_id") or "")
    if not SERVICENOW_SYS_ID_RE.fullmatch(sys_id):
        raise ValueError(f"invalid ServiceNow attachment sys_id: {sys_id!r}")

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out_dir.is_symlink():
        raise RuntimeError(f"attachment directory must not be a symlink: {out_dir}")
    root = out_dir.resolve(strict=True)
    destination = root / f"{sys_id.lower()}{suffix}"
    if destination.is_symlink():
        raise RuntimeError(f"attachment destination must not be a symlink: {destination}")
    resolved = destination.resolve(strict=False)
    if resolved.parent != root:
        raise RuntimeError(f"attachment destination escapes storage directory: {destination}")
    return destination


def write_attachment_bytes(destination: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"cannot safely open attachment destination {destination}: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)


def download_context_attachments(sn: ServiceNowClient, context: dict[str, Any], workdir: Path) -> None:
    for att in context.get("attachments", []):
        if att.get("local_path"):
            continue
        out = attachment_destination(workdir / "attachments", att)
        data = sn.attachment_bytes(att["sys_id"])
        write_attachment_bytes(out, data)
        att["local_path"] = str(out)


def choose_attachment(context: dict[str, Any], kind: str) -> Path | None:
    terms = ["cpuse", "package"] if kind == "package" else ["dependency"]
    for att in context.get("attachments", []):
        name = (att.get("file_name") or "").lower()
        if all(t in name for t in terms) and name.endswith((".csv", ".xlsx")):
            return Path(att["local_path"])
    candidates = [
        str(a.get("file_name") or "")
        for a in context.get("attachments", [])
        if (a.get("file_name") or "").lower().endswith((".csv", ".xlsx"))
    ]
    if candidates:
        raise ValueError(
            f"cannot identify {kind} attachment by filename; rename it to include "
            f"{'CPUSE Package' if kind == 'package' else 'Dependency'}: {', '.join(candidates)}"
        )
    return None


def build_base_plan(args: argparse.Namespace, steps: list[dict[str, Any]], discovered: dict[str, Any] | None = None, values: dict[str, str] | None = None) -> dict[str, Any]:
    values = values or {}
    activity_in = args.activity_type or values.get("activity_type") or "Software Patch Activity"
    activity = ACTIVITY_MAP.get(activity_in, activity_in)
    target_ips = split_values(args.target_ips or values.get("target_ips") or values.get("firewall_ips"))
    mds_host = args.mds_host or values.get("mds_host") or values.get("mds_host_ip")
    current_version = args.current_version or values.get("current_version") or ""
    target_version = args.target_version or values.get("target_version") or current_version
    icap_mode = resolve_icap_mode(args.icap_mode, values.get("icap_mode"))
    deployment_backend = resolve_deployment_backend(getattr(args, "deployment_backend", None))
    preserve = str(args.preserve_original_active if args.preserve_original_active is not None else values.get("preserve_original_active", "true")).lower() not in {"false", "0", "no", "n"}
    tester = str(args.tester_gate if args.tester_gate is not None else values.get("tester_gate", "true")).lower() not in {"false", "0", "no", "n"}
    discovered = discovered or {}
    members = discovered.get("members") or []
    package_source_dir = args.package_source_dir or "/var/log/tmp"
    cma_name = args.cma_name or discovered.get("cma_name") or discovered.get("domain") or values.get("cma_name") or ""
    domain = discovered.get("domain") or values.get("domain") or ""
    cma_ip = args.cma_ip or discovered.get("cma_ip") or values.get("cma_ip") or ""
    cluster_mode = discovered.get("cluster_mode") or ("cluster" if len(members) != 1 else "standalone")
    target_take = args.target_take or infer_target_take(steps)
    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "change": {"number": args.chg_number or args.change_number or "MANUAL", "activity_type": activity, "state": "Implement", "environment": args.environment or values.get("environment") or "lab"},
        "checkpoint": {
            "current_version": current_version,
            "target_version": target_version,
            "target_take": target_take,
            "cluster_name": args.cluster_name or discovered.get("cluster_name") or values.get("cluster_name") or "",
            "cluster_mode": cluster_mode,
            "mds_host": mds_host,
            "cma_name": cma_name,
            "domain": domain,
            "cma_ip": cma_ip,
            "target_ips": target_ips,
            "policy_package": args.policy_package or discovered.get("policy_package") or values.get("policy_package") or "",
            "members": normalize_members(members, target_ips),
            "preserve_original_active": preserve,
            "original_active_member": "",
            "require_one_active_member": cluster_mode != "standalone",
            "icap_mode": icap_mode,
        },
        "execution": {
            "method": (
                "Direct CPUSE/Clish"
                if activity == "Deployment Agent Update"
                else "Management Web API Central Deployment"
                if deployment_backend == "api"
                else "CDT (Central Deployment Tool)"
            ),
            "deployment_backend": "direct" if activity == "Deployment Agent Update" else deployment_backend,
            "staging_method": "cprid_from_mds",
            "package_source_dir": package_source_dir,
            "maintenance_plan": "Generated by ServiceNow-first runner",
            "playbook": "",
            "tester_pause": tester,
            "support_capture_script": args.support_capture_script or DEFAULT_SUPPORT_CAPTURE,
        },
        "package_steps": steps,
        "workflow_gates": [
            {"name": "tester_validation_after_first_member", "enabled": tester, "after_phase": "failover-to-first", "decision_source": "servicenow_ctask_or_simulated"},
            {"name": "restore_original_active", "enabled": preserve, "after_phase": "second-member", "decision_source": "automation"},
        ],
        "evidence": {"support_capture_pre": True, "support_capture_after_member_complete": True, "support_capture_final": True, "diff_required": True, "requirements": "ServiceNow-first workflow evidence"},
    }


def normalize_members(members: list[dict[str, Any]], target_ips: list[str]) -> list[dict[str, Any]]:
    out = []
    if members:
        for i, m in enumerate(members, 1):
            ip = m.get("ip") or m.get("management_ip") or (target_ips[i-1] if i-1 < len(target_ips) else "")
            out.append({"slot": m.get("slot") or f"member_{chr(96+i)}", "hostname": m.get("hostname") or m.get("name") or f"member-{i}", "ip": ip, "management_ip": m.get("management_ip") or ip, "access_ip": m.get("access_ip") or ip})
    else:
        for i, ip in enumerate(target_ips[:2], 1):
            out.append({"slot": f"member_{chr(96+i)}", "hostname": f"member-{i}", "ip": ip, "management_ip": ip, "access_ip": ip})
    return out


def infer_target_take(steps: list[dict[str, Any]]) -> str:
    for step in steps:
        text = " ".join(str(step.get(k, "")) for k in ("package_name", "source_path", "name"))
        # Blink filenames can contain both the OS build (for example T777) and
        # the bundled JHF take (JHF_T60). Prefer an explicitly identified JHF
        # take so postcheck compares CPUSE's JHF take rather than the OS build.
        m = re.search(r"(?:Take\s*|JHF[_\s-]*T)(\d+)", text, re.I)
        if m:
            return m.group(1)
        if "blink" not in text.lower():
            m = re.search(r"_T(\d+)", text, re.I)
            if m:
                return m.group(1)
    return ""


def runner_vars(plan: dict[str, Any], plan_path: Path, phase: str = "", step: str = "") -> dict[str, Any]:
    cp = plan["checkpoint"]
    members = cp.get("members") or []
    a = members[0] if len(members) > 0 else {}
    b = members[1] if len(members) > 1 else a
    return {
        "chg_number": plan["change"]["number"],
        "activity_type": plan["change"]["activity_type"],
        "activity_plan_file": str(plan_path),
        "activity_plan": plan,
        "change_id": 0,
        "phase": phase,
        "step": step,
        "cluster_mode": cp.get("cluster_mode", "cluster"),
        "cluster_name": cp.get("cluster_name", ""),
        "mds_host": cp.get("mds_host", ""),
        "cma_name": cp.get("cma_name", ""),
        "domain": cp.get("domain", ""),
        "cma_ip": cp.get("cma_ip", ""),
        "policy_package": cp.get("policy_package", ""),
        "target_ips": "\n".join(cp.get("target_ips") or []),
        "member_a_hostname": a.get("hostname", ""),
        "member_a_ip": a.get("access_ip") or a.get("ip", ""),
        "member_b_hostname": b.get("hostname", ""),
        "member_b_ip": b.get("access_ip") or b.get("ip", ""),
        "target_version": cp.get("target_version", ""),
        "target_take": cp.get("target_take", ""),
        "icap_mode": cp.get("icap_mode", "disabled"),
        "execution_method": plan["execution"].get("method", ""),
        "staging_method": plan["execution"].get("staging_method", "cprid_from_mds"),
        "package_source_dir": plan["execution"].get("package_source_dir", "/var/log/tmp"),
        "preserve_original_active": cp.get("preserve_original_active", True),
        "tester_pause": plan["execution"].get("tester_pause", True),
        "checkpoint_execute_upgrade": True,
        "checkpoint_execute_direct": True,
        "package_stage_confirmed": False,
    }


def run_playbook(ansible: Path, playbook: str, vars_path: Path, env: dict[str, str], log_path: Path, extra: dict[str, Any] | None = None) -> int:
    cmd = [str(ansible), str(ANSIBLE_DIR / "playbooks" / playbook), "-i", str(ANSIBLE_DIR / "inventory" / "hosts.yml"), "--extra-vars", f"@{vars_path}"]
    if extra:
        for k, v in extra.items():
            cmd.extend(["--extra-vars", json.dumps({k: v})])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(ANSIBLE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
        return proc.wait()


def post_phase(sn: ServiceNowClient | None, context: dict[str, Any] | None, phase: str, status: str, detail: str, attachment: Path | None = None) -> None:
    line = f"Check Point automation phase {status}: {PHASE_LABELS.get(phase, phase)}\n{detail}"
    if not sn or not context:
        return
    impl = context.get("implementation_task")
    chg = context.get("chg")
    # Post one authoritative note to the CHG. The ServiceNow mirror BR copies CHG
    # automation notes to the implementation CTASK, avoiding duplicate CTASK entries.
    if chg:
        sn.post_work_note("change_request", chg["sys_id"], line)
    elif impl:
        sn.post_work_note("change_task", impl["sys_id"], line)
    if impl and attachment and attachment.exists():
        sn.upload_attachment("change_task", impl["sys_id"], attachment.name, attachment.read_bytes())


def discover_targets(args: argparse.Namespace, ansible: Path, env: dict[str, str], run_dir: Path, values: dict[str, str]) -> dict[str, Any]:
    temp_plan = build_base_plan(args, [], None, values)
    temp_path = run_dir / "initial_discovery_plan.json"
    temp_path.write_text(json.dumps(temp_plan, indent=2) + "\n")
    vars_data = runner_vars(temp_plan, temp_path, "discover-targets", "")
    vars_path = run_dir / "discover_vars.json"
    vars_path.write_text(json.dumps(vars_data, indent=2) + "\n")
    rc = run_playbook(ansible, "02_discover_targets.yml", vars_path, env, run_dir / "logs" / "02_discover_targets.log")
    if rc != 0:
        raise RuntimeError("target discovery failed")
    report = ANSIBLE_DIR / "reports" / f"02_discover_targets_{temp_plan['change']['number']}.json"
    if not report.exists():
        raise RuntimeError(f"discovery report not found: {report}")
    return json.loads(report.read_text())


def workflow_steps(plan: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    steps: list[tuple[str, str, str, dict[str, Any]]] = []
    package_steps = plan["package_steps"]
    has_install = any(s["action"] in {"install", "upgrade"} for s in package_steps)
    is_da_activity = (
        plan.get("change", {}).get("activity_type") == "Deployment Agent Update"
        or bool(package_steps) and all(ps.get("package_type") == "deployment_agent" for ps in package_steps)
    )
    is_major_activity = plan.get("change", {}).get("activity_type") == "Major Version Upgrade"
    deployment_backend = plan.get("execution", {}).get("deployment_backend") or (
        "api" if "Web API" in plan.get("execution", {}).get("method", "") else "cdt"
    )
    is_api = deployment_backend == "api"
    if is_api and plan.get("checkpoint", {}).get("cluster_mode") == "standalone":
        raise ValueError("Management Web API deployment currently requires a cluster object")
    if is_api and len(plan.get("checkpoint", {}).get("members") or []) != 2:
        raise ValueError("Management Web API deployment currently requires exactly two cluster members")
    if is_api and len(package_steps) != 1:
        raise ValueError("Management Web API deployment currently requires exactly one package step")

    if is_da_activity:
        steps.extend([
            ("validate-plan", "01_validate_activity_plan.yml", "", {}),
            ("init", "00_precheck.yml", "", {}),
            ("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}),
        ])
        if has_install:
            steps.extend([
                ("stage-files", "06_validate_mds_package.yml", "", {}),
                ("stage-files", "05_airgap_package_gate.yml", "", {}),
            ])
        for ps in package_steps:
            step = ps["name"]
            steps.append(("install-deployment-agent", "08_validate_package_prerequisites.yml", step, {}))
            steps.append(("install-deployment-agent", "30_direct_package_step.yml", step, {"checkpoint_execute_direct": True}))
        steps.append(("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}))
        return steps

    if is_api:
        steps.extend([
            ("validate-plan", "01_validate_activity_plan.yml", "", {}),
            ("init", "00_precheck.yml", "", {}),
            ("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}),
            ("cluster-state-capture", "11_capture_cluster_state.yml", "", {}),
            ("baseline-capture", "12_support_capture.yml", "", {}),
        ])
        package_step = package_steps[0]
        step = package_step["name"]
        if package_step["action"] in {"install", "upgrade"}:
            steps.extend([
                ("stage-files", "06_validate_mds_package.yml", "", {}),
                ("stage-files", "39_api_repository_package.yml", step, {}),
            ])
        is_remove = package_step["action"] in {"remove", "uninstall"}
        steps.extend([
            ("first-member", "08_validate_package_prerequisites.yml", step, {}),
            ("first-member", "40_api_verify_package.yml", step, {}),
            (
                "first-member",
                "30_direct_package_step.yml" if is_remove else "41_api_execute_package.yml",
                step,
                {"checkpoint_execute_direct": True} if is_remove else {"checkpoint_execute_api": True},
            ),
        ])
        if is_remove:
            steps.append(("failover-to-first", "23_failover_to_member.yml", "", {}))
        if is_major_activity:
            steps.extend([
                ("mixed-version-policy-gate", "31_major_policy_gate.yml", "", {}),
                ("mvc-on", "32_major_mvc.yml", "", {}),
                ("failover-to-first", "23_failover_to_member.yml", "", {}),
            ])
        if plan["execution"].get("tester_pause"):
            steps.append(("approve-testers", "__gate__", "", {}))
        steps.extend([
            ("second-member", "08_validate_package_prerequisites.yml", step, {}),
            (
                "second-member",
                "30_direct_package_step.yml" if is_remove else "41_api_execute_package.yml",
                step,
                {"checkpoint_execute_direct": True} if is_remove else {"checkpoint_execute_api": True},
            ),
        ])
        if is_major_activity:
            steps.extend([
                ("final-policy-install", "31_major_policy_gate.yml", "", {}),
                ("mvc-off", "32_major_mvc.yml", "", {}),
            ])
        if plan["checkpoint"].get("preserve_original_active"):
            steps.append(("restore-original-active", "61_restore_original_active.yml", "", {}))
        steps.extend([
            ("final-support-capture", "12_support_capture.yml", "", {}),
            ("support-diff", "62_support_diff.yml", "", {}),
            ("postcheck", "60_postcheck.yml", "", {}),
        ])
        return steps

    if is_major_activity:
        if plan.get("checkpoint", {}).get("cluster_mode") == "standalone":
            raise ValueError("Major Version Upgrade currently requires a two-member cluster workflow")
        steps.extend([
            ("validate-plan", "01_validate_activity_plan.yml", "", {}),
            ("init", "00_precheck.yml", "", {}),
            ("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}),
            ("cluster-state-capture", "11_capture_cluster_state.yml", "", {}),
            ("baseline-capture", "12_support_capture.yml", "", {}),
            ("stage-files", "06_validate_mds_package.yml", "", {}),
            ("stage-files", "05_airgap_package_gate.yml", "", {}),
        ])
        for ps in package_steps:
            step = ps["name"]
            steps.extend([
                ("first-member", "08_validate_package_prerequisites.yml", step, {}),
                ("first-member", "10_cdt_generate_candidates.yml", step, {}),
                ("first-member", "20_cdt_execute_guarded.yml", step, {"checkpoint_execute_upgrade": True}),
            ])
        steps.extend([
            ("mixed-version-policy-gate", "31_major_policy_gate.yml", "", {}),
            ("mvc-on", "32_major_mvc.yml", "", {}),
            ("failover-to-first", "23_failover_to_member.yml", "", {}),
        ])
        if plan["execution"].get("tester_pause"):
            steps.append(("approve-testers", "__gate__", "", {}))
        for ps in package_steps:
            step = ps["name"]
            steps.extend([
                ("second-member", "08_validate_package_prerequisites.yml", step, {}),
                ("second-member", "10_cdt_generate_candidates.yml", step, {}),
                ("second-member", "20_cdt_execute_guarded.yml", step, {"checkpoint_execute_upgrade": True}),
            ])
        steps.extend([
            ("final-policy-install", "31_major_policy_gate.yml", "", {}),
            ("mvc-off", "32_major_mvc.yml", "", {}),
        ])
        if plan["checkpoint"].get("preserve_original_active"):
            steps.append(("restore-original-active", "61_restore_original_active.yml", "", {}))
        steps.extend([
            ("final-support-capture", "12_support_capture.yml", "", {}),
            ("support-diff", "62_support_diff.yml", "", {}),
            ("postcheck", "60_postcheck.yml", "", {}),
        ])
        return steps

    steps.extend([
        ("validate-plan", "01_validate_activity_plan.yml", "", {}),
        ("init", "00_precheck.yml", "", {}),
        ("deployment-agent-readiness", "07_validate_deployment_agent.yml", "", {}),
        ("cluster-state-capture", "11_capture_cluster_state.yml", "", {}),
        ("baseline-capture", "12_support_capture.yml", "", {}),
    ])
    if has_install:
        steps.extend([
            ("stage-files", "06_validate_mds_package.yml", "", {}),
            ("stage-files", "05_airgap_package_gate.yml", "", {}),
        ])
    for member_phase in ["first-member", "second-member"] if plan["checkpoint"].get("cluster_mode") != "standalone" else ["first-member"]:
        if member_phase == "second-member" and plan["execution"].get("tester_pause"):
            steps.append(("approve-testers", "__gate__", "", {}))
        for ps in package_steps:
            step = ps["name"]
            steps.append((member_phase, "08_validate_package_prerequisites.yml", step, {}))
            if ps.get("package_type") == "deployment_agent" or "Direct" in plan["execution"].get("method", ""):
                steps.append(("install-deployment-agent" if ps.get("package_type") == "deployment_agent" else member_phase, "30_direct_package_step.yml", step, {"checkpoint_execute_direct": True}))
            else:
                steps.append((member_phase, "10_cdt_generate_candidates.yml", step, {}))
                steps.append((member_phase, "20_cdt_execute_guarded.yml", step, {"checkpoint_execute_upgrade": True}))
        if member_phase == "first-member" and plan["checkpoint"].get("cluster_mode") != "standalone":
            steps.append(("failover-to-first", "23_failover_to_member.yml", "", {}))
    if plan["checkpoint"].get("preserve_original_active") and plan["checkpoint"].get("cluster_mode") != "standalone":
        steps.append(("restore-original-active", "61_restore_original_active.yml", "", {}))
    steps.extend([
        ("final-support-capture", "12_support_capture.yml", "", {}),
        ("support-diff", "62_support_diff.yml", "", {}),
        ("postcheck", "60_postcheck.yml", "", {}),
    ])
    return steps


def validate_phase_boundaries(
    steps: list[tuple[str, str, str, dict[str, Any]]],
    *,
    start_at: str,
    stop_after: str,
    skip_discovery: bool,
) -> None:
    phases = {phase for phase, _playbook, _step, _extra in steps}
    if start_at and start_at not in phases:
        raise SystemExit(f"ERROR: --start-at phase {start_at!r} is not present in this workflow")
    if stop_after == "discover-targets" and skip_discovery:
        raise SystemExit("ERROR: --stop-after discover-targets cannot be used with --skip-discovery")
    if stop_after and stop_after != "discover-targets" and stop_after not in phases:
        raise SystemExit(f"ERROR: --stop-after phase {stop_after!r} is not present in this workflow")


def main() -> int:
    os.umask(0o077)
    ap = argparse.ArgumentParser()
    ap.add_argument("--chg-number", default="")
    ap.add_argument("--chg-sys-id", default="", help="ServiceNow change_request sys_id; use when change numbers are duplicated")
    ap.add_argument("--change-number", default="")
    ap.add_argument("--instance", default=os.environ.get("SN_INSTANCE", ""))
    ap.add_argument("--sn-username", default=os.environ.get("SN_USERNAME", ""))
    ap.add_argument("--sn-password", default=os.environ.get("SN_PASSWORD", ""))
    ap.add_argument("--package-file", type=Path)
    ap.add_argument("--dependency-file", type=Path)
    ap.add_argument("--target-ips", default="")
    ap.add_argument("--mds-host", default="")
    ap.add_argument("--cma-name", default="")
    ap.add_argument("--cma-ip", default="")
    ap.add_argument("--cluster-name", default="")
    ap.add_argument("--policy-package", default="")
    ap.add_argument("--activity-type", default="")
    ap.add_argument("--environment", default="lab")
    ap.add_argument("--current-version", default="")
    ap.add_argument("--target-version", default="")
    ap.add_argument("--target-take", default="")
    ap.add_argument("--icap-mode", choices=["required", "optional", "disabled"], default=None)
    ap.add_argument("--deployment-backend", choices=["cdt", "api"], default="cdt", help="Package execution backend; catalog integration continues to default to CDT")
    ap.add_argument("--package-source-dir", default="/var/log/tmp")
    ap.add_argument("--support-capture-script", default=DEFAULT_SUPPORT_CAPTURE)
    ap.add_argument("--preserve-original-active", default="true")
    ap.add_argument("--tester-gate", default="true")
    ap.add_argument("--ansible-playbook", type=Path, default=DEFAULT_ANSIBLE_PLAYBOOK)
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument("--simulate-gates", action="store_true", help="Auto-approve tester gates for lab validation")
    ap.add_argument("--lab-override-governance", action="store_true", help="Bypass ServiceNow marker/state/approval/readiness checks for controlled lab validation only")
    ap.add_argument("--start-at", default="", help="Resume at phase id")
    ap.add_argument("--stop-after", default="", help="Stop after phase id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.chg_number and not args.chg_sys_id:
        raise SystemExit("ERROR: provide --chg-number or --chg-sys-id")

    sn = None
    context = None
    values: dict[str, str] = {}
    if args.instance and args.sn_username and args.sn_password:
        sn = ServiceNowClient(args.instance, args.sn_username, args.sn_password)
        context = service_now_context(sn, args.chg_number, args.chg_sys_id)
        validate_service_now_governance(context, allow_lab_override=args.lab_override_governance)
        if not args.chg_number:
            args.chg_number = str(context["chg"].get("number") or args.chg_sys_id)
        values = context.get("values", {})

    RUNS_DIR.mkdir(exist_ok=True)
    run_dir = RUNS_DIR / f"{args.chg_number}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
    run_dir.mkdir(parents=True)
    (run_dir / "attachments").mkdir()
    (run_dir / "logs").mkdir()

    if sn and context:
        download_context_attachments(sn, context, run_dir)
        if not args.package_file:
            args.package_file = choose_attachment(context, "package")
        if not args.dependency_file:
            args.dependency_file = choose_attachment(context, "dependency")

    if not args.package_file or not args.package_file.exists():
        raise SystemExit("ERROR: package CSV/XLSX is required. Provide --package-file or attach CPUSE Package to RITM/CHG.")
    rows = parse_tabular_file(args.package_file)
    steps = package_steps_from_rows(rows, args.package_source_dir)
    if args.dependency_file and args.dependency_file.exists():
        apply_dependency_rows(steps, parse_tabular_file(args.dependency_file))
    if not steps:
        raise SystemExit("ERROR: no package steps parsed from CPUSE package file")

    env = os.environ.copy()
    if not env.get("CP_PASSWORD") or not env.get("CP_EXPERT_PASSWORD"):
        raise SystemExit("ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD must be set for gateway/MDS automation")
    ansible = args.ansible_playbook
    if not ansible.exists():
        raise SystemExit(f"ERROR: ansible-playbook not found: {ansible}")

    discovered = None
    if not args.skip_discovery:
        post_phase(sn, context, "discover-targets", "started", "Target discovery started from ServiceNow runner.")
        discovered = discover_targets(args, ansible, env, run_dir, values)
        post_phase(sn, context, "discover-targets", "completed", "Target discovery completed and resolved CMA/cluster/member context.", run_dir / "logs" / "02_discover_targets.log")
        if args.stop_after == "discover-targets":
            summary = {"status": "stopped", "stopped_after": "discover-targets", "chg_number": args.chg_number, "run_dir": str(run_dir), "finished_at": utc_now()}
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary, indent=2))
            return CONTROLLED_STOP_RC

    plan = build_base_plan(args, steps, discovered, values)
    plan_path = run_dir / f"{args.chg_number}_activity_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    vars_path = run_dir / f"{args.chg_number}_vars.json"

    all_steps = workflow_steps(plan)
    validate_phase_boundaries(
        all_steps, start_at=args.start_at, stop_after=args.stop_after, skip_discovery=args.skip_discovery
    )
    active = not bool(args.start_at)
    executed_phases = 0
    stopped_after = ""
    for phase, playbook, step, extra in all_steps:
        if args.start_at and phase == args.start_at:
            active = True
        if not active:
            continue
        executed_phases += 1
        if playbook == "__gate__":
            if args.simulate_gates:
                post_phase(sn, context, phase, "simulated", "Tester gate auto-approved for lab simulation.")
                continue
            post_phase(sn, context, phase, "waiting", "Tester validation is required before continuing.")
            print(f"STOP: tester gate waiting at {phase}. Re-run with --simulate-gates or --start-at second-member after approval.")
            return 20
        vars_path.write_text(json.dumps(runner_vars(plan, plan_path, phase, step), indent=2) + "\n")
        log_path = run_dir / "logs" / f"{phase}_{step or 'none'}_{playbook}.log"
        post_phase(sn, context, phase, "started", f"Running {playbook}{' step '+step if step else ''}.")
        if args.dry_run:
            rc = 0
            log_path.write_text(f"DRY RUN: would run {playbook} phase={phase} step={step}\n")
        else:
            try:
                rc = run_playbook(ansible, playbook, vars_path, env, log_path, extra)
            except KeyboardInterrupt:
                state = {"failed_phase": phase, "failed_playbook": playbook, "failed_step": step, "failed_log": str(log_path), "time": utc_now(), "reason": "interrupted"}
                (run_dir / "resume_state.json").write_text(json.dumps(state, indent=2) + "\n")
                post_phase(sn, context, phase, "interrupted", f"Workflow interrupted during {playbook}; resume checkpoint recorded for evidence reference {run_dir.name}.", log_path)
                print(json.dumps(state, indent=2))
                return 130
        if rc != 0:
            state = {"failed_phase": phase, "failed_playbook": playbook, "failed_step": step, "failed_log": str(log_path), "time": utc_now()}
            (run_dir / "resume_state.json").write_text(json.dumps(state, indent=2) + "\n")
            post_phase(sn, context, phase, "failed", f"{playbook} failed with rc={rc}. Engineer remediation required; resume checkpoint recorded for evidence reference {run_dir.name}.", log_path)
            return rc
        post_phase(sn, context, phase, "completed", f"{playbook} completed successfully.", log_path)
        if args.stop_after and phase == args.stop_after:
            stopped_after = phase
            print(f"Stopped after requested phase {phase}")
            break

    if executed_phases == 0:
        raise SystemExit("ERROR: workflow selected zero executable phases; refusing to report success")
    summary = {
        "status": "stopped" if stopped_after else "completed",
        "chg_number": args.chg_number,
        "run_dir": str(run_dir),
        "activity_plan": str(plan_path),
        "finished_at": utc_now(),
    }
    if stopped_after:
        summary["stopped_after"] = stopped_after
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if not stopped_after:
        post_phase(sn, context, "postcheck", "completed", f"ServiceNow-first workflow completed. Evidence reference: {run_dir.name}", run_dir / "summary.json")
    print(json.dumps(summary, indent=2))
    return CONTROLLED_STOP_RC if stopped_after else 0


if __name__ == "__main__":
    raise SystemExit(main())
