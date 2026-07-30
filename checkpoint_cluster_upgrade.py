#!/usr/bin/env python3
"""
Conservative Check Point ClusterXL CPUSE precheck and rolling-upgrade helper.

Defaults are read-only. Use --phase precheck first. Destructive or disruptive
actions require --execute.
"""

from __future__ import annotations

import argparse
import difflib
import base64
import getpass
import os
import pty
import re
import select
import shlex
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Iterable


DEFAULT_PACKAGE = "Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tgz"
DEFAULT_FAILOVER_DOWN = "clusterXL_admin down"
DEFAULT_FAILOVER_UP = "clusterXL_admin up"
DEFAULT_SUPPORT_SCRIPT = str(
    Path(__file__).resolve().parent
    / "ansible"
    / "scripts"
    / "gateway_support_commands.example.sh"
)
PROMPT_RE = re.compile(rb"(?m)(?:(?:\x1b\][^\x07]*\x07)?\[Expert@[^\r\n]+:\d+\]#\s*$|(?:^|\r?\n)[A-Za-z0-9_.-]+>\s*$)")
PASSWORD_RE = re.compile(rb"(?i)(?:password|passcode).*:\s*$")
MORE_RE = re.compile(rb"-- More --|Press any key to continue", re.I)


def proxy_connect_helper(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: --proxy-connect-helper HOST PORT", file=sys.stderr)
        return 2
    target_host, target_port = argv[1], int(argv[2])
    proxy_url = os.environ.get("CP_SSH_PROXY", "")
    if not proxy_url:
        print("CP_SSH_PROXY is not set", file=sys.stderr)
        return 2

    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() != "http":
        print("Only HTTP CONNECT proxies are supported", file=sys.stderr)
        return 2
    proxy_host = parsed.hostname or parsed.path.split(":", 1)[0]
    proxy_port = parsed.port or 8080
    proxy_user = os.environ.get("CP_SSH_PROXY_USER", "") or (parsed.username or "")
    proxy_password = os.environ.get("CP_SSH_PROXY_PASSWORD", "") or (parsed.password or "")

    with socket.create_connection((proxy_host, proxy_port), timeout=30) as sock:
        request = [
            f"CONNECT {target_host}:{target_port} HTTP/1.1",
            f"Host: {target_host}:{target_port}",
            "Proxy-Connection: Keep-Alive",
        ]
        if proxy_user or proxy_password:
            token = base64.b64encode(f"{proxy_user}:{proxy_password}".encode()).decode()
            request.append(f"Proxy-Authorization: Basic {token}")
        request.append("")
        request.append("")
        sock.sendall("\r\n".join(request).encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if len(response) > 65536:
                break
        status = response.split(b"\r\n", 1)[0]
        if b" 200 " not in status and not status.endswith(b" 200"):
            print(status.decode(errors="replace"), file=sys.stderr)
            return 1

        sock.setblocking(False)
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        while True:
            readable, _, _ = select.select([stdin_fd, sock], [], [])
            if stdin_fd in readable:
                data = os.read(stdin_fd, 16384)
                if not data:
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    sock.sendall(data)
            if sock in readable:
                try:
                    data = sock.recv(16384)
                except BlockingIOError:
                    continue
                if not data:
                    return 0
                os.write(stdout_fd, data)


class CheckPointError(RuntimeError):
    pass


@dataclass
class CommandResult:
    command: str
    output: str


@dataclass
class Gateway:
    host: str
    name: str = ""
    local_state: str = "UNKNOWN"
    peer_states: dict[str, str] = field(default_factory=dict)
    version: str = ""
    interfaces_ok: bool = False
    pnotes_ok: bool = False
    icap_ok: bool | None = None
    required_interfaces: int | None = None
    required_secured_interfaces: int | None = None
    cluster_interfaces: list[dict[str, object]] = field(default_factory=list)
    virtual_cluster_interfaces: list[dict[str, str]] = field(default_factory=list)


class SshPty:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        connect_timeout: int = 10,
        strict_host_key_checking: str = "accept-new",
        log_prefix: str = "",
        ssh_proxy: str = "",
        ssh_proxy_user: str = "",
        ssh_proxy_password: str = "",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self.strict_host_key_checking = strict_host_key_checking
        self.log_prefix = log_prefix or host
        self.ssh_proxy = ssh_proxy
        self.ssh_proxy_user = ssh_proxy_user
        self.ssh_proxy_password = ssh_proxy_password
        self.pid: int | None = None
        self.fd: int | None = None
        self.buffer = b""

    def connect(self) -> None:
        argv = [
            "ssh",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            f"StrictHostKeyChecking={self.strict_host_key_checking}",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
        ]
        if self.ssh_proxy:
            helper = Path(__file__).resolve()
            proxy_command = f"{sys.executable} {helper} --proxy-connect-helper %h %p"
            argv.extend(["-o", f"ProxyCommand={proxy_command}"])
        argv.append(f"{self.username}@{self.host}")

        pid, fd = pty.fork()
        if pid == 0:
            if self.ssh_proxy:
                os.environ["CP_SSH_PROXY"] = self.ssh_proxy
                if self.ssh_proxy_user:
                    os.environ["CP_SSH_PROXY_USER"] = self.ssh_proxy_user
                if self.ssh_proxy_password:
                    os.environ["CP_SSH_PROXY_PASSWORD"] = self.ssh_proxy_password
            os.execvp(argv[0], argv)
        self.pid = pid
        self.fd = fd
        self._expect_login()

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            self.sendline("exit")
            time.sleep(0.2)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        self.fd = None
        self.pid = None

    def _read_some(self, timeout: float) -> bytes:
        if self.fd is None:
            raise CheckPointError("SSH session is not connected")
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, 8192)
        except OSError as exc:
            raise CheckPointError(f"SSH session to {self.host} closed: {exc}") from exc

    def _expect_login(self) -> None:
        deadline = time.time() + self.connect_timeout + 20
        while time.time() < deadline:
            chunk = self._read_some(1)
            if chunk:
                self.buffer += chunk
                if b"REMOTE HOST IDENTIFICATION HAS CHANGED" in self.buffer:
                    raise CheckPointError(
                        f"{self.host}: SSH host key mismatch. Fix known_hosts manually."
                    )
                if PASSWORD_RE.search(self.buffer):
                    self._write(self.password + "\n")
                    self.buffer = b""
                elif PROMPT_RE.search(self.buffer):
                    self.buffer = b""
                    return
        raise CheckPointError(f"{self.host}: timed out waiting for login prompt")

    def _write(self, data: str) -> None:
        if self.fd is None:
            raise CheckPointError("SSH session is not connected")
        os.write(self.fd, data.encode())

    def sendline(self, line: str) -> None:
        self._write(line + "\n")

    def drain_pending(self, *, quiet_for: float = 0.2, max_wait: float = 2.0) -> None:
        if self.fd is None:
            return
        end = time.time() + max_wait
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], quiet_for)
            if not r:
                return
            try:
                os.read(self.fd, 8192)
            except OSError:
                return

    def run(
        self,
        command: str,
        *,
        timeout: int = 60,
        redact_command: bool = False,
    ) -> CommandResult:
        self.drain_pending()
        self.buffer = b""
        self.sendline(command)
        deadline = time.time() + timeout
        out = b""
        while time.time() < deadline:
            chunk = self._read_some(1)
            if not chunk:
                continue
            out += chunk
            if MORE_RE.search(out[-200:]):
                self._write(" ")
                continue
            if PROMPT_RE.search(out):
                text = strip_ansi(out.decode(errors="replace"))
                return CommandResult("<redacted>" if redact_command else command, text)
        raise CheckPointError(f"{self.host}: command timed out: {command}")

    def enter_expert(self, expert_password: str, *, timeout: int = 30) -> None:
        self.buffer = b""
        self.sendline("expert")
        deadline = time.time() + timeout
        out = b""
        sent_password = False
        nudged = False
        sent_at = 0.0
        while time.time() < deadline:
            chunk = self._read_some(1)
            if chunk:
                out += chunk
            if not sent_password and PASSWORD_RE.search(out):
                self._write(expert_password + "\n")
                out = b""
                sent_password = True
                sent_at = time.time()
                continue
            if sent_password and not nudged and time.time() - sent_at > 3:
                self._write("\n")
                nudged = True
            if PROMPT_RE.search(out):
                self.drain_pending()
                return
            if b"Wrong password" in out:
                raise CheckPointError(f"{self.host}: wrong expert password")
        raise CheckPointError(f"{self.host}: timed out entering expert mode")


