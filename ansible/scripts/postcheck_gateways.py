#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from monitor_gateways import sample_gateway  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402


def normalize_take(value: object) -> str:
    text = str(value or '').strip()
    match = re.search(r'(?i)(?:take|jhf[_\s-]*t?|^t)\s*([0-9]+)', text)
    if match:
        return match.group(1)
    if text.isdigit():
        return text
    return text

def normalize_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip().lower()


def token_variants(token: str) -> list[str]:
    raw = str(token or '').strip()
    if not raw:
        return []
    base = Path(raw).name
    values = {raw, base}
    for value in list(values):
        if value.endswith('.tar'):
            values.add(value[:-4] + '.tgz')
        if value.endswith('.tgz'):
            values.add(value[:-4] + '.tar')
        values.add(value.replace('_', ' '))
        values.add(value.replace('-', ' '))
    take_match = re.search(r'(?:jhf[_ -]?t|take[_ -]?|\bt)(\d{1,4})\b', raw, re.IGNORECASE)
    if take_match:
        take = take_match.group(1)
        values.add(f'take {take}')
        values.add(f't{take}')
        values.add(f'/{take}')
    return [normalize_text(value) for value in values if normalize_text(value)]


def token_present(token: str, inventory_text: str) -> bool:
    inv = normalize_text(inventory_text)
    return any(variant in inv for variant in token_variants(token))


