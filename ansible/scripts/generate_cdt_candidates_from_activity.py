#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402

from governed_cdt_artifacts import atomic_write_private_json  # noqa: E402


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


SAFE_PACKAGE_FILENAME_RE = re.compile(
    r'(?<![A-Za-z0-9_.+-])'
    r'([A-Za-z0-9][A-Za-z0-9_.+-]*\.(?:tgz|tar))'
    r'(?![A-Za-z0-9_.+-])',
    re.IGNORECASE,
)
PACKAGE_FILENAME_PATTERN = r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.(?:tgz|tar)'
PACKAGE_STATUS_PATTERN = r'Installed|Not[ _-]?Installed|Uninstalled|Removed'
PACKAGE_ROW_RE = re.compile(
    rf'^\s*(?:Name\s*:\s*)?({PACKAGE_FILENAME_PATTERN})\s*\|\s*'
    rf'Status\s*:\s*({PACKAGE_STATUS_PATTERN})\s*$',
    re.IGNORECASE,
)
PACKAGE_NAME_ROW_RE = re.compile(
    rf'^\s*Name\s*:\s*({PACKAGE_FILENAME_PATTERN})\s*$', re.IGNORECASE
)
PACKAGE_STATUS_ROW_RE = re.compile(
    rf'^\s*Status\s*:\s*({PACKAGE_STATUS_PATTERN})\s*$', re.IGNORECASE
)
PACKAGE_TABLE_HEADER_RE = re.compile(
    r'^\s*(?:Blink Images|Installed Packages|'
    r'(?:Package\s+)?Name\s*\|\s*Status)\s*$',
    re.IGNORECASE,
)
PACKAGE_TABLE_SEPARATOR_RE = re.compile(
    r'^\s*(?:[-=]{3,}|\+(?:-+\+)+)\s*$'
)


def package_filename_tokens(text: str) -> list[str]:
    """Extract complete package filename tokens, never extension prefixes."""
    return SAFE_PACKAGE_FILENAME_RE.findall(text)


def package_identity_matches_alias(candidate: str, aliases: list[str]) -> bool:
    candidate_lower = candidate.lower()
    candidate_stem = re.sub(r'\.(?:tgz|tar)$', '', candidate_lower)
    for alias in aliases:
        base = Path(str(alias)).name.strip()
        alias_tokens = package_filename_tokens(base)
        if len(alias_tokens) == 1 and alias_tokens[0] == base:
            alias_lower = alias_tokens[0].lower()
            alias_stem = re.sub(r'\.(?:tgz|tar)$', '', alias_lower)
            if candidate_lower == alias_lower or candidate_stem == alias_stem:
                return True
        elif base and candidate_stem == base.lower():
            return True
        take = take_from_text(str(alias))
        if take and re.search(
            fr'(?:_T{re.escape(take)}(?:_|\.|$)|'
            fr'#{re.escape(take)}(?:_|\.|$)|'
            fr'Bundle_T{re.escape(take)}(?:_|\.|$)|'
            fr'Take[ _-]?{re.escape(take)}(?:_|\.|$))',
            candidate,
            re.IGNORECASE,
        ):
            return True
    return False


def package_candidates_from_history(history: str, aliases: list[str], package_type: str) -> list[str]:
    """Return alias-matching identities without inferring current state or chronology."""
    del package_type
    candidates: set[str] = set()
    for line in history.splitlines():
        for candidate in package_filename_tokens(line):
            if package_identity_matches_alias(candidate, aliases):
                candidates.add(candidate)
    return sorted(candidates)