def strip_ansi(value: str) -> str:
    value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
    value = re.sub(r"\x1b\].*?\x07", "", value)
    value = value.replace("\r", "")
    value = value.replace("\b", "")
    return value


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def parse_cluster_state(output: str) -> tuple[str, dict[str, str], bool]:
    local_state = "UNKNOWN"
    states: dict[str, str] = {}
    pnotes_ok = "Active PNOTEs: None" in output
    for line in output.splitlines():
        m = re.match(r"\s*\d+\s*(?:\(local\))?\s+\S+\s+\S+\s+(\S+)\s+(\S+)", line)
        if not m:
            continue
        state, name = m.group(1), m.group(2)
        states[name] = state
        if "(local)" in line:
            local_state = state
    return local_state, states, pnotes_ok


def parse_hostname(config_output: str, fallback: str) -> str:
    m = re.search(r"set hostname\s+(\S+)", config_output)
    return m.group(1) if m else fallback


def parse_cluster_interfaces(output: str) -> dict[str, object]:
    required = re.search(r"Required interfaces:\s+(\d+)", output)
    required_secured = re.search(r"Required secured interfaces:\s+(\d+)", output)
    interfaces: list[dict[str, object]] = []
    virtual_interfaces: list[dict[str, str]] = []
    in_interface_table = False
    in_virtual_table = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Interface Name:"):
            in_interface_table = True
            in_virtual_table = False
            continue
        if line.startswith("S - sync"):
            in_interface_table = False
            continue
        if line.startswith("Virtual cluster interfaces:"):
            in_virtual_table = True
            in_interface_table = False
            continue
        if line.startswith("[") or line.startswith("cphaprob "):
            continue

        if in_interface_table:
            match = re.match(r"^(\S+)(?:\s+\(S\))?\s+(.+?)\s*$", line)
            if match:
                sync = "(S)" in line.split(match.group(1), 1)[1].split(match.group(2), 1)[0]
                status = match.group(2).strip()
                interfaces.append(
                    {
                        "name": match.group(1),
                        "sync": sync,
                        "status": status,
                        "monitored": status.lower() != "non-monitored",
                    }
                )
            continue

        if in_virtual_table:
            match = re.match(r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s*$", line)
            if match:
                virtual_interfaces.append({"name": match.group(1), "ip": match.group(2)})

    required_count = int(required.group(1)) if required else None
    required_secured_count = int(required_secured.group(1)) if required_secured else None
    monitored_up = [
        iface
        for iface in interfaces
        if iface.get("monitored") and str(iface.get("status", "")).upper() == "UP"
    ]
    ok = required_count is not None and len(monitored_up) >= required_count
    return {
        "required_interfaces": required_count,
        "required_secured_interfaces": required_secured_count,
        "interfaces": interfaces,
        "virtual_interfaces": virtual_interfaces,
        "ok": ok,
    }


def interfaces_are_up(output: str) -> bool:
    return bool(parse_cluster_interfaces(output)["ok"])


def cluster_interface_signature(gw: Gateway | dict[str, object]) -> dict[str, object]:
    if isinstance(gw, Gateway):
        required = gw.required_interfaces
        required_secured = gw.required_secured_interfaces
        interfaces = gw.cluster_interfaces
        virtual_interfaces = gw.virtual_cluster_interfaces
    else:
        required = gw.get("required_interfaces")
        required_secured = gw.get("required_secured_interfaces")
        interfaces = gw.get("cluster_interfaces", [])
        virtual_interfaces = gw.get("virtual_cluster_interfaces", [])
    monitored = [
        {
            "name": str(iface.get("name", "")),
            "sync": bool(iface.get("sync")),
        }
        for iface in interfaces
        if isinstance(iface, dict) and iface.get("monitored")
    ]
    virtual = [
        {
            "name": str(iface.get("name", "")),
            "ip": str(iface.get("ip", "")),
        }
        for iface in virtual_interfaces
        if isinstance(iface, dict)
    ]
    return {
        "required_interfaces": required,
        "required_secured_interfaces": required_secured,
        "monitored_interfaces": sorted(monitored, key=lambda item: item["name"]),
        "virtual_cluster_interfaces": sorted(virtual, key=lambda item: (item["name"], item["ip"])),
    }


def cluster_interface_inventory_matches(
    baseline: Gateway | dict[str, object], current: Gateway | dict[str, object]
) -> bool:
    return cluster_interface_signature(baseline) == cluster_interface_signature(current)


def parse_icap_status(cpwd_output: str, listener_output: str, process_output: str) -> bool:
    cpwd_lines = [line.strip() for line in cpwd_output.splitlines() if "CICAP" in line.upper()]
    cpwd_running = any(
        not re.search(r"\bCICAP\s+\d+\s+T\b", line, re.I)
        for line in cpwd_lines
    )
    listener_present = any(
        ":1344" in line and "grep" not in line and "ss -lntp" not in line and "netstat -lntp" not in line
        for line in listener_output.splitlines()
    )
    process_present = any(
        "c-icap" in line.lower() and "grep" not in line.lower()
        for line in process_output.splitlines()
    )
    return cpwd_running and listener_present and process_present


def connect(args: argparse.Namespace, host: str) -> SshPty:
    session = SshPty(
        host,
        args.username,
        args.password,
        strict_host_key_checking=args.strict_host_key_checking,
        ssh_proxy=getattr(args, "ssh_proxy", ""),
        ssh_proxy_user=getattr(args, "ssh_proxy_user", ""),
        ssh_proxy_password=getattr(args, "ssh_proxy_password", ""),
    )
    log(f"{host}: connecting")
    session.connect()
    return session


def precheck_gateway(
    args: argparse.Namespace, host: str, *, expert_checks: bool = True
) -> tuple[Gateway, list[CommandResult]]:
    session = connect(args, host)
    results: list[CommandResult] = []
    gw = Gateway(host=host)
    try:
        for command in [
            "show configuration hostname",
            "show version all",
            "cphaprob state",
            "cphaprob -a if",
            "show snapshots",
            "show backups",
            "show installer status all",
            "show installer policy",
            "show installer packages",
        ]:
            res = session.run(command, timeout=120)
            results.append(res)
            if command == "show configuration hostname":
                gw.name = parse_hostname(res.output, host)
            elif command == "show version all":
                gw.version = one_line(res.output)
            elif command == "cphaprob state":
                gw.local_state, gw.peer_states, gw.pnotes_ok = parse_cluster_state(res.output)
            elif command == "cphaprob -a if":
                inventory = parse_cluster_interfaces(res.output)
                gw.interfaces_ok = bool(inventory["ok"])
                gw.required_interfaces = inventory["required_interfaces"]  # type: ignore[assignment]
                gw.required_secured_interfaces = inventory["required_secured_interfaces"]  # type: ignore[assignment]
                gw.cluster_interfaces = inventory["interfaces"]  # type: ignore[assignment]
                gw.virtual_cluster_interfaces = inventory["virtual_interfaces"]  # type: ignore[assignment]

        if expert_checks and args.expert_password and args.icap_mode != "disabled":
            session.enter_expert(args.expert_password)
            cpwd = session.run("cpwd_admin list | grep -i icap", timeout=60)
            listener = session.run("ss -lntp | grep ':1344' || netstat -lntp | grep ':1344'", timeout=60)
            process = session.run("ps -ef | grep -i '[c]-icap'", timeout=60)
            results.extend([cpwd, listener, process])
            gw.icap_ok = parse_icap_status(cpwd.output, listener.output, process.output)
            session.run("exit", timeout=10)
        return gw, results
    finally:
        session.close()


def one_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return " | ".join(lines[:5])


def create_backup(args: argparse.Namespace, host: str) -> str:
    require_execute(args, "create local Gaia backup")
    session = connect(args, host)
    try:
        session.run("add backup local", timeout=30)
        for _ in range(args.backup_wait_seconds // 5):
            res = session.run("show backup status", timeout=30)
            if "Local backup succeeded" in res.output:
                return res.output
            if "failed" in res.output.lower():
                raise CheckPointError(f"{host}: backup failed:\n{res.output}")
            time.sleep(5)
        raise CheckPointError(f"{host}: backup did not finish in time")
    finally:
        session.close()


def package_lookup_terms(package: str) -> list[str]:
    terms = [package.lower()]
    take = re.search(r"(?:^|[_-])T(\d+)(?:[_-]|$)", package, re.I)
    if take:
        terms.append(f"take {take.group(1)}")
    return terms


def package_table_has_ready_package(output: str, package: str) -> bool:
    terms = package_lookup_terms(package)
    negative_statuses = (
        "not downloaded",
        "not installed",
        "unavailable",
        "not available",
        "failed",
    )
    ready_statuses = ("downloaded", "installed", "available for install")
    for line in output.splitlines():
        lower = line.lower()
        if not any(term in lower for term in terms):
            continue
        if any(status in lower for status in negative_statuses):
            continue
        if any(status in lower for status in ready_statuses):
            return True
    return False


def package_table_has_installed_target(
    output: str, package: str, target_take: str
) -> bool:
    package_stem = re.sub(
        r"\.(?:tgz|tar)$", "", Path(package).name.lower(), flags=re.IGNORECASE
    )
    take_pattern = re.compile(
        rf"(?:\btake[ _-]?{re.escape(target_take)}\b|_t{re.escape(target_take)}(?:_|\b))",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        lower = line.lower()
        if any(
            marker in lower for marker in ("not installed", "uninstalled", "failed")
        ):
            continue
        if not re.search(r"\binstalled\b", lower):
            continue
        if package_stem not in lower:
            continue
        if take_pattern.search(line):
            return True
    return False


def version_output_matches_target(output: str, target_version: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9.]){re.escape(target_version)}(?![A-Za-z0-9.])",
            output,
            re.IGNORECASE,
        )
    )


