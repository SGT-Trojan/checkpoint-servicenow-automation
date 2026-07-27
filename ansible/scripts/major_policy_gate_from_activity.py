#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def shell_quote(v: str) -> str:
    return shlex.quote(v)


def json_from_output(output: str) -> dict:
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith('{'):
            text = '\n'.join(lines[i:])
            end = text.rfind('}')
            if end >= 0:
                return json.loads(text[:end + 1])
    return {}


def run(session, command: str, timeout: int = 600) -> str:
    out = session.run(command, timeout=timeout).output
    print(f'===== MDS: {command} =====')
    print(out.rstrip())
    return out


def api_json(session, command: str, timeout: int = 600) -> dict:
    output = run(session, command, timeout=timeout)
    data = json_from_output(output)
    if not data:
        detail = output.strip().splitlines()[-1] if output.strip() else 'empty output'
        raise RuntimeError(f'management API command failed: {detail}')
    if data.get('code') and data.get('message'):
        raise RuntimeError(f"management API command failed: {data['code']}: {data['message']}")
    return data


def wait_task(session, domain: str, task_id: str, timeout: int = 1800) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        data = api_json(session, f'mgmt_cli -r true -d {shell_quote(domain)} show-task task-id {shell_quote(task_id)} --format json', timeout=300)
        last = data
        status = str(data.get('tasks', [{}])[0].get('status') or data.get('status') or '').lower()
        if status and status not in {'in progress', 'in-progress', 'running'}:
            return data
        time.sleep(15)
    raise TimeoutError(f'task {task_id} did not finish; last={last}')


def install_policy(session, domain: str, package: str, target: str, allow_partial: bool, layer: str = 'access') -> dict:
    partial = 'false' if allow_partial else 'true'
    if layer not in {'access', 'threat-prevention'}:
        raise ValueError(f'unsupported policy layer: {layer}')
    access, threat = ('true', 'false') if layer == 'access' else ('false', 'true')
    cmd = (
        f'mgmt_cli -r true -d {shell_quote(domain)} install-policy '
        f'policy-package {shell_quote(package)} targets {shell_quote(target)} '
        f'access {access} threat-prevention {threat} install-on-all-cluster-members-or-fail {partial} --format json'
    )
    data = api_json(session, cmd, timeout=600)
    task_id = data.get('task-id') or data.get('tasks', [{}])[0].get('task-id') or ''
    if not task_id:
        raise RuntimeError('install-policy did not return a task-id')
    return wait_task(session, domain, task_id)


def set_cluster_version(session, domain: str, cluster: str, version: str) -> None:
    api_json(session, f'mgmt_cli -r true -d {shell_quote(domain)} set simple-cluster name {shell_quote(cluster)} version {shell_quote(version)} --format json', timeout=300)
    data = api_json(session, f'mgmt_cli -r true -d {shell_quote(domain)} publish --format json', timeout=300)
    task_id = data.get('task-id') or ''
    if task_id:
        wait_task(session, domain, task_id, timeout=900)


def summarize_task(label: str, data: dict, expected_partial: bool) -> None:
    print(f'===== {label} task summary =====')
    print(json.dumps(data, indent=2, sort_keys=True))
    if not data:
        raise RuntimeError(f'{label}: policy task returned no result')
    blob = json.dumps(data).lower()
    if expected_partial:
        if 'failed' not in blob and 'warning' not in blob and 'succeeded with warnings' not in blob:
            print('WARN: mixed-version policy install did not show expected partial/warning markers')
    else:
        if 'failed' in blob or 'succeeded with warnings' in blob:
            raise RuntimeError(f'{label}: final policy install had failure/warning markers')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--activity-plan-file', required=True)
    ap.add_argument('--phase', required=True, choices=['mixed-version-policy-gate', 'final-policy-install'])
    ap.add_argument('--username', default='admin')
    args = ap.parse_args()
    plan = json.loads(Path(args.activity_plan_file).read_text())
    cp = plan.get('checkpoint') or {}
    members = cp.get('members') or []
    mds_host = cp.get('mds_host')
    cma_name = cp.get('cma_name') or ''
    domain = cp.get('domain') or ''
    cma_ip = cp.get('cma_ip') or ''
    cluster = cp.get('cluster_name')
    package = cp.get('policy_package') or 'CP-FW-Policy'
    current_version = cp.get('current_version') or 'R81.20'
    target_version = cp.get('target_version') or 'R82'
    for name, value in {'mds_host': mds_host, 'cma_name': cma_name, 'domain': domain, 'cluster_name': cluster, 'policy_package': package}.items():
        if not value:
            raise SystemExit(f'ERROR: activity plan missing {name}')
    password = os.environ.get('CP_PASSWORD', '')
    expert_password = os.environ.get('CP_EXPERT_PASSWORD', '')
    if not password or not expert_password:
        raise SystemExit('ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required')
    cp_args = c.parse_args(['--members'] + [m['ip'] for m in members[:2]] + ['--username', args.username, '--phase', 'precheck'])
    session = c.connect(cp_args, mds_host)
    try:
        session.enter_expert(expert_password)
        run(session, f'mdsenv {shell_quote(cma_name)}', timeout=120)
        if args.phase == 'mixed-version-policy-gate':
            print('===== Mixed-version policy gate =====')
            set_cluster_version(session, domain, cluster, target_version)
            r82_task = install_policy(session, domain, package, cluster, allow_partial=True)
            summarize_task(f'{target_version} partial install', r82_task, expected_partial=True)
            set_cluster_version(session, domain, cluster, current_version)
            old_task = install_policy(session, domain, package, cluster, allow_partial=True)
            summarize_task(f'{current_version} partial install', old_task, expected_partial=True)
        else:
            print('===== Final target-version policy install =====')
            set_cluster_version(session, domain, cluster, target_version)
            final_access = install_policy(session, domain, package, cluster, allow_partial=False, layer='access')
            summarize_task(f'{target_version} final Access Control install', final_access, expected_partial=False)
            final_threat = install_policy(session, domain, package, cluster, allow_partial=False, layer='threat-prevention')
            summarize_task(f'{target_version} final Threat Prevention install', final_threat, expected_partial=False)
        return 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
