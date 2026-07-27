#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def parse_build(text: str) -> int | None:
    m = re.search(r'Build number:\s*(\d+)', text)
    return int(m.group(1)) if m else None


def parse_da_package_build(value: str) -> int | None:
    name = Path(value).name
    m = re.search(r'DeploymentAgent[_-]0*(\d+)', name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def package_da_steps(plan: dict) -> list[dict]:
    return [s for s in plan.get('package_steps') or [] if (s.get('package_type') or '').lower() == 'deployment_agent']


def integer_from_any(*values: object) -> int | None:
    for value in values:
        if value in (None, ''):
            continue
        m = re.search(r'\d+', str(value))
        if m:
            return int(m.group(0))
    return None


def remote_file_metadata(session: c.SshPty, path: str) -> dict:
    script = f"""from pathlib import Path
import hashlib
import json
p = Path({path!r})
exists = p.exists()
size = p.stat().st_size if exists else 0
h1 = hashlib.sha1()
h256 = hashlib.sha256()
if exists:
    with p.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h1.update(chunk)
            h256.update(chunk)
print(json.dumps({{
    'path': str(p),
    'exists': exists,
    'size': size,
    'sha1': h1.hexdigest() if exists else '',
    'sha256': h256.hexdigest() if exists else '',
}}))
"""
    encoded = base64.b64encode(script.encode()).decode()
    launcher = "import base64; exec(base64.b64decode('" + encoded + "'))"
    result = session.run('python3 -c ' + json.dumps(launcher), timeout=600)
    print(f'===== MDS: validate offline Deployment Agent package {path} =====')
    print(result.output.rstrip())
    for line in result.output.splitlines():
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            return json.loads(line)
    return {'exists': False, 'path': path, 'size': 0, 'sha1': '', 'sha256': ''}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--minimum-build', nargs='?', default='', const='')
    parser.add_argument('--offline-package-path', nargs='?', default='', const='')
    parser.add_argument('--offline-package-build', nargs='?', default='', const='')
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get('checkpoint') or {}
    execution = plan.get('execution') or {}
    members = checkpoint.get('members') or []
    if not members:
        print('ERROR: activity plan has no checkpoint.members', file=sys.stderr)
        return 2

    da_steps = package_da_steps(plan)
    da_step = da_steps[0] if da_steps else {}
    offline_path = args.offline_package_path or da_step.get('source_path') or execution.get('deployment_agent_package_path') or checkpoint.get('deployment_agent_package_path') or ''
    required_build = integer_from_any(
        args.minimum_build,
        execution.get('minimum_deployment_agent_build'),
        checkpoint.get('minimum_deployment_agent_build'),
        da_step.get('minimum_build'),
        da_step.get('requires_present'),
    )
    offline_build = integer_from_any(args.offline_package_build, execution.get('deployment_agent_package_build'), da_step.get('expected_build'))
    if offline_path and offline_build is None:
        offline_build = parse_da_package_build(offline_path)
    if required_build is None and da_step and (da_step.get('action') or 'install').lower() in {'install', 'upgrade'} and offline_build is not None:
        required_build = offline_build

    cp_args = c.parse_args(['--members', members[0]['ip'], members[-1]['ip'], '--username', args.username, '--phase', 'precheck'])

    print('===== Deployment Agent readiness contract =====')
    print(f"Required minimum DA build: {required_build if required_build is not None else 'not declared'}")
    print(f"Offline DA package path: {offline_path or 'not declared'}")
    print(f"Offline DA package build: {offline_build if offline_build is not None else 'unknown'}")
    print('Production air-gap note: cloud DA auto-update status is informational only; offline package readiness controls remediation.')

    failures = 0
    installed = []
    for member in members:
        host = member['ip']
        session = c.connect(cp_args, host)
        try:
            out = session.run('show installer status all', timeout=180).output
            print(f'===== {host}: show installer status all =====')
            print(out.rstrip())
            build = parse_build(out)
            installed.append({'host': host, 'build': build})
            if build is None:
                print(f'ERROR: could not parse installed DA build on {host}', file=sys.stderr)
                failures += 1
            elif required_build is not None and build < required_build:
                print(f'{host}: installed DA build {build} is below required build {required_build}')
            elif required_build is not None:
                print(f'{host}: installed DA build {build} satisfies required build {required_build}')
            else:
                print(f'{host}: installed DA build {build}; no hard minimum declared')
        finally:
            session.close()

    needs_update = [row for row in installed if row['build'] is not None and required_build is not None and row['build'] < required_build]

    if required_build is None:
        print('WARNING: no minimum_deployment_agent_build was declared. DA readiness is informational only.')
        return 2 if failures else 0

    if needs_update:
        if not offline_path:
            print('ERROR: one or more gateways need a DA update, but no offline Deployment Agent package path was declared.', file=sys.stderr)
            failures += 1
        if offline_build is None:
            print('ERROR: offline Deployment Agent package build could not be determined. Provide deployment_agent_package_build/minimum_deployment_agent_build in the activity plan.', file=sys.stderr)
            failures += 1
        elif offline_build < required_build:
            print(f'ERROR: offline DA package build {offline_build} is below required build {required_build}. Provide a newer offline package before proceeding.', file=sys.stderr)
            failures += 1

        if offline_path and checkpoint.get('mds_host'):
            mds = c.connect(cp_args, checkpoint['mds_host'])
            try:
                mds.enter_expert(cp_args.expert_password)
                metadata = remote_file_metadata(mds, offline_path)
                if not metadata.get('exists'):
                    print(f'ERROR: offline Deployment Agent package not found on MDS: {offline_path}', file=sys.stderr)
                    failures += 1
                else:
                    print(f"Validated offline DA package on MDS: {offline_path} size={metadata['size']} sha1={metadata['sha1']} sha256={metadata['sha256']}")
                    if da_step.get('checksum_sha1') and da_step['checksum_sha1'].lower() != metadata['sha1'].lower():
                        print('ERROR: offline DA package SHA1 mismatch', file=sys.stderr)
                        failures += 1
                    if da_step.get('checksum_sha256') and da_step['checksum_sha256'].lower() != metadata['sha256'].lower():
                        print('ERROR: offline DA package SHA256 mismatch', file=sys.stderr)
                        failures += 1
            finally:
                mds.close()

        if failures == 0:
            print('DA update is required and a suitable offline package is available. Run the install-deployment-agent step before CDT/CPUSE package execution.')
    else:
        print('All gateways satisfy the required DA build. Offline DA remediation is not required for this activity.')

    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