def installer_return_code(output: str) -> int | None:
    matches = re.findall(r"(?:^|\s)__RC=(\d+)(?:\s|$)", output)
    return int(matches[-1]) if matches else None


def expert_installer_command(package: str) -> str:
    clish_command = f"installer install {shlex.quote(package)}"
    return (
        f"clish -c {shlex.quote(clish_command)}; "
        "rc=$?; printf '\\n__RC=%s\\n' \"$rc\""
    )

def acquire_clish_lock(session: SshPty, host: str) -> None:
    res = session.run("lock database override", timeout=60)
    lower = res.output.lower()
    if "error" in lower or "failed" in lower:
        raise CheckPointError(f"{host}: could not acquire Gaia configuration lock:\n{res.output}")


def download_and_verify(args: argparse.Namespace, host: str) -> None:
    require_execute(args, "download and verify CPUSE package")
    session = connect(args, host)
    try:
        acquire_clish_lock(session, host)
        packages = session.run("show installer packages", timeout=120)
        print_section(host, packages)
        package_ready = package_table_has_ready_package(packages.output, args.package)

        if package_ready:
            log(f"{host}: {args.package} is already downloaded/installable; skipping download")
        else:
            log(f"{host}: downloading {args.package}")
            res = session.run(
                f"installer download {shlex.quote(args.package)}",
                timeout=args.download_timeout,
            )
            print_section(host, res)
            log(f"{host}: confirming download state in the package table")
            packages = session.run("show installer packages", timeout=120)
            print_section(host, packages)
            if not package_table_has_ready_package(packages.output, args.package):
                raise CheckPointError(
                    f"{host}: package download did not reach an installable/downloaded state"
                )

        log(f"{host}: verifying {args.package}")
        verify = session.run(
            f"installer verify {shlex.quote(args.package)}",
            timeout=args.verify_timeout,
        )
        print_section(host, verify)
        verify_lower = verify.output.lower()
        failure_markers = [
            "configuration lock present",
            "installation is not allowed",
            "not verified",
            "verification failed",
            "verify failed",
            "failed to verify",
            "is not available for installation",
            "quitting interactive mode due to time-out",
        ]
        success_markers = [
            "verification completed successfully",
            "verified successfully",
            "installation is allowed",
            "package is allowed to be installed",
            "successfully verified",
        ]
        if any(marker in verify_lower for marker in failure_markers):
            raise CheckPointError(f"{host}: CPUSE verification failed; install stopped")
        if not any(marker in verify_lower for marker in success_markers):
            raise CheckPointError(f"{host}: CPUSE verification was inconclusive; install stopped")
    finally:
        session.close()


