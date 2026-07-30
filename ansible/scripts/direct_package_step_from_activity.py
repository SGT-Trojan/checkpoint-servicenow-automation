#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402
import generate_cdt_candidates_from_activity as cdt_candidates  # noqa: E402


def package_from_plan(plan: dict, step_name: str) -> dict:
    for step in plan.get('package_steps') or []:
        if step.get('name') == step_name:
            return step
    raise SystemExit(f'ERROR: step {step_name!r} not found in activity plan')


def load_cluster_state(reports_dir: Path, chg_number: str) -> dict:
    path = reports_dir / f'cluster_initial_state_{chg_number}.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def member_ips_for_phase(plan: dict, phase: str, reports_dir: Path) -> list[str]:
    checkpoint = plan.get('checkpoint', {})
    members = checkpoint.get('members') or []
    if not members:
        raise SystemExit('ERROR: activity plan has no checkpoint.members')
    cluster_mode = checkpoint.get('cluster_mode') or 'cluster'
    if cluster_mode == 'standalone' or len(members) == 1:
        return [members[0]['ip']]

    member_ips = [m["ip"] for m in members]
    if phase == "install-deployment-agent":
        return member_ips
    if phase not in {"first-member", "second-member"}:
        raise SystemExit(f"ERROR: unsupported direct package phase {phase!r}")

    chg_number = (plan.get("change") or {}).get("number", "unknown")
    state = load_cluster_state(reports_dir, chg_number)
    original_active = state.get("original_active_host")
    original_standby = state.get("original_standby_host")
    if not original_active or not original_standby:
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} must identify original active and standby members"
        )
    if original_active == original_standby or {original_active, original_standby} != set(member_ips):
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} does not match the activity-plan members"
        )
    return [original_standby if phase == "first-member" else original_active]


def package_identifier(step: dict) -> str:
    source_path = step.get('source_path') or ''
    package_name = step.get('package_name') or ''
    if package_name and package_name not in {step.get('name'), step.get('step_name')}:
        return package_name
    if source_path:
        return Path(source_path).name
    return package_name


