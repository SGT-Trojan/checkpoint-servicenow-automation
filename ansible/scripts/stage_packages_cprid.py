#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import shlex
import sys
from pathlib import Path

for candidate in [
    Path(__file__).resolve().parents[2],
    Path(__file__).resolve().parents[2] / 'tools',
    Path(__file__).resolve().parents[3] / 'tools',
]:
    sys.path.insert(0, str(candidate))
import checkpoint_cluster_upgrade as c  # noqa: E402

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def package_steps(plan: dict) -> list[dict]:
    return [step for step in plan.get('package_steps', []) if step.get('action', 'install') in {'install', 'upgrade'}]


def package_source_dir(plan: dict) -> str:
    execution = plan.get('execution') or {}
    return execution.get('package_source_dir') or '/var/log/tmp'


def resolve_mds_package_path(plan: dict, step: dict) -> str:
    raw = (step.get('source_path') or step.get('package_name') or '').strip()
    if not raw:
        return ''
    if raw.startswith('/'):
        return raw
    return str(Path(package_source_dir(plan)) / raw)


def resolve_gateway_package_path(plan: dict, step: dict) -> str:
    raw = (step.get('source_path') or step.get('package_name') or '').strip()
    if not raw:
        return ''
    if raw.startswith('/'):
        return raw
    return str(Path(package_source_dir(plan)) / Path(raw).name)


def member_cprid_ip(member: dict) -> str:
    return (member.get('management_ip') or member.get('ip') or member.get('access_ip') or '').strip()


def cprid_mkdir_command(ip: str, remote_dir: str) -> str:
    return ' '.join([
        'cprid_util', 'mkdir',
        '-dir', shlex.quote(remote_dir),
        '-perms', '0755',
        '-server', shlex.quote(ip),
    ])


def cprid_putfile_command(ip: str, local_file: str, remote_file: str) -> str:
    return ' '.join([
        'cprid_util', 'putfile',
        '-local_file', shlex.quote(local_file),
        '-remote_file', shlex.quote(remote_file),
        '-perms', '0644',
        '-server', shlex.quote(ip),
    ])


def cprid_file_stat_command(ip: str, remote_file: str) -> str:
    return ' '.join([
        'cprid_util', 'file_stat',
        '-remote_file', shlex.quote(remote_file),
        '-server', shlex.quote(ip),
    ])


def verification_script(path: str) -> str:
    script = """from pathlib import Path
import hashlib
import json
p = Path(%r)
exists = p.exists()
size = p.stat().st_size if exists else 0
h1 = hashlib.sha1()
h256 = hashlib.sha256()
if exists:
    with p.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h1.update(chunk)
            h256.update(chunk)
print(json.dumps({'path': str(p), 'exists': exists, 'size': size, 'sha1': h1.hexdigest() if exists else '', 'sha256': h256.hexdigest() if exists else ''}))
""" % path
    launcher = "import base64; exec(base64.b64decode('" + base64.b64encode(script.encode()).decode() + "'))"
    return 'python3 -c ' + shlex.quote(launcher)


def parse_json_line(output: str) -> dict | None:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def member_ssh_ip(member: dict) -> str:
    return (member.get('access_ip') or member.get('ip') or member.get('management_ip') or '').strip()


