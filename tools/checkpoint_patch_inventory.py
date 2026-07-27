#!/usr/bin/env python3
"""Inventory installed Check Point CPUSE patches across every CMA and gateway.

Run locally in Expert mode on a Multi-Domain Server. Discovery uses the local
Management API. Package collection uses CMA-scoped CPRID and does not require
gateway SSH credentials.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import html
import ipaddress
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("checkpoint_patch_inventory")
DEFAULT_PAGE_SIZE = 200
DEFAULT_TIMEOUT = 240

CSV_FIELDS = [
    "collected_at",
    "domain",
    "domain_uid",
    "cma_name",
    "cma_ip",
    "cluster_name",
    "gateway_name",
    "gateway_uid",
    "gateway_ip",
    "object_type",
    "gateway_version",
    "os_build",
    "os_kernel",
    "da_build",
    "da_agent_status",
    "da_connection_status",
    "package_section",
    "package_name",
    "package_type",
    "package_status",
    "inferred_release",
    "inferred_take",
    "package_family",
    "collection_status",
    "warning",
    "error",
]

SUMMARY_FIELDS = [
    "collected_at",
    "domain",
    "cma_name",
    "cma_ip",
    "cluster_name",
    "gateway_name",
    "gateway_uid",
    "gateway_ip",
    "object_type",
    "gateway_version",
    "os_build",
    "da_build",
    "current_jhf_take",
    "installed_patch_count",
    "collection_status",
    "warning",
    "error",
]


class CommandError(RuntimeError):
    def __init__(self, message: str, command: str = "", output: str = ""):
        super().__init__(message)
        self.command = command
        self.output = output


@dataclass(frozen=True)
class DomainContext:
    name: str
    uid: str
    cma_name: str
    cma_ip: str
    active: bool


@dataclass(frozen=True)
class Gateway:
    domain: str
    domain_uid: str
    cma_name: str
    cma_ip: str
    cluster_name: str
    name: str
    uid: str
    ip: str
    object_type: str
    management_version: str = ""


@dataclass
class PackageRecord:
    section: str
    name: str
    package_type: str
    status: str
    inferred_release: str = ""
    inferred_take: str = ""
    family: str = ""


@dataclass
class GatewayResult:
    gateway: Gateway
    collected_at: str
    status: str
    version: str = ""
    os_build: str = ""
    os_kernel: str = ""
    da_build: str = ""
    da_agent_status: str = ""
    da_connection_status: str = ""
    packages: list[PackageRecord] = field(default_factory=list)
    warning: str = ""
    error: str = ""
    raw_output: str = ""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def shell_quote(value: object) -> str:
    return shlex.quote(str(value))


def run_bash(command: str, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
    LOG.debug("running: %s", command)
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        raise CommandError(f"command timed out after {timeout}s", command, output) from exc
    if check and result.returncode != 0:
        raise CommandError(
            f"command failed with rc={result.returncode}",
            command,
            result.stdout,
        )
    return result


def json_from_output(output: str) -> dict[str, Any]:
    starts = [idx for idx, char in enumerate(output) if char == "{"]
    for start in starts:
        candidate = output[start:].strip()
        for end in range(len(candidate), 0, -1):
            if candidate[end - 1] != "}":
                continue
            try:
                value = json.loads(candidate[:end])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no JSON object found in command output")


def mgmt_cli_json(arguments: str, timeout: int) -> dict[str, Any]:
    command = f"mgmt_cli -r true {arguments} --format json"
    result = run_bash(command, timeout=timeout)
    try:
        return json_from_output(result.stdout)
    except ValueError as exc:
        raise CommandError(f"could not parse Management API JSON: {exc}", command, result.stdout) from exc


def paged_objects(
    base_arguments: str,
    keys: Iterable[str],
    page_size: int,
    timeout: int,
) -> list[dict[str, Any]]:
    offset = 0
    objects: list[dict[str, Any]] = []
    total: int | None = None
    while total is None or offset < total:
        data = mgmt_cli_json(
            f"{base_arguments} limit {page_size} offset {offset} details-level full",
            timeout,
        )
        page: list[dict[str, Any]] = []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                page = [item for item in value if isinstance(item, dict)]
                break
        objects.extend(page)
        raw_total = data.get("total")
        total = int(raw_total) if isinstance(raw_total, (int, str)) and str(raw_total).isdigit() else len(objects)
        if not page or len(objects) >= total:
            break
        offset += len(page)
    return objects


def management_server(domain: dict[str, Any]) -> dict[str, Any] | None:
    servers = [s for s in (domain.get("servers") or []) if isinstance(s, dict)]
    management = [
        server
        for server in servers
        if "management server" in str(server.get("type") or "").lower()
        and "log" not in str(server.get("type") or "").lower()
    ]
    if not management:
        return None
    management.sort(key=lambda item: (not bool(item.get("active")), str(item.get("name") or "")))
    return management[0]


def discover_domains(page_size: int, timeout: int, selected: set[str]) -> tuple[list[DomainContext], list[str]]:
    rows = paged_objects("show domains", ("objects", "domains"), page_size, timeout)
    domains: list[DomainContext] = []
    errors: list[str] = []
    for row in rows:
        name = str(row.get("name") or row.get("domain-name") or "").strip()
        if not name:
            continue
        if selected and name not in selected:
            continue
        server = management_server(row)
        if not server:
            errors.append(f"{name}: no Domain Management Server found; logging-only servers were excluded")
            continue
        domains.append(
            DomainContext(
                name=name,
                uid=str(row.get("uid") or ""),
                cma_name=str(server.get("name") or name),
                cma_ip=str(server.get("ipv4-address") or server.get("ip-address") or ""),
                active=bool(server.get("active", True)),
            )
        )
    return sorted(domains, key=lambda item: item.name.lower()), errors


def gateway_ip(obj: dict[str, Any]) -> str:
    for key in ("ipv4-address", "ip-address", "main-ip"):
        value = str(obj.get(key) or "").strip()
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            continue
    return ""


def cluster_member_names(obj: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for value in obj.get("cluster-member-names") or []:
        if isinstance(value, str) and value:
            names.add(value)
    for key in ("cluster-members", "members"):
        for member in obj.get(key) or []:
            if isinstance(member, dict) and member.get("name"):
                names.add(str(member["name"]))
    return names


def is_gateway_object(obj: dict[str, Any]) -> bool:
    object_type = str(obj.get("type") or "").lower().replace("_", "-")
    if not object_type:
        return False
    if "cluster" in object_type and "member" not in object_type:
        return False
    if object_type in {
        "cluster-member",
        "simple-gateway",
        "vsx-gateway",
        "vsx-cluster-member",
        "cpmigatewayplain",
        "gateway-plain",
        "maestro-gateway",
    }:
        return True
    if "gateway" in object_type and not any(
        blocked in object_type for blocked in ("management", "log", "host", "interoperable")
    ):
        return True
    return False


def domain_gateway_objects(domain: DomainContext, page_size: int, timeout: int) -> list[dict[str, Any]]:
    return paged_objects(
        f"-d {shell_quote(domain.name)} show gateways-and-servers",
        ("objects", "gateways-and-servers"),
        page_size,
        timeout,
    )


def gateway_records(
    domain: DomainContext,
    objects: list[dict[str, Any]],
    selected: set[str],
) -> tuple[list[Gateway], list[str]]:
    parent_by_member: dict[str, str] = {}
    for obj in objects:
        object_type = str(obj.get("type") or "").lower()
        if "cluster" not in object_type or "member" in object_type:
            continue
        cluster_name = str(obj.get("name") or "")
        for member_name in cluster_member_names(obj):
            parent_by_member[member_name] = cluster_name

    gateways: list[Gateway] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for obj in objects:
        if not is_gateway_object(obj):
            continue
        name = str(obj.get("name") or "").strip()
        ip = gateway_ip(obj)
        if selected and name not in selected and ip not in selected:
            continue
        if not name:
            errors.append(f"{domain.name}: gateway object without a name was skipped")
            continue
        if not ip:
            errors.append(f"{domain.name}/{name}: no valid management IPv4 address")
            continue
        uid = str(obj.get("uid") or "")
        key = (uid, name, ip)
        if key in seen:
            continue
        seen.add(key)
        gateways.append(
            Gateway(
                domain=domain.name,
                domain_uid=domain.uid,
                cma_name=domain.cma_name,
                cma_ip=domain.cma_ip,
                cluster_name=parent_by_member.get(name, ""),
                name=name,
                uid=uid,
                ip=ip,
                object_type=str(obj.get("type") or ""),
                management_version=str(obj.get("version") or ""),
            )
        )
    gateways.sort(key=lambda item: (item.cluster_name.lower(), item.name.lower(), item.ip))
    return gateways, errors


def cma_command(cma_name: str, command: str) -> str:
    return f"mdsenv {shell_quote(cma_name)} >/dev/null 2>&1 && {command}"


def cprid_capture(gateway: Gateway, timeout: int) -> str:
    token = safe_filename(f"cpinv_{os.getpid()}_{uuid.uuid4().hex[:10]}")
    remote_file = f"/tmp/{token}.out"
    fd, local_name = tempfile.mkstemp(prefix=token + "_", suffix=".out", dir="/var/log/tmp")
    os.close(fd)
    local_file = Path(local_name)
    sections = [
        ("version", "show version all"),
        ("installer_status", "show installer status all"),
        ("packages_installed", "show installer packages installed"),
        ("packages_all", "show installer packages"),
    ]
    script_parts: list[str] = []
    for section, command in sections:
        script_parts.append(f"printf '%s\\n' {shell_quote('__CPINV_BEGIN_' + section.upper() + '__')}")
        script_parts.append(f"/bin/clish -c {shell_quote(command)}")
        script_parts.append(f"printf '%s\\n' {shell_quote('__CPINV_END_' + section.upper() + '__')}")
    remote_script = "{ " + "; ".join(script_parts) + "; } > " + shell_quote(remote_file) + " 2>&1"
    rexec = (
        f"cprid_util -server {shell_quote(gateway.ip)} rexec "
        f"-rcmd /bin/sh -c {shell_quote(remote_script)}"
    )
    getfile = (
        f"cprid_util -server {shell_quote(gateway.ip)} getfile "
        f"-remote_file {shell_quote(remote_file)} -local_file {shell_quote(local_file)}"
    )
    cleanup = f"cprid_util -server {shell_quote(gateway.ip)} rexec -rcmd /bin/rm {shell_quote(remote_file)}"
    try:
        run_bash(cma_command(gateway.cma_name, rexec), timeout=timeout)
        run_bash(cma_command(gateway.cma_name, getfile), timeout=timeout)
        if not local_file.exists():
            raise CommandError("CPRID getfile completed but no local output was created", getfile)
        return local_file.read_text(errors="replace")
    finally:
        try:
            run_bash(cma_command(gateway.cma_name, cleanup), timeout=min(timeout, 60), check=False)
        finally:
            local_file.unlink(missing_ok=True)


def split_marked_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    pattern = re.compile(r"^__CPINV_BEGIN_([A-Z_]+)__\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        name = match.group(1).lower()
        end_marker = f"__CPINV_END_{match.group(1)}__"
        start = match.end()
        boundary = text.find(end_marker, start)
        if boundary < 0:
            boundary = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[name] = text[start:boundary].strip()
    return sections


def package_table(text: str, second_header: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    current_section = ""
    rows: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    in_table = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if "Connection error" in stripped or "might be incomplete" in stripped:
            warnings.append(normalize_space(stripped.strip("* ")))
            continue
        title_match = re.match(r"^\*\*\s+([^*].*?)\s+\*\*$", stripped)
        if title_match:
            title = normalize_space(title_match.group(1))
            if title and set(title) != {"*"}:
                current_section = title
                in_table = False
            continue
        if stripped.startswith("Display name") and second_header.lower() in stripped.lower():
            in_table = True
            continue
        if stripped.startswith("**"):
            continue
        if not in_table:
            continue
        parts = re.split(r"\s{2,}", stripped, maxsplit=1)
        if len(parts) != 2:
            continue
        name, value = normalize_space(parts[0]), normalize_space(parts[1])
        if not name or not value:
            continue
        rows.append((current_section or "Uncategorized", name, value))
    return rows, warnings


def normalize_package_key(value: str) -> str:
    return normalize_space(value).casefold()


def infer_package_fields(name: str, section: str, package_type: str) -> tuple[str, str, str]:
    text = normalize_space(name)
    release = ""
    match = re.search(r"(?i)(?<![A-Z0-9])R(\d+(?:[._]\d+)?)(?![A-Z0-9])", text)
    if match:
        release = "R" + match.group(1).replace("_", ".")
    take = ""
    for pattern in (
        r"(?i)(?:^|[_\s-])JHF[_\s-]*T?(\d+)(?:[_\s-]|$)",
        r"(?i)\bTake\s*#?\s*(\d+)\b",
        r"(?i)(?:^|[_-])T(\d+)(?:[_-]|\b)",
    ):
        match = re.search(pattern, text)
        if match:
            take = match.group(1)
            break
    lowered = f"{section} {package_type} {text}".lower()
    if "blink" in lowered:
        family = "blink"
    elif "wrapper" in lowered:
        family = "wrapper"
    elif "jumbo" in lowered or re.search(r"(?i)\bJHF\b", text):
        family = "jhf"
    elif "hotfix" in lowered:
        family = "hotfix"
    elif "deployment agent" in lowered:
        family = "deployment_agent"
    else:
        family = "other"
    return release, take, family


def parse_version(text: str) -> tuple[str, str, str]:
    version = ""
    os_build = ""
    kernel = ""
    match = re.search(r"(?im)^Product version\s+Check Point Gaia\s+([^\s|]+)", text)
    if not match:
        match = re.search(r"(?i)Check Point Gaia\s+(R\d+(?:\.\d+)?)", text)
    if match:
        version = match.group(1).strip()
    match = re.search(r"(?im)^OS build\s+([^\s|]+)", text)
    if match:
        os_build = match.group(1).strip()
    match = re.search(r"(?im)^OS kernel version\s+(.+)$", text)
    if match:
        kernel = match.group(1).strip()
    return version, os_build, kernel


def parse_da_status(text: str) -> tuple[str, str, str]:
    build = ""
    agent = ""
    connection = ""
    match = re.search(r"(?im)^Build number:\s*(\d+)", text)
    if match:
        build = match.group(1)
    match = re.search(r"(?im)^Agent:\s*(.+)$", text)
    if match:
        agent = match.group(1).strip()
    match = re.search(r"(?im)^Network connection:\s*(.+)$", text)
    if match:
        connection = match.group(1).strip()
    return build, agent, connection


def parse_gateway_output(gateway: Gateway, text: str) -> GatewayResult:
    collected = utc_now()
    sections = split_marked_sections(text)
    required = {"version", "installer_status", "packages_installed", "packages_all"}
    missing = sorted(required.difference(sections))
    if missing:
        return GatewayResult(
            gateway=gateway,
            collected_at=collected,
            status="failed",
            error="missing CPRID output sections: " + ", ".join(missing),
            raw_output=text,
        )
    version, os_build, kernel = parse_version(sections["version"])
    da_build, agent, connection = parse_da_status(sections["installer_status"])
    installed, warnings_a = package_table(sections["packages_installed"], "Type")
    all_packages, warnings_b = package_table(sections["packages_all"], "Status")
    status_map = {normalize_package_key(name): status for _, name, status in all_packages}
    packages: list[PackageRecord] = []
    for section, name, package_type in installed:
        status = status_map.get(normalize_package_key(name), "Installed")
        release, take, family = infer_package_fields(name, section, package_type)
        packages.append(
            PackageRecord(
                section=section,
                name=name,
                package_type=package_type,
                status=status,
                inferred_release=release,
                inferred_take=take,
                family=family,
            )
        )
    warning = "; ".join(dict.fromkeys(warnings_a + warnings_b))
    return GatewayResult(
        gateway=gateway,
        collected_at=collected,
        status="success",
        version=version or gateway.management_version,
        os_build=os_build,
        os_kernel=kernel,
        da_build=da_build,
        da_agent_status=agent,
        da_connection_status=connection,
        packages=packages,
        warning=warning,
        raw_output=text,
    )


def collect_gateway(gateway: Gateway, timeout: int, raw_dir: Path | None) -> GatewayResult:
    LOG.info("collecting %s/%s (%s)", gateway.domain, gateway.name, gateway.ip)
    try:
        text = cprid_capture(gateway, timeout)
        result = parse_gateway_output(gateway, text)
    except Exception as exc:
        output = exc.output if isinstance(exc, CommandError) else ""
        result = GatewayResult(
            gateway=gateway,
            collected_at=utc_now(),
            status="failed",
            error=str(exc),
            raw_output=output,
        )
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / (
            safe_filename(gateway.domain)
            + "__"
            + safe_filename(gateway.name)
            + "__"
            + safe_filename(gateway.ip)
            + ".txt"
        )
        raw_path.write_text(result.raw_output)
    return result


def package_csv_rows(result: GatewayResult) -> list[dict[str, str]]:
    base = {
        "collected_at": result.collected_at,
        "domain": result.gateway.domain,
        "domain_uid": result.gateway.domain_uid,
        "cma_name": result.gateway.cma_name,
        "cma_ip": result.gateway.cma_ip,
        "cluster_name": result.gateway.cluster_name,
        "gateway_name": result.gateway.name,
        "gateway_uid": result.gateway.uid,
        "gateway_ip": result.gateway.ip,
        "object_type": result.gateway.object_type,
        "gateway_version": result.version,
        "os_build": result.os_build,
        "os_kernel": result.os_kernel,
        "da_build": result.da_build,
        "da_agent_status": result.da_agent_status,
        "da_connection_status": result.da_connection_status,
        "collection_status": result.status,
        "warning": result.warning,
        "error": result.error,
    }
    if not result.packages:
        return [
            {
                **base,
                "package_section": "",
                "package_name": "",
                "package_type": "",
                "package_status": "No installed packages reported" if result.status == "success" else "",
                "inferred_release": "",
                "inferred_take": "",
                "package_family": "",
            }
        ]
    return [
        {
            **base,
            "package_section": package.section,
            "package_name": package.name,
            "package_type": package.package_type,
            "package_status": package.status,
            "inferred_release": package.inferred_release,
            "inferred_take": package.inferred_take,
            "package_family": package.family,
        }
        for package in result.packages
    ]


def summary_row(result: GatewayResult) -> dict[str, str | int]:
    takes = [
        int(package.inferred_take)
        for package in result.packages
        if package.family == "jhf" and package.inferred_take.isdigit()
    ]
    return {
        "collected_at": result.collected_at,
        "domain": result.gateway.domain,
        "cma_name": result.gateway.cma_name,
        "cma_ip": result.gateway.cma_ip,
        "cluster_name": result.gateway.cluster_name,
        "gateway_name": result.gateway.name,
        "gateway_uid": result.gateway.uid,
        "gateway_ip": result.gateway.ip,
        "object_type": result.gateway.object_type,
        "gateway_version": result.version,
        "os_build": result.os_build,
        "da_build": result.da_build,
        "current_jhf_take": str(max(takes)) if takes else "",
        "installed_patch_count": len(result.packages),
        "collection_status": result.status,
        "warning": result.warning,
        "error": result.error,
    }


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def preflight() -> None:
    if os.geteuid() != 0:
        raise SystemExit("ERROR: run this script in Expert/root mode on the MDS")
    for command in ("mgmt_cli", "mdsenv", "cprid_util"):
        result = run_bash(f"command -v {shell_quote(command)}", timeout=30, check=False)
        if result.returncode != 0:
            raise SystemExit(f"ERROR: required MDS command not found: {command}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(
        description="Discover all CMAs and managed gateways, then report installed CPUSE patches."
    )
    parser.add_argument(
        "--output-dir",
        default=f"/var/log/tmp/checkpoint_patch_inventory_{stamp}",
        help="Run output directory (default: timestamped directory under /var/log/tmp)",
    )
    parser.add_argument("--workers", type=int, default=4, help="Concurrent CPRID gateway queries")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per CPRID command timeout")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--domain", action="append", default=[], help="Limit to a domain name; repeatable")
    parser.add_argument("--gateway", action="append", default=[], help="Limit to gateway name or IP; repeatable")
    parser.add_argument("--no-raw", action="store_true", help="Do not retain raw gateway command output")
    parser.add_argument("--discovery-only", action="store_true", help="Discover CMAs/gateways without CPRID collection")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.workers > 32:
        raise SystemExit("ERROR: --workers must be between 1 and 32")
    if args.page_size < 1 or args.page_size > 500:
        raise SystemExit("ERROR: --page-size must be between 1 and 500")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    log_handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(output_dir / "run.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=log_handlers,
    )

    preflight()
    domain_filter = set(args.domain)
    gateway_filter = set(args.gateway)
    domains, discovery_errors = discover_domains(args.page_size, args.timeout, domain_filter)
    if not domains:
        (output_dir / "discovery.json").write_text(
            json.dumps({"domains": [], "gateways": [], "errors": discovery_errors}, indent=2) + "\n"
        )
        LOG.error("no Domain Management Servers discovered")
        return 2

    gateways: list[Gateway] = []
    for domain in domains:
        try:
            objects = domain_gateway_objects(domain, args.page_size, args.timeout)
            found, errors = gateway_records(domain, objects, gateway_filter)
            gateways.extend(found)
            discovery_errors.extend(errors)
            LOG.info("domain %s: discovered %d managed gateway targets", domain.name, len(found))
        except Exception as exc:
            discovery_errors.append(f"{domain.name}: gateway discovery failed: {exc}")

    discovery_payload = {
        "generated_at": utc_now(),
        "domains": [asdict(domain) for domain in domains],
        "gateways": [asdict(gateway) for gateway in gateways],
        "errors": discovery_errors,
    }
    (output_dir / "discovery.json").write_text(json.dumps(discovery_payload, indent=2) + "\n")

    if args.discovery_only:
        results = [
            GatewayResult(gateway=gateway, collected_at=utc_now(), status="discovery_only")
            for gateway in gateways
        ]
    else:
        raw_dir = None if args.no_raw else output_dir / "raw"
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(collect_gateway, gateway, args.timeout, raw_dir): gateway
                for gateway in gateways
            }
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

    results.sort(key=lambda item: (item.gateway.domain.lower(), item.gateway.cluster_name.lower(), item.gateway.name.lower()))
    package_rows = [row for result in results for row in package_csv_rows(result)]
    write_csv(output_dir / "checkpoint_patch_inventory.csv", CSV_FIELDS, package_rows)
    write_csv(output_dir / "checkpoint_gateway_summary.csv", SUMMARY_FIELDS, [summary_row(result) for result in results])

    error_rows = [
        {
            "scope": "gateway",
            "domain": result.gateway.domain,
            "gateway": result.gateway.name,
            "ip": result.gateway.ip,
            "error": result.error,
        }
        for result in results
        if result.status == "failed"
    ]
    error_rows.extend(
        {"scope": "discovery", "domain": "", "gateway": "", "ip": "", "error": error}
        for error in discovery_errors
    )
    write_csv(output_dir / "errors.csv", ["scope", "domain", "gateway", "ip", "error"], error_rows)

    successful = sum(result.status == "success" for result in results)
    failed = sum(result.status == "failed" for result in results)
    LOG.info(
        "complete: domains=%d gateways=%d successful=%d failed=%d discovery_errors=%d output=%s",
        len(domains),
        len(gateways),
        successful,
        failed,
        len(discovery_errors),
        output_dir,
    )
    print(output_dir)
    return 1 if failed or discovery_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