def installed_package_identities(table: str) -> list[str]:
    """Parse the authoritative current installed-package table, never history."""
    if re.search(r'[^\x09\x0a\x0d\x20-\x7e]', table):
        raise RuntimeError('installed-package table contains unsupported control text')
    cleaned = table.replace('\r\n', '\n')
    if '\r' in cleaned:
        raise RuntimeError('installed-package table contains a bare carriage return')
    without_zero_errors = re.sub(
        r'\b(?:Errors?\s*:\s*0|No errors|0 errors)\b',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    if re.search(
        r'\b(?:errors?|failed|failure|invalid command|permission denied|'
        r'not found|timed out|exception)\b',
        without_zero_errors,
        re.IGNORECASE,
    ):
        raise RuntimeError('installed-package table contains command error text')
    extension_lookalike = re.search(
        r'\.(?:tgz|tar)[A-Za-z0-9_.+-]+', cleaned, re.IGNORECASE
    )
    if extension_lookalike:
        raise RuntimeError(
            'installed-package table contains a non-package extension lookalike: '
            f'{extension_lookalike.group(0)!r}'
        )

    states: dict[str, tuple[str, bool]] = {}
    recognized_rows = 0
    empty_markers = 0
    rc_markers = 0
    lines = cleaned.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if re.fullmatch(r'\s*__RC=0\s*', line):
            rc_markers += 1
            index += 1
            continue
        if re.fullmatch(r'\s*No installed packages match\s*', line, re.IGNORECASE):
            empty_markers += 1
            index += 1
            continue
        if PACKAGE_TABLE_HEADER_RE.fullmatch(line) or PACKAGE_TABLE_SEPARATOR_RE.fullmatch(line):
            index += 1
            continue
        row = PACKAGE_ROW_RE.fullmatch(line)
        if row:
            name, status = row.groups()
            index += 1
        else:
            name_row = PACKAGE_NAME_ROW_RE.fullmatch(line)
            if not name_row or index + 1 >= len(lines):
                raise RuntimeError(
                    f'installed-package table contains an unknown or malformed line: {line!r}'
                )
            status_row = PACKAGE_STATUS_ROW_RE.fullmatch(lines[index + 1])
            if not status_row:
                raise RuntimeError(
                    'installed-package table has an incomplete two-line package row: '
                    f'{line!r}'
                )
            name = name_row.group(1)
            status = status_row.group(1)
            index += 2
        installed = status.casefold() == 'installed'
        normalized_name = name.casefold()
        if normalized_name in states:
            prior_name, _ = states[normalized_name]
            raise RuntimeError(
                'installed-package table has a duplicate normalized identity: '
                f'{prior_name!r} and {name!r}'
            )
        recognized_rows += 1
        states[normalized_name] = (name, installed)
    if rc_markers > 1:
        raise RuntimeError('installed-package table has multiple exit-status markers')
    if empty_markers > 1:
        raise RuntimeError('installed-package table has multiple empty-state markers')
    if empty_markers and recognized_rows:
        raise RuntimeError(
            'installed-package table mixes an empty-state marker with package rows'
        )
    if not recognized_rows and empty_markers != 1:
        raise RuntimeError(
            'installed-package table has no recognized rows or valid empty-state marker'
        )
    return sorted(name for name, installed in states.values() if installed)


def package_identity_is_installed(table: str, package_name: str) -> bool:
    expected = Path(package_name).name.lower()
    expected_stem = re.sub(r'\.(?:tgz|tar)$', '', expected)
    for installed in installed_package_identities(table):
        current = installed.lower()
        current_stem = re.sub(r'\.(?:tgz|tar)$', '', current)
        if current == expected or current_stem == expected_stem:
            return True
    return False


def resolve_current_remove_identity(
    history: str,
    installed_table: str,
    aliases: list[str],
    package_type: str,
) -> str:
    history_candidates = package_candidates_from_history(
        history, aliases, package_type
    )
    current_installed = installed_package_identities(installed_table)
    matches = sorted(set(history_candidates).intersection(current_installed))
    if len(matches) != 1:
        raise RuntimeError(
            "remove identity must be uniquely alias-resolved in CPInstLog and "
            "currently installed on the selected member; "
            f"history_candidates={history_candidates} "
            f"current_installed={current_installed} matches={matches}"
        )
    return matches[0]


def resolve_remove_package_ref(
    run,
    selected_member: dict,
    package: dict,
    step_name: str,
    _fallback_ref: str | None,
) -> str:
    aliases = package_aliases(package, step_name)
    pattern = cpinstlog_grep_pattern(aliases)
    gateway = (
        selected_member.get('management_ip')
        or selected_member.get('ip')
    )
    if not gateway:
        raise SystemExit(
            'ERROR: selected remove member has no management address for mandatory '
            'CPInstLog resolution'
        )

    safe_step = re.sub(r'[^A-Za-z0-9_.-]+', '_', step_name).strip('_') or 'remove_step'
    print('===== CPRID CPInstLog uninstall package resolver =====')
    print(f'Selected management member: {gateway}')
    print(f'Aliases: {aliases}')
    print(f'Pattern: {pattern}')
    remote_tmp = f'/tmp/snowlite_cpinstlog_{safe_step}.out'
    local_tmp = f'/var/log/tmp/snowlite_cpinstlog_{safe_step}.out'
    installed_remote_tmp = f'/tmp/snowlite_installed_{safe_step}.out'
    installed_local_tmp = f'/var/log/tmp/snowlite_installed_{safe_step}.out'
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
    run(f"cprid_util -server {shell_quote(str(gateway))} rexec -rcmd /bin/rm {shell_quote(remote_tmp)}", timeout=60)

    installed_command = (
        "clish -c 'show installer packages installed' > "
        f"{installed_remote_tmp} 2>&1"
    )
    run(
        f"cprid_util -server {shell_quote(str(gateway))} rexec -rcmd /bin/sh -c "
        f"{shell_quote(installed_command)}",
        timeout=180,
    )
    run(
        f"cprid_util -server {shell_quote(str(gateway))} getfile "
        f"-remote_file {shell_quote(installed_remote_tmp)} "
        f"-local_file {shell_quote(installed_local_tmp)}",
        timeout=180,
    )
    installed_table = run(
        f"python3 -c \"from pathlib import Path; p=Path({py_string(installed_local_tmp)}); "
        "print(p.read_text(errors='replace') if p.exists() else '')\"",
        timeout=120,
    )
    print(f'===== {gateway}: current installed-package table =====')
    print(installed_table.rstrip())
    run(
        f"cprid_util -server {shell_quote(str(gateway))} rexec -rcmd /bin/rm "
        f"{shell_quote(installed_remote_tmp)}",
        timeout=60,
    )
    try:
        selected = resolve_current_remove_identity(
            history,
            installed_table,
            aliases,
            package.get('package_type') or '',
        )
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(
        "CDT uninstall package reference resolved from selected-member CPInstLog "
        f"and current installed-package table via CPRID: {selected}"
    )
    return selected


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


def select_member_for_removal(
    members: list[dict],
    target_policy: str,
    target_ip: str | None,
    run,
    step_name: str,
) -> dict:
    if target_ip:
        matches = [
            member
            for member in members
            if str(member.get("management_ip") or member.get("ip") or "") == target_ip
        ]
        if len(matches) != 1:
            raise SystemExit(
                "ERROR: removal target IP does not identify exactly one plan member"
            )
        return matches[0]

    safe_step = re.sub(r"[^A-Za-z0-9_.-]+", "_", step_name).strip("_") or "remove"
    matches: list[dict] = []
    for index, member in enumerate(members, 1):
        gateway = str(member.get("management_ip") or member.get("ip") or "")
        if not gateway:
            continue
        remote_tmp = f"/tmp/snowlite_cluster_state_{safe_step}_{index}.out"
        local_tmp = f"/var/log/tmp/snowlite_cluster_state_{safe_step}_{index}.out"
        command = (
            f"cprid_util -server {shell_quote(gateway)} rexec -rcmd /bin/sh -c "
            f"{shell_quote('cphaprob state > ' + remote_tmp)}"
        )
        run(command, timeout=120)
        run(
            f"cprid_util -server {shell_quote(gateway)} getfile "
            f"-remote_file {shell_quote(remote_tmp)} -local_file {shell_quote(local_tmp)}",
            timeout=120,
        )
        output = run(
            f"python3 -c \"from pathlib import Path; p=Path({py_string(local_tmp)}); "
            "print(p.read_text(errors='replace') if p.exists() else '')\"",
            timeout=120,
        )
        run(
            f"cprid_util -server {shell_quote(gateway)} rexec -rcmd /bin/rm "
            f"{shell_quote(remote_tmp)}",
            timeout=60,
        )
        local_state, _peer_states, _pnotes_ok = c.parse_cluster_state(output)
        normalized_state = local_state.lower()
        if normalized_state not in {"active", "standby"}:
            raise SystemExit(
                f"ERROR: could not determine one cluster state for selected-member "
                f"resolution on {gateway}: {local_state}"
            )
        if normalized_state == target_policy:
            matches.append(member)
    if len(matches) != 1:
        raise SystemExit(
            f"ERROR: expected exactly one {target_policy} plan member for removal "
            f"identity resolution, found {len(matches)}"
        )
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--step')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--plan-path')
    parser.add_argument('--candidates-path')
    parser.add_argument('--target-policy', choices=['standby', 'active'], default='standby')
    parser.add_argument('--target-ip')
    parser.add_argument('--phase', choices=['first-member', 'second-member'], required=True)
    parser.add_argument('--resolution-output', type=Path, required=True)
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    if re.fullmatch(r"run_[0-9a-f]{64}", args.operation_id) is None:
        print("ERROR: invalid governed operation ID", file=sys.stderr)
        return 2

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
        removal_member = None
        if action == 'remove':
            removal_member = select_member_for_removal(
                members,
                args.target_policy,
                args.target_ip,
                run,
                step_name,
            )
            package_path = resolve_remove_package_ref(
                run,
                removal_member,
                package,
                step_name,
                package_path,
            )
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
        if removal_member is not None:
            resolved_management_ip = str(
                removal_member.get("management_ip") or removal_member.get("ip") or ""
            )
            if target["ip_address"] != resolved_management_ip:
                print(
                    "ERROR: CDT selected member differs from the member whose "
                    "installed removal identity was resolved",
                    file=sys.stderr,
                )
                return 2
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
        matching_members = [
            member
            for member in members
            if (
                member.get("management_ip") or member.get("ip")
            ) == target["ip_address"]
            or (
                member.get("hostname")
                and member.get("hostname") == target["object_name"]
            )
        ]
        if len(matching_members) != 1:
            print("ERROR: selected CDT candidate does not map to exactly one plan member", file=sys.stderr)
            return 2
        selected_member = matching_members[0]
        reconciliation_host = (
            selected_member.get("access_ip")
            or selected_member.get("ip")
            or selected_member.get("management_ip")
        )
        if not reconciliation_host:
            print("ERROR: selected plan member has no reconciliation address", file=sys.stderr)
            return 2
        resolved_package_name = Path(str(package_path)).name
        if (
            not resolved_package_name
            or re.fullmatch(r'[A-Za-z0-9_.+-]+', resolved_package_name) is None
        ):
            print('ERROR: resolved package identity is unsafe', file=sys.stderr)
            return 2
        plan_bytes = Path(args.activity_plan_file).read_bytes()
        try:
            atomic_write_private_json(
                args.resolution_output,
                {
                    'schema': 1,
                    'operation_id': args.operation_id,
                    'change_identity': str(chg_number),
                    'activity_plan_sha256': hashlib.sha256(plan_bytes).hexdigest(),
                    'phase': args.phase,
                    'step_name': step_name,
                    'action': action,
                    'target_host': reconciliation_host,
                    'selected_candidate_ip': target['ip_address'],
                    'package_name': resolved_package_name,
                    'package_type': package_type,
                    'target_version': str(
                        package.get('target_version')
                        or checkpoint.get('target_version')
                        or ''
                    ),
                    'target_take': str(
                        package.get('target_take')
                        or checkpoint.get('target_take')
                        or ''
                    ),
                    'target_build': str(package.get('target_build') or ''),
                    'identity_source': (
                        'gateway-cpinstlog-via-cprid'
                        if action == 'remove'
                        else 'immutable-activity-plan'
                    ),
                    'context_id': secrets.token_hex(32),
                    'created_at_ns': time.time_ns(),
                },
            )
        except FileExistsError as exc:
            raise SystemExit(
                "ERROR: CDT context already exists; completed or uncertain "
                "artifact paths are immutable. Do not overwrite or retry this "
                "phase in the same governed operation."
            ) from exc
        print(f"Selected target: {target['object_name']} {target['ip_address']} ({target['state']})")
        print(f'CDT reconciliation context: {args.resolution_output}')
        print(f'Raw CDT candidates backup path: {raw_path}')
        print(f'CDT plan path: {plan_path}')
        print(f'CDT candidates path: {candidates_path}')
        return 0
    finally:
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