def listify(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass
        return [part.strip() for part in re.split(r'[\n,]', value) if part.strip()]
    return [str(value).strip()]


def load_activity_plan(path: str) -> dict:
    if not path:
        return {}
    plan_path = Path(path)
    if not plan_path.exists():
        raise SystemExit(f'ERROR: activity plan file does not exist: {path}')
    try:
        return json.loads(plan_path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f'ERROR: activity plan file is not valid JSON: {path}: {exc}')


def package_reference(step: dict) -> str:
    step_name = str(step.get('name') or '').strip()
    for key in ('source_path', 'package_name'):
        value = str(step.get(key) or '').strip()
        if value and value != step_name:
            return value
    return ''


def final_package_expectations(plan: dict) -> tuple[list[str], list[str]]:
    present: list[str] = []
    absent: list[str] = []
    for step in plan.get('package_steps') or []:
        action = str(step.get('action') or '').strip().lower()
        package_type = str(step.get('package_type') or '').strip().lower()
        ref = package_reference(step)
        major = action == 'upgrade' and (package_type in {'blink', 'major_upgrade', 'blink_image'} or 'blink' in ref.lower())
        if action in {'remove', 'uninstall'}:
            if ref:
                absent.append(ref)
        elif action in {'install', 'upgrade'} and not major:
            # Gaia may report JHF installs as a normalized display name such as
            # "R82 Jumbo Hotfix Accumulator Recommended Jumbo Take 91" rather
            # than the original MDS .tar filename. The explicit target-take
            # check below is the authoritative final-state validation for JHFs.
            if ref and package_type not in {'jhf', 'jumbo', 'jumbo_hotfix'}:
                present.append(ref)
        absent.extend(listify(step.get('requires_absent')))
    def dedupe(values: list[str]) -> list[str]:
        output = []
        for value in values:
            value = str(value).strip()
            if value and value not in output:
                output.append(value)
        return output
    return dedupe(present), dedupe(absent)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--members', nargs=2, required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--target-take', required=True)
    parser.add_argument('--absent-take', default='')
    parser.add_argument('--icap-mode', choices=['required', 'optional', 'disabled'], default='optional')
    parser.add_argument('--state-file', default='')
    parser.add_argument('--activity-plan-file', default='')
    args = parser.parse_args()

    cp_args = c.parse_args([
        '--members', args.members[0], args.members[1],
        '--username', args.username,
        '--phase', 'precheck',
        '--icap-mode', args.icap_mode,
    ])
    fake_args = argparse.Namespace(include_take=True)
    members = []
    errors = []
    for host in args.members:
        try:
            sample = sample_gateway(fake_args, host, cp_args)
            members.append(sample)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{host}: {exc}')
    interface_mismatches = []
    if args.state_file:
        state_path = Path(args.state_file)
        if not state_path.exists():
            errors.append(f'baseline state file not found: {state_path}')
        else:
            baseline = json.loads(state_path.read_text(encoding='utf-8'))
            baseline_by_host = {str(m.get('host')): m for m in baseline.get('members', [])}
            for member in members:
                host = str(member.get('host'))
                base_member = baseline_by_host.get(host)
                if not base_member:
                    interface_mismatches.append({'host': host, 'error': 'missing from baseline state'})
                    continue
                baseline_sig = c.cluster_interface_signature(base_member)
                current_sig = c.cluster_interface_signature(member)
                if baseline_sig != current_sig:
                    interface_mismatches.append(
                        {
                            'host': host,
                            'baseline': baseline_sig,
                            'current': current_sig,
                        }
                    )

    package_state_checks = []
    package_state_errors = []
    plan = load_activity_plan(args.activity_plan_file)
    expected_present, expected_absent = final_package_expectations(plan)
    removal_only = bool(plan.get('package_steps')) and all(str(step.get('action') or '').strip().lower() in {'remove', 'uninstall'} for step in plan.get('package_steps') or [])
    if expected_present or expected_absent:
        for host in args.members:
            try:
                session = c.connect(cp_args, host)
                try:
                    output = session.run('show installer packages installed', timeout=240).output
                finally:
                    session.close()
            except Exception as exc:  # noqa: BLE001
                package_state_errors.append(f'{host}: could not read installer package inventory: {exc}')
                continue
            check = {'host': host, 'expected_present': [], 'expected_absent': []}
            for token in expected_present:
                found = token_present(token, output)
                check['expected_present'].append({'token': token, 'present': found})
                if not found:
                    package_state_errors.append(f'{host}: expected package/token is not present: {token}')
            for token in expected_absent:
                found = token_present(token, output)
                check['expected_absent'].append({'token': token, 'present': found})
                if found:
                    package_state_errors.append(f'{host}: removed/absent package token is still present: {token}')
            package_state_checks.append(check)

    print(json.dumps({'members': members, 'errors': errors, 'interface_mismatches': interface_mismatches, 'package_state_checks': package_state_checks, 'package_state_errors': package_state_errors}, indent=2, sort_keys=True))

    if errors:
        return 2
    if package_state_errors:
        print('ERROR: package final-state validation failed: ' + '; '.join(package_state_errors), file=sys.stderr)
        return 2
    if interface_mismatches:
        print('ERROR: monitored cluster interface inventory differs from captured baseline', file=sys.stderr)
        return 2
    states = [str(m.get('cluster_state', '')).upper() for m in members]
    if sum(1 for state in states if state.startswith('ACTIVE')) != 1:
        print('ERROR: expected exactly one ACTIVE member', file=sys.stderr)
        return 2
    if not all(m.get('pnotes_ok') and m.get('interfaces_ok') for m in members):
        print('ERROR: PNOTEs or interfaces are not clean on all members', file=sys.stderr)
        return 2
    if args.icap_mode == 'required' and not all(m.get('icap_ok') is True for m in members):
        print('ERROR: ICAP required but not OK on all members', file=sys.stderr)
        return 2
    absent_take = normalize_take(args.absent_take)
    if not absent_take and removal_only:
        absent_take = normalize_take(args.target_take)
    if absent_take:
        members_still_on_take = [
            str(m.get('host'))
            for m in members
            if normalize_take(m.get('take')) == absent_take
        ]
        if members_still_on_take:
            print(
                f'ERROR: removed take {absent_take} is still reported by members: '
                + ', '.join(members_still_on_take),
                file=sys.stderr,
            )
            return 2
    else:
        target_take = normalize_take(args.target_take)
        if target_take:
            if not all(normalize_take(m.get('take')) == target_take for m in members):
                print(f'ERROR: not all members report target take {args.target_take}', file=sys.stderr)
                return 2
        else:
            print('No target take or absent take supplied; postcheck performed health-only validation.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
