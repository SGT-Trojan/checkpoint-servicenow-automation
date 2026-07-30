#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


for candidate in [
    Path(__file__).resolve().parents[2],
    Path(__file__).resolve().parents[2] / 'tools',
    Path(__file__).resolve().parents[3] / 'tools',
]:
    sys.path.insert(0, str(candidate))
import checkpoint_cluster_upgrade as c  # noqa: E402


def load_plan(path: str) -> dict:
    plan_path = Path(path)
    if not plan_path.exists():
        raise SystemExit(f'ERROR: activity plan file does not exist: {path}')
    try:
        return json.loads(plan_path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f'ERROR: activity plan file is not valid JSON: {path}: {exc}')


def package_from_plan(plan: dict, step_name: str) -> dict:
    for step in plan.get('package_steps') or []:
        if step.get('name') == step_name:
            return step
    raise SystemExit(f"ERROR: package step {step_name!r} not found in activity plan")


def load_cluster_state(reports_dir: Path, chg_number: str) -> dict:
    path = reports_dir / f'cluster_initial_state_{chg_number}.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def target_members_for_phase(plan: dict, phase: str, reports_dir: Path) -> list[dict]:
    checkpoint = plan.get('checkpoint') or {}
    members = checkpoint.get('members') or []
    if not members:
        raise SystemExit('ERROR: activity plan has no checkpoint.members')
    if (checkpoint.get('cluster_mode') or 'cluster') == 'standalone' or len(members) == 1:
        return [members[0]]

    if phase in {"install-deployment-agent", "deployment-agent-readiness"}:
        return members
    if phase not in {"first-member", "second-member"}:
        raise SystemExit(f"ERROR: unsupported package prerequisite phase {phase!r}")

    chg_number = (plan.get("change") or {}).get("number", "unknown")
    state = load_cluster_state(reports_dir, chg_number)
    by_ip = {m["ip"]: m for m in members}
    original_active = state.get("original_active_host")
    original_standby = state.get("original_standby_host")
    if not original_active or not original_standby:
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} must identify original active and standby members"
        )
    if original_active == original_standby or {original_active, original_standby} != set(by_ip):
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} does not match the activity-plan members"
        )
    target_ip = original_standby if phase == "first-member" else original_active
    return [by_ip[target_ip]]


def normalize(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip().lower()


def token_variants(token: str) -> list[str]:
    raw = token.strip()
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
        values.add(f'/ {take}')
        values.add(f'/{take}')
    return [normalize(v) for v in values if normalize(v)]


def token_present(token: str, inventory_text: str) -> bool:
    inv = normalize(inventory_text)
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


def is_major_upgrade_step(step: dict) -> bool:
    action = str(step.get('action') or '').strip().lower()
    package_type = str(step.get('package_type') or '').strip().lower()
    blob = ' '.join(str(step.get(k) or '') for k in ('name', 'package_name', 'source_path')).lower()
    return action in {'upgrade', 'install'} and (
        package_type in {'blink', 'major_upgrade', 'blink_image'} or 'blink' in blob
    )


def parse_size_to_gb(value: str) -> float | None:
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?)\s*(?:b|bytes)?', value.strip(), re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).upper() or 'G'
    factors = {'K': 1 / (1024 * 1024), 'M': 1 / 1024, 'G': 1, 'T': 1024, 'P': 1024 * 1024}
    return amount * factors.get(unit, 1)


