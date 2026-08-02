#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402
import generate_cdt_candidates_from_activity as cdt_candidates  # noqa: E402

OPERATION_ID_RE = re.compile(r"run_[0-9a-f]{64}")
STANDALONE_RUN_ID_RE = re.compile(r"run_[0-9a-f]{64}")
STANDALONE_OPERATION_ID_RE = re.compile(r"operation_[0-9a-f]{64}")
MUTATION_INTENT_VERSION = 3
RECONCILIATION_VERSION = 3


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def mutation_intent_document(intent: dict[str, str]) -> dict[str, object]:
    return {"schema": MUTATION_INTENT_VERSION, **intent}


def package_from_plan(plan: dict, step_name: str) -> dict:
    for step in plan.get('package_steps') or []:
        if step.get('name') == step_name:
            return step
    raise SystemExit(f'ERROR: step {step_name!r} not found in activity plan')


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
    except json.JSONDecodeError:
        return {}


def member_ips_for_phase(
    plan: dict,
    phase: str,
    reports_dir: Path | None,
    state_file: Path | None = None,
) -> list[str]:
    checkpoint = plan.get('checkpoint', {})
    members = checkpoint.get('members') or []
    if not members:
        raise SystemExit('ERROR: activity plan has no checkpoint.members')
    cluster_mode = checkpoint.get('cluster_mode') or 'cluster'
    if cluster_mode == 'standalone' or len(members) == 1:
        return [members[0]['ip']]

    member_ips: list[str] = []
    member_identities: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for member in members:
        value = member.get("ip") if isinstance(member, dict) else None
        if not isinstance(value, str) or not value:
            raise SystemExit("ERROR: activity-plan member IPs must be non-empty strings")
        try:
            identity = ipaddress.ip_address(value)
        except ValueError as exc:
            raise SystemExit(
                f"ERROR: invalid activity-plan member IP address {value!r}"
            ) from exc
        member_ips.append(value)
        member_identities.append(identity)
    if len(member_identities) != len(set(member_identities)):
        raise SystemExit("ERROR: activity-plan member IP addresses must be distinct")
    if phase == "install-deployment-agent":
        return member_ips
    if phase not in {"first-member", "second-member"}:
        raise SystemExit(f"ERROR: unsupported direct package phase {phase!r}")

    chg_number = (plan.get("change") or {}).get("number", "unknown")
    state = load_cluster_state(reports_dir, chg_number, state_file)
    original_active = state.get("original_active_host")
    original_standby = state.get("original_standby_host")
    if not original_active or not original_standby:
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} must identify original active and standby members"
        )
    try:
        captured_identities = {
            ipaddress.ip_address(original_active),
            ipaddress.ip_address(original_standby),
        }
    except ValueError as exc:
        raise SystemExit(
            "ERROR: captured cluster state contains an invalid member IP"
        ) from exc
    if len(captured_identities) != 2 or captured_identities != set(member_identities):
        raise SystemExit(
            f"ERROR: captured cluster state for {chg_number} does not match the activity-plan members"
        )
    dispatch_by_identity = dict(zip(member_identities, member_ips, strict=True))
    selected = (
        ipaddress.ip_address(original_standby)
        if phase == "first-member"
        else ipaddress.ip_address(original_active)
    )
    return [dispatch_by_identity[selected]]


def package_identifier(step: dict) -> str:
    source_path = step.get('source_path') or ''
    package_name = step.get('package_name') or ''
    if package_name and package_name not in {step.get('name'), step.get('step_name')}:
        return package_name
    if source_path:
        return Path(source_path).name
    return package_name


