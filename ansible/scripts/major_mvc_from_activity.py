#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def load_cluster_state(
    reports_dir: Path | None,
    chg_number: str,
    state_file: Path | None = None,
) -> dict:
    path = state_file or (
        reports_dir / f'cluster_initial_state_{chg_number}.json'
        if reports_dir is not None
        else None
    )
    if path is None:
        return {}
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def members_for_phase(
    plan: dict,
    phase: str,
    reports_dir: Path | None,
    state_file: Path | None = None,
) -> list[str]:
    members = [
        str(m.get('ip'))
        for m in (plan.get('checkpoint', {}).get('members') or [])
        if m.get('ip')
    ]
    if len(members) != 2 or len(set(members)) != 2:
        raise SystemExit('ERROR: MVC requires exactly two distinct activity-plan members')
    state = load_cluster_state(
        reports_dir,
        (plan.get('change') or {}).get('number', 'unknown'),
        state_file,
    )
    if phase == 'mvc-on':
        target = state.get('original_standby_host')
        if target not in members:
            raise SystemExit(
                'ERROR: captured original standby member does not match the activity plan'
            )
        return [target]
    if phase == 'mvc-off':
        return members
    raise SystemExit(f'ERROR: unsupported MVC phase {phase}')


def mvc_return_code(output: str) -> int | None:
    matches = re.findall(r'(?:^|\s)__RC=(\d+)(?:\s|$)', output)
    return int(matches[-1]) if matches else None


def parse_mvc_state(output: str) -> str:
    states = set()
    for line in output.splitlines():
        state = line.strip().lower()
        if state in {'on', 'off'}:
            states.add('enabled' if state == 'on' else 'disabled')
    if len(states) != 1:
        raise RuntimeError('cphaprob mvc did not report one unambiguous MVC state')
    return states.pop()


def run_host(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    command: str,
    expected_state: str,
) -> str:
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        session.enter_expert(expert_password)
        executed = (
            f"{command}; rc=$?; printf '\\n__RC=%s\\n' \"$rc\""
        )
        out = session.run(executed, timeout=120).output
        print(f'===== {host}: {command} =====')
        print(out.rstrip())
        rc = mvc_return_code(out)
        if rc is None:
            raise RuntimeError(f'{host}: MVC command did not return an exit status')
        if rc != 0:
            raise RuntimeError(f'{host}: MVC command failed with exit status {rc}')
        check = session.run('cphaprob mvc', timeout=60).output
        print(f'===== {host}: cphaprob mvc =====')
        print(check.rstrip())
        actual_state = parse_mvc_state(check)
        if actual_state != expected_state:
            raise RuntimeError(
                f'{host}: cphaprob mvc reported {actual_state}; expected {expected_state}'
            )
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
    state_source = ap.add_mutually_exclusive_group(required=True)
    state_source.add_argument('--reports-dir')
    state_source.add_argument('--state-file')
    ap.add_argument('--phase', required=True, choices=['mvc-on', 'mvc-off'])
    ap.add_argument('--username', default='admin')
    args = ap.parse_args()
    plan = json.loads(Path(args.activity_plan_file).read_text())
    password = os.environ.get('CP_PASSWORD', '')
    expert_password = os.environ.get('CP_EXPERT_PASSWORD', '')
    if not password or not expert_password:
        raise SystemExit('ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required')
    targets = members_for_phase(
        plan,
        args.phase,
        Path(args.reports_dir) if args.reports_dir else None,
        Path(args.state_file) if args.state_file else None,
    )
    command = 'cphaconf mvc on' if args.phase == 'mvc-on' else 'cphaconf mvc off'
    expected_state = 'enabled' if args.phase == 'mvc-on' else 'disabled'
    print(f'===== MVC execution plan: {args.phase} =====')
    print('Targets: ' + ', '.join(targets))
    for host in targets:
        run_host(
            host, args.username, password, expert_password, command, expected_state
        )
    for host in targets:
        wait_cluster(host, args.username, password)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
