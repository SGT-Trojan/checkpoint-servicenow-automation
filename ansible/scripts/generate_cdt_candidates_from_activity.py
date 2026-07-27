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


def package_from_plan(plan: dict, step_name: str | None) -> dict:
    steps = plan.get('package_steps') or []
    if step_name:
        for step in steps:
            if step.get('name') == step_name:
                return step
        raise SystemExit(f'ERROR: step {step_name!r} not found in activity plan')
    if not steps:
        raise SystemExit('ERROR: activity plan has no package_steps')
    return steps[0]


def build_deployment_plan(chg_number: str, package_ref: str, package_type: str, step_name: str, action: str) -> str:
    if action == 'remove':
        package_filename = Path(package_ref).name
        package_actions = f'  <uninstall_cpuse_package filename="{package_filename}" />'
        description = f'ServiceNow generated CDT uninstall plan for {package_type} package {package_filename}'
    else:
        package_actions = f'  <import_package path="{package_ref}" />\n  <install_package path="{package_ref}" />'
        description = f'ServiceNow generated CDT install plan for {package_type} package {package_ref}'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CDT_Deployment_Plan>
  <plan_settings>
    <name value="{chg_number} {step_name}" />
    <description value="{description}" />
    <update_cpuse value="true" />
    <connectivityupgrade value="true" />
  </plan_settings>
{package_actions}
</CDT_Deployment_Plan>
"""


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def py_string(value: str) -> str:
    return repr(value)

def normalize_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip().lower()


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


def package_aliases(package: dict, step_name: str) -> list[str]:
    values = [
        package.get('source_path'),
        package.get('package_name'),
        package.get('display_name'),
        package.get('name'),
        step_name,
    ]
    values.extend(listify(package.get('requires_present')))
    values.extend(listify(package.get('requires_absent')))
    aliases: list[str] = []
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if value and value not in aliases:
            aliases.append(value)
    return aliases


def take_from_text(value: str) -> str | None:
    match = re.search(r'(?:jhf[_ -]?t|take[_ -]?|bundle[_ -]?t|\bt)(\d{1,4})\b', value, re.IGNORECASE)
    return match.group(1) if match else None


def filename_from_token(value: str) -> str | None:
    base = Path(value.strip()).name
    if re.search(r'\.(?:tgz|tar)$', base, re.IGNORECASE):
        return base[:-4] + '.tgz'
    return None


def cpinstlog_grep_pattern(aliases: list[str]) -> str:
    parts: list[str] = []
    for alias in aliases:
        base = Path(alias).name.strip()
        if base:
            parts.append(re.escape(base))
            if base.endswith('.tar'):
                parts.append(re.escape(base[:-4] + '.tgz'))
            if base.endswith('.tgz'):
                parts.append(re.escape(base[:-4] + '.tar'))
        take = take_from_text(alias)
        if take:
            parts.extend([
                fr'BUNDLE_[A-Za-z0-9_]*#{take}\b',
                fr'JUMBO_HF_MAIN#{take}\b',
                fr'_T{take}(?:_|\b)',
                fr'Take[ _-]?{take}\b',
            ])
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return '|'.join(unique) or r'$^'


def package_candidates_from_history(history: str, aliases: list[str], package_type: str) -> list[str]:
    filenames = re.findall(r'([A-Za-z0-9_.+-]+\.(?:tgz|tar))', history)
    normalized_aliases = [normalize_text(alias) for alias in aliases]
    takes = {take for alias in aliases if (take := take_from_text(alias))}
    wanted_type = (package_type or '').lower()
    scored: list[tuple[int, str]] = []
    for name in filenames:
        candidate = name[:-4] + '.tgz' if name.endswith('.tar') else name
        norm = normalize_text(candidate)
        score = 0
        if any(alias and alias in norm for alias in normalized_aliases):
            score += 20
        for take in takes:
            if re.search(fr'(?:_T{take}(?:_|\b)|#{take}\b|Bundle_T{take}(?:_|\b))', candidate, re.IGNORECASE):
                score += 25
        if wanted_type == 'jhf' and re.search(r'jumbo|jhf|bundle', candidate, re.IGNORECASE):
            score += 10
        if wanted_type == 'wrapper' and re.search(r'wrapper|hotfix', candidate, re.IGNORECASE):
            score += 10
        if 'Uninstall' in history and candidate in history:
            score += 1
        if score > 0:
            scored.append((score, candidate))
    # Stable de-dupe, highest score first.
    best: dict[str, int] = {}
    for score, candidate in scored:
        best[candidate] = max(score, best.get(candidate, 0))
    return [name for name, _ in sorted(best.items(), key=lambda item: (-item[1], item[0]))]


def resolve_remove_package_ref(session, run, checkpoint: dict, package: dict, step_name: str, fallback_ref: str | None) -> str:
    aliases = package_aliases(package, step_name)
    explicit = [filename_from_token(alias) for alias in aliases]
    explicit = [value for value in explicit if value]
    pattern = cpinstlog_grep_pattern(aliases)
    members = checkpoint.get('members') or []
    if not members:
        if explicit:
            return explicit[0]
        raise SystemExit('ERROR: remove package step has no members available for CPInstLog resolver')

    safe_step = re.sub(r'[^A-Za-z0-9_.-]+', '_', step_name).strip('_') or 'remove_step'
    all_history: list[str] = []
    print('===== CPRID CPInstLog uninstall package resolver =====')
    print(f'Aliases: {aliases}')
    print(f'Pattern: {pattern}')
    for idx, member in enumerate(members, 1):
        gateway = member.get('management_ip') or member.get('ip') or member.get('access_ip')
        if not gateway:
            continue
        remote_tmp = f'/tmp/snowlite_cpinstlog_{safe_step}_{idx}.out'
        local_tmp = f'/var/log/tmp/snowlite_cpinstlog_{safe_step}_{idx}.out'
        grep_cmd = (
            "grep -hE "
            f"{shell_quote(pattern)} "
            "/opt/CPInstLog/collectors/da_actions_collector_*.csv* "
            "/opt/CPInstLog/DA_Actions.xml "
            "/opt/CPInstLog/da_cli.elg "
            "2>/dev/null"
        )
        rexec = f"cprid_util -server {shell_quote(str(gateway))} rexec -rcmd /bin/sh -c {shell_quote(grep_cmd + ' > ' + remote_tmp)}"
        run(rexec, timeout=180)
        getfile = (
            f"cprid_util -server {shell_quote(str(gateway))} getfile "
            f"-remote_file {shell_quote(remote_tmp)} -local_file {shell_quote(local_tmp)}"
        )
        run(getfile, timeout=180)
        history = run(f"python3 -c \"from pathlib import Path; p=Path({py_string(local_tmp)}); print(p.read_text(errors='replace') if p.exists() else '')\"", timeout=120)
        if history.strip():
            print(f'===== {gateway}: CPInstLog resolver output =====')
            print(history.rstrip())
            all_history.append(history)
        run(f"cprid_util -server {shell_quote(str(gateway))} rexec -rcmd /bin/rm {shell_quote(remote_tmp)}", timeout=60)

    candidates = package_candidates_from_history('\n'.join(all_history), aliases, package.get('package_type') or '')
    if candidates:
        if len(candidates) != 1:
            raise SystemExit(
                'ERROR: CPInstLog CPRID resolver found multiple matching package identities; '
                f'provide an explicit full package filename or correct the request: {candidates}'
            )
        selected = candidates[0]
        print(f"CDT uninstall package reference resolved from gateway CPInstLog via CPRID: {selected}")
        return selected
    if explicit:
        print(f"WARNING: CPInstLog CPRID resolver found no history; using explicit filename token {explicit[0]}")
        return explicit[0]
    if fallback_ref:
        fallback_name = Path(fallback_ref).name
        if fallback_name and fallback_name != step_name:
            print(f"WARNING: CPInstLog CPRID resolver found no history; using fallback package reference {fallback_name}")
            return fallback_name[:-4] + '.tgz' if fallback_name.endswith('.tar') else fallback_name
    raise SystemExit(
        'ERROR: could not resolve remove package filename from gateway CPInstLog via CPRID. '
        'Provide an explicit package filename or verify CPRID/SIC/CPD connectivity.'
    )


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


def select_target(rows: list[dict[str, str]], policy: str, target_ip: str | None) -> dict[str, str]:
    if target_ip:
        matches = [row for row in rows if row['ip_address'] == target_ip]
        if len(matches) != 1:
            raise SystemExit(f'ERROR: target IP {target_ip} was not found exactly once in CDT candidates')
        return matches[0]

    if policy not in {'standby', 'active'}:
        raise SystemExit(f'ERROR: unsupported target policy {policy!r}')
    matches = [row for row in rows if row['state'] == policy]
    if len(matches) != 1:
        raise SystemExit(f'ERROR: expected exactly one {policy} candidate, found {len(matches)}')
    return matches[0]


def controlled_candidates_text(original_text: str, rows: list[dict[str, str]], target: dict[str, str]) -> str:
    row_by_ip = {row['ip_address']: row for row in rows}
    output = []
    for line in original_text.splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) == 6 and parts[2] in row_by_ip:
            row = row_by_ip[parts[2]]
            order = '1' if row['ip_address'] == target['ip_address'] else '-'
            output.append(
                f"{row['object_name']:>20} , {row['cluster_name']:>20} , {row['ip_address']:>14} , "
                f"{row['version_jhf_take']:>16} , {row['state']:>20} , {order:>13}"
            )
        else:
            output.append(line)
    output.append('')
    return '\n'.join(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--step')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--plan-path')
    parser.add_argument('--candidates-path')
    parser.add_argument('--target-policy', choices=['standby', 'active'], default='standby')
    parser.add_argument('--target-ip')
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    checkpoint = plan.get('checkpoint', {})
    change = plan.get('change', {})
    package = package_from_plan(plan, args.step)

    members = checkpoint.get('members') or []
    if len(members) != 2:
        print('ERROR: CDT cluster candidate generation currently expects exactly two members', file=sys.stderr)
        return 2

    mds_host = checkpoint.get('mds_host')
    cma_env = checkpoint.get('cma_name')
    cma_ip = checkpoint.get('cma_ip')
    cluster = checkpoint.get('cluster_name')
    chg_number = change.get('number', 'unknown')
    action = (package.get('action') or 'install').lower()
    package_path = package.get('source_path') or package.get('package_name')
    step_name = package.get('name') or args.step or 'package_step'
    package_type = package.get('package_type') or 'package'
    plan_path = args.plan_path or f'/var/log/tmp/{chg_number}_{step_name}_cdt_plan.xml'
    candidates_path = args.candidates_path or f'/var/log/tmp/{chg_number}_{step_name}_cdt_candidates.csv'

    required = {
        'mds_host': mds_host,
        'cma_name': cma_env,
        'cma_ip': cma_ip,
        'cluster_name': cluster,
    }
    if action != 'remove':
        required['package_path'] = package_path
    missing = [k for k, v in required.items() if not v]
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
        if action == 'remove':
            package_path = resolve_remove_package_ref(session, run, checkpoint, package, step_name, package_path)
        plan_xml = build_deployment_plan(chg_number, package_path, package_type, step_name, action)
        plan_hex = plan_xml.encode().hex()
        write_cmd = "python3 -c \"from pathlib import Path; Path(%s).write_bytes(bytes.fromhex('%s'))\"" % (py_string(plan_path), plan_hex)
        run(write_cmd, timeout=120)
        run(f'ls -l {shell_quote(plan_path)}', timeout=120)
        generate_cmd = (
            f'/opt/CPcdt/CentralDeploymentTool -generate '
            f'-candidates={shell_quote(candidates_path)} '
            f'-deploymentplan={shell_quote(plan_path)} '
            f'-server={cma_ip}'
        )
        generate_output = run(generate_cmd, timeout=1200)
        if 'The generated candidates list is:' not in generate_output:
            print('ERROR: CDT did not report a generated candidates list', file=sys.stderr)
            return 2
        candidate_text = read_remote_text(candidates_path)
        print(f'===== MDS file content: {candidates_path} =====')
        print(candidate_text.rstrip())
        all_rows = parse_candidates(candidate_text)
        print('===== Parsed Candidates Before Control =====')
        print(json.dumps(all_rows, indent=2, sort_keys=True))

        expected_ips = {m.get('management_ip') or m.get('ip') for m in members}
        expected_names = {m['hostname'] for m in members if m.get('hostname')}
        rows = [
            row for row in all_rows
            if row['ip_address'] in expected_ips
            or (expected_names and row['object_name'] in expected_names)
            or row['cluster_name'] == cluster
        ]
        rows = [row for row in rows if row['ip_address'] in expected_ips or row['object_name'] in expected_names]
        print('===== Parsed Target Candidates =====')
        print(json.dumps(rows, indent=2, sort_keys=True))
        if len(rows) != 2:
            print(f'ERROR: expected exactly 2 target candidates, got {len(rows)} from {len(all_rows)} total CDT candidates', file=sys.stderr)
            return 2
        if {row['ip_address'] for row in rows} != expected_ips:
            print('ERROR: target candidate IPs do not match expected members', file=sys.stderr)
            return 2
        if expected_names and {row['object_name'] for row in rows} != expected_names:
            print('ERROR: target candidate object names do not match expected members', file=sys.stderr)
            return 2
        if any(row['cluster_name'] != cluster for row in rows):
            print('ERROR: one or more target candidates are not in the expected cluster', file=sys.stderr)
            return 2
        if {row['state'] for row in rows} != {'active', 'standby'}:
            print('ERROR: expected one active and one standby target candidate', file=sys.stderr)
            return 2

        target = select_target(rows, args.target_policy, args.target_ip)
        raw_path = candidates_path + '.raw'
        controlled_text = controlled_candidates_text(candidate_text, all_rows, target)
        raw_hex = candidate_text.encode().hex()
        controlled_hex = controlled_text.encode().hex()
        run("python3 -c \"from pathlib import Path; Path(%s).write_bytes(bytes.fromhex('%s')); Path(%s).write_bytes(bytes.fromhex('%s'))\"" % (py_string(raw_path), raw_hex, py_string(candidates_path), controlled_hex), timeout=120)
        controlled_candidate_text = read_remote_text(candidates_path)
        print(f'===== Controlled MDS file content: {candidates_path} =====')
        print(controlled_candidate_text.rstrip())
        controlled_rows = parse_candidates(controlled_candidate_text)
        print('===== Parsed Candidates After Control =====')
        print(json.dumps(controlled_rows, indent=2, sort_keys=True))
        enabled = [row for row in controlled_rows if row['upgrade_order'] == '1']
        disabled = [row for row in controlled_rows if row['upgrade_order'] == '-']
        if len(enabled) != 1 or enabled[0]['ip_address'] != target['ip_address']:
            print('ERROR: controlled candidate file does not enable exactly the selected target', file=sys.stderr)
            return 2
        if len(disabled) != 1:
            print('ERROR: controlled candidate file does not disable exactly one peer', file=sys.stderr)
            return 2
        print(f"Selected target: {target['object_name']} {target['ip_address']} ({target['state']})")
        print(f'Raw CDT candidates backup path: {raw_path}')
        print(f'CDT plan path: {plan_path}')
        print(f'CDT candidates path: {candidates_path}')
        return 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