def verify_gateway_package_over_ssh(member: dict, username: str, path: str) -> dict | None:
    host = member_ssh_ip(member)
    if not host:
        return None
    cp_args = c.parse_args(['--members', host, host, '--username', username, '--phase', 'precheck'])
    session = c.connect(cp_args, host)
    try:
        session.enter_expert(cp_args.expert_password)
        output = session.run(verification_script(path), timeout=600).output
        print(f'===== {host}: verify staged package {path} =====')
        print(output.rstrip())
        return parse_json_line(output)
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Stage Check Point packages from MDS to gateways using CPRID.')
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--username', default='admin')
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get('checkpoint') or {}
    execution = plan.get('execution') or {}
    members = checkpoint.get('members') or []
    mds_host = checkpoint.get('mds_host')
    cma_name = checkpoint.get('cma_name') or ''
    staging_method = execution.get('staging_method') or ''
    execution_method = execution.get('method') or ''

    if staging_method != 'cprid_from_mds':
        print(f'CPRID staging skipped: staging_method={staging_method!r}')
        return 0
    if 'CDT' in execution_method:
        print('CPRID staging skipped: CDT executes from the MDS package path and does not require gateway-side package copy.')
        return 0
    if not mds_host:
        print('ERROR: mds_host is required for CPRID staging', file=sys.stderr)
        return 2
    if not members:
        print('ERROR: at least one target gateway member is required for CPRID staging', file=sys.stderr)
        return 2

    steps = package_steps(plan)
    if not steps:
        print('No install/upgrade package steps require CPRID staging.')
        return 0

    first_member_ip = member_cprid_ip(members[0]) or '127.0.0.1'
    cp_args = c.parse_args(['--members', first_member_ip, first_member_ip, '--username', args.username, '--phase', 'precheck'])
    session = c.connect(cp_args, mds_host)
    failures = 0

    def run(command: str, timeout: int = 600) -> str:
        print(f'===== MDS: {command} =====', flush=True)
        result = session.run(command, timeout=timeout)
        print(result.output.rstrip(), flush=True)
        return result.output

    try:
        session.enter_expert(cp_args.expert_password)
        if cma_name:
            run('mdsenv ' + shlex.quote(cma_name), timeout=120)
        for step in steps:
            mds_path = resolve_mds_package_path(plan, step)
            gateway_path = resolve_gateway_package_path(plan, step)
            if not mds_path or not gateway_path:
                print(f"ERROR: step {step.get('name')} has no resolvable package path", file=sys.stderr)
                failures += 1
                continue

            mds_check = run('test -f ' + shlex.quote(mds_path) + ' && ls -l ' + shlex.quote(mds_path), timeout=120)
            if 'No such file' in mds_check or 'cannot access' in mds_check:
                print(f'ERROR: source package missing on MDS: {mds_path}', file=sys.stderr)
                failures += 1
                continue

            parent = str(Path(gateway_path).parent)
            for member in members:
                ip = member_cprid_ip(member)
                name = member.get('hostname') or member.get('object_name') or ip
                if not ip:
                    print(f'ERROR: member {name} has no management/access IP for CPRID', file=sys.stderr)
                    failures += 1
                    continue

                print(f'CPRID_STAGE_ACTION member={name} ip={ip} source={mds_path} destination={gateway_path}', flush=True)
                mkdir_output = run(cprid_mkdir_command(ip, parent), timeout=180)
                if any(token in mkdir_output.lower() for token in ['failed', 'error', 'not found', 'denied']):
                    print(f'ERROR: CPRID mkdir failed on {name} ({ip})', file=sys.stderr)
                    failures += 1
                    continue

                put_output = run(cprid_putfile_command(ip, mds_path, gateway_path), timeout=7200)
                if any(token in put_output.lower() for token in ['failed', 'error', 'not found', 'denied']):
                    print(f'ERROR: CPRID putfile failed on {name} ({ip})', file=sys.stderr)
                    failures += 1
                    continue

                stat_output = run(cprid_file_stat_command(ip, gateway_path), timeout=180)
                if 'file_size' not in stat_output or any(token in stat_output.lower() for token in ['failed', 'error', 'not found', 'denied']):
                    print(f'ERROR: CPRID file_stat failed after putfile on {name} ({ip}) for {gateway_path}', file=sys.stderr)
                    failures += 1
                    continue

                data = verify_gateway_package_over_ssh(member, args.username, gateway_path)
                if not data or not data.get('exists'):
                    print(f'ERROR: SSH verification failed after CPRID staging on {name} ({ip}) for {gateway_path}', file=sys.stderr)
                    failures += 1
                    continue
                print(f"Validated gateway package on {name} ({ip}): {data['path']} size={data['size']} sha1={data['sha1']} sha256={data['sha256']}")
                if step.get('checksum_sha1') and step['checksum_sha1'].lower() != data['sha1'].lower():
                    print(f"ERROR: SHA1 mismatch on {name} ({ip}) for {gateway_path}", file=sys.stderr)
                    failures += 1
                if step.get('checksum_sha256') and step['checksum_sha256'].lower() != data['sha256'].lower():
                    print(f"ERROR: SHA256 mismatch on {name} ({ip}) for {gateway_path}", file=sys.stderr)
                    failures += 1
        return 2 if failures else 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