def install_package(args: argparse.Namespace, host: str) -> None:
    require_execute(args, "install CPUSE package")
    if not args.expert_password:
        raise CheckPointError("Expert password is required to capture installer status")
    session = connect(args, host)
    try:
        session.enter_expert(args.expert_password)
        log(f"{host}: installing {args.package}")
        try:
            result = session.run(
                expert_installer_command(args.package), timeout=args.install_timeout
            )
        except CheckPointError as exc:
            log(
                f"{host}: installer session ended before status was returned; "
                "target reconciliation is required after reconnect "
                f"({exc})"
            )
            return
        print_section(host, result)
        rc = installer_return_code(result.output)
        if rc is None:
            raise CheckPointError(f"{host}: installer did not return an exit status")
        if rc != 0:
            raise CheckPointError(f"{host}: installer failed with exit status {rc}")
    finally:
        session.close()


def verify_rolling_target(args: argparse.Namespace, host: str) -> None:
    session = connect(args, host)
    try:
        version = session.run("show version all", timeout=120)
        packages = session.run("show installer packages installed", timeout=120)
        print_section(host, version)
        print_section(host, packages)
        if not version_output_matches_target(version.output, args.target_version):
            raise CheckPointError(
                f"{host}: target version {args.target_version} was not reached"
            )
        if not package_table_has_installed_target(
            packages.output, args.package, args.target_take
        ):
            raise CheckPointError(
                f"{host}: target Take {args.target_take} and exact package "
                "were not confirmed as installed"
            )
    finally:
        session.close()