def parse_restore_point_space_gb(show_snapshots_output: str) -> float | None:
    patterns = [
        r'Amount of space available for restore points is\s+([^\r\n]+)',
        r'available for restore points\s*[:=]\s*([^\r\n]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, show_snapshots_output, re.IGNORECASE)
        if match:
            return parse_size_to_gb(match.group(1))
    return None


def parse_snapshot_names(show_snapshots_output: str) -> list[str]:
    names: list[str] = []
    for line in show_snapshots_output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(('snapshot', 'amount ', 'name ', 'description', '---')):
            continue
        match = re.match(r'^([A-Za-z0-9_.-]+)(?:\s+.*)?$', stripped)
        if match:
            names.append(match.group(1))
    return names


def validate_restore_point_space(host: str, show_snapshots_output: str, min_gb: float) -> int:
    print(f'===== {host}: show snapshots =====')
    print(show_snapshots_output.rstrip())
    available_gb = parse_restore_point_space_gb(show_snapshots_output)
    names = parse_snapshot_names(show_snapshots_output)
    cleanup_candidates = [name for name in names if re.search(r'(?i)^blink|blink[_-]?r|failed|upgrade', name)]
    if available_gb is None:
        print(f'ERROR: could not parse restore-point space from show snapshots on {host}', file=sys.stderr)
        return 1
    print(f'Parsed restore-point free space on {host}: {available_gb:.2f} GB; required minimum: {min_gb:.2f} GB')
    if available_gb < min_gb:
        print(
            f'ERROR: restore-point space on {host} is below major-upgrade threshold: '
            f'{available_gb:.2f} GB available, {min_gb:.2f} GB required.',
            file=sys.stderr,
        )
        if cleanup_candidates:
            print(f'Cleanup candidates on {host}: {", ".join(cleanup_candidates)}', file=sys.stderr)
        else:
            print(f'No obvious stale Blink/upgrade snapshot cleanup candidates were parsed on {host}.', file=sys.stderr)
        return 1
    print(f'OK: restore-point space on {host} meets major-upgrade threshold.')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--reports-dir', required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--step', required=True)
    parser.add_argument('--username', default='admin')
    args = parser.parse_args()

    plan = load_plan(args.activity_plan_file)
    step = package_from_plan(plan, args.step)
    requires_present = listify(step.get('requires_present'))
    requires_absent = listify(step.get('requires_absent'))
    targets = target_members_for_phase(plan, args.phase, Path(args.reports_dir))

    print('===== Package prerequisite validation =====')
    print(f"Change: {(plan.get('change') or {}).get('number', 'unknown')}")
    print(f"Phase: {args.phase}")
    print(f"Step: {step.get('name')} ({step.get('action')} {step.get('package_type')})")
    print(f"Targets: {', '.join(t['ip'] for t in targets)}")
    print(f"Requires present: {requires_present or '[]'}")
    print(f"Requires absent: {requires_absent or '[]'}")

    action = str(step.get('action') or '').strip().lower()
    step_name = str(step.get('name') or '').strip()
    package_ref = str(step.get('source_path') or step.get('package_name') or '').strip()
    if action in {'remove', 'uninstall'}:
        if package_ref == step_name:
            package_ref = ''
        if not package_ref and requires_present:
            package_ref = requires_present[0]
            print(f'Using required-present token as removal package alias: {package_ref}')
        if not package_ref:
            print('ERROR: removal steps require an explicit CPUSE package filename/name or an alias such as JHF_T91 / Take 91.', file=sys.stderr)
            return 2
        if package_ref == step_name:
            print('ERROR: removal package reference resolves to the workflow step name, not a CPUSE package filename/name.', file=sys.stderr)
            return 2
        if not re.search(r'\.(?:tgz|tar)$|JHF|HOTFIX|Bundle|Take|wrapper|\bT\d{1,4}\b', package_ref, re.IGNORECASE):
            print(f'ERROR: removal package reference does not look like a CPUSE package name or supported alias: {package_ref}', file=sys.stderr)
            return 2

    major_upgrade = is_major_upgrade_step(step)
    min_restore_gb = float((plan.get('execution') or {}).get('major_upgrade_min_restore_point_gb') or 35.0)

    if not requires_present and not requires_absent and not major_upgrade:
        print('No package prerequisites declared for this step. Passing validation.')
        return 0

    members = (plan.get('checkpoint') or {}).get('members') or targets
    cp_args = c.parse_args(['--members', members[0]['ip'], members[-1]['ip'], '--username', args.username, '--phase', 'precheck'])
    failures = 0

    for target in targets:
        host = target['ip']
        session = c.connect(cp_args, host)
        try:
            snapshot_output = session.run('show snapshots', timeout=120).output if major_upgrade else ''
            output = session.run('show installer packages', timeout=240).output
        finally:
            session.close()

        if major_upgrade:
            failures += validate_restore_point_space(host, snapshot_output, min_restore_gb)

        print(f'===== {host}: show installer packages =====')
        print(output.rstrip())

        for token in requires_present:
            if token_present(token, output):
                print(f'OK: required-present token found on {host}: {token}')
            else:
                print(f'ERROR: required-present token missing on {host}: {token}', file=sys.stderr)
                failures += 1
        for token in requires_absent:
            if token_present(token, output):
                print(f'ERROR: required-absent token is present on {host}: {token}', file=sys.stderr)
                failures += 1
            else:
                print(f'OK: required-absent token not present on {host}: {token}')

    if failures:
        print(f'Package prerequisite validation failed with {failures} unmet requirement(s).', file=sys.stderr)
        return 2
    print('Package prerequisite validation passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
