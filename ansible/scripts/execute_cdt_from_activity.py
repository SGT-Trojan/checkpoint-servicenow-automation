#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def py_string(value: str) -> str:
    return repr(value)


def parse_candidates(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) != 6:
            continue
        if parts[0] in {'Object Name', ''}:
            continue
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', parts[2]):
            continue
        rows.append({
            'object_name': parts[0],
            'cluster_name': parts[1],
            'ip_address': parts[2],
            'version_jhf_take': parts[3],
            'state': parts[4].lower(),
            'upgrade_order': parts[5],
        })
    return rows


def validate_candidate_identity(
    rows: list[dict[str, str]], members: list[dict], cluster_name: str
) -> None:
    expected_ips = {str(member.get("management_ip") or member.get("ip") or "") for member in members}
    expected_names = {
        str(member.get("hostname") or member.get("object_name") or "") for member in members
    }
    expected_ips.discard("")
    expected_names.discard("")
    if len(expected_ips) != 2 or {row["ip_address"] for row in rows} != expected_ips:
        raise ValueError("candidate IP identities do not match the two activity-plan members")
    if expected_names and {row["object_name"] for row in rows} != expected_names:
        raise ValueError("candidate object identities do not match the activity-plan members")
    if not cluster_name or any(row["cluster_name"] != cluster_name for row in rows):
        raise ValueError("candidate cluster identity does not match the activity plan")
    enabled = [row for row in rows if row["upgrade_order"] == "1"]
    if len(enabled) != 1 or enabled[0]["state"] != "standby":
        raise ValueError("enabled CDT candidate must be the current standby member")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--step')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--plan-path', required=True)
    parser.add_argument('--candidates-path', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--timeout', type=int, default=14400)
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get('checkpoint', {})
    members = checkpoint.get('members') or []
    if len(members) != 2:
        print('ERROR: CDT execution currently expects exactly two cluster members', file=sys.stderr)
        return 2

    mds_host = checkpoint.get('mds_host')
    cma_env = checkpoint.get('cma_name')
    cma_ip = checkpoint.get('cma_ip')
    cluster_name = checkpoint.get('cluster_name')
    missing = [name for name, value in {'mds_host': mds_host, 'cma_name': cma_env, 'cma_ip': cma_ip, 'cluster_name': cluster_name}.items() if not value]
    if missing:
        print(f'ERROR: activity plan missing required fields: {", ".join(missing)}', file=sys.stderr)
        return 2

    cp_args = c.parse_args([
        '--members', members[0]['ip'], members[1]['ip'],
        '--username', args.username,
        '--phase', 'precheck',
    ])
    session = c.connect(cp_args, mds_host)

    def run(command: str, timeout: int = 300) -> str:
        result = session.run(command, timeout=timeout)
        print(f'===== MDS: {command} =====')
        print(result.output.rstrip())
        return result.output

    def read_remote_text(path: str) -> str:
        command = "python3 -c \"from pathlib import Path; print(Path(%s).read_bytes().hex())\"" % py_string(path)
        output = run(command, timeout=120)
        for line in output.splitlines():
            line = line.strip()
            if line and len(line) % 2 == 0 and re.fullmatch(r'[0-9a-fA-F]+', line):
                return bytes.fromhex(line).decode()
        raise RuntimeError(f'could not read clean hex content from {path}')

    try:
        session.enter_expert(cp_args.expert_password)
        run(f'mdsenv {cma_env}', timeout=120)
        run(f'ls -l {shell_quote(args.plan_path)} {shell_quote(args.candidates_path)}', timeout=120)
        candidate_text = read_remote_text(args.candidates_path)
        print(f'===== Controlled candidate file: {args.candidates_path} =====')
        print(candidate_text.rstrip())
        rows = parse_candidates(candidate_text)
        enabled = [row for row in rows if row['upgrade_order'] == '1']
        disabled = [row for row in rows if row['upgrade_order'] == '-']
        if len(rows) != 2:
            print(f'ERROR: expected exactly 2 candidates, got {len(rows)}', file=sys.stderr)
            return 2
        if len(enabled) != 1 or len(disabled) != 1:
            print('ERROR: controlled candidate file must have exactly one enabled member and one disabled peer', file=sys.stderr)
            return 2
        try:
            validate_candidate_identity(rows, members, cluster_name)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Selected execution target: {enabled[0]['object_name']} {enabled[0]['ip_address']} ({enabled[0]['state']})")

        execute_cmd = (
            f'/opt/CPcdt/CentralDeploymentTool -execute '
            f'-candidates={shell_quote(args.candidates_path)} '
            f'-deploymentplan={shell_quote(args.plan_path)} '
            f'-server={cma_ip}'
        )
        if not args.execute:
            print('CDT execution is disabled. Planned command only:')
            print(execute_cmd)
            return 3

        output = run(execute_cmd, timeout=args.timeout)
        lower = output.lower()
        fatal_markers = [
            'candidate list error has occurred',
            'an error has occurred in stage',
            'entity: ',
            'installation failed',
            'execution finished with errors',
        ]
        nonfatal_mail_error = (
            'failed to send mail' in lower
            or 'mail message failure' in lower
            or 'email server is not configured' in lower
            or 'notification email will not be sent' in lower
        )
        if any(marker in lower for marker in fatal_markers):
            print('ERROR: CDT reported a fatal execution error', file=sys.stderr)
            return 2
        if 'error' in lower and not nonfatal_mail_error:
            print('ERROR: CDT output contains an unclassified error', file=sys.stderr)
            return 2
        return 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
