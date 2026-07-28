#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def parse_candidates(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            continue
        if parts[0] in {"Object Name", ""}:
            continue
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[2]):
            continue
        rows.append(
            {
                "object_name": parts[0],
                "cluster_name": parts[1],
                "ip_address": parts[2],
                "version_jhf_take": parts[3],
                "state": parts[4],
                "upgrade_order": parts[5],
            }
        )
    return rows


def render_candidates(rows: list[dict[str, str]]) -> str:
    lines = [
        "Object Name,Cluster Name,IP Address,Version/JHF Take,State,Upgrade Order",
    ]
    for row in rows:
        lines.append(
            ",".join(
                [
                    row["object_name"],
                    row["cluster_name"],
                    row["ip_address"],
                    row["version_jhf_take"],
                    row["state"],
                    row["upgrade_order"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def render_preserving_cdt_format(text: str, orders_by_ip: dict[str, str]) -> str:
    output: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) == 6:
            stripped = [part.strip() for part in parts]
            ip_address = stripped[2]
            if ip_address in orders_by_ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip_address):
                parts[-1] = f"{orders_by_ip[ip_address]:>14}"
                line = ",".join(parts)
                seen.add(ip_address)
        output.append(line)
    missing = set(orders_by_ip) - seen
    if missing:
        raise c.CheckPointError(f"Could not update candidate rows for: {', '.join(sorted(missing))}")
    return "\n".join(output) + "\n"


def clean_cdt_candidate_text(text: str) -> str:
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip() == "Central Deployment Tool Candidates List:":
            start = index
            break
    cleaned: list[str] = []
    for line in lines[start:]:
        if line.startswith("[Expert@"):
            break
        cleaned.append(line)
    return "\n".join(cleaned) + "\n"


def build_cp_args(args: argparse.Namespace) -> argparse.Namespace:
    return c.parse_args(
        [
            "--members",
            args.members[0],
            args.members[1],
            "--username",
            args.username,
            "--phase",
            "precheck",
            "--icap-mode",
            args.icap_mode,
            "--execute",
        ]
    )


def collect_state(args: argparse.Namespace) -> dict[str, object]:
    cp_args = build_cp_args(args)
    gateways = c.run_precheck(cp_args)
    active = c.choose_active(gateways)
    standby = c.choose_standby(gateways)
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "original_active_host": active.host,
        "original_active_name": active.name,
        "original_standby_host": standby.host,
        "original_standby_name": standby.name,
        "members": [
            {
                "host": gw.host,
                "name": gw.name,
                "state": gw.local_state,
                "pnotes_ok": gw.pnotes_ok,
                "interfaces_ok": gw.interfaces_ok,
                "icap_ok": gw.icap_ok,
                "required_interfaces": gw.required_interfaces,
                "required_secured_interfaces": gw.required_secured_interfaces,
                "cluster_interfaces": gw.cluster_interfaces,
                "virtual_cluster_interfaces": gw.virtual_cluster_interfaces,
                "cluster_interface_signature": c.cluster_interface_signature(gw),
            }
            for gw in gateways
        ],
    }


def write_state(args: argparse.Namespace) -> int:
    state = collect_state(args)
    path = Path(args.state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def read_state(args: argparse.Namespace) -> dict[str, object]:
    return json.loads(Path(args.state_file).read_text(encoding="utf-8"))


def mds_run(args: argparse.Namespace, command: str, timeout: int = 300) -> str:
    cp_args = build_cp_args(args)
    session = c.connect(cp_args, args.mds_host)
    try:
        session.enter_expert(cp_args.expert_password)
        result = session.run(command, timeout=timeout)
        print(f"===== MDS: {command} =====")
        print(result.output.rstrip())
        return result.output
    finally:
        session.close()


def prepare_candidates(args: argparse.Namespace) -> int:
    state = read_state(args)
    if args.phase == "phase1":
        target_host = str(state["original_standby_host"])
    elif args.phase == "phase2":
        target_host = str(state["original_active_host"])
    else:
        raise ValueError(args.phase)

    text = clean_cdt_candidate_text(mds_run(args, f"cat {args.source_candidates}", timeout=120))
    rows = parse_candidates(text)
    if {row["ip_address"] for row in rows} != set(args.members):
        print("ERROR: source candidate IPs do not match expected members", file=sys.stderr)
        return 2
    if target_host not in {row["ip_address"] for row in rows}:
        print(f"ERROR: target host {target_host} not present in candidate list", file=sys.stderr)
        return 2

    orders_by_ip: dict[str, str] = {}
    for row in rows:
        row["upgrade_order"] = "1" if row["ip_address"] == target_host else "-"
        orders_by_ip[row["ip_address"]] = row["upgrade_order"]

    output = render_preserving_cdt_format(text, orders_by_ip)
    hex_text = output.encode().hex()
    mds_run(
        args,
        (
            "python3 -c "
            f"\"from pathlib import Path; Path('{args.dest_candidates}').write_bytes(bytes.fromhex('{hex_text}'))\""
        ),
        timeout=120,
    )
    mds_run(args, f"cat {args.dest_candidates}", timeout=120)
    print(
        json.dumps(
            {
                "phase": args.phase,
                "target_host": target_host,
                "dest_candidates": args.dest_candidates,
                "rows": rows,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def wait_for_target_active(
    cp_args: argparse.Namespace,
    target_host: str,
    old_active_host: str | None,
    description: str,
    timeout: int,
) -> list[c.Gateway]:
    deadline = time.time() + timeout
    last_gateways: list[c.Gateway] = []
    while time.time() < deadline:
        last_gateways = c.collect_gateways(cp_args)
        by_host = {gw.host: gw for gw in last_gateways}
        target = by_host.get(target_host)
        old_active = by_host.get(old_active_host) if old_active_host else None
        if target and target.local_state.upper().startswith("ACTIVE"):
            if old_active is None or old_active.local_state.upper() in {"DOWN", "STANDBY"}:
                return last_gateways
        time.sleep(5)
    raise c.CheckPointError(f"Timed out waiting for {description}")


def failover_to(args: argparse.Namespace, target_host: str) -> int:
    cp_args = build_cp_args(args)
    gateways = c.run_precheck(cp_args)
    active = c.choose_active(gateways)
    if active.host == target_host:
        print(f"{target_host} is already ACTIVE")
        return 0

    if target_host not in {gw.host for gw in gateways}:
        print(f"ERROR: target host {target_host} is not a cluster member", file=sys.stderr)
        return 2

    print(f"Moving ACTIVE state from {active.host} to {target_host}")
    c.clusterxl_admin(cp_args, active.host, "down")
    wait_for_target_active(
        cp_args,
        target_host,
        active.host,
        f"{target_host} to become ACTIVE after {active.host} was administratively down",
        args.failover_wait_seconds,
    )
    print(f"Returning {active.host} to normal ClusterXL operation")
    c.clusterxl_admin(cp_args, active.host, "up")
    wait_for_target_active(
        cp_args,
        target_host,
        active.host,
        f"{target_host} to stay ACTIVE after {active.host} was returned to normal operation",
        args.failover_wait_seconds,
    )
    return 0


def restore_original_active(args: argparse.Namespace) -> int:
    state = read_state(args)
    return failover_to(args, str(state["original_active_host"]))


def first_take(output: str) -> str:
    jumbo = re.findall(r"(?:HOTFIX|BUNDLE)_R(?:\d+_?\d*)_JUMBO_HF_MAIN\s+Take:\s+(\d+)", output)
    if jumbo:
        return jumbo[-1]
    if "No hotfixes" in output:
        return "0"
    return "unknown"


def assert_member_take(args: argparse.Namespace) -> int:
    cp_args = build_cp_args(args)
    session = c.connect(cp_args, args.target_host)
    try:
        session.enter_expert(cp_args.expert_password)
        result = session.run(
            "cpinfo -y all | egrep -i '(HOTFIX|BUNDLE)_R[0-9_]+_JUMBO_HF_MAIN|No hotfixes' | head -120",
            timeout=120,
        )
        take = first_take(result.output)
    finally:
        session.close()

    print(json.dumps({"host": args.target_host, "take": take, "expected_take": str(args.target_take)}))
    if str(take) != str(args.target_take):
        print(f"ERROR: {args.target_host} reported take {take}, expected {args.target_take}", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "capture-state",
            "prepare-candidates",
            "failover-to",
            "restore-original-active",
            "assert-member-take",
        ],
    )
    parser.add_argument("--members", nargs=2, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--icap-mode", choices=["required", "optional", "disabled"], default="optional")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--mds-host", default="")
    parser.add_argument("--source-candidates", default="")
    parser.add_argument("--dest-candidates", default="")
    parser.add_argument("--phase", choices=["phase1", "phase2"], default="phase1")
    parser.add_argument("--target-host", default="")
    parser.add_argument("--target-take", default="")
    parser.add_argument("--failover-wait-seconds", type=int, default=600)
    args = parser.parse_args()

    if args.action == "capture-state":
        return write_state(args)
    if args.action == "prepare-candidates":
        if not args.mds_host or not args.source_candidates or not args.dest_candidates:
            parser.error("--mds-host, --source-candidates, and --dest-candidates are required")
        return prepare_candidates(args)
    if args.action == "failover-to":
        if not args.target_host:
            parser.error("--target-host is required")
        return failover_to(args, args.target_host)
    if args.action == "restore-original-active":
        return restore_original_active(args)
    if args.action == "assert-member-take":
        if not args.target_host:
            parser.error("--target-host is required")
        if not str(args.target_take).strip():
            parser.error("--target-take is required")
        return assert_member_take(args)
    raise ValueError(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