def deployment_agent_package_build(step: dict) -> int | None:
    for value in (step.get("source_path"), step.get("package_name")):
        match = re.search(r"DeploymentAgent[_-]0*(\d+)", str(value or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def installed_deployment_agent_build(text: str) -> int | None:
    match = re.search(r"Build number:\s*(\d+)", text)
    return int(match.group(1)) if match else None


def commands_for_step(step: dict) -> list[str]:
    action = (step.get('action') or 'install').lower()
    package_type = (step.get('package_type') or '').lower()
    source_path = step.get('source_path') or ''
    package_name = package_identifier(step)
    if package_type == 'deployment_agent':
        if not source_path:
            raise SystemExit('ERROR: deployment_agent step requires source_path')
        return [f'installer agent install {source_path}', 'show installer status all']
    if action in {'install', 'upgrade'}:
        if not source_path:
            raise SystemExit('ERROR: install/upgrade step requires source_path')
        return [
            f'installer import local {source_path}',
            f'installer verify {package_name}',
            f'installer install {package_name}',
            'show installer status all',
            'show installer packages',
        ]
    if action == 'remove':
        if not package_name:
            raise SystemExit('ERROR: remove step requires package_name or source_path filename')
        return [f'installer uninstall {package_name}', 'reboot if CPUSE does not reboot automatically']
    raise SystemExit(f'ERROR: unsupported package action {action!r}')


def rc_captured_clish_command(command: str) -> str:
    return (
        f"clish -c {shlex.quote(command)}; "
        "rc=$?; printf '\\n__RC=%s\\n' \"$rc\""
    )


def run_checked(
    session: c.SshPty,
    host: str,
    command: str,
    timeout: int,
) -> str:
    executed = rc_captured_clish_command(command)
    result = session.run(executed, timeout=timeout)
    print(f'===== {host}: {command} =====')
    print(result.output.rstrip())
    rc = c.installer_return_code(result.output)
    if rc is None:
        raise RuntimeError(f'{host}: command did not return an exit status: {command}')
    if rc != 0:
        raise RuntimeError(f'{host}: command failed with exit status {rc}: {command}')
    lower = result.output.lower()
    fatal = ['failed', 'error', 'not allowed', 'not found', 'cannot']
    secondary_scan = re.sub(r"\b(?:no errors|errors?\s*:\s*0|0 errors?)\b", "", lower)
    if any(marker in secondary_scan for marker in fatal):
        raise RuntimeError(f'{host}: command reported failure marker: {command}')
    return result.output


def blocked_hotfixes_from_uninstall(text: str) -> list[str]:
    match = re.search(r'Uninstall the hotfix\(es\)\s+(.+?)\s+and try again', text, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    raw = match.group(1).replace('\n', ' ')
    return [part.strip(' .,;') for part in re.split(r'\s*,\s*|\s+and\s+', raw) if part.strip(' .,;')]


def resolve_remove_package_name(session: c.SshPty, step: dict) -> str:
    aliases = cdt_candidates.package_aliases(step, str(step.get("name") or "remove"))
    pattern = cdt_candidates.cpinstlog_grep_pattern(aliases)
    command = (
        "grep -hE "
        f"{cdt_candidates.shell_quote(pattern)} "
        "/opt/CPInstLog/collectors/da_actions_collector_*.csv* "
        "/opt/CPInstLog/DA_Actions.xml "
        "/opt/CPInstLog/da_cli.elg "
        "2>/dev/null"
    )
    history = session.run(command, timeout=180).output
    candidates = cdt_candidates.package_candidates_from_history(
        history,
        aliases,
        str(step.get("package_type") or ""),
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "local CPInstLog resolver must find exactly one uninstall identity; "
            f"aliases={aliases} candidates={candidates}"
        )
    print(f"Resolved direct uninstall identity from local CPInstLog: {candidates[0]}")
    return candidates[0]


def run_interactive_uninstall(session: c.SshPty, host: str, package_name: str, timeout: int) -> str:
    command = f'installer uninstall {package_name}'
    print(f'===== {host}: {command} =====')
    session.drain_pending()
    session.buffer = b''
    session.sendline(command)
    out = b''
    sent_confirmation = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = session._read_some(1)
        except Exception as exc:
            out += f'\nSESSION_CLOSED: {exc}\n'.encode()
            break
        if not chunk:
            continue
        out += chunk
        text = c.strip_ansi(out.decode(errors='replace'))
        if not sent_confirmation and re.search(r'(?i)Do you want to continue.*\[y\]es', text):
            session._write('y\n')
            sent_confirmation = True
        if 'Operation cancelled' in text:
            raise RuntimeError(f'{host}: uninstall was cancelled')
        if 'Result:' in text and c.PROMPT_RE.search(out):
            print(text.rstrip())
            if 'uninstalled successfully' in text.lower():
                return text
            blockers = blocked_hotfixes_from_uninstall(text)
            if blockers:
                blocker_list = ', '.join(blockers)
                raise RuntimeError(
                    f'{host}: uninstall of {package_name} is blocked by dependent hotfix(es): '
                    f'{blocker_list}. Add those as earlier remove steps or mark them in requires_absent.'
                )
            raise RuntimeError(f'{host}: uninstall did not report success')
    text = c.strip_ansi(out.decode(errors='replace'))
    print(text.rstrip())
    raise TimeoutError(f'{host}: timed out waiting for uninstall result')


def package_identity_is_installed(output: str, package_name: str) -> bool:
    filename = Path(package_name).name.lower()
    stem = re.sub(r"\.(?:tgz|tar)$", "", filename, flags=re.IGNORECASE)
    patterns = [re.escape(filename)]
    if stem != filename:
        patterns.append(rf"{re.escape(stem)}\.(?:tgz|tar)")
    for line in output.splitlines():
        lower = line.lower()
        if any(marker in lower for marker in ("not installed", "uninstalled", "removed")):
            continue
        for pattern in patterns:
            bounded = rf"(?<![A-Za-z0-9_.-])(?:{pattern})(?![A-Za-z0-9_.-])"
            if re.search(bounded, lower):
                return True
    return False


def verify_package_absent(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    package_name: str,
) -> None:
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        session.enter_expert(expert_password)
        output = run_checked(
            session,
            host,
            "show installer packages installed",
            180,
        )
        if package_identity_is_installed(output, package_name):
            raise RuntimeError(
                f"{host}: uninstall reconciliation found package still installed: "
                f"{package_name}"
            )
        print(f"{host}: confirmed package absent after uninstall: {package_name}")
    finally:
        session.close()


def wait_for_ssh_return(host: str, username: str, password: str, timeout: int) -> None:
    deadline = time.time() + timeout
    last_error = ''
    while time.time() < deadline:
        session = None
        try:
            session = c.SshPty(host, username, password, connect_timeout=8)
            session.connect()
            output = session.run('show version all', timeout=60).output
            print(f'===== {host}: returned after reboot =====')
            print(output.rstrip())
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(20)
        finally:
            if session:
                session.close()
    raise TimeoutError(f'{host}: timed out waiting for reboot return: {last_error}')


def wait_for_auto_reboot_start(host: str, username: str, password: str, grace: int) -> bool:
    if grace <= 0:
        return False
    print(f'===== {host}: waiting up to {grace}s for CPUSE automatic reboot =====')
    deadline = time.time() + grace
    while time.time() < deadline:
        session = None
        try:
            session = c.SshPty(host, username, password, connect_timeout=8)
            session.connect()
            session.run('show version all', timeout=30)
        except Exception as exc:
            print(f'{host}: SSH became unavailable; treating this as automatic reboot start: {exc}')
            return True
        finally:
            if session:
                session.close()
        time.sleep(15)
    print(f'{host}: no automatic reboot observed during grace period')
    return False


def reboot_and_wait(host: str, username: str, password: str, timeout: int) -> None:
    print(f'===== {host}: reboot and wait =====')
    try:
        session = c.SshPty(host, username, password, connect_timeout=10)
        session.connect()
        try:
            session.run('reboot', timeout=20)
        except Exception as exc:
            print(f'{host}: reboot command disconnected/timed out as expected: {exc}')
        finally:
            session.close()
    except Exception as exc:
        print(f'{host}: reboot command could not open a new session, assuming reboot is already in progress: {exc}')

    wait_for_ssh_return(host, username, password, timeout)


def wait_cluster_ready(host: str, username: str, password: str, timeout: int) -> None:
    deadline = time.time() + timeout
    last_output = ''
    while time.time() < deadline:
        session = None
        try:
            session = c.SshPty(host, username, password, connect_timeout=8)
            session.connect()
            output = session.run('cphaprob state', timeout=60).output
            last_output = output
            print(f'===== {host}: cphaprob state =====')
            print(output.rstrip())
            if 'Active PNOTEs: None' in output and any(state in output for state in ['STANDBY', 'ACTIVE']):
                return
        except Exception as exc:
            last_output = str(exc)
        finally:
            if session:
                session.close()
        time.sleep(30)
    raise TimeoutError(f'{host}: ClusterXL did not become ready after reboot. Last output: {last_output}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    parser.add_argument('--reports-dir', required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--step', required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--timeout', type=int, default=14400)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--auto-reboot-grace', type=int, default=7200)
    parser.add_argument('--explicit-reboot-fallback', action='store_true')
    args = parser.parse_args()

    plan = json.loads(Path(args.activity_plan_file).read_text())
    step = package_from_plan(plan, args.step)
    targets = member_ips_for_phase(plan, args.phase, Path(args.reports_dir))
    commands = commands_for_step(step)

    print('===== Direct package execution plan =====')
    print(f"Change: {(plan.get('change') or {}).get('number', 'unknown')}")
    print(f"Phase: {args.phase}")
    print(f"Step: {step.get('name')} ({step.get('action')} {step.get('package_type')})")
    print(f"Targets: {', '.join(targets)}")
    print('Commands:')
    for command in commands:
        print(f'  {command}')

    if not args.execute:
        print('Execution disabled. Re-run with --execute after approval.')
        return 3

    password = os.environ.get('CP_PASSWORD', '')
    expert_password = os.environ.get('CP_EXPERT_PASSWORD', '')
    if not password or not expert_password:
        raise SystemExit('ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required')

    action = (step.get('action') or 'install').lower()
    package_type = (step.get('package_type') or '').lower()

    def execute_host(host: str) -> None:
        print(f'===== Gateway {host} =====')
        session = c.SshPty(host, args.username, password, connect_timeout=20)
        try:
            session.connect()
            session.enter_expert(expert_password)
            if package_type == "deployment_agent" and action in {"install", "upgrade"}:
                requested_build = deployment_agent_package_build(step)
                status = run_checked(
                    session, host, "show installer status all", 180
                )
                installed_build = installed_deployment_agent_build(status)
                if requested_build is not None and installed_build is not None and installed_build >= requested_build:
                    print(f"{host}: Deployment Agent build {installed_build} already satisfies requested build {requested_build}; installation is an idempotent no-op.")
                    return
            if action == 'remove':
                package_name = resolve_remove_package_name(session, step)
                session.run('exit', timeout=30)
                run_interactive_uninstall(session, host, package_name, args.timeout)
                session.close()
                if wait_for_auto_reboot_start(host, args.username, password, args.auto_reboot_grace):
                    wait_for_ssh_return(host, args.username, password, min(args.timeout, 1800))
                elif args.explicit_reboot_fallback:
                    reboot_and_wait(host, args.username, password, min(args.timeout, 1800))
                else:
                    raise TimeoutError(
                        f'{host}: CPUSE did not start automatic reboot within '
                        f'{args.auto_reboot_grace}s after successful uninstall; '
                        'explicit reboot fallback is disabled'
                    )
                verify_package_absent(
                    host, args.username, password, expert_password, package_name
                )
                wait_cluster_ready(host, args.username, password, min(args.timeout, 1800))
            else:
                for command in commands:
                    run_checked(session, host, command, args.timeout)
        finally:
            session.close()

    if package_type == 'deployment_agent' and action in {'install', 'upgrade'} and len(targets) > 1:
        print('===== Deployment Agent parallel execution =====')
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            future_map = {pool.submit(execute_host, host): host for host in targets}
            failures = []
            for future in concurrent.futures.as_completed(future_map):
                host = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append(f'{host}: {exc}')
            if failures:
                raise RuntimeError('Deployment Agent install failed on: ' + '; '.join(failures))
    else:
        for host in targets:
            execute_host(host)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
