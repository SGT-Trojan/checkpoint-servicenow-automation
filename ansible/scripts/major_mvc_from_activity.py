#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def load_cluster_state(reports_dir: Path, chg_number: str) -> dict:
    path = reports_dir / f'cluster_initial_state_{chg_number}.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def members_for_phase(plan: dict, phase: str, reports_dir: Path) -> list[str]:
    members = [m['ip'] for m in (plan.get('checkpoint', {}).get('members') or []) if m.get('ip')]
    state = load_cluster_state(reports_dir, (plan.get('change') or {}).get('number', 'unknown'))
    if phase == 'mvc-on':
        return [state.get('original_standby_host') or (members[1] if len(members) > 1 else members[0])]
    if phase == 'mvc-off':
        return members
    raise SystemExit(f'ERROR: unsupported MVC phase {phase}')


def run_host(host: str, username: str, password: str, expert_password: str, command: str) -> str:
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        session.enter_expert(expert_password)
        out = session.run(command, timeout=120).output
        print(f'===== {host}: {command} =====')
        print(out.rstrip())
        check = session.run('cphaprob mvc', timeout=60).output
        print(f'===== {host}: cphaprob mvc =====')
        print(check.rstrip())
        return out + '\n' + check
    finally:
        session.close()


def wait_cluster(host: str, username: str, password: str, timeout: int = 900) -> None:
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        session = None
        try:
            session = c.SshPty(host, username, password, connect_timeout=10)
            session.connect()
            out = session.run('cphaprob state', timeout=60).output
            last = out
            print(f'===== {host}: cphaprob state =====')
            print(out.rstrip())
            if 'Active PNOTEs: None' in out and ('ACTIVE' in out or 'STANDBY' in out):
                return
        except Exception as exc:
            last = str(exc)
        finally:
            if session:
                session.close()
        time.sleep(20)
    raise TimeoutError(f'{host}: cluster did not settle after MVC operation. Last output: {last}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--activity-plan-file', required=True)
    ap.add_argument('--reports-dir', required=True)
    ap.add_argument('--phase', required=True, choices=['mvc-on', 'mvc-off'])
    ap.add_argument('--username', default='admin')
    args = ap.parse_args()
    plan = json.loads(Path(args.activity_plan_file).read_text())
    password = os.environ.get('CP_PASSWORD', '')
    expert_password = os.environ.get('CP_EXPERT_PASSWORD', '')
    if not password or not expert_password:
        raise SystemExit('ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required')
    targets = members_for_phase(plan, args.phase, Path(args.reports_dir))
    command = 'cphaconf mvc on' if args.phase == 'mvc-on' else 'cphaconf mvc off'
    print(f'===== MVC execution plan: {args.phase} =====')
    print('Targets: ' + ', '.join(targets))
    for host in targets:
        run_host(host, args.username, password, expert_password, command)
    for host in targets:
        wait_cluster(host, args.username, password)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
