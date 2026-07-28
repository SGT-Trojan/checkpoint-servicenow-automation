#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def package_steps(plan: dict) -> list[dict]:
    return [step for step in plan.get('package_steps', []) if step.get('action', 'install') in {'install', 'upgrade'}]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--username', default='admin')
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get('checkpoint', {})
    members = checkpoint.get('members') or []
    mds_host = checkpoint.get('mds_host')
    if not mds_host:
        print('ERROR: mds_host is required for MDS package validation', file=sys.stderr)
        return 2
    if not members:
        print('ERROR: at least one gateway member is required', file=sys.stderr)
        return 2

    cp_args = c.parse_args(['--members', members[0]['ip'], members[-1]['ip'], '--username', args.username, '--phase', 'precheck'])
    session = c.connect(cp_args, mds_host)

    def run(command: str, timeout: int = 180) -> str:
        result = session.run(command, timeout=timeout)
        print(f'===== MDS: {command} =====')
        print(result.output.rstrip())
        return result.output

    try:
        session.enter_expert(cp_args.expert_password)
        steps = package_steps(plan)
        if not steps:
            print('No install/upgrade package steps require MDS validation.')
            return 0
        failures = 0
        for step in steps:
            if not (step.get("checksum_sha1") or step.get("checksum_sha256")):
                print(
                    f"ERROR: step {step.get('name')} requires a published SHA1 or SHA256 checksum",
                    file=sys.stderr,
                )
                failures += 1
                continue
            path = step.get('source_path') or step.get('package_name')
            if not path:
                print(f"ERROR: step {step.get('name')} has no source_path", file=sys.stderr)
                failures += 1
                continue
            remote_script = f"""from pathlib import Path
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
            encoded_script = base64.b64encode(remote_script.encode()).decode()
            launcher = "import base64; exec(base64.b64decode('" + encoded_script + "'))"
            check_cmd = "python3 -c " + json.dumps(launcher)
            output = run(check_cmd, timeout=600)
            data = None
            for line in output.splitlines():
                line = line.strip()
                if line.startswith('{') and line.endswith('}'):
                    data = json.loads(line)
                    break
            if not data or not data.get('exists'):
                print(f'ERROR: package not found on MDS: {path}', file=sys.stderr)
                failures += 1
                continue
            print(f"Validated package on MDS: {path} size={data['size']} sha1={data['sha1']} sha256={data['sha256']}")
            if step.get('checksum_sha1') and step['checksum_sha1'].lower() != data['sha1'].lower():
                print(f"ERROR: SHA1 mismatch for {path}", file=sys.stderr)
                failures += 1
            if step.get('checksum_sha256') and step['checksum_sha256'].lower() != data['sha256'].lower():
                print(f"ERROR: SHA256 mismatch for {path}", file=sys.stderr)
                failures += 1
        return 2 if failures else 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