def wait_for_reconnect(args: argparse.Namespace, host: str) -> None:
    deadline = time.time() + args.reconnect_timeout
    last_error = ""
    while time.time() < deadline:
        try:
            gw, _ = precheck_gateway(args, host, expert_checks=False)
            log(f"{host}: reconnected; local cluster state is {gw.local_state}")
            return
        except Exception as exc:  # noqa: BLE001 - report retry cause
            last_error = str(exc)
            time.sleep(15)
    raise CheckPointError(f"{host}: did not reconnect in time. Last error: {last_error}")


def require_execute(args: argparse.Namespace, action: str) -> None:
    if not args.execute:
        raise CheckPointError(f"Refusing to {action} without --execute")


def print_section(host: str, result: CommandResult) -> None:
    print(f"\n===== {host}: {result.command} =====")
    print(result.output.rstrip())


def report_gateways(gateways: Iterable[Gateway]) -> None:
    print("\n===== Summary =====")
    for gw in gateways:
        icap = "skipped" if gw.icap_ok is None else ("ok" if gw.icap_ok else "NOT OK")
        monitored = [iface for iface in gw.cluster_interfaces if iface.get("monitored")]
        print(
            f"{gw.host} {gw.name}: state={gw.local_state}, "
            f"pnotes={'ok' if gw.pnotes_ok else 'NOT OK'}, "
            f"interfaces={'ok' if gw.interfaces_ok else 'NOT OK'} "
            f"(required={gw.required_interfaces}, monitored={len(monitored)}, "
            f"virtual={len(gw.virtual_cluster_interfaces)}), "
            f"icap={icap}"
        )


def choose_standby(gateways: list[Gateway]) -> Gateway:
    standby = [gw for gw in gateways if gw.local_state.upper() == "STANDBY"]
    if len(standby) != 1:
        raise CheckPointError("Expected exactly one standby member")
    return standby[0]


def choose_active(gateways: list[Gateway]) -> Gateway:
    active = [gw for gw in gateways if gw.local_state.upper().startswith("ACTIVE")]
    if len(active) != 1:
        raise CheckPointError("Expected exactly one active member")
    return active[0]


def gateway_ready(gw: Gateway, icap_mode: str) -> bool:
    return (
        gw.pnotes_ok
        and gw.interfaces_ok
        and gw.local_state != "UNKNOWN"
        and (icap_mode != "required" or gw.icap_ok is True)
    )


def run_precheck(args: argparse.Namespace) -> list[Gateway]:
    gateways: list[Gateway] = []
    for host in args.members:
        gw, results = precheck_gateway(args, host)
        gateways.append(gw)
        if args.verbose:
            for result in results:
                print_section(host, result)
    report_gateways(gateways)
    bad = [gw.host for gw in gateways if not gateway_ready(gw, args.icap_mode)]
    if bad:
        raise CheckPointError(f"Precheck failed on: {', '.join(bad)}")
    return gateways


def run_download_verify(args: argparse.Namespace) -> None:
    gateways = run_precheck(args)
    target = choose_standby(gateways) if args.target == "standby" else gateway_by_host(args, args.target)
    if args.create_backup:
        log(f"{target.host}: creating local backup")
        print(create_backup(args, target.host))
    download_and_verify(args, target.host)


def collect_gateways(args: argparse.Namespace) -> list[Gateway]:
    gateways: list[Gateway] = []
    for host in args.members:
        gw, results = precheck_gateway(args, host)
        gateways.append(gw)
        if args.verbose:
            for result in results:
                print_section(host, result)
    report_gateways(gateways)
    return gateways


def gateway_by_host(args: argparse.Namespace, host: str) -> Gateway:
    gw, _ = precheck_gateway(args, host)
    return gw


def run_expert_command(
    args: argparse.Namespace, host: str, command: str, *, timeout: int = 60
) -> CommandResult:
    require_execute(args, f"run expert command on {host}: {command}")
    if not args.expert_password:
        raise CheckPointError("Expert password is required")
    session = connect(args, host)
    try:
        session.enter_expert(args.expert_password)
        return session.run(command, timeout=timeout)
    finally:
        session.close()


def clusterxl_admin(args: argparse.Namespace, host: str, action: str) -> CommandResult:
    if action not in {"down", "up"}:
        raise ValueError(action)
    command = DEFAULT_FAILOVER_DOWN if action == "down" else DEFAULT_FAILOVER_UP
    result = run_expert_command(args, host, command, timeout=60)
    print_section(host, result)
    expected = "administratively down" if action == "down" else "normal operation"
    if expected not in result.output:
        raise CheckPointError(f"{host}: unexpected clusterXL_admin {action} output")
    return result