def requested_deployment_agent_minimum_build(step: dict) -> int:
    explicit = step.get("requested_build", step.get("target_build", step.get("build")))
    if explicit is not None and explicit != "":
        if isinstance(explicit, bool):
            raise RuntimeError(
                "deployment_agent requested build must be a positive integer"
            )
        value = str(explicit).strip()
        if not re.fullmatch(r"\d+", value):
            raise RuntimeError(
                "deployment_agent requested build must be a positive integer"
            )
        build = int(value)
        if build <= 0:
            raise RuntimeError(
                "deployment_agent requested build must be a positive integer"
            )
        return build
    for value in (step.get("source_path"), step.get("package_name")):
        match = re.search(r"DeploymentAgent[_-]0*(\d+)", str(value or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    raise RuntimeError(
        "deployment_agent step requires requested_build or a "
        "DeploymentAgent_<build> package identity"
    )


def installed_deployment_agent_build(text: str) -> int | None:
    matches = {int(value) for value in re.findall(r"Build number:\s*(\d+)", text)}
    return next(iter(matches)) if len(matches) == 1 else None


def verify_deployment_agent_minimum_build(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    requested_minimum_build: int,
) -> dict[str, str]:
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        session.enter_expert(expert_password)
        status = run_checked(session, host, "show installer status all", 180)
        installed_build = installed_deployment_agent_build(status)
        if installed_build is None:
            raise RuntimeError(
                f"{host}: Deployment Agent reconciliation returned no unique installed build"
            )
        if installed_build < requested_minimum_build:
            raise RuntimeError(
                f"{host}: Deployment Agent build {installed_build} is below "
                f"requested minimum build {requested_minimum_build}"
            )
        return {
            "host": host,
            "requested_minimum_build": str(requested_minimum_build),
            "observed_build": str(installed_build),
        }
    finally:
        session.close()


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
        installer_action = 'upgrade' if action == 'upgrade' else 'install'
        return [
            f'installer import local {source_path}',
            f'installer verify {package_name}',
            f'installer {installer_action} {package_name}',
            'show installer status all',
            'show installer packages',
        ]
    if action == 'remove':
        if not package_name:
            raise SystemExit('ERROR: remove step requires package_name or source_path filename')
        return [f'installer uninstall {package_name}', 'reboot if CPUSE does not reboot automatically']
    raise SystemExit(f'ERROR: unsupported package action {action!r}')


def clish_command_requires_lock(command: str) -> bool:
    return command.startswith("installer ")

def installer_confirmation_pattern(
    command: str, package_name: str
) -> re.Pattern[bytes]:
    match = re.fullmatch(r"installer (install|upgrade) .+", command)
    if match is None:
        raise RuntimeError(
            f"installer confirmation is unsupported for command: {command}"
        )
    filename = Path(package_name).name
    stem = re.sub(r"\.(?:tgz|tar)$", "", filename, flags=re.IGNORECASE)
    if not stem:
        raise RuntimeError("installer confirmation requires a package identity")
    package_pattern = (
        re.escape(stem).encode() + rb"\.(?:tgz|tar)"
        if stem != filename
        else re.escape(filename).encode()
    )
    choices = (
        rb"\(\[y\]es / \[n\]o / \[s\]uppress reboot\)"
        if match.group(1) == "install"
        else rb"\(\[y\]es / \[n\]o\)"
    )
    return re.compile(
        rb"The machine will automatically reboot after "
        + match.group(1).encode()
        + rb" of "
        + package_pattern
        + rb"\.\s*Do you want to continue\? "
        + choices
        + rb"\s*",
        re.IGNORECASE,
    )


def run_checked(
    session: c.SshPty,
    host: str,
    command: str,
    timeout: int,
    confirmation_pattern: re.Pattern[bytes] | None = None,
) -> str:
    if confirmation_pattern is None:
        result = session.run_interactive_clish(
            command,
            acquire_lock=clish_command_requires_lock(command),
            timeout=timeout,
        )
    else:
        result = session.run_interactive_clish(
            command,
            acquire_lock=clish_command_requires_lock(command),
            timeout=timeout,
            confirmation_pattern=confirmation_pattern,
            confirmation_response="y",
        )
    print(f'===== {host}: {command} =====')
    print(result.output.rstrip())
    rc = c.installer_return_code(result.output)
    completion = getattr(result, "completion", "")
    if completion == "clish-prompt":
        if rc is not None:
            raise RuntimeError(
                f'{host}: parent Clish output contained an ambiguous shell exit marker: {command}'
            )
    elif rc is None:
        raise RuntimeError(f'{host}: command did not return an exit status: {command}')
    elif rc != 0:
        raise RuntimeError(f'{host}: command failed with exit status {rc}: {command}')
    lower = result.output.lower()
    fatal = [
        'failed',
        'error',
        'not allowed',
        'not found',
        'cannot',
        'clinfr',
        'invalid command',
        'unknown command',
    ]
    lower = re.sub(
        r"(?im)^(?:/bin/)?(?:ba)?sh: warning: setlocale: lc_all: "
        r"cannot change locale \(c\.utf-8\)\r?$",
        "",
        lower,
    )
    secondary_scan = re.sub(r"\b(?:no errors|errors?\s*:\s*0|0 errors?)\b", "", lower)
    if any(marker in secondary_scan for marker in fatal):
        raise RuntimeError(f'{host}: command reported failure marker: {command}')
    return result.output


def run_installer_mutation(
    session: c.SshPty,
    host: str,
    command: str,
    timeout: int,
    confirmation_pattern: re.Pattern[bytes] | None = None,
) -> bool:
    """Run an installer command; return True only for a provisional disconnect."""
    try:
        run_checked(
            session,
            host,
            command,
            timeout,
            confirmation_pattern=confirmation_pattern,
        )
        return False
    except c.CheckPointError as exc:
        text = str(exc).lower()
        if not any(
            marker in text for marker in ("closed", "timed out", "not connected")
        ):
            raise
        print(
            f"{host}: installer session disconnected before an exit status was available; "
            "the operation remains uncommitted until exact reconciliation succeeds: "
            f"{exc}"
        )
        return True


def blocked_hotfixes_from_uninstall(text: str) -> list[str]:
    match = re.search(r'Uninstall the hotfix\(es\)\s+(.+?)\s+and try again', text, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    raw = match.group(1).replace('\n', ' ')
    return [part.strip(' .,;') for part in re.split(r'\s*,\s*|\s+and\s+', raw) if part.strip(' .,;')]


def remove_identity_inputs(
    session: c.SshPty, step: dict
) -> tuple[list[str], str, str]:
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
    installed_table = run_checked(
        session,
        str(getattr(session, "host", "gateway")),
        "show installer packages installed",
        180,
    )
    return aliases, history, installed_table


def resolve_remove_package_name(session: c.SshPty, step: dict) -> str:
    aliases, history, installed_table = remove_identity_inputs(session, step)
    try:
        selected = cdt_candidates.resolve_current_remove_identity(
            history,
            installed_table,
            aliases,
            str(step.get("package_type") or ""),
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "local removal resolver did not find one exact currently installed "
            f"identity: {exc}"
        ) from exc
    print(
        "Resolved direct uninstall identity from local CPInstLog aliases and "
        f"current installed-package table: {selected}"
    )
    return selected


def validate_persisted_remove_identity(
    session: c.SshPty, step: dict, persisted_identity: str
) -> None:
    aliases, history, installed_table = remove_identity_inputs(session, step)
    history_candidates = cdt_candidates.package_candidates_from_history(
        history,
        aliases,
        str(step.get("package_type") or ""),
    )
    if persisted_identity not in history_candidates:
        raise RuntimeError(
            "persisted uninstall identity is no longer supported by fresh local "
            f"CPInstLog alias evidence: identity={persisted_identity!r} "
            f"candidates={history_candidates}"
        )
    current_installed = cdt_candidates.installed_package_identities(installed_table)
    if persisted_identity in current_installed:
        raise RuntimeError(
            "persisted uninstall identity remains in the authoritative current "
            f"installed-package table: {persisted_identity}"
        )


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
    return cdt_candidates.package_identity_is_installed(output, package_name)


def verify_package_absent(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    package_name: str,
) -> dict[str, str]:
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
        return {
            'host': host,
            'package_name': package_name,
            'result': 'exact-package-absence-confirmed',
        }
    finally:
        session.close()


def installed_jhf_take(output: str, target_version: str) -> str | None:
    target_release = target_version.upper().replace(".", "_")
    matches = re.findall(
        r"(?:HOTFIX|BUNDLE)_(R\d+(?:_\d+)*)_JUMBO_HF_MAIN\s+Take:\s*(\d+)",
        output,
        re.IGNORECASE,
    )
    matching_takes = [
        take for release, take in matches if release.upper() == target_release
    ]
    return matching_takes[-1] if matching_takes else None


def run_expert_checked(
    session: c.SshPty,
    host: str,
    command: str,
    timeout: int,
) -> str:
    executed = f"{command}; rc=$?; printf '\\n__RC=%s\\n' \"$rc\""
    result = session.run(executed, timeout=timeout)
    print(f'===== {host}: {command} =====')
    print(result.output.rstrip())
    rc = c.installer_return_code(result.output)
    if rc is None:
        raise RuntimeError(f'{host}: reconciliation command did not return an exit status: {command}')
    if rc != 0:
        raise RuntimeError(f'{host}: reconciliation command failed with exit status {rc}: {command}')
    return result.output


def target_state_for_step(plan: dict, step: dict) -> tuple[str, str, str]:
    checkpoint = plan.get('checkpoint') or {}
    target_version = str(step.get('target_version') or checkpoint.get('target_version') or '')
    target_take = str(step.get('target_take') or checkpoint.get('target_take') or '')
    package_name = package_identifier(step)
    if not target_version:
        raise RuntimeError('install/upgrade reconciliation requires checkpoint.target_version')
    if not re.fullmatch(r'\d{1,4}', target_take):
        raise RuntimeError('install/upgrade reconciliation requires a numeric checkpoint.target_take')
    if not package_name:
        raise RuntimeError('install/upgrade reconciliation requires an exact package identity')
    return target_version, target_take, package_name


def verify_package_present(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    target_version: str,
    target_take: str,
    package_name: str,
) -> dict[str, str]:
    session = c.SshPty(host, username, password, connect_timeout=20)
    try:
        session.connect()
        session.enter_expert(expert_password)
        version_output = run_checked(session, host, 'show version all', 180)
        if not c.version_output_matches_target(version_output, target_version):
            raise RuntimeError(
                f'{host}: reconciliation found the wrong release; expected {target_version}'
            )
        take_output = run_expert_checked(
            session,
            host,
            "cpinfo -y all | egrep -i '(HOTFIX|BUNDLE)_R[0-9_]+_JUMBO_HF_MAIN|No hotfixes' | head -120",
            180,
        )
        installed_take = installed_jhf_take(take_output, target_version)
        if installed_take != target_take:
            actual = installed_take or 'missing'
            raise RuntimeError(
                f'{host}: reconciliation found Take {actual}; expected Take {target_take}'
            )
        packages_output = run_checked(
            session, host, 'show installer packages installed', 180
        )
        if not package_identity_is_installed(packages_output, package_name):
            raise RuntimeError(
                f'{host}: reconciliation did not find exact installed package: {package_name}'
            )
        result = {
            'host': host,
            'target_version': target_version,
            'target_take': target_take,
            'package_name': package_name,
            'result': 'exact-target-confirmed',
        }
        print('RECONCILIATION=' + json.dumps(result, sort_keys=True))
        return result
    finally:
        session.close()


def wait_for_package_present(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    target_version: str,
    target_take: str,
    package_name: str,
    timeout: int,
) -> dict[str, str]:
    deadline = time.time() + timeout
    last_error = ''
    while time.time() < deadline:
        try:
            return verify_package_present(
                host,
                username,
                password,
                expert_password,
                target_version,
                target_take,
                package_name,
            )
        except c.CheckPointError as exc:
            if 'host key mismatch' in str(exc).lower():
                raise
            last_error = str(exc)
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(30)
    raise TimeoutError(
        f'{host}: exact target reconciliation did not succeed before timeout: {last_error}'
    )


def write_reconciliation(path: Path, result: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_reconciliation_fd(fd: int, result: dict[str, str]) -> None:
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError(
            "inherited reconciliation descriptor is not a private owned regular file"
        )
    if metadata.st_size != 0:
        raise RuntimeError("inherited reconciliation descriptor contains stale bytes")
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RuntimeError("failed to write inherited reconciliation descriptor")
        view = view[written:]
    os.fsync(fd)


def standalone_context(
    args: argparse.Namespace,
    plan_sha256: str | None = None,
) -> dict[str, str] | None:
    names = (
        "standalone_run_id",
        "standalone_plan_sha256",
        "standalone_phase",
        "standalone_operation_id",
        "standalone_completion_id",
        "standalone_event_nonce",
    )
    values = {name: str(getattr(args, name, "") or "") for name in names}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise RuntimeError("standalone reconciliation context must be supplied completely")
    if STANDALONE_RUN_ID_RE.fullmatch(values["standalone_run_id"]) is None:
        raise RuntimeError("standalone reconciliation context has an invalid run ID")
    for name in (
        "standalone_plan_sha256",
        "standalone_completion_id",
        "standalone_event_nonce",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", values[name]) is None:
            raise RuntimeError(
                f"standalone reconciliation context has an invalid {name}"
            )
    if (
        STANDALONE_OPERATION_ID_RE.fullmatch(
            values["standalone_operation_id"]
        )
        is None
    ):
        raise RuntimeError(
            "standalone reconciliation context has an invalid operation ID"
        )
    if values["standalone_phase"] != str(args.phase):
        raise RuntimeError("standalone reconciliation phase does not match helper phase")
    if plan_sha256 is not None and values["standalone_plan_sha256"] != plan_sha256:
        raise RuntimeError("standalone reconciliation plan hash does not match input")
    if (
        getattr(args, "reconciliation_fd", None) is None
        and not getattr(args, "reconciliation_file", None)
    ):
        raise RuntimeError("standalone execution requires reconciliation output")
    return {
        "run_id": values["standalone_run_id"],
        "plan_sha256": values["standalone_plan_sha256"],
        "phase": values["standalone_phase"],
        "operation_id": values["standalone_operation_id"],
        "completion_id": values["standalone_completion_id"],
        "event_nonce": values["standalone_event_nonce"],
    }


def bound_reconciliation(
    args: argparse.Namespace,
    result: dict[str, str],
    intent: dict[str, str] | None,
) -> dict[str, str | int]:
    context = standalone_context(args)
    if context is None:
        return result
    if intent is None:
        raise RuntimeError("standalone reconciliation requires mutation intent binding")
    reserved = {
        "schema_version",
        "run_id",
        "plan_sha256",
        "phase",
        "operation_id",
        "completion_id",
        "event_nonce",
        "mutation_intent_sha256",
    }
    if reserved.intersection(result):
        raise RuntimeError("reconciliation result contains reserved binding fields")
    return {
        **result,
        "schema_version": RECONCILIATION_VERSION,
        **context,
        "mutation_intent_sha256": hashlib.sha256(
            canonical_json(mutation_intent_document(intent))
        ).hexdigest(),
    }


def emit_reconciliation(
    args: argparse.Namespace,
    result: dict[str, str],
    intent: dict[str, str] | None = None,
) -> None:
    payload = bound_reconciliation(args, result, intent)
    if args.reconciliation_fd is not None:
        write_reconciliation_fd(args.reconciliation_fd, payload)
    elif args.reconciliation_file:
        write_reconciliation(Path(args.reconciliation_file), payload)

def read_mutation_intent(
    path: Path | None,
    expected: dict[str, str],
) -> dict[str, str] | None:
    if path is None:
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"mutation intent cannot be opened safely: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("mutation intent must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("mutation intent must have mode 0600")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            intent = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"mutation intent is unreadable: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(intent, dict) or intent.get("schema") != MUTATION_INTENT_VERSION:
        raise RuntimeError("mutation intent has an unsupported schema")
    for key, value in expected.items():
        if intent.get(key) != value:
            raise RuntimeError(f"mutation intent does not match expected {key}")
    package_name = intent.get("package_name")
    if not isinstance(package_name, str) or not package_name:
        raise RuntimeError("mutation intent has no exact package identity")
    return intent


def persist_mutation_intent(path: Path, intent: dict[str, str]) -> bool:
    """Persist intent before dispatch; return False when an identical intent exists."""
    existing = read_mutation_intent(path, intent)
    if existing is not None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not stat.S_ISDIR(path.parent.stat().st_mode):
        raise RuntimeError("mutation intent directory must be a real directory")
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError("mutation intent directory must not be group/world accessible")
    payload = mutation_intent_document(intent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if read_mutation_intent(path, intent) is not None:
                return False
            raise
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def uncertain_intent_error(path: Path, exc: Exception) -> RuntimeError:
    return RuntimeError(
        "persisted mutation intent could not be reconciled; its dispatch state is "
        "uncertain and it must never be cleared, deleted, or reused. Preserve the "
        f"run directory and intent evidence at {path}, restore the authorized clean "
        "snapshot/baseline, then start a new run directory with a new protected plan "
        f"instance. Last reconciliation error: {exc}"
    )


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
            if 'host key mismatch' in str(exc).lower():
                raise
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


def policy_handoff_allowed(package_type: str, action: str) -> bool:
    return package_type == "blink" and action in {"install", "upgrade"}


def wait_cluster_ready(
    host: str,
    username: str,
    password: str,
    timeout: int,
    *,
    allow_policy_handoff: bool = False,
) -> None:
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
            if allow_policy_handoff and 'HA module not started' in output:
                print(
                    f'{host}: Blink target is reconciled but ClusterXL awaits the '
                    'mandatory new-version policy phase; continuing to that gate'
                )
                return
        except Exception as exc:
            last_output = str(exc)
        finally:
            if session:
                session.close()
        time.sleep(30)
    raise TimeoutError(f'{host}: ClusterXL did not become ready after reboot. Last output: {last_output}')




def governed_intent_path(
    intent_dir: Path,
    operation_id: str,
    phase: str,
    step_name: str,
    host: str,
) -> Path:
    if not OPERATION_ID_RE.fullmatch(operation_id):
        raise RuntimeError("governed mutation intent requires a valid operation ID")
    if intent_dir.is_symlink() or not intent_dir.is_dir():
        raise RuntimeError("governed mutation intent directory must be a real directory")
    metadata = intent_dir.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(
            "governed mutation intent directory must be owner-only mode 0700"
        )
    identity = json.dumps(
        {
            "operation_id": operation_id,
            "phase": phase,
            "step": step_name,
            "host": host,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return intent_dir / f"{hashlib.sha256(identity).hexdigest()}.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-plan-file', required=True)
    state_source = parser.add_mutually_exclusive_group(required=True)
    state_source.add_argument('--reports-dir')
    state_source.add_argument('--state-file')
    parser.add_argument('--phase', required=True)
    parser.add_argument('--step', required=True)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--timeout', type=int, default=14400)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--auto-reboot-grace', type=int, default=7200)
    parser.add_argument('--explicit-reboot-fallback', action='store_true')
    reconciliation_output = parser.add_mutually_exclusive_group()
    reconciliation_output.add_argument('--reconciliation-file')
    reconciliation_output.add_argument('--reconciliation-fd', type=int)
    parser.add_argument('--mutation-intent-file')
    parser.add_argument('--operation-id', default='')
    parser.add_argument('--mutation-intent-dir')
    parser.add_argument('--standalone-run-id', default='')
    parser.add_argument('--standalone-plan-sha256', default='')
    parser.add_argument('--standalone-phase', default='')
    parser.add_argument('--standalone-operation-id', default='')
    parser.add_argument('--standalone-completion-id', default='')
    parser.add_argument('--standalone-event-nonce', default='')
    parser.add_argument('--standalone-reconciliation-only', action='store_true')
    args = parser.parse_args()

    plan_bytes = Path(args.activity_plan_file).read_bytes()
    plan = json.loads(plan_bytes)
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    standalone = standalone_context(args, plan_sha256)
    if args.standalone_reconciliation_only and standalone is None:
        raise RuntimeError(
            "--standalone-reconciliation-only requires standalone context"
        )
    step = package_from_plan(plan, args.step)
    targets = member_ips_for_phase(
        plan,
        args.phase,
        Path(args.reports_dir) if args.reports_dir else None,
        Path(args.state_file) if args.state_file else None,
    )
    commands = commands_for_step(step)
    package_type = (step.get('package_type') or '').lower()
    requested_da_minimum_build = (
        requested_deployment_agent_minimum_build(step)
        if package_type == 'deployment_agent'
        else None
    )

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

    if not args.mutation_intent_file and not args.mutation_intent_dir:
        raise SystemExit(
            "ERROR: executing direct operations requires durable mutation intent state"
        )
    if bool(args.mutation_intent_dir) != bool(args.operation_id):
        raise SystemExit(
            "ERROR: --mutation-intent-dir and --operation-id must be supplied together"
        )

    password = os.environ.get('CP_PASSWORD', '')
    expert_password = os.environ.get('CP_EXPERT_PASSWORD', '')
    if not password or not expert_password:
        raise SystemExit('ERROR: CP_PASSWORD and CP_EXPERT_PASSWORD are required')

    action = (step.get('action') or 'install').lower()

    def execute_host(host: str) -> None:
        print(f'===== Gateway {host} =====')
        if args.mutation_intent_dir:
            intent_path = governed_intent_path(
                Path(args.mutation_intent_dir),
                args.operation_id,
                args.phase,
                str(step.get("name") or args.step),
                host,
            )
        else:
            intent_path = Path(args.mutation_intent_file)
        intent_base = {
            "host": host,
            "action": action,
            "step_name": str(step.get("name") or args.step),
            "plan_sha256": plan_sha256,
            "requested_package_name": str(step.get("package_name") or ""),
            "requested_source_path": str(step.get("source_path") or ""),
            "requested_package_type": str(step.get("package_type") or ""),
        }
        if args.operation_id:
            intent_base.update({"operation_id": args.operation_id, "phase": args.phase})
        if standalone:
            intent_base.update(
                {
                    "standalone_run_id": standalone["run_id"],
                    "standalone_operation_id": standalone["operation_id"],
                    "standalone_completion_id": standalone["completion_id"],
                    "standalone_event_nonce": standalone["event_nonce"],
                    "phase": standalone["phase"],
                }
            )

        def reconcile_intent(intent: dict[str, str]) -> None:
            package_name = intent["package_name"]
            print(
                f"{host}: prior mutation dispatch intent found; retry is "
                "reconciliation-only"
            )
            try:
                if package_type == "deployment_agent":
                    if intent.get("requested_minimum_build") != str(
                        requested_da_minimum_build
                    ):
                        raise RuntimeError(
                            "mutation intent does not match requested Deployment Agent "
                            "minimum build"
                        )
                    reconciliation = verify_deployment_agent_minimum_build(
                        host,
                        args.username,
                        password,
                        expert_password,
                        requested_da_minimum_build,
                    )
                elif action == "remove":
                    identity_session = c.SshPty(
                        host, args.username, password, connect_timeout=20
                    )
                    try:
                        identity_session.connect()
                        identity_session.enter_expert(expert_password)
                        validate_persisted_remove_identity(
                            identity_session, step, package_name
                        )
                    finally:
                        identity_session.close()
                    reconciliation = verify_package_absent(
                        host, args.username, password, expert_password, package_name
                    )
                else:
                    target_version, target_take, expected_package = target_state_for_step(
                        plan, step
                    )
                    for key, value in (
                        ("target_version", target_version),
                        ("target_take", target_take),
                        ("package_name", expected_package),
                    ):
                        if intent.get(key) != value:
                            raise RuntimeError(
                                f"mutation intent does not match expected {key}"
                            )
                    reconciliation = wait_for_package_present(
                        host,
                        args.username,
                        password,
                        expert_password,
                        target_version,
                        target_take,
                        package_name,
                        min(args.timeout, 1800),
                    )
            except c.CheckPointError:
                # Connection and host-key stops remain safe to retry because this
                # path can only reconcile; it cannot dispatch another mutation.
                raise
            except Exception as exc:
                raise uncertain_intent_error(intent_path, exc) from exc
            emit_reconciliation(args, reconciliation, intent)
            if package_type != "deployment_agent":
                wait_cluster_ready(
                    host,
                    args.username,
                    password,
                    min(args.timeout, 1800),
                    allow_policy_handoff=policy_handoff_allowed(package_type, action),
                )

        existing_intent = read_mutation_intent(intent_path, intent_base)
        if existing_intent is not None:
            reconcile_intent(existing_intent)
            return
        session = c.SshPty(host, args.username, password, connect_timeout=20)
        try:
            session.connect()
            session.enter_expert(expert_password)
            if package_type == "deployment_agent" and action in {"install", "upgrade"}:
                status = run_checked(
                    session, host, "show installer status all", 180
                )
                installed_build = installed_deployment_agent_build(status)
                if installed_build is None:
                    raise RuntimeError(
                        f"{host}: current Deployment Agent build is missing or ambiguous"
                    )
                if installed_build >= requested_da_minimum_build:
                    print(
                        f"{host}: Deployment Agent build {installed_build} already "
                        "satisfies requested minimum build "
                        f"{requested_da_minimum_build}; installation is an "
                        "idempotent no-op."
                    )
                    no_op_intent = {
                        **intent_base,
                        "package_name": Path(
                            str(step.get("source_path") or "")
                        ).name,
                        "requested_minimum_build": str(requested_da_minimum_build),
                        "observed_build_before_dispatch": str(installed_build),
                    }
                    emit_reconciliation(
                        args,
                        {
                            "host": host,
                            "requested_minimum_build": str(
                                requested_da_minimum_build
                            ),
                            "observed_build": str(installed_build),
                            "result": "minimum-build-satisfied",
                        },
                        no_op_intent,
                    )
                    return
                dispatch_intent = {
                    **intent_base,
                    "package_name": Path(
                        str(step.get("source_path") or "")
                    ).name,
                    "requested_minimum_build": str(requested_da_minimum_build),
                    "observed_build_before_dispatch": str(installed_build),
                }
                if not persist_mutation_intent(intent_path, dispatch_intent):
                    session.close()
                    existing_intent = read_mutation_intent(
                        intent_path, dispatch_intent
                    )
                    if existing_intent is None:
                        raise RuntimeError(
                            "mutation intent disappeared before reconciliation"
                        )
                    reconcile_intent(existing_intent)
                    return
                dispatch_error = None
                try:
                    run_installer_mutation(
                        session, host, commands[0], args.timeout
                    )
                except Exception as exc:
                    dispatch_error = exc
                finally:
                    session.close()
                try:
                    reconciliation = verify_deployment_agent_minimum_build(
                        host,
                        args.username,
                        password,
                        expert_password,
                        requested_da_minimum_build,
                    )
                except Exception as exc:
                    detail = (
                        f"{dispatch_error}; reconciliation failed: {exc}"
                        if dispatch_error
                        else str(exc)
                    )
                    raise uncertain_intent_error(
                        intent_path, RuntimeError(detail)
                    ) from exc
                if dispatch_error:
                    print(
                        f"{host}: installer outcome was unavailable ({dispatch_error}); "
                        "fresh build reconciliation proved success"
                    )
                emit_reconciliation(args, reconciliation, dispatch_intent)
                return
            if package_type != "deployment_agent" and action in {"install", "upgrade"}:
                target_version, target_take, package_name = target_state_for_step(
                    plan, step
                )
                planned_intent = {
                    **intent_base,
                    "package_name": package_name,
                    "target_version": target_version,
                    "target_take": target_take,
                }
                try:
                    reconciliation = verify_package_present(
                        host,
                        args.username,
                        password,
                        expert_password,
                        target_version,
                        target_take,
                        package_name,
                    )
                except c.CheckPointError:
                    raise
                except RuntimeError as exc:
                    print(
                        f'{host}: target is not already reconciled; proceeding with '
                        f'the approved installer step: {exc}'
                    )
                else:
                    print(
                        f'{host}: exact target already reconciled; installer execution '
                        'is an idempotent no-op'
                    )
                    if not persist_mutation_intent(intent_path, planned_intent):
                        existing_intent = read_mutation_intent(
                            intent_path, planned_intent
                        )
                        if existing_intent is None:
                            raise RuntimeError(
                                "mutation intent disappeared before reconciliation"
                            )
                        planned_intent = existing_intent
                    emit_reconciliation(args, reconciliation, planned_intent)
                    return
                if standalone and args.standalone_reconciliation_only:
                    raise RuntimeError(
                        f"{host}: pending member operation is reconciliation-only; "
                        "the target is not reconciled and mutation redispatch is prohibited"
                    )
            if action == 'remove':
                package_name = resolve_remove_package_name(session, step)
                dispatch_intent = {
                    **intent_base,
                    "package_name": package_name,
                }
                if standalone and args.standalone_reconciliation_only:
                    try:
                        reconciliation = verify_package_absent(
                            host,
                            args.username,
                            password,
                            expert_password,
                            package_name,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"{host}: pending member operation is reconciliation-only; "
                            "package absence is not proven and mutation redispatch is prohibited"
                        ) from exc
                    if not persist_mutation_intent(intent_path, dispatch_intent):
                        existing_intent = read_mutation_intent(
                            intent_path, dispatch_intent
                        )
                        if existing_intent is None:
                            raise RuntimeError(
                                "mutation intent disappeared before reconciliation"
                            )
                        dispatch_intent = existing_intent
                    emit_reconciliation(args, reconciliation, dispatch_intent)
                    wait_cluster_ready(
                        host,
                        args.username,
                        password,
                        min(args.timeout, 1800),
                    )
                    return
                if intent_path is not None and not persist_mutation_intent(
                    intent_path, dispatch_intent
                ):
                    session.close()
                    existing_intent = read_mutation_intent(
                        intent_path, dispatch_intent
                    )
                    if existing_intent is None:
                        raise RuntimeError(
                            "mutation intent disappeared before reconciliation"
                        )
                    reconcile_intent(existing_intent)
                    return
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
                reconciliation = verify_package_absent(
                    host, args.username, password, expert_password, package_name
                )
                emit_reconciliation(args, reconciliation, dispatch_intent)
                wait_cluster_ready(
                    host,
                    args.username,
                    password,
                    min(args.timeout, 1800),
                    allow_policy_handoff=policy_handoff_allowed(package_type, action),
                )
            else:
                target_version, target_take, package_name = target_state_for_step(
                    plan, step
                )
                package_status = run_checked(
                    session, host, "show installer packages", args.timeout
                )
                if c.package_table_has_ready_package(package_status, package_name):
                    print(
                        f"{host}: exact package is already imported/installable; "
                        "skipping redundant import"
                    )
                else:
                    run_checked(session, host, commands[0], args.timeout)
                run_checked(session, host, commands[1], args.timeout)
                dispatch_intent = planned_intent
                if intent_path is not None and not persist_mutation_intent(
                    intent_path, dispatch_intent
                ):
                    session.close()
                    existing_intent = read_mutation_intent(
                        intent_path, dispatch_intent
                    )
                    if existing_intent is None:
                        raise RuntimeError(
                            "mutation intent disappeared before reconciliation"
                        )
                    reconcile_intent(existing_intent)
                    return
                disconnected = run_installer_mutation(
                    session,
                    host,
                    commands[2],
                    args.timeout,
                    confirmation_pattern=installer_confirmation_pattern(
                        commands[2], package_name
                    ),
                )
                session.close()
                reboot_expected = bool(step.get('reboot_expected', True))
                if disconnected:
                    wait_for_ssh_return(
                        host, args.username, password, min(args.timeout, 1800)
                    )
                elif reboot_expected:
                    if wait_for_auto_reboot_start(
                        host, args.username, password, args.auto_reboot_grace
                    ):
                        wait_for_ssh_return(
                            host, args.username, password, min(args.timeout, 1800)
                        )
                    elif args.explicit_reboot_fallback:
                        reboot_and_wait(
                            host, args.username, password, min(args.timeout, 1800)
                        )
                    else:
                        raise TimeoutError(
                            f'{host}: installer returned without an observed reboot within '
                            f'{args.auto_reboot_grace}s; exact reconciliation was not attempted'
                        )
                reconciliation = wait_for_package_present(
                    host,
                    args.username,
                    password,
                    expert_password,
                    target_version,
                    target_take,
                    package_name,
                    min(args.timeout, 1800),
                )
                emit_reconciliation(args, reconciliation, dispatch_intent)
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
