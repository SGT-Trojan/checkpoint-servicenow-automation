#!/usr/bin/env python3
"""Guarded Check Point Management Web API package deployment backend."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402
import generate_cdt_candidates_from_activity as cdt_candidates  # noqa: E402


IN_PROGRESS = {"in progress", "in-progress", "running", "pending", "queued"}
SUCCESS = {"succeeded", "success", "completed"}
GIB = 1024**3


def quote(value: object) -> str:
    return shlex.quote(str(value))


def json_from_output(output: str) -> dict[str, Any]:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            text = "\n".join(lines[index:])
            end = text.rfind("}")
            if end >= 0:
                try:
                    return json.loads(text[: end + 1])
                except json.JSONDecodeError:
                    continue
    return {}


def task_id(data: dict[str, Any]) -> str:
    direct = str(data.get("task-id") or "")
    if direct:
        return direct
    tasks = data.get("tasks") or []
    if tasks:
        return str(tasks[0].get("task-id") or tasks[0].get("uid") or "")
    return ""


def task_status(data: dict[str, Any]) -> str:
    tasks = data.get("tasks") or []
    value = tasks[0].get("status") if tasks else data.get("status")
    return str(value or "").strip().lower()


def repository_package_ids(value: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for collection_key in ("packages", "objects"):
                rows = node.get(collection_key)
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        identity = row.get("package-id") or row.get("package-name") or row.get("name")
                        if identity:
                            found.append(str(identity))
            for key, item in node.items():
                if key not in {"packages", "objects"}:
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return list(dict.fromkeys(found))


def repository_inventory(api: "ManagementApi", domain: str) -> list[str]:
    offset = 0
    limit = 500
    expected_total: int | None = None
    found: list[str] = []
    while True:
        page = api.call(domain, f"show repository-packages limit {limit} offset {offset}")
        rows = repository_package_ids(page)
        if rows and all(identity in found for identity in rows):
            raise RuntimeError("repository package pagination returned a duplicate page")
        found.extend(rows)
        if "total" not in page:
            if len(rows) < limit:
                return list(dict.fromkeys(found))
            offset += len(rows)
            continue
        total = int(page["total"])
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("repository package total changed during pagination")
        offset += len(rows)
        if offset >= total:
            return list(dict.fromkeys(found))
        if not rows:
            raise RuntimeError("repository package pagination ended before the advertised total")


def api_package_name(step: dict[str, Any]) -> str:
    value = str(step.get("package_name") or step.get("source_path") or "").strip()
    name = Path(value).name
    if name.lower().endswith(".tar"):
        return name[:-4] + ".tgz"
    return name


def take_number(*values: str) -> str:
    text = " ".join(values)
    for pattern in (
        r"jhf[_ -]?t(\d{1,4})(?!\d)",
        r"take[_ -]?(\d{1,4})(?!\d)",
        r"bundle[_ -]?t(\d{1,4})(?!\d)",
        r"\bt(\d{1,4})(?!\d)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


def release_token(value: str) -> str:
    match = re.search(r"R(\d+)(?:[._](\d+))?", value or "", re.I)
    if not match:
        return ""
    return "R" + match.group(1) + ("_" + match.group(2) if match.group(2) else "")


def numeric_output(output: str, description: str) -> int:
    values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
    if not values:
        raise RuntimeError(f"could not read {description} from MDS")
    return values[-1]


def remote_file_size(session: Any, path: str) -> int:
    output = session.run(
        f"test -f {quote(path)} && stat -c %s {quote(path)} || echo 0",
        timeout=120,
    ).output
    return numeric_output(output, f"package size for {path}")


def filesystem_free_bytes(session: Any, path: str) -> int:
    output = session.run(
        f"df -Pk {quote(path)} | awk 'NR==2 {{print $4}}'",
        timeout=120,
    ).output
    return numeric_output(output, f"free space on {path}") * 1024


def deployment_cache_bytes(session: Any) -> int:
    output = session.run(
        "du -sk /opt/CPDepCon-R*/cache 2>/dev/null | "
        "awk '{total += $1} END {print total + 0}'",
        timeout=120,
    ).output
    return numeric_output(output, "Central Deployment cache size") * 1024


def validate_api_workspace(
    session: Any,
    step: dict[str, Any],
    *,
    operation: str,
    is_major: bool,
) -> None:
    source = str(step.get("source_path") or "")
    source_size = remote_file_size(session, source) if source else 0
    reserve = int(os.environ.get("CHECKPOINT_API_WORKSPACE_RESERVE_BYTES", 2 * GIB))
    floor = 12 * GIB if is_major else 5 * GIB
    required = max(source_size + reserve, floor)

    if operation == "repository":
        free = filesystem_free_bytes(session, "/var/log")
        print(
            "API repository workspace: "
            f"free={free} required={required} source_size={source_size} reserve={reserve}"
        )
        if free < required:
            raise RuntimeError(
                "insufficient /var/log space for API repository import: "
                f"free={free} required={required}"
            )
        return

    free = filesystem_free_bytes(session, "/")
    cache = deployment_cache_bytes(session)
    print(
        "API deployment workspace: "
        f"free={free} existing_cache={cache} effective={free + cache} "
        f"required={required} reserve={reserve}"
    )
    if free < reserve:
        raise RuntimeError(
            f"insufficient root filesystem reserve for API deployment: free={free} reserve={reserve}"
        )
    if free + cache < required:
        raise RuntimeError(
            "insufficient root/cache capacity for API deployment: "
            f"effective={free + cache} required={required}"
        )


def cprid_member_output(
    run_mds: Any,
    gateway: str,
    command: str,
    label: str,
    index: int,
) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")
    remote_tmp = f"/tmp/checkpoint_api_reconcile_{os.getpid()}_{index}_{safe}.out"
    local_tmp = f"/var/log/tmp/checkpoint_api_reconcile_{os.getpid()}_{index}_{safe}.out"
    clish = f"clish -c {quote(command)} > {quote(remote_tmp)} 2>&1"
    run_mds(
        f"cprid_util -server {quote(gateway)} rexec -rcmd /bin/sh -c {quote(clish)}",
        timeout=180,
    )
    run_mds(
        f"cprid_util -server {quote(gateway)} getfile "
        f"-remote_file {quote(remote_tmp)} -local_file {quote(local_tmp)}",
        timeout=180,
    )
    output = run_mds(f"cat {quote(local_tmp)}", timeout=120)
    run_mds(
        f"cprid_util -server {quote(gateway)} rexec -rcmd /bin/rm {quote(remote_tmp)}",
        timeout=60,
    )
    run_mds(f"rm -f {quote(local_tmp)}", timeout=60)
    return output


def major_upgrade_completed_count(
    run_mds: Any,
    checkpoint: dict[str, Any],
    step: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    members = checkpoint.get("members") or []
    target_version = str(checkpoint.get("target_version") or "")
    package_name = api_package_name(step)
    completed = 0
    states: list[dict[str, Any]] = []
    for index, member in enumerate(members, 1):
        gateway = str(member.get("management_ip") or member.get("ip") or "")
        if not gateway:
            raise RuntimeError("member is missing its management IP during API failure reconciliation")
        version = cprid_member_output(run_mds, gateway, "show version all", "version", index)
        packages = cprid_member_output(
            run_mds, gateway, "show installer packages installed", "packages", index
        )
        version_ok = bool(
            re.search(
                rf"Product version\s+Check Point Gaia\s+{re.escape(target_version)}(?:\s|$)",
                version,
                re.I,
            )
        )
        package_ok = cdt_candidates.package_identity_is_installed(
            packages, package_name
        )
        completed += int(version_ok and package_ok)
        states.append(
            {
                "gateway": gateway,
                "target_version": version_ok,
                "package_installed": package_ok,
            }
        )
    return completed, states


def major_upgrade_completed_despite_api_failure(
    run_mds: Any,
    checkpoint: dict[str, Any],
    step: dict[str, Any],
    phase: str,
    baseline_completed: int,
) -> bool:
    members = checkpoint.get("members") or []
    expected_count = 1 if phase == "first-member" else len(members)
    completed, states = major_upgrade_completed_count(run_mds, checkpoint, step)
    print("===== Major upgrade API failure reconciliation =====")
    print(json.dumps(states, indent=2, sort_keys=True))
    return baseline_completed == expected_count - 1 and completed == expected_count


def member_installed_sets(data: dict[str, Any]) -> list[set[str]]:
    result: list[set[str]] = []
    for target in data.get("targets") or []:
        members = target.get("cluster-members") or [target]
        for member in members:
            installed = ((member.get("packages") or {}).get("installed") or [])
            result.append({str(row.get("package-id")) for row in installed if row.get("package-id")})
    return result


def resolve_remove_identity(data: dict[str, Any], step: dict[str, Any], checkpoint: dict[str, Any]) -> str:
    installed_sets = member_installed_sets(data)
    if not installed_sets:
        raise RuntimeError("Management API returned no cluster-member package inventory")
    cluster_packages = set.union(*installed_sets)
    requested = " ".join(
        str(step.get(key) or "") for key in ("package_name", "source_path", "name")
    )
    explicit = api_package_name(step)
    if explicit and explicit in cluster_packages:
        return explicit
    take = take_number(requested)
    version = release_token(str(checkpoint.get("current_version") or checkpoint.get("target_version") or ""))
    candidates = []
    for name in sorted(cluster_packages):
        if take and not re.search(rf"(?:_T{re.escape(take)}(?:_|\b)|Take[ _-]?{re.escape(take)}\b)", name, re.I):
            continue
        if version and version.lower() not in name.replace(".", "_").lower():
            continue
        candidates.append(name)
    if len(candidates) != 1:
        raise RuntimeError(
            "uninstall identity must resolve to exactly one package installed on at least one member; "
            f"version={version or 'unknown'} take={take or 'unknown'} candidates={candidates}"
        )
    return candidates[0]


def resolve_remove_identity_via_cprid(
    api: "ManagementApi",
    session: Any,
    global_domain: str,
    checkpoint: dict[str, Any],
    step: dict[str, Any],
    step_name: str,
) -> str:
    """Resolve an installed filename when Central Deployment inventory is empty."""
    repository_ids = repository_inventory(api, global_domain)
    requested = " ".join(str(step.get(key) or "") for key in ("package_name", "source_path", "name"))
    take = take_number(requested)
    version = release_token(str(checkpoint.get("current_version") or checkpoint.get("target_version") or ""))
    repository_matches = [
        name
        for name in repository_ids
        if (not take or re.search(rf"(?:_T{re.escape(take)}(?:_|\b)|Take[ _-]?{re.escape(take)}\b)", name, re.I))
        and (not version or version.lower() in name.replace(".", "_").lower())
    ]
    if len(repository_matches) > 1:
        raise RuntimeError(
            "repository identity is ambiguous for uninstall fallback; "
            f"version={version or 'unknown'} take={take or 'unknown'} candidates={repository_matches}"
        )
    fallback_ref = repository_matches[0] if repository_matches else None

    def run_mds(command: str, timeout: int = 120) -> str:
        return session.run(command, timeout=timeout).output

    members = checkpoint.get("members") or []
    if not members:
        raise RuntimeError("CPRID uninstall fallback has no plan members")
    resolved_members: list[str] = []
    for member in members:
        try:
            resolved_members.append(
                cdt_candidates.resolve_remove_package_ref(
                    run_mds,
                    member,
                    step,
                    step_name,
                    fallback_ref,
                )
            )
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from exc
    unique_resolved = sorted(set(resolved_members))
    if len(unique_resolved) != 1:
        raise RuntimeError(
            "selected-member CPRID identities disagree across API fallback targets: "
            f"{resolved_members}"
        )
    selected = unique_resolved[0]

    if repository_matches and selected != repository_matches[0]:
        raise RuntimeError(
            "CPInstLog and API repository identities disagree; "
            f"cpinstlog={selected} repository={repository_matches[0]}"
        )
    if take and take_number(selected) != take:
        raise RuntimeError(f"CPInstLog identity does not match requested Take {take}: {selected}")
    if version and release_token(selected) != version:
        raise RuntimeError(f"CPInstLog identity does not match requested release {version}: {selected}")
    return selected


class ManagementApi:
    def __init__(self, session: Any):
        self.session = session

    def call(self, domain: str, command: str, timeout: int = 600, asynchronous: bool = False) -> dict[str, Any]:
        domain_arg = f" -d {quote(domain)}" if domain else ""
        sync_arg = " --sync false" if asynchronous else ""
        full = f"mgmt_cli -r true{domain_arg} {command}{sync_arg} --format json"
        output = self.session.run(full, timeout=timeout).output
        print(f"===== Management API: {command.split()[0]} domain={domain or 'System Data'} =====")
        print(output.rstrip())
        data = json_from_output(output)
        if not data:
            detail = output.strip().splitlines()[-1] if output.strip() else "empty output"
            raise RuntimeError(f"Management API returned no JSON: {detail}")
        if data.get("code") and data.get("message"):
            raise RuntimeError(f"Management API error {data['code']}: {data['message']}")
        return data

    def wait(self, domain: str, identifier: str, timeout: int) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        consecutive_transport_failures = 0
        while time.time() < deadline:
            try:
                last = self.call(domain, f"show-task task-id {quote(identifier)}", timeout=300)
            except RuntimeError as exc:
                if not str(exc).startswith("Management API returned no JSON:"):
                    raise
                consecutive_transport_failures += 1
                if consecutive_transport_failures > 3:
                    raise RuntimeError(
                        f"Management API task {identifier} polling failed after "
                        f"{consecutive_transport_failures} consecutive transport/login errors"
                    ) from exc
                print(
                    f"WARNING: transient Management API polling failure "
                    f"{consecutive_transport_failures}/3 for task {identifier}: {exc}"
                )
                time.sleep(15)
                continue
            consecutive_transport_failures = 0
            status = task_status(last)
            if status in IN_PROGRESS or not status:
                time.sleep(15)
                continue
            if status not in SUCCESS:
                raise RuntimeError(f"Management API task {identifier} ended with status {status}: {json.dumps(last)}")
            return last
        raise TimeoutError(f"Management API task {identifier} timed out after {timeout}s: {last}")

    def task_call(self, domain: str, command: str, timeout: int) -> dict[str, Any]:
        result = self.call(domain, command, timeout=600, asynchronous=True)
        identifier = task_id(result)
        if not identifier:
            status = task_status(result)
            if status in SUCCESS:
                return result
            raise RuntimeError("Management API mutating command returned no task-id")
        return self.wait(domain, identifier, timeout)


def select_step(plan: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [row for row in plan.get("package_steps") or [] if row.get("name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"package step {name!r} must resolve exactly once")
    return matches[0]


def repository_package(api: ManagementApi, step: dict[str, Any], global_domain: str, timeout: int) -> str:
    source = Path(str(step.get("source_path") or ""))
    if not source.name:
        raise RuntimeError("install/upgrade step is missing source_path")
    desired = api_package_name(step)
    existing = repository_inventory(api, global_domain)
    if desired in existing:
        print(f"Repository package already present: {desired}")
        return desired
    command = (
        f"add repository-package name {quote(source.name)} "
        f"path {quote(str(source.parent))} source local"
    )
    api.task_call(global_domain, command, timeout)
    ids = repository_inventory(api, global_domain)
    stem = re.sub(r"\.(?:tar|tgz)$", "", source.name, flags=re.I).lower()
    matches = [name for name in ids if re.sub(r"\.(?:tar|tgz)$", "", name, flags=re.I).lower() == stem]
    if len(matches) != 1:
        raise RuntimeError(f"repository import did not expose exactly one package identity for {source.name}: {matches}")
    print(f"Repository package identity: {matches[0]}")
    return matches[0]


def inventory(api: ManagementApi, domain: str, cluster: str) -> dict[str, Any]:
    return api.call(
        domain,
        f"show-software-packages-per-targets targets.1 {quote(cluster)} display.installed any",
        timeout=900,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activity-plan-file", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--phase", choices=["stage-files", "first-member", "second-member"], required=True)
    parser.add_argument("--operation", choices=["repository", "verify", "execute"], required=True)
    parser.add_argument("--global-domain", default="Global")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get("checkpoint") or {}
    execution = plan.get("execution") or {}
    if execution.get("deployment_backend") != "api":
        raise SystemExit("ERROR: activity plan is not authorized for the Management Web API backend")
    if checkpoint.get("cluster_mode") != "cluster":
        raise SystemExit("ERROR: Management Web API backend currently requires a cluster")
    if len(checkpoint.get("members") or []) != 2:
        raise SystemExit("ERROR: Management Web API backend currently requires exactly two cluster members")
    if len(plan.get("package_steps") or []) != 1:
        raise SystemExit("ERROR: Management Web API backend currently requires exactly one package step")
    step = select_step(plan, args.step)
    domain = str(checkpoint.get("domain") or "")
    cma_name = str(checkpoint.get("cma_name") or "")
    cluster = str(checkpoint.get("cluster_name") or "")
    mds_host = str(checkpoint.get("mds_host") or "")
    if not all((domain, cma_name, cluster, mds_host)):
        raise SystemExit("ERROR: activity plan is missing MDS/CMA domain or cluster identity")
    password = os.environ.get("CP_PASSWORD", "")
    expert_password = os.environ.get("CP_EXPERT_PASSWORD", "")
    if not password or not expert_password:
        raise SystemExit("ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required")

    members = checkpoint.get("members") or []
    cp_args = c.parse_args([
        "--members", *[str(row.get("ip")) for row in members[:2]],
        "--username", args.username, "--phase", "precheck",
    ])
    session = c.connect(cp_args, mds_host)
    try:
        session.enter_expert(expert_password)
        print(session.run(f"mdsenv {quote(cma_name)}", timeout=120).output.rstrip())
        api = ManagementApi(session)
        is_major = plan.get("change", {}).get("activity_type") == "Major Version Upgrade"
        if args.operation == "repository":
            if step.get("action") not in {"install", "upgrade"}:
                raise RuntimeError("repository operation is valid only for install/upgrade steps")
            desired = api_package_name(step)
            if desired not in repository_inventory(api, args.global_domain):
                validate_api_workspace(
                    session,
                    step,
                    operation="repository",
                    is_major=is_major,
                )
            repository_package(api, step, args.global_domain, args.timeout)
            return 0

        current_inventory = inventory(api, domain, cluster)
        identity_source = "Management API inventory"
        if step.get("action") in {"remove", "uninstall"}:
            if any(member_installed_sets(current_inventory)):
                package_name = resolve_remove_identity(current_inventory, step, checkpoint)
            else:
                print(
                    "WARNING: Management API installed-package inventory is empty; "
                    "using the MDS CPRID/CPInstLog identity fallback."
                )
                package_name = resolve_remove_identity_via_cprid(
                    api,
                    session,
                    args.global_domain,
                    checkpoint,
                    step,
                    args.step,
                )
                identity_source = "gateway CPInstLog via MDS CPRID"
        else:
            package_name = api_package_name(step)
        if not package_name:
            raise RuntimeError("could not determine Management API package identity")
        print(f"Resolved API package identity: {package_name} ({identity_source})")

        if args.operation == "verify":
            if step.get("action") in {"remove", "uninstall"}:
                print(f"Removal verification passed using {identity_source}: {package_name}")
                print(json.dumps(current_inventory, indent=2, sort_keys=True))
                return 0
            command = (
                f"verify-software-package name {quote(package_name)} targets.1 {quote(cluster)} "
                "download-package true download-package-from central"
            )
            result = api.task_call(domain, command, args.timeout)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if step.get("action") in {"remove", "uninstall"}:
            raise RuntimeError(
                "Management API uninstall has no safe per-member cluster strategy on this API version; "
                "use the workflow's guarded direct CPUSE removal fallback"
            )
        if not args.execute:
            print("Management API execution is disabled; pass --execute only after workflow authorization.")
            return 3
        validate_api_workspace(
            session,
            step,
            operation="execute",
            is_major=is_major,
        )
        strategy = (
            "non-active-members-and-failover"
            if args.phase == "first-member" and not is_major
            else "non-active-members-no-failover"
        )
        if step.get("action") in {"remove", "uninstall"}:
            command = f"uninstall-software-package name {quote(package_name)} targets.1 {quote(cluster)}"
        else:
            method = "upgrade" if step.get("action") == "upgrade" or step.get("package_type") in {"blink", "major_upgrade", "blink_image"} else "install"
            command = (
                f"install-software-package name {quote(package_name)} targets.1 {quote(cluster)} "
                f"method {method} package-location central"
            )
        command += (
            " cluster-installation-settings.cluster-delay 0"
            f" cluster-installation-settings.cluster-strategy {strategy}"
        )
        print(f"Cluster strategy: {strategy}")
        run_mds = lambda command, timeout=120: session.run(command, timeout=timeout).output
        major_baseline_completed: int | None = None
        if is_major:
            major_baseline_completed, baseline_states = major_upgrade_completed_count(
                run_mds, checkpoint, step
            )
            expected_baseline = 0 if args.phase == "first-member" else len(members) - 1
            print("===== Major upgrade pre-execution member state =====")
            print(json.dumps(baseline_states, indent=2, sort_keys=True))
            if major_baseline_completed != expected_baseline:
                raise RuntimeError(
                    "major upgrade phase has an unexpected completed-member baseline: "
                    f"phase={args.phase} completed={major_baseline_completed} "
                    f"expected={expected_baseline}"
                )
        try:
            result = api.task_call(domain, command, args.timeout)
        except RuntimeError as exc:
            if not (
                is_major
                and "ended with status failed" in str(exc)
                and major_baseline_completed is not None
                and major_upgrade_completed_despite_api_failure(
                    run_mds,
                    checkpoint,
                    step,
                    args.phase,
                    major_baseline_completed,
                )
            ):
                raise
            print(
                "WARNING: Management API reported terminal failure after the expected "
                "rolling member count reached the target version and exact Blink identity; "
                "accepting the gateway state as reconciled completion."
            )
            result = {
                "status": "reconciled-success",
                "api_error": str(exc),
                "phase": args.phase,
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        print("===== Post-operation package inventory =====")
        print(json.dumps(inventory(api, domain, cluster), indent=2, sort_keys=True))
        return 0
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