def load_support_commands(script_path: str) -> list[str]:
    path = Path(script_path).expanduser()
    if not path.exists():
        raise CheckPointError(f"Support command script not found: {path}")
    commands: list[str] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#!"):
            continue
        if line.startswith("#"):
            continue
        if line in {"set -x", "set +x"}:
            continue
        commands.append(line)
    if not commands:
        raise CheckPointError(f"No commands found in support command script: {path}")
    return commands


def remove_echoed_command(output: str, command: str) -> str:
    lines = output.splitlines()
    if lines and lines[0].strip() == command:
        return "\n".join(lines[1:])
    return output


def run_support_capture(args: argparse.Namespace) -> list[Path]:
    if not args.expert_password:
        raise CheckPointError("Support capture requires expert password")

    commands = load_support_commands(args.support_script)
    output_dir = Path(args.support_output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = sanitize_filename(args.support_label or stamp)
    written: list[Path] = []

    for host in args.members:
        path = output_dir / f"{label}_{sanitize_filename(host)}_support_capture.txt"
        log(f"{host}: running support capture to {path}")
        session = connect(args, host)
        try:
            session.enter_expert(args.expert_password)
            with path.open("w", encoding="utf-8") as fh:
                fh.write(f"# support_capture_host={host}\n")
                fh.write(f"# support_capture_label={label}\n")
                fh.write(f"# support_capture_time={datetime.now().isoformat(timespec='seconds')}\n")
                fh.write(f"# support_script={Path(args.support_script).expanduser()}\n")
                fh.write(f"# command_count={len(commands)}\n\n")
                for index, command in enumerate(commands, start=1):
                    header = f"===== COMMAND {index:03d}: {command} ====="
                    log(f"{host}: {index}/{len(commands)} {command}")
                    fh.write(header + "\n")
                    fh.flush()
                    try:
                        result = session.run(
                            command,
                            timeout=args.support_command_timeout,
                        )
                        body = remove_echoed_command(result.output, command)
                        fh.write(body.rstrip() + "\n")
                    except Exception as exc:  # noqa: BLE001 - preserve capture progress
                        fh.write(f"ERROR: {exc}\n")
                        fh.write("Capture continued after reconnecting the SSH session.\n")
                        try:
                            session.close()
                        except Exception:
                            pass
                        session = connect(args, host)
                        session.enter_expert(args.expert_password)
                    finally:
                        fh.write(f"===== END COMMAND {index:03d} =====\n\n")
                        fh.flush()
            written.append(path)
        finally:
            session.close()
    return written


def run_support_diff(args: argparse.Namespace) -> Path:
    if not args.diff_before or not args.diff_after:
        raise CheckPointError("support-diff requires --diff-before and --diff-after")
    before = Path(args.diff_before).expanduser()
    after = Path(args.diff_after).expanduser()
    if not before.exists():
        raise CheckPointError(f"Before file not found: {before}")
    if not after.exists():
        raise CheckPointError(f"After file not found: {after}")
    output_dir = Path(args.support_output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.diff_output).expanduser() if args.diff_output else output_dir / (
        f"diff_{sanitize_filename(before.stem)}__{sanitize_filename(after.stem)}.txt"
    )
    before_lines = before.read_text(errors="replace").splitlines(keepends=True)
    after_lines = after.read_text(errors="replace").splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=str(before),
        tofile=str(after),
        lineterm="",
    )
    out.write_text("".join(diff), encoding="utf-8")
    return out


def support_section(text: str, command: str) -> str:
    marker = f": {command} ====="
    i = text.find(marker)
    if i < 0:
        return ""
    start = text.rfind("===== COMMAND ", 0, i)
    end = text.find("===== END COMMAND", i)
    if start < 0 or end < 0:
        return ""
    return text[start:end]


def support_status(ok: bool, label: str, detail: str = "") -> str:
    state = "PASS" if ok else "WARN"
    return f"[{state}] {label}" + (f": {detail}" if detail else "")


