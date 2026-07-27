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


def first_take(output: str) -> str:
    jumbo = re.findall(r'(?:HOTFIX|BUNDLE)_R(?:\d+_?\d*)_JUMBO_HF_MAIN\s+Take:\s+(\d+)', output)
    if jumbo:
        return jumbo[-1]
    if 'No hotfixes' in output:
        return '0'
    return 'unknown'


def sample_gateway(args: argparse.Namespace, host: str, cp_args: argparse.Namespace) -> dict[str, object]:
    gw, results = c.precheck_gateway(cp_args, host)
    sample: dict[str, object] = {
        'host': host,
        'name': gw.name,
        'cluster_state': gw.local_state,
        'pnotes_ok': gw.pnotes_ok,
        'interfaces_ok': gw.interfaces_ok,
        'icap_ok': gw.icap_ok,
        'required_interfaces': gw.required_interfaces,
        'required_secured_interfaces': gw.required_secured_interfaces,
        'cluster_interfaces': gw.cluster_interfaces,
        'virtual_cluster_interfaces': gw.virtual_cluster_interfaces,
        'cluster_interface_signature': c.cluster_interface_signature(gw),
        'version_summary': gw.version,
        'take': 'unknown',
        'error': '',
    }
    if args.include_take and cp_args.expert_password:
        session = c.connect(cp_args, host)
        try:
            session.enter_expert(cp_args.expert_password)
            result = session.run("cpinfo -y all | egrep -i '(HOTFIX|BUNDLE)_R[0-9_]+_JUMBO_HF_MAIN|No hotfixes' | head -120", timeout=120)
            sample['take'] = first_take(result.output)
            session.run('exit', timeout=10)
        finally:
            session.close()
    return sample


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--members', nargs=2, required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--icap-mode', choices=['required', 'optional', 'disabled'], default='optional')
    parser.add_argument('--include-take', action='store_true')
    parser.add_argument('--output-jsonl', default='')
    args = parser.parse_args()

    cp_args = c.parse_args([
        '--members', args.members[0], args.members[1],
        '--username', args.username,
        '--phase', 'precheck',
        '--icap-mode', args.icap_mode,
    ])

    output = Path(args.output_jsonl) if args.output_jsonl else None
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)

    bad_samples = 0
    for index in range(1, args.samples + 1):
        record: dict[str, object] = {
            'sample': index,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'members': [],
        }
        print(f"===== Monitor Sample {index}/{args.samples} {record['timestamp']} =====", flush=True)
        for host in args.members:
            try:
                member = sample_gateway(args, host, cp_args)
            except Exception as exc:  # noqa: BLE001 - monitoring should keep sampling
                bad_samples += 1
                member = {'host': host, 'error': str(exc)}
            record['members'].append(member)
            print(json.dumps(member, sort_keys=True), flush=True)
        states = [m.get('cluster_state', '').upper() for m in record['members'] if not m.get('error')]
        active_count = sum(1 for state in states if state.startswith('ACTIVE'))
        standby_count = sum(1 for state in states if state == 'STANDBY')
        record['active_count'] = active_count
        record['standby_count'] = standby_count
        record['cluster_shape_ok'] = active_count == 1 and standby_count <= 1
        if not record['cluster_shape_ok']:
            bad_samples += 1
        if output:
            with output.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(record, sort_keys=True) + '\n')
        if index < args.samples:
            time.sleep(args.interval)
    return 0 if bad_samples == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