def analyze_support_capture(path: Path) -> list[str]:
    text = path.read_text(errors="replace")
    lines: list[str] = [f"===== Analysis: {path} ====="]
    command_count = text.count("===== COMMAND ")
    end_count = text.count("===== END COMMAND ")
    lines.append(support_status(command_count > 0 and command_count == end_count, "capture completeness", f"commands={command_count}, ends={end_count}"))
    lines.append(support_status("\nERROR:" not in text and "Capture stopped" not in text, "capture runtime errors"))

    cluster = support_section(text, "cphaprob stat")
    member_states = []
    for line in cluster.splitlines():
        m = re.match(r"\s*\d+\s*(?:\(local\))?\s+\S+\s+\S+\s+(ACTIVE|STANDBY|DOWN)\s+\S+", line)
        if m:
            member_states.append(m.group(1))
    active_count = member_states.count("ACTIVE")
    standby_count = member_states.count("STANDBY")
    down_count = member_states.count("DOWN")
    lines.append(support_status("Active PNOTEs: None" in cluster, "ClusterXL active PNOTEs are clear"))
    lines.append(support_status(active_count == 1 and standby_count == 1 and down_count == 0, "ClusterXL member roles", f"ACTIVE={active_count}, STANDBY={standby_count}, DOWN={down_count}"))

    ifs = support_section(text, "cphaprob -a if")
    required_match = re.search(r"Required interfaces:\s+(\d+)", ifs)
    up_count = len(re.findall(r"^\s*\S+(?:\s+\(S\))?\s+UP\s*$", ifs, re.M))
    required = int(required_match.group(1)) if required_match else 0
    lines.append(support_status(required > 0 and up_count >= required, "monitored interfaces are UP", f"required={required}, up={up_count}"))

    sync = support_section(text, "cphaprob syncstat")
    lines.append(support_status("Sync status: OK" in sync, "ClusterXL sync status OK"))

    ha = support_section(text, "cpstat ha")
    lines.append(support_status("Status:       OK" in ha and "HA started:   yes" in ha, "cpstat HA status OK and started"))

    fwver = support_section(text, "fw ver")
    m = re.search(r"software version\s+(.+)", fwver)
    lines.append(support_status(bool(m), "firewall version present", m.group(1).strip() if m else "missing"))

    cpinfo = support_section(text, "cpinfo -y all")
    take_matches = sorted(set(re.findall(r"HOTFIX_R82_JUMBO_HF_MAIN\s+Take:\s+(\d+)", cpinfo)))
    lines.append(support_status(bool(take_matches), "R82 jumbo take present", ",".join(take_matches) if take_matches else "missing"))
    if "HOTFIX_R82_JHF_T60_074_MAIN" in cpinfo:
        lines.append("[INFO] T60_074 hotfix present: package compatibility context")

    df = support_section(text, "df -h")
    high_mounts = []
    for line in df.splitlines():
        m = re.search(r"(\d+)%\s+(\S+)$", line)
        if m and int(m.group(1)) >= 85:
            high_mounts.append(f"{m.group(2)}={m.group(1)}%")
    lines.append(support_status(not high_mounts, "filesystem usage below 85%", ", ".join(high_mounts) if high_mounts else "ok"))

    accel = support_section(text, "fwaccel stat")
    lines.append(support_status("enabled" in accel.lower(), "SecureXL acceleration enabled"))

    policy = support_section(text, "fw stat -l")
    lines.append(support_status("CP-FW-Policy" in policy, "security policy installed", "CP-FW-Policy" if "CP-FW-Policy" in policy else "missing"))

    icap_process = "c-icap" in text
    icap_listener = re.search(r"(?:0\.0\.0\.0|\*)[:.]1344\b", text) is not None
    lines.append(support_status(icap_process, "ICAP c-icap process present"))
    lines.append(support_status(icap_listener, "ICAP TCP 1344 listener present"))

    overview = support_section(text, 'clish -c "show interfaces overview all"')
    for iface in ["eth0", "eth1", "eth2", "eth3"]:
        lines.append(support_status(re.search(rf"^\s*{iface}\s+\S+\s+Up\b", overview, re.M) is not None, f"{iface} Gaia state Up"))

    noisy = []
    for needle in ["No such device", "No such file or directory", "Get operation failed", "Cannot get device ring settings"]:
        count = text.count(needle)
        if count:
            noisy.append(f"{needle}={count}")
    lines.append("[INFO] expected generic-script noise: " + (", ".join(noisy) if noisy else "none"))
    return lines


def run_support_analyze(args: argparse.Namespace) -> None:
    if not args.capture_files:
        raise CheckPointError("support-analyze requires --capture-files")
    for capture in args.capture_files:
        path = Path(capture).expanduser()
        if not path.exists():
            raise CheckPointError(f"Capture file not found: {path}")
        for line in analyze_support_capture(path):
            print(line)
        print()


def wait_for_cluster_condition(
    args: argparse.Namespace,
    predicate,
    description: str,
    *,
    timeout: int | None = None,
) -> list[Gateway]:
    deadline = time.time() + (timeout or args.failover_wait_seconds)
    last_gateways: list[Gateway] = []
    while time.time() < deadline:
        last_gateways = collect_gateways(args)
        if predicate(last_gateways):
            return last_gateways
        time.sleep(5)
    raise CheckPointError(f"Timed out waiting for cluster condition: {description}")


def run_failover_test(args: argparse.Namespace) -> None:
    require_execute(args, "perform failover test")
    gateways = run_precheck(args)
    original_active = choose_active(gateways)
    original_standby = choose_standby(gateways)

    log(
        f"Failover test: moving ACTIVE from {original_active.host} "
        f"to {original_standby.host}"
    )
    clusterxl_admin(args, original_active.host, "down")

    def failed_over(gws: list[Gateway]) -> bool:
        by_host = {gw.host: gw for gw in gws}
        return (
            by_host[original_active.host].local_state.upper() == "DOWN"
            and by_host[original_standby.host].local_state.upper().startswith("ACTIVE")
            and by_host[original_standby.host].icap_ok is True
        )

    wait_for_cluster_condition(
        args,
        failed_over,
        "original active DOWN and original standby ACTIVE with ICAP up",
    )

    log(f"Restoring {original_active.host} to normal ClusterXL operation")
    clusterxl_admin(args, original_active.host, "up")

    def restored(gws: list[Gateway]) -> bool:
        by_host = {gw.host: gw for gw in gws}
        return (
            by_host[original_standby.host].local_state.upper().startswith("ACTIVE")
            and by_host[original_active.host].local_state.upper() == "STANDBY"
            and all(gw.pnotes_ok and gw.interfaces_ok and gw.icap_ok is True for gw in gws)
        )

    wait_for_cluster_condition(
        args,
        restored,
        "original active restored as STANDBY with clean PNOTEs and ICAP up",
    )
    log("Failover test completed successfully")


def run_rolling(args: argparse.Namespace) -> None:
    gateways = run_precheck(args)
    standby = choose_standby(gateways)
    active = choose_active(gateways)

    for gw in [standby, active]:
        if args.create_backup:
            log(f"{gw.host}: creating local backup")
            print(create_backup(args, gw.host))

    download_and_verify(args, standby.host)
    install_package(args, standby.host)
    wait_for_reconnect(args, standby.host)
    verify_rolling_target(args, standby.host)

    log(f"{active.host}: forcing failover with {DEFAULT_FAILOVER_DOWN}")
    clusterxl_admin(args, active.host, "down")

    def failover_moved(gws: list[Gateway]) -> bool:
        by_host = {gw.host: gw for gw in gws}
        return (
            standby.host in by_host
            and active.host in by_host
            and by_host[standby.host].local_state.upper().startswith("ACTIVE")
            and by_host[active.host].local_state.upper() == "DOWN"
            and by_host[standby.host].icap_ok is True
        )

    gateways = wait_for_cluster_condition(
        args,
        failover_moved,
        "upgraded standby promoted to ACTIVE after administrative down",
    )
    new_active = choose_active(gateways)
    if new_active.host != standby.host:
        raise CheckPointError("Failover did not move ACTIVE state to upgraded standby")

    download_and_verify(args, active.host)
    install_package(args, active.host)
    wait_for_reconnect(args, active.host)
    verify_rolling_target(args, active.host)
    run_precheck(args)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Point ClusterXL CPUSE precheck/download/verify/rolling install helper"
    )
    parser.add_argument("--members", nargs=2, default=[], metavar=("GW1", "GW2"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", default="CP_PASSWORD")
    parser.add_argument("--expert-password-env", default="CP_EXPERT_PASSWORD")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--target-version", default="")
    parser.add_argument("--target-take", default="")
    parser.add_argument(
        "--phase",
        choices=[
            "precheck",
            "download-verify",
            "support-capture",
            "support-diff",
            "support-analyze",
            "failover-test",
            "rolling",
        ],
        default="precheck",
    )
    parser.add_argument(
        "--target",
        default="standby",
        help="download-verify target: 'standby' or a member IP/hostname",
    )
    parser.add_argument("--execute", action="store_true", help="allow change actions")
    parser.add_argument("--create-backup", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--strict-host-key-checking", default="accept-new")
    parser.add_argument("--ssh-proxy", default=os.environ.get("CP_SSH_PROXY", ""), help="HTTP CONNECT proxy URL, e.g. http://proxy.example:8080")
    parser.add_argument("--ssh-proxy-user", default=os.environ.get("CP_SSH_PROXY_USER", ""))
    parser.add_argument("--ssh-proxy-password-env", default="CP_SSH_PROXY_PASSWORD")
    parser.add_argument("--download-timeout", type=int, default=3600)
    parser.add_argument("--verify-timeout", type=int, default=1200)
    parser.add_argument("--install-timeout", type=int, default=7200)
    parser.add_argument("--backup-wait-seconds", type=int, default=600)
    parser.add_argument("--reconnect-timeout", type=int, default=1800)
    parser.add_argument("--support-script", default=os.environ.get("CP_SUPPORT_SCRIPT", DEFAULT_SUPPORT_SCRIPT))
    parser.add_argument("--support-output-dir", default="checkpoint_captures")
    parser.add_argument("--support-label", default="")
    parser.add_argument("--support-command-timeout", type=int, default=300)
    parser.add_argument("--diff-before", default="")
    parser.add_argument("--diff-after", default="")
    parser.add_argument("--diff-output", default="")
    parser.add_argument("--capture-files", nargs="*", default=[])
    parser.add_argument(
        "--failover-wait-seconds",
        type=int,
        default=120,
        help="seconds to wait for ClusterXL state transitions",
    )
    parser.add_argument(
        "--icap-mode",
        choices=["required", "optional", "disabled"],
        default="required",
        help="ICAP validation mode: required fails precheck, optional reports only, disabled skips ICAP checks",
    )
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[\w. -]+", args.package):
        parser.error("--package contains unsafe characters")
    if args.phase == "rolling":
        if not args.target_version:
            parser.error("--target-version is required for --phase rolling")
        if not re.fullmatch(r"\d{1,4}", args.target_take):
            parser.error(
                "--target-take is required for --phase rolling and must be numeric"
            )

    if args.phase in {"support-diff", "support-analyze"}:
        args.password = ""
        args.expert_password = ""
        args.ssh_proxy_password = ""
        return args

    args.ssh_proxy_password = os.environ.get(args.ssh_proxy_password_env, "")
    args.password = os.environ.get(args.password_env) or getpass.getpass(
        f"Password for {args.username}: "
    )
    args.expert_password = os.environ.get(args.expert_password_env)
    if args.expert_password is None:
        entered = getpass.getpass("Expert password, blank to skip expert checks: ")
        args.expert_password = entered or ""
    return args


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--proxy-connect-helper":
        return proxy_connect_helper(argv)
    args = parse_args(argv)
    try:
        if args.phase == "precheck":
            run_precheck(args)
        elif args.phase == "download-verify":
            run_download_verify(args)
        elif args.phase == "support-capture":
            paths = run_support_capture(args)
            for path in paths:
                print(path)
        elif args.phase == "support-diff":
            print(run_support_diff(args))
        elif args.phase == "support-analyze":
            run_support_analyze(args)
        elif args.phase == "failover-test":
            run_failover_test(args)
        elif args.phase == "rolling":
            run_rolling(args)
        return 0
    except CheckPointError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
