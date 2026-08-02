from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "direct_package_step_from_activity.py"
DIRECT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "direct_package"
SPEC = importlib.util.spec_from_file_location("direct_package_step_from_activity", SCRIPT)
assert SPEC and SPEC.loader
direct = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = direct
SPEC.loader.exec_module(direct)


class Result:
    def __init__(self, output: str):
        self.output = output


class Session:
    host = "192.0.2.20"

    def __init__(self, history: str, installed_table: str | None = None):
        self.history = history
        self.installed_table = history if installed_table is None else installed_table
        self.clish_calls: list[tuple[str, bool]] = []

    def run(self, command: str, timeout: int = 0) -> Result:
        del timeout
        if "show installer packages installed" in command:
            return Result(self.installed_table + "\n__RC=0\n")
        return Result(self.history)

    def run_interactive_clish(
        self,
        command: str,
        *,
        acquire_lock: bool,
        timeout: int = 0,
    ) -> Result:
        self.clish_calls.append((command, acquire_lock))
        return self.run(command, timeout=timeout)


class DirectStateSelectionTests(unittest.TestCase):
    def test_explicit_state_file_controls_member_selection(self) -> None:
        plan = {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "reports"
            reports.mkdir()
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                '{"original_active_host":"192.0.2.21",'
                '"original_standby_host":"192.0.2.20"}'
            )
            inherited = root / "inherited-state.json"
            inherited.write_text(
                '{"original_active_host":"192.0.2.20",'
                '"original_standby_host":"192.0.2.21"}'
            )
            self.assertEqual(
                direct.member_ips_for_phase(
                    plan, "first-member", reports, inherited
                ),
                ["192.0.2.21"],
            )


class DirectRemoveIdentityTests(unittest.TestCase):
    def test_take_alias_resolves_from_local_cpinstlog(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        history = f"Install,CPUpdates,{package},BUNDLE_R81_20_JUMBO_HF_MAIN#76"
        self.assertEqual(
            direct.resolve_remove_package_name(
                Session(history, f"{package} | Status: Installed"),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            ),
            package,
        )

    def test_ambiguous_local_history_fails_closed(self) -> None:
        history = "\n".join([
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz",
        ])
        with self.assertRaisesRegex(RuntimeError, "one exact"):
            direct.resolve_remove_package_name(
                Session(
                    history,
                    "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz | "
                    "Status: Installed\n"
                    "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz | "
                    "Status: Installed",
                ),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            )

    def test_fresh_resolution_rejects_untrusted_current_tables(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        step = {
            "name": "remove_Take_76",
            "action": "remove",
            "package_name": "Take 76",
            "package_type": "jhf",
        }
        installed = f"{package} | Status: Installed"
        for table in (
            "",
            "   ",
            "Error: unavailable",
            "malformed output",
            f"{installed}\nmalformed output",
            "No installed packages match\nmalformed output",
            "Installed Packages\nmalformed output",
            f"No installed packages match\n{installed}",
            "No installed packages match\nNo installed packages match",
            f"{package} | Sta\x1b[31mtus: Installed",
            f"{installed}\n{installed}",
            f"{installed}\n{package.lower()} | Status: Installed",
        ):
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                direct.resolve_remove_package_name(
                    Session(f"Installed {package}", table), step
                )

    def test_resumed_resolution_requires_trustworthy_absence_output(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        step = {
            "name": "remove_Take_76",
            "action": "remove",
            "package_name": "Take 76",
            "package_type": "jhf",
        }
        installed = f"{package} | Status: Installed"
        removed = f"{package} | Status: Removed"
        for table in (
            "",
            "   ",
            "Error: unavailable",
            "malformed output",
            f"{installed}\nmalformed output",
            "No installed packages match\nmalformed output",
            "Installed Packages\nmalformed output",
            f"No installed packages match\n{installed}",
            "No installed packages match\nNo installed packages match",
            f"{package} | Sta\x1b[31mtus: Removed",
            f"{removed}\n{removed}",
            f"{removed}\n{package.lower()} | Status: Removed",
        ):
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                direct.validate_persisted_remove_identity(
                    Session(f"Installed {package}", table), step, package
                )
        direct.validate_persisted_remove_identity(
            Session(f"Installed {package}", "No installed packages match"),
            step,
            package,
        )


class OutcomeSession:
    def __init__(self, output: str):
        self.output = output
        self.commands: list[str] = []
        self.connected = False
        self.expert_entered = False
        self.closed = False
        self.clish_calls: list[tuple[str, bool]] = []

    def connect(self) -> None:
        self.connected = True

    def enter_expert(self, _password: str) -> None:
        self.expert_entered = True

    def run(self, command: str, timeout: int = 0) -> Result:
        self.commands.append(command)
        return Result(self.output)

    def run_interactive_clish(
        self,
        command: str,
        *,
        acquire_lock: bool,
        timeout: int = 0,
    ) -> Result:
        self.clish_calls.append((command, acquire_lock))
        return self.run(command, timeout)

    def close(self) -> None:
        self.closed = True


class ProductionClishHarness:
    run_interactive_clish = direct.c.SshPty.run_interactive_clish
    enter_expert = direct.c.SshPty.enter_expert
    _enter_expert_with_password = direct.c.SshPty._enter_expert_with_password
    _read_parent_clish_command = direct.c.SshPty._read_parent_clish_command
    _read_parent_clish_command_with_confirmation = (
        direct.c.SshPty._read_parent_clish_command_with_confirmation
    )
    _read_until_pattern = direct.c.SshPty._read_until_pattern
    _parse_parent_clish_output = direct.c.SshPty._parse_parent_clish_output

    def __init__(self, mode: str = "success"):
        self.host = "192.0.2.20"
        self.mode = mode
        self.buffer = b""
        self.channel_pid = 4242
        self.events: list[tuple[int, str, str]] = []
        self.chunks: list[bytes] = []
        self._session_mode = "expert"
        self._expert_password = "expert-password"

    def drain_pending(self, **_kwargs: object) -> None:
        self.events.append((self.channel_pid, "drain", ""))

    def sendline(self, line: str) -> None:
        self.events.append((self.channel_pid, "sendline", line))
        if line == "clish":
            self.chunks.append(
                b"clish\r\n"
                b"CLINFR0479 You can't start interactive session from another "
                b"interactive session\r\n"
                b"[Expert@CP-FW-B:0]# "
            )
        elif line == "exit":
            if self.mode == "parent-transition-failure":
                self.chunks.append(
                    b"exit\r\nexit\r\nunexpected transition output\r\n"
                    b"CP-FW-B> "
                )
            else:
                self.chunks.append(
                    (
                        DIRECT_FIXTURES
                        / "parent_clish_exit_transition.txt"
                    ).read_bytes()
                )
        elif line == "lock database override":
            if self.mode == "lock-failure":
                self.chunks.append(
                    b"lock database override\r\n"
                    b"CLINFR0519 Configuration lock present. Can not execute.\r\n"
                    b"CP-FW-B> "
                )
            else:
                self.chunks.append(
                    b"lock database override\r\nCP-FW-B> "
                )
        elif line == "expert":
            if self.mode == "reentry-failure":
                self.chunks.append(b"expert\r\nCP-FW-B> ")
            else:
                self.chunks.append(b"expert\r\nEnter expert password: ")
        elif (
            line.startswith("installer install ")
            and self.mode
            in {
                "install-confirmation",
                "wrong-install-confirmation",
                "duplicate-install-confirmation",
            }
        ):
            package = (
                "Different_Package_T76_FULL.tgz"
                if self.mode == "wrong-install-confirmation"
                else "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
            )
            prompt = (
                "The machine will automatically reboot after install of "
                f"{package}. \r\n"
                "Do you want to continue? ([y]es / [n]o / [s]uppress reboot)  "
            )
            if self.mode == "duplicate-install-confirmation":
                prompt += "\r\n" + prompt
            trailer = (
                "\r\nCP-FW-B> "
                if self.mode == "wrong-install-confirmation"
                else ""
            )
            self.chunks.append((f"{line}\r\n{prompt}{trailer}").encode())
        elif line.startswith("installer upgrade ") and self.mode in {
            "upgrade-confirmation",
            "upgrade-install-choices",
        }:
            package = (
                "blink_image_1.1_Check_Point_R82_T777_JHF_T60_"
                "SecurityGateway.tgz"
            )
            choices = (
                "([y]es / [n]o / [s]uppress reboot)"
                if self.mode == "upgrade-install-choices"
                else "([y]es / [n]o)"
            )
            prompt = (
                "The machine will automatically reboot after upgrade of "
                f"{package}. \r\nDo you want to continue? {choices}  "
            )
            trailer = (
                "\r\nCP-FW-B> "
                if self.mode == "upgrade-install-choices"
                else ""
            )
            self.chunks.append((f"{line}\r\n{prompt}{trailer}").encode())
        elif line == "y":
            if self.mode not in {"install-confirmation", "upgrade-confirmation"}:
                raise AssertionError("confirmation sent for an unapproved prompt")
            self.chunks.append(
                b"y\r\nResult: Installed successfully\r\nCP-FW-B> "
            )
        elif line.startswith(("installer ", "show ")):
            if line == "show version all" and self.mode == "success":
                self.chunks.append(
                    (
                        DIRECT_FIXTURES / "parent_clish_show_version.txt"
                    ).read_bytes()
                )
                return
            if self.mode == "truncated":
                self.chunks.append(f"{line}\r\npartial output".encode())
                return
            if self.mode == "prompt-shaped":
                self.chunks.append(
                    (
                        f"{line}\r\nSPOOF> \r\n"
                        "Operation completed successfully\r\nCP-FW-B> "
                    ).encode()
                )
                return
            if self.mode == "trailing-output":
                self.chunks.extend(
                    (
                        (
                            f"{line}\r\nOperation completed successfully\r\n"
                            "CP-FW-B> "
                        ).encode(),
                        b"late output\r\n",
                    )
                )
                return
            detail = (
                "CLINFR0815 Invalid command or package cannot be processed"
                if self.mode == "command-failure"
                else "Operation completed successfully"
            )
            self.chunks.append(
                f"{line}\r\n{detail}\r\nCP-FW-B> ".encode()
            )

    def _write(self, data: str) -> None:
        self.events.append((self.channel_pid, "write", "<expert-password>"))
        if data != "expert-password\n":
            raise AssertionError(f"unexpected PTY write: {data!r}")
        if self.mode == "wrong-expert-password":
            self.chunks.append(b"Wrong password\r\nCP-FW-B> ")
        else:
            self.chunks.append(b"\r\n[Expert@CP-FW-B:0]# ")

    def _read_some(self, _timeout: float) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.mode == "truncated":
            raise direct.c.CheckPointError("synthetic truncated PTY")
        return b""


class DirectOutcomeTests(unittest.TestCase):
    def test_connect_enables_bounded_server_keepalives(self) -> None:
        session = direct.c.SshPty("192.0.2.10", "admin", "secret")
        with (
            mock.patch.object(direct.c.pty, "fork", return_value=(0, 55)),
            mock.patch.object(
                direct.c.os, "execvp", side_effect=RuntimeError("captured")
            ) as execvp,
        ):
            with self.assertRaisesRegex(RuntimeError, "captured"):
                session.connect()

        argv = execvp.call_args.args[1]
        self.assertIn("ServerAliveInterval=15", argv)
        self.assertIn("ServerAliveCountMax=3", argv)

    def test_policy_handoff_is_blink_install_or_upgrade_only(self) -> None:
        for package_type, action, expected in [
            ("blink", "install", True),
            ("blink", "upgrade", True),
            ("blink", "remove", False),
            ("jhf", "install", False),
            ("deployment_agent", "upgrade", False),
        ]:
            with self.subTest(package_type=package_type, action=action):
                self.assertEqual(
                    direct.policy_handoff_allowed(package_type, action), expected
                )

    def test_blink_can_handoff_ha_start_to_policy_gate(self) -> None:
        session = OutcomeSession("HA module not started.\n")
        with mock.patch.object(direct.c, "SshPty", return_value=session):
            direct.wait_cluster_ready(
                "192.0.2.20",
                "admin",
                "password",
                1,
                allow_policy_handoff=True,
            )

        self.assertEqual(session.commands, ["cphaprob state"])

    def test_non_blink_operation_rejects_ha_module_not_started(self) -> None:
        session = OutcomeSession("HA module not started.\n")
        with (
            mock.patch.object(direct.c, "SshPty", return_value=session),
            mock.patch.object(direct.time, "time", side_effect=[0, 0, 2]),
            mock.patch.object(direct.time, "sleep"),
        ):
            with self.assertRaisesRegex(TimeoutError, "did not become ready"):
                direct.wait_cluster_ready(
                    "192.0.2.20", "admin", "password", 1
                )

    def test_connect_sets_wide_pty_before_login(self) -> None:
        session = direct.c.SshPty("192.0.2.10", "admin", "secret")
        expected_winsize = struct.pack("HHHH", 24, 512, 0, 0)

        def assert_resized_before_login() -> None:
            ioctl.assert_called_once_with(
                55,
                direct.c.termios.TIOCSWINSZ,
                expected_winsize,
            )

        with (
            mock.patch.object(direct.c.pty, "fork", return_value=(1234, 55)),
            mock.patch.object(direct.c.fcntl, "ioctl") as ioctl,
            mock.patch.object(session, "_expect_login") as expect_login,
        ):
            expect_login.side_effect = assert_resized_before_login
            session.connect()

        ioctl.assert_called_once_with(
            55,
            direct.c.termios.TIOCSWINSZ,
            expected_winsize,
        )
        expect_login.assert_called_once_with()

    def fixture(self, name: str) -> str:
        return (DIRECT_FIXTURES / name).read_text()

    def test_command_builder_keeps_exact_package_identity(self) -> None:
        commands = direct.commands_for_step(
            {
                "name": "install",
                "action": "install",
                "package_type": "jhf",
                "source_path": "/var/log/tmp/synthetic.tgz",
            }
        )
        self.assertIn("installer verify synthetic.tgz", commands)
        self.assertIn("installer install synthetic.tgz", commands)

    def test_upgrade_uses_installer_upgrade(self) -> None:
        commands = direct.commands_for_step(
            {
                "name": "upgrade",
                "action": "upgrade",
                "package_type": "blink",
                "source_path": "/var/log/tmp/synthetic.tar",
            }
        )
        self.assertIn("installer upgrade synthetic.tar", commands)
        self.assertNotIn("installer install synthetic.tar", commands)

    def test_deployment_agent_dispatches_once_then_reconciles_fresh(self) -> None:
        plan = {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "install_agent",
                    "action": "install",
                    "package_type": "deployment_agent",
                    "source_path": "/var/log/tmp/DeploymentAgent_9999.tgz",
                }
            ],
        }
        initial = OutcomeSession("Build number: 100\n__RC=0\n")
        reconciler = OutcomeSession("Build number: 9999\n__RC=0\n")
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            intent_path = Path(tmp) / "intent.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "install-deployment-agent",
                "--step", "install_agent",
                "--mutation-intent-file", str(intent_path),
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(
                    direct.c, "SshPty", side_effect=[initial, reconciler]
                ),
            ):
                self.assertEqual(direct.main(), 0)

        self.assertEqual(len(initial.commands), 2)
        self.assertIn("show installer status all", initial.commands[0])
        self.assertIn("installer agent install", initial.commands[1])
        self.assertEqual(len(reconciler.commands), 1)

    def test_installer_disconnect_is_provisional_but_rc_failure_is_not(self) -> None:
        disconnected = OutcomeSession("")
        disconnected.run = mock.Mock(
            side_effect=direct.c.CheckPointError(
                self.fixture("installer_disconnect.txt").strip()
            )
        )
        self.assertTrue(
            direct.run_installer_mutation(
                disconnected, "192.0.2.20", "installer upgrade synthetic.tar", 60
            )
        )
        with self.assertRaisesRegex(RuntimeError, "exit status 7"):
            direct.run_installer_mutation(
                OutcomeSession(self.fixture("rc_nonzero.txt")),
                "192.0.2.20",
                "installer upgrade synthetic.tar",
                60,
            )

    def test_rc_capture_rejects_nonzero_and_missing_status(self) -> None:
        for fixture_name, message in (
            ("rc_nonzero.txt", "exit status 7"),
            ("rc_missing.txt", "did not return an exit status"),
        ):
            with self.subTest(fixture=fixture_name):
                session = OutcomeSession(self.fixture(fixture_name))
                with self.assertRaisesRegex(RuntimeError, message):
                    direct.run_checked(
                        session,
                        "192.0.2.20",
                        "installer install synthetic.tgz",
                        60,
                    )

    def test_rc_capture_wraps_clish_and_accepts_zero(self) -> None:
        session = OutcomeSession("Operation completed successfully\n__RC=0\n")
        direct.run_checked(
            session,
            "192.0.2.20",
            "installer install synthetic.tgz",
            60,
        )
        self.assertEqual(
            session.clish_calls,
            [("installer install synthetic.tgz", True)],
        )

    def test_only_installer_commands_require_clish_lock(self) -> None:
        self.assertTrue(
            direct.clish_command_requires_lock(
                "installer import local /var/log/tmp/synthetic.tgz"
            )
        )
        self.assertFalse(direct.clish_command_requires_lock("show version all"))

    def test_enter_expert_accepts_verified_direct_expert_login(self) -> None:
        session = ProductionClishHarness()
        session._expert_password = None
        session.enter_expert("expert-password")
        self.assertEqual(session._session_mode, "expert")
        self.assertEqual(session._expert_password, "expert-password")
        self.assertFalse(any(event[1] == "sendline" for event in session.events))

    def test_parent_clish_prompt_timeout_invalidates_session_mode(self) -> None:
        session = ProductionClishHarness()
        session._session_mode = "clish"
        with self.assertRaisesRegex(
            direct.c.CheckPointError,
            "timed out waiting for synthetic parent Clish prompt",
        ):
            session._read_until_pattern(
                direct.c.CLISH_PROMPT_RE,
                deadline=time.time() - 1,
                context="synthetic parent Clish prompt",
            )
        self.assertEqual(session._session_mode, "unknown")

    def test_production_channel_uses_parent_clish_and_reenters_expert(self) -> None:
        session = ProductionClishHarness()
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            output = direct.run_checked(
                session,
                "192.0.2.20",
                "installer import local /var/log/tmp/synthetic.tgz",
                60,
            )
        self.assertIn("Operation completed successfully", output)
        self.assertNotIn("__RC=", output)
        self.assertEqual({event[0] for event in session.events}, {4242})
        sent = [event[2] for event in session.events if event[1] == "sendline"]
        self.assertEqual(
            sent,
            [
                "exit",
                "lock database override",
                "installer import local /var/log/tmp/synthetic.tgz",
                "expert",
            ],
        )
        self.assertNotIn("clish", sent)
        self.assertEqual(session._session_mode, "expert")

    def test_install_confirmation_is_exact_and_sent_once(self) -> None:
        command = (
            "installer install "
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar"
        )
        session = ProductionClishHarness(mode="install-confirmation")
        pattern = direct.installer_confirmation_pattern(
            command,
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar",
        )
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            output = direct.run_checked(
                session,
                "192.0.2.20",
                command,
                60,
                confirmation_pattern=pattern,
            )
        self.assertIn("Installed successfully", output)
        sent = [event[2] for event in session.events if event[1] == "sendline"]
        self.assertEqual(
            sent,
            [
                "exit",
                "lock database override",
                command,
                "y",
                "expert",
            ],
        )
        self.assertEqual(session._session_mode, "expert")

    def test_install_confirmation_rejects_wrong_or_duplicate_prompt(self) -> None:
        command = (
            "installer install "
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar"
        )
        pattern = direct.installer_confirmation_pattern(
            command,
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar",
        )
        for mode, message in (
            ("wrong-install-confirmation", "exact parent Clish confirmation"),
            ("duplicate-install-confirmation", "ambiguous parent Clish confirmation"),
        ):
            session = ProductionClishHarness(mode=mode)
            with (
                self.subTest(mode=mode),
                mock.patch.object(
                    direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
                ),
                self.assertRaisesRegex(direct.c.CheckPointError, message),
            ):
                direct.run_checked(
                    session,
                    "192.0.2.20",
                    command,
                    60,
                    confirmation_pattern=pattern,
                )
            sent = [
                event[2] for event in session.events if event[1] == "sendline"
            ]
            self.assertNotIn("y", sent)

    def test_upgrade_confirmation_accepts_exact_blink_prompt(self) -> None:
        package = (
            "blink_image_1.1_Check_Point_R82_T777_JHF_T60_"
            "SecurityGateway.tgz"
        )
        command = f"installer upgrade {package}"
        session = ProductionClishHarness(mode="upgrade-confirmation")
        pattern = direct.installer_confirmation_pattern(command, package)
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            output = direct.run_checked(
                session,
                "192.0.2.20",
                command,
                60,
                confirmation_pattern=pattern,
            )
        self.assertIn("Installed successfully", output)
        sent = [event[2] for event in session.events if event[1] == "sendline"]
        self.assertEqual(
            sent,
            ["exit", "lock database override", command, "y", "expert"],
        )

    def test_upgrade_confirmation_rejects_install_choice_set(self) -> None:
        package = (
            "blink_image_1.1_Check_Point_R82_T777_JHF_T60_"
            "SecurityGateway.tgz"
        )
        command = f"installer upgrade {package}"
        session = ProductionClishHarness(mode="upgrade-install-choices")
        pattern = direct.installer_confirmation_pattern(command, package)
        with (
            mock.patch.object(
                direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
            ),
            self.assertRaisesRegex(
                direct.c.CheckPointError,
                "exact parent Clish confirmation",
            ),
        ):
            direct.run_checked(
                session,
                "192.0.2.20",
                command,
                60,
                confirmation_pattern=pattern,
            )

    def test_parser_accepts_exact_live_crlf_fixtures(self) -> None:
        session = ProductionClishHarness()
        transition = (
            DIRECT_FIXTURES / "parent_clish_exit_transition.txt"
        ).read_bytes()
        show_version = (
            DIRECT_FIXTURES / "parent_clish_show_version.txt"
        ).read_bytes()

        self.assertEqual(
            session._parse_parent_clish_output(
                transition,
                "exit",
                expected_echo_count=2,
            ),
            "",
        )
        self.assertIn(
            "Product version Check Point Gaia R81.20",
            session._parse_parent_clish_output(
                show_version,
                "show version all",
            ),
        )
        with self.assertRaisesRegex(
            direct.c.CheckPointError,
            "ambiguous or truncated",
        ):
            session._parse_parent_clish_output(transition, "exit")

    def test_duplicate_exit_must_be_exact_and_adjacent(self) -> None:
        session = ProductionClishHarness()
        malformed = (
            b"exit\r\nnoise\r\nexit\r\nCP-FW-B> ",
            b"exit\r\nexit\r\nexit\r\nCP-FW-B> ",
            b"exit\r\nCP-FW-B> ",
        )
        for transcript in malformed:
            with self.subTest(transcript=transcript), self.assertRaises(
                direct.c.CheckPointError
            ):
                session._parse_parent_clish_output(
                    transcript,
                    "exit",
                    expected_echo_count=2,
                )

        with self.assertRaisesRegex(
            direct.c.CheckPointError,
            "ambiguous or truncated",
        ):
            session._parse_parent_clish_output(
                b"show version all\r\nshow version all\r\nCP-FW-B> ",
                "show version all",
            )


    def test_clinfr0479_nested_clish_regression_is_not_reachable(self) -> None:
        session = ProductionClishHarness()
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            direct.run_checked(
                session,
                "192.0.2.20",
                "installer verify synthetic.tgz",
                60,
            )
        self.assertFalse(
            any(
                event[1] == "sendline" and event[2] == "clish"
                for event in session.events
            )
        )

    def test_lock_failure_prevents_mutation_and_restores_expert(self) -> None:
        session = ProductionClishHarness(mode="lock-failure")
        with (
            mock.patch.object(
                direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
            ),
            self.assertRaisesRegex(
                direct.c.CheckPointError,
                "could not acquire Gaia configuration lock",
            ),
        ):
            direct.run_checked(
                session,
                "192.0.2.20",
                "installer import local /var/log/tmp/synthetic.tgz",
                60,
            )
        sent = [event[2] for event in session.events if event[1] == "sendline"]
        self.assertNotIn(
            "installer import local /var/log/tmp/synthetic.tgz",
            sent,
        )
        self.assertEqual(sent, ["exit", "lock database override", "expert"])
        self.assertEqual(session._session_mode, "expert")

    def test_command_failure_text_fails_after_safe_expert_reentry(self) -> None:
        session = ProductionClishHarness(mode="command-failure")
        with (
            mock.patch.object(
                direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
            ),
            self.assertRaisesRegex(RuntimeError, "failure marker"),
        ):
            direct.run_checked(
                session,
                "192.0.2.20",
                "installer install synthetic.tgz",
                60,
            )
        self.assertEqual(session._session_mode, "expert")
        self.assertEqual(
            [event[2] for event in session.events if event[1] == "sendline"][-1],
            "expert",
        )

    def test_parent_clish_rejects_ambiguous_truncated_and_trailing_output(self) -> None:
        cases = (
            ("prompt-shaped", "ambiguous or truncated"),
            ("truncated", "synthetic truncated PTY"),
            ("trailing-output", "output followed parent Clish prompt"),
        )
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            for mode, message in cases:
                with self.subTest(mode=mode), self.assertRaisesRegex(
                    direct.c.CheckPointError,
                    message,
                ):
                    direct.run_checked(
                        ProductionClishHarness(mode=mode),
                        "192.0.2.20",
                        "installer install synthetic.tgz",
                        60,
                    )

    def test_parent_transition_and_expert_reentry_fail_closed(self) -> None:
        for mode, message in (
            ("parent-transition-failure", "unexpected output"),
            ("reentry-failure", "expert transition returned to Clish"),
        ):
            with (
                self.subTest(mode=mode),
                mock.patch.object(
                    direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
                ),
                self.assertRaisesRegex(direct.c.CheckPointError, message),
            ):
                direct.run_checked(
                    ProductionClishHarness(mode=mode),
                    "192.0.2.20",
                    "installer install synthetic.tgz",
                    60,
                )

    def test_read_only_query_uses_parent_clish_without_lock_and_fails_on_error(self) -> None:
        session = ProductionClishHarness()
        with mock.patch.object(
            direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
        ):
            direct.run_checked(session, "192.0.2.20", "show version all", 60)
        sent = [event[2] for event in session.events if event[1] == "sendline"]
        self.assertNotIn("lock database override", sent)

        with (
            mock.patch.object(
                direct.c, "CLISH_FINAL_PROMPT_QUIET_SECONDS", 0.001
            ),
            self.assertRaisesRegex(RuntimeError, "failure marker"),
        ):
            direct.run_checked(
                ProductionClishHarness(mode="command-failure"),
                "192.0.2.20",
                "show installer status all",
                60,
            )

    def test_invalid_command_is_rejected_before_parent_transition(self) -> None:
        session = ProductionClishHarness()
        with self.assertRaisesRegex(
            direct.c.CheckPointError,
            "invalid interactive Clish command",
        ):
            direct.run_checked(
                session,
                "192.0.2.20",
                "installer install synthetic.tgz\nshow version all",
                60,
            )
        self.assertEqual(session.events, [])

    def test_exact_locale_warning_is_benign_but_other_cannot_is_fatal(self) -> None:
        direct.run_checked(
            OutcomeSession(
                "sh: warning: setlocale: LC_ALL: cannot change locale (C.UTF-8)\n"
                "Operation completed successfully\n__RC=0\n"
            ),
            "192.0.2.20",
            "installer import local /var/log/tmp/synthetic.tgz",
            60,
        )
        with self.assertRaisesRegex(RuntimeError, "failure marker"):
            direct.run_checked(
                OutcomeSession("Package cannot be imported\n__RC=0\n"),
                "192.0.2.20",
                "installer import local /var/log/tmp/synthetic.tgz",
                60,
            )

    def test_fatal_marker_cannot_be_canceled_by_tolerated_text(self) -> None:
        session = OutcomeSession(
            "Operation failed before completion\nNo errors in cleanup\n__RC=0\n"
        )
        with self.assertRaisesRegex(RuntimeError, "failure marker"):
            direct.run_checked(
                session,
                "192.0.2.20",
                "installer install synthetic.tgz",
                60,
            )

    def test_zero_error_summary_is_not_a_fatal_marker(self) -> None:
        for summary in ("Errors: 0", "Error: 0", "No errors", "0 errors"):
            with self.subTest(summary=summary):
                direct.run_checked(
                    OutcomeSession(f"{summary}\nOperation completed successfully\n__RC=0\n"),
                    "192.0.2.20",
                    "show installer status all",
                    60,
                )

    def test_exact_package_presence_rejects_negative_and_lookalike_rows(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        present = self.fixture("package_still_installed.txt")
        absent = self.fixture("package_absent.txt")
        self.assertTrue(direct.package_identity_is_installed(present, package))
        tar_package = package.removesuffix(".tgz") + ".tar"
        self.assertTrue(direct.package_identity_is_installed(present, tar_package))
        self.assertFalse(
            direct.package_identity_is_installed(absent, package)
        )
        with self.assertRaisesRegex(RuntimeError, "extension lookalike"):
            direct.package_identity_is_installed(
                present.replace(".tgz", ".tgz.bak"), tar_package
            )
        self.assertFalse(
            direct.package_identity_is_installed(
                f"{package} | Status: Not Installed", package
            )
        )
        self.assertFalse(
            direct.package_identity_is_installed(
                package.replace("_FULL.tgz", "_SPECIAL_FULL.tgz")
                + " | Status: Installed",
                package,
            )
        )

    def test_post_uninstall_reconciliation_fails_if_package_remains(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        session = OutcomeSession(self.fixture("package_still_installed.txt"))
        with mock.patch.object(direct.c, "SshPty", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "still installed"):
                direct.verify_package_absent(
                    "192.0.2.20", "admin", "password", "expert", package
                )
        self.assertTrue(session.connected)
        self.assertTrue(session.expert_entered)
        self.assertTrue(session.closed)

    def test_post_uninstall_reconciliation_accepts_exact_absence(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        session = OutcomeSession(self.fixture("package_absent.txt"))
        with mock.patch.object(direct.c, "SshPty", return_value=session):
            direct.verify_package_absent(
                "192.0.2.20", "admin", "password", "expert", package
            )
        self.assertTrue(session.closed)

    def test_post_uninstall_reconciliation_rejects_untrusted_absence_output(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        installed = f"{package} | Status: Installed"
        removed = f"{package} | Status: Removed"
        for output in (
            "",
            "   ",
            "Error: unavailable",
            "malformed output",
            f"{installed}\nmalformed output",
            "No installed packages match\nmalformed output",
            "Installed Packages\nmalformed output",
            f"No installed packages match\n{installed}",
            "No installed packages match\nNo installed packages match",
            f"{package} | Sta\x1b[31mtus: Removed",
            f"{removed}\n{removed}",
            f"{removed}\n{package.lower()} | Status: Removed",
        ):
            session = OutcomeSession(output + "\n__RC=0\n")
            with (
                self.subTest(output=output),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                self.assertRaises(RuntimeError),
            ):
                direct.verify_package_absent(
                    "192.0.2.20", "admin", "password", "expert", package
                )

    def test_post_uninstall_reconciliation_accepts_explicit_empty_state(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        session = OutcomeSession("No installed packages match\n__RC=0\n")
        with mock.patch.object(direct.c, "SshPty", return_value=session):
            direct.verify_package_absent(
                "192.0.2.20", "admin", "password", "expert", package
            )
        self.assertTrue(session.closed)

    def test_target_reconciliation_requires_exact_version_take_and_package(self) -> None:
        package = "Check_Point_R82_Blink_T60.tar"

        class ReconcileSession(OutcomeSession):
            def run(self, command: str, timeout: int = 0) -> Result:
                self.commands.append(command)
                if "show version all" in command:
                    return Result("Product version R82 Build 777\n__RC=0\n")
                if "cpinfo -y all" in command:
                    return Result(
                        "BUNDLE_R82_JUMBO_HF_MAIN Take: 60\n__RC=0\n"
                    )
                if "show installer packages installed" in command:
                    return Result(f"{package} | Status: Installed\n__RC=0\n")
                raise AssertionError(command)

        with mock.patch.object(direct.c, "SshPty", return_value=ReconcileSession("")):
            result = direct.verify_package_present(
                "192.0.2.20", "admin", "password", "expert", "R82", "60", package
            )
        self.assertEqual(result["result"], "exact-target-confirmed")

        cases = {
            "wrong release": (self.fixture("version_wrong_r8120.txt"), self.fixture("take_60.txt"), self.fixture("blink_present.txt")),
            "Take 60": (self.fixture("version_r82.txt"), self.fixture("take_missing.txt"), self.fixture("blink_present.txt")),
            "exact installed package": (self.fixture("version_r82.txt"), self.fixture("take_60.txt"), self.fixture("blink_wrong.txt")),
        }
        for message, outputs in cases.items():
            class WrongSession(ReconcileSession):
                def run(self, command: str, timeout: int = 0) -> Result:
                    if "show version all" in command:
                        return Result(outputs[0])
                    if "cpinfo -y all" in command:
                        return Result(outputs[1])
                    return Result(outputs[2])

            with self.subTest(message=message), mock.patch.object(
                direct.c, "SshPty", return_value=WrongSession("")
            ), self.assertRaisesRegex(RuntimeError, message):
                direct.verify_package_present(
                    "192.0.2.20", "admin", "password", "expert", "R82", "60", package
                )

    def test_take_reconciliation_is_bound_to_exact_target_release(self) -> None:
        output = self.fixture("take_multi_release.txt")
        self.assertEqual(direct.installed_jhf_take(output, "R81.20"), "60")
        self.assertEqual(direct.installed_jhf_take(output, "R82"), "91")
        self.assertNotEqual(direct.installed_jhf_take(output, "R82"), "60")

    def test_install_retry_after_dispatch_is_reconciliation_only(self) -> None:
        package = "Check_Point_R82_Blink_T60.tar"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "target_version": "R82",
                "target_take": "60",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "upgrade",
                    "action": "upgrade",
                    "package_type": "blink",
                    "source_path": f"/var/log/tmp/{package}",
                }
            ],
        }
        session = OutcomeSession("Operation completed successfully\n__RC=0\n")
        reconciliation = {
            "host": "192.0.2.20",
            "target_version": "R82",
            "target_take": "60",
            "package_name": package,
            "result": "exact-target-confirmed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            reconciliation_path = root / "reconciliation.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "first-member",
                "--step", "upgrade",
                "--mutation-intent-file", str(intent_path),
                "--reconciliation-file", str(reconciliation_path),
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                mock.patch.object(
                    direct, "verify_package_present", side_effect=RuntimeError("not installed")
                ),
                mock.patch.object(
                    direct,
                    "run_installer_mutation",
                    side_effect=RuntimeError("simulated crash after dispatch"),
                ) as mutation,
                mock.patch.object(
                    direct, "wait_for_package_present", return_value=reconciliation
                ) as reconcile,
                mock.patch.object(direct, "wait_cluster_ready"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash after dispatch"):
                    direct.main()
                self.assertTrue(intent_path.exists())
                self.assertEqual(intent_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(direct.main(), 0)
                written_reconciliation = json.loads(
                    reconciliation_path.read_text()
                )

        mutation.assert_called_once()
        reconcile.assert_called_once()
        self.assertEqual(written_reconciliation, reconciliation)

    def test_remove_retry_never_redispatches_and_still_present_fails_closed(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "remove",
                    "action": "remove",
                    "package_type": "jhf",
                    "package_name": "Take76",
                }
            ],
        }
        session = OutcomeSession("")
        reconciliation = {
            "host": "192.0.2.20",
            "package_name": package,
            "result": "exact-package-absence-confirmed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "first-member",
                "--step", "remove",
                "--mutation-intent-file", str(intent_path),
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                mock.patch.object(
                    direct, "resolve_remove_package_name", return_value=package
                ) as resolver,
                mock.patch.object(
                    direct, "validate_persisted_remove_identity"
                ) as validate_persisted,
                mock.patch.object(
                    direct,
                    "run_interactive_uninstall",
                    side_effect=RuntimeError("simulated crash after dispatch"),
                ) as mutation,
                mock.patch.object(
                    direct,
                    "verify_package_absent",
                    side_effect=[RuntimeError("package still installed"), reconciliation],
                ) as reconcile,
                mock.patch.object(direct, "wait_cluster_ready"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash after dispatch"):
                    direct.main()
                with self.assertRaisesRegex(RuntimeError, "still installed"):
                    direct.main()
                self.assertEqual(direct.main(), 0)

        mutation.assert_called_once()
        self.assertEqual(reconcile.call_count, 2)
        resolver.assert_called_once()
        self.assertEqual(validate_persisted.call_count, 2)

    def test_stale_mismatched_intent_cannot_complete_removal(self) -> None:
        requested_package = "Take76"
        unrelated_package = "Unrelated_Package_T1.tgz"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "remove",
                    "action": "remove",
                    "package_type": "jhf",
                    "package_name": requested_package,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            plan_path.write_text(json.dumps(plan))
            plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            direct.persist_mutation_intent(
                intent_path,
                {
                    "host": "192.0.2.20",
                    "action": "remove",
                    "step_name": "remove",
                    "plan_sha256": plan_sha256,
                    "requested_package_name": "Take91",
                    "requested_source_path": "",
                    "requested_package_type": "jhf",
                    "package_name": unrelated_package,
                },
            )
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "first-member",
                "--step", "remove",
                "--mutation-intent-file", str(intent_path),
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct, "verify_package_absent") as reconcile,
                self.assertRaisesRegex(
                    RuntimeError, "expected requested_package_name"
                ),
            ):
                direct.main()
            reconcile.assert_not_called()

    def test_same_alias_intent_cannot_reconcile_unrelated_resolved_identity(self) -> None:
        requested_alias = "Take76"
        unrelated_package = "Unrelated_Package_T1.tgz"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "remove",
                    "action": "remove",
                    "package_type": "jhf",
                    "package_name": requested_alias,
                }
            ],
        }
        session = OutcomeSession("")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            plan_path.write_text(json.dumps(plan))
            direct.persist_mutation_intent(
                intent_path,
                {
                    "host": "192.0.2.20",
                    "action": "remove",
                    "step_name": "remove",
                    "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                    "requested_package_name": requested_alias,
                    "requested_source_path": "",
                    "requested_package_type": "jhf",
                    "package_name": unrelated_package,
                },
            )
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file",
                str(plan_path),
                "--reports-dir",
                tmp,
                "--phase",
                "first-member",
                "--step",
                "remove",
                "--mutation-intent-file",
                str(intent_path),
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                mock.patch.object(
                    direct,
                    "validate_persisted_remove_identity",
                    side_effect=RuntimeError(
                        "persisted uninstall identity is not supported by alias evidence"
                    ),
                ) as validate_persisted,
                mock.patch.object(direct, "verify_package_absent") as reconcile,
                mock.patch.object(direct, "run_interactive_uninstall") as uninstall,
                self.assertRaisesRegex(
                    RuntimeError,
                    "persisted uninstall identity",
                ),
            ):
                direct.main()
            validate_persisted.assert_called_once_with(
                session,
                plan["package_steps"][0],
                unrelated_package,
            )
            reconcile.assert_not_called()
            uninstall.assert_not_called()
            self.assertTrue(session.connected)
            self.assertTrue(session.expert_entered)
            self.assertTrue(session.closed)

    def test_intent_reader_validates_plan_and_all_requested_identity_fields(self) -> None:
        intent = {
            "host": "192.0.2.20",
            "action": "upgrade",
            "step_name": "upgrade",
            "plan_sha256": hashlib.sha256(b"exact plan bytes").hexdigest(),
            "requested_package_name": "R82_Blink",
            "requested_source_path": "/var/log/tmp/R82_Blink.tgz",
            "requested_package_type": "blink",
            "package_name": "R82_Blink.tgz",
            "target_version": "R82",
            "target_take": "60",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            direct.persist_mutation_intent(path, intent)
            for field in (
                "plan_sha256",
                "requested_package_name",
                "requested_source_path",
                "requested_package_type",
            ):
                expected = dict(intent)
                expected[field] = "mismatch"
                with self.subTest(field=field), self.assertRaisesRegex(
                    RuntimeError, f"expected {field}"
                ):
                    direct.read_mutation_intent(path, expected)

    def test_pre_dispatch_crash_never_redispatches_and_requires_new_run(self) -> None:
        package = "Check_Point_R82_Blink_T60.tar"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "target_version": "R82",
                "target_take": "60",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [
                {
                    "name": "upgrade",
                    "action": "upgrade",
                    "package_type": "blink",
                    "source_path": f"/var/log/tmp/{package}",
                }
            ],
        }
        session = OutcomeSession("Operation completed successfully\n__RC=0\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "first-member",
                "--step", "upgrade",
                "--mutation-intent-file", str(intent_path),
                "--execute",
            ]
            original_persist = direct.persist_mutation_intent

            def crash_after_intent(path: Path, intent: dict[str, str]) -> bool:
                original_persist(path, intent)
                raise RuntimeError("simulated crash before installer dispatch")

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                mock.patch.object(
                    direct, "verify_package_present", side_effect=RuntimeError("not installed")
                ),
                mock.patch.object(
                    direct, "persist_mutation_intent", side_effect=crash_after_intent
                ),
                mock.patch.object(direct, "run_installer_mutation") as mutation,
            ):
                with self.assertRaisesRegex(RuntimeError, "before installer dispatch"):
                    direct.main()
            self.assertTrue(intent_path.exists())
            mutation.assert_not_called()

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(
                    direct,
                    "wait_for_package_present",
                    side_effect=RuntimeError("target is not installed"),
                ),
                mock.patch.object(direct, "run_installer_mutation") as retry_mutation,
                self.assertRaisesRegex(
                    RuntimeError,
                    "never be cleared, deleted, or reused.*restore the authorized clean snapshot/baseline.*new run directory",
                ),
            ):
                direct.main()
            retry_mutation.assert_not_called()

    def test_standalone_reconciliation_only_without_intent_never_dispatches(self) -> None:
        package = "Check_Point_R82_Blink_T60.tar"
        plan = {
            "change": {"number": "STANDALONE_TEST"},
            "checkpoint": {
                "cluster_mode": "standalone",
                "target_version": "R82",
                "target_take": "60",
                "members": [{"ip": "192.0.2.20"}],
            },
            "package_steps": [{
                "name": "upgrade",
                "action": "upgrade",
                "package_type": "blink",
                "source_path": f"/var/log/tmp/{package}",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_path = root / "intents" / "first-member.json"
            reconciliation_path = root / "reconciliation.json"
            plan_path.write_text(json.dumps(plan))
            plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            argv = [
                "direct_package_step_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--reports-dir", tmp,
                "--phase", "first-member",
                "--step", "upgrade",
                "--mutation-intent-file", str(intent_path),
                "--reconciliation-file", str(reconciliation_path),
                "--standalone-run-id", "run_" + "1" * 64,
                "--standalone-plan-sha256", plan_sha256,
                "--standalone-phase", "first-member",
                "--standalone-operation-id", "operation_" + "2" * 64,
                "--standalone-completion-id", "3" * 64,
                "--standalone-event-nonce", "4" * 64,
                "--standalone-reconciliation-only",
                "--execute",
            ]
            session = OutcomeSession("not installed\\n__RC=0\\n")
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(
                    os.environ,
                    {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
                    clear=False,
                ),
                mock.patch.object(direct.c, "SshPty", return_value=session),
                mock.patch.object(
                    direct, "verify_package_present",
                    side_effect=RuntimeError("not installed"),
                ),
                mock.patch.object(direct, "persist_mutation_intent") as persist,
                mock.patch.object(direct, "run_installer_mutation") as mutation,
                self.assertRaisesRegex(RuntimeError, "redispatch is prohibited"),
            ):
                direct.main()
            self.assertFalse(intent_path.exists())
            persist.assert_not_called()
            mutation.assert_not_called()

    def test_intent_reader_rejects_symlink_and_insecure_mode(self) -> None:
        intent = {
            "host": "192.0.2.20",
            "action": "remove",
            "step_name": "remove",
            "plan_sha256": hashlib.sha256(b"plan").hexdigest(),
            "requested_package_name": "Take76",
            "requested_source_path": "",
            "requested_package_type": "jhf",
            "package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_path = root / "real.json"
            link_path = root / "linked.json"
            direct.persist_mutation_intent(real_path, intent)
            link_path.symlink_to(real_path)
            with self.assertRaisesRegex(RuntimeError, "opened safely"):
                direct.read_mutation_intent(link_path, intent)
            real_path.chmod(0o640)
            with self.assertRaisesRegex(RuntimeError, "mode 0600"):
                direct.read_mutation_intent(real_path, intent)

    def test_disconnect_reconciliation_retries_until_exact_target(self) -> None:
        expected = {
            "host": "192.0.2.20",
            "result": "exact-target-confirmed",
        }
        with mock.patch.object(
            direct,
            "verify_package_present",
            side_effect=[RuntimeError("wrong release"), expected],
        ) as verify, mock.patch.object(direct.time, "sleep"):
            result = direct.wait_for_package_present(
                "192.0.2.20",
                "admin",
                "password",
                "expert",
                "R82",
                "60",
                "Check_Point_R82_Blink_T60.tar",
                60,
            )
        self.assertEqual(result, expected)
        self.assertEqual(verify.call_count, 2)

    def test_target_reconciliation_inputs_are_mandatory(self) -> None:
        base = {
            "name": "upgrade",
            "action": "upgrade",
            "package_type": "blink",
            "source_path": "/var/log/tmp/Check_Point_R82_Blink_T60.tar",
        }
        with self.assertRaisesRegex(RuntimeError, "target_version"):
            direct.target_state_for_step({"checkpoint": {"target_take": "60"}}, base)
        with self.assertRaisesRegex(RuntimeError, "target_take"):
            direct.target_state_for_step({"checkpoint": {"target_version": "R82"}}, base)

    def test_reconciliation_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "member.json"
            direct.write_reconciliation(path, {"result": "confirmed"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path).st_size, len(path.read_bytes()))




class DeploymentAgentIntentTests(unittest.TestCase):
    def plan(self, members: list[str] | None = None) -> dict:
        members = members or ["192.0.2.20"]
        return {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "cluster_mode": "cluster" if len(members) > 1 else "standalone",
                "members": [{"ip": host} for host in members],
            },
            "package_steps": [{
                "name": "install_agent",
                "action": "install",
                "package_type": "deployment_agent",
                "source_path": "/var/log/tmp/DeploymentAgent_9999.tgz",
            }],
        }

    def argv(self, plan_path: Path, root: Path, *extra: str) -> list[str]:
        return [
            "direct_package_step_from_activity.py",
            "--activity-plan-file", str(plan_path),
            "--reports-dir", str(root),
            "--phase", "install-deployment-agent",
            "--step", "install_agent",
            *extra,
            "--execute",
        ]

    def environment(self):
        return mock.patch.dict(
            os.environ,
            {"CP_PASSWORD": "password", "CP_EXPERT_PASSWORD": "expert"},
            clear=False,
        )

    def test_requested_minimum_build_is_required_and_strict(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires requested_build"):
            direct.requested_deployment_agent_minimum_build({
                "package_type": "deployment_agent",
                "source_path": "/var/log/tmp/agent-package.tgz",
            })
        for value in (True, 1.5, "one", "0", -1):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "positive integer"
            ):
                direct.requested_deployment_agent_minimum_build({"requested_build": value})
        self.assertEqual(
            direct.requested_deployment_agent_minimum_build({"requested_build": "00123"}),
            123,
        )

    def test_lower_equal_higher_and_multiple_build_policy(self) -> None:
        for observed, accepted in ((9998, False), (9999, True), (10000, True)):
            with self.subTest(observed=observed):
                session = OutcomeSession(
                    f"Build number: {observed}\n__RC=0\n"
                )
                with mock.patch.object(
                    direct.c, "SshPty", return_value=session
                ):
                    if accepted:
                        result = direct.verify_deployment_agent_minimum_build(
                            "192.0.2.20", "admin", "password", "expert", 9999
                        )
                        self.assertEqual(
                            result,
                            {
                                "host": "192.0.2.20",
                                "requested_minimum_build": "9999",
                                "observed_build": str(observed),
                            },
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "below requested"):
                            direct.verify_deployment_agent_minimum_build(
                                "192.0.2.20", "admin", "password", "expert", 9999
                            )

        multiple = OutcomeSession(
            "Build number: 9999\nBuild number: 10000\n__RC=0\n"
        )
        with (
            mock.patch.object(direct.c, "SshPty", return_value=multiple),
            self.assertRaisesRegex(RuntimeError, "no unique installed build"),
        ):
            direct.verify_deployment_agent_minimum_build(
                "192.0.2.20", "admin", "password", "expert", 9999
            )

    def test_equal_and_higher_builds_are_idempotent_noops_and_never_downgrade(
        self,
    ) -> None:
        for observed in (9999, 10000):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as tmp:
                session = OutcomeSession(
                    f"Build number: {observed}\n__RC=0\n"
                )
                root = Path(tmp)
                plan_path = root / "plan.json"
                intent_path = root / "intent.json"
                reconciliation_path = root / "reconciliation.json"
                plan_path.write_text(json.dumps(self.plan()))
                argv = self.argv(
                    plan_path,
                    root,
                    "--mutation-intent-file",
                    str(intent_path),
                    "--reconciliation-file",
                    str(reconciliation_path),
                )
                with (
                    mock.patch.object(sys, "argv", argv),
                    self.environment(),
                    mock.patch.object(direct.c, "SshPty", return_value=session),
                    mock.patch.object(direct, "run_installer_mutation") as mutation,
                ):
                    self.assertEqual(direct.main(), 0)
                mutation.assert_not_called()
                self.assertFalse(intent_path.exists())
                reconciliation = json.loads(reconciliation_path.read_text())
                self.assertEqual(
                    reconciliation["requested_minimum_build"], "9999"
                )
                self.assertEqual(reconciliation["observed_build"], str(observed))
                self.assertEqual(
                    reconciliation["result"], "minimum-build-satisfied"
                )

    def test_missing_rc_or_disconnect_uses_fresh_reconciliation(self) -> None:
        for outcome in (
            RuntimeError("command did not return an exit status"),
            direct.c.CheckPointError("session closed"),
        ):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan_path = root / "plan.json"
                intent_path = root / "intent.json"
                plan_path.write_text(json.dumps(self.plan()))
                initial = OutcomeSession("Build number: 100\n__RC=0\n")
                argv = self.argv(
                    plan_path, root, "--mutation-intent-file", str(intent_path)
                )
                with (
                    mock.patch.object(sys, "argv", argv),
                    self.environment(),
                    mock.patch.object(direct.c, "SshPty", return_value=initial),
                    mock.patch.object(
                        direct, "run_installer_mutation", side_effect=outcome
                    ) as mutation,
                    mock.patch.object(
                        direct,
                        "verify_deployment_agent_minimum_build",
                        return_value={
                            "host": "192.0.2.20",
                            "requested_minimum_build": "9999",
                            "observed_build": "9999",
                        },
                    ) as reconcile,
                ):
                    self.assertEqual(direct.main(), 0)
                mutation.assert_called_once()
                reconcile.assert_called_once()
                self.assertTrue(intent_path.exists())
                intent = json.loads(intent_path.read_text())
                self.assertEqual(intent["schema"], direct.MUTATION_INTENT_VERSION)
                self.assertEqual(intent["requested_minimum_build"], "9999")
                self.assertEqual(
                    intent["observed_build_before_dispatch"], "100"
                )

    def test_stale_or_malformed_reconciliation_never_redispatches(self) -> None:
        for failure in (
            RuntimeError("installed build is stale"),
            RuntimeError("installed build is malformed"),
        ):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan_path = root / "plan.json"
                intent_path = root / "intent.json"
                plan_path.write_text(json.dumps(self.plan()))
                argv = self.argv(
                    plan_path, root, "--mutation-intent-file", str(intent_path)
                )
                initial = OutcomeSession("Build number: 100\n__RC=0\n")
                with (
                    mock.patch.object(sys, "argv", argv),
                    self.environment(),
                    mock.patch.object(direct.c, "SshPty", return_value=initial),
                    mock.patch.object(
                        direct, "run_installer_mutation", return_value=False
                    ) as mutation,
                    mock.patch.object(
                        direct,
                        "verify_deployment_agent_minimum_build",
                        side_effect=failure,
                    ),
                    self.assertRaisesRegex(RuntimeError, "dispatch state is uncertain"),
                ):
                    direct.main()
                self.assertTrue(intent_path.exists())
                mutation.assert_called_once()

                with (
                    mock.patch.object(sys, "argv", argv),
                    self.environment(),
                    mock.patch.object(
                        direct,
                        "verify_deployment_agent_minimum_build",
                        side_effect=failure,
                    ),
                    mock.patch.object(direct, "run_installer_mutation") as retry,
                    self.assertRaisesRegex(RuntimeError, "dispatch state is uncertain"),
                ):
                    direct.main()
                retry.assert_not_called()

    def test_parallel_partial_failure_and_retry_use_per_host_intents(self) -> None:
        hosts = ["192.0.2.20", "192.0.2.21"]
        operation_id = "run_" + "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            intent_dir = root / "intents"
            intent_dir.mkdir(mode=0o700)
            plan_path.write_text(json.dumps(self.plan(hosts)))
            argv = self.argv(
                plan_path,
                root,
                "--operation-id", operation_id,
                "--mutation-intent-dir", str(intent_dir),
            )

            def session_factory(host, *_args, **_kwargs):
                return OutcomeSession("Build number: 100\n__RC=0\n")

            def first_reconcile(host, *_args, **_kwargs):
                if host == hosts[1]:
                    raise RuntimeError("stale build")
                return {
                    "host": host,
                    "requested_minimum_build": "9999",
                    "observed_build": "9999",
                }

            with (
                mock.patch.object(sys, "argv", argv),
                self.environment(),
                mock.patch.object(direct.c, "SshPty", side_effect=session_factory),
                mock.patch.object(
                    direct, "run_installer_mutation", return_value=False
                ) as mutation,
                mock.patch.object(
                    direct,
                    "verify_deployment_agent_minimum_build",
                    side_effect=first_reconcile,
                ),
                self.assertRaisesRegex(RuntimeError, "192.0.2.21"),
            ):
                direct.main()
            self.assertEqual(mutation.call_count, 2)
            self.assertEqual(len(list(intent_dir.glob("*.json"))), 2)

            with (
                mock.patch.object(sys, "argv", argv),
                self.environment(),
                mock.patch.object(
                    direct,
                    "verify_deployment_agent_minimum_build",
                    side_effect=lambda host, *_a, **_k: {
                        "host": host,
                        "requested_minimum_build": "9999",
                        "observed_build": "9999",
                    },
                ) as reconcile,
                mock.patch.object(direct, "run_installer_mutation") as retry,
            ):
                self.assertEqual(direct.main(), 0)
            retry.assert_not_called()
            self.assertEqual(reconcile.call_count, 2)

    def test_governed_intent_names_are_hash_derived_per_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            operation_id = "run_" + "b" * 64
            first = direct.governed_intent_path(
                root, operation_id, "first-member", "remove Take 91", "192.0.2.20"
            )
            second = direct.governed_intent_path(
                root, operation_id, "first-member", "remove Take 91", "192.0.2.21"
            )
            self.assertNotEqual(first, second)
            self.assertRegex(first.name, r"^[0-9a-f]{64}\.json$")
            self.assertNotIn("192.0.2.20", first.name)
            self.assertEqual(
                first,
                direct.governed_intent_path(
                    root,
                    operation_id,
                    "first-member",
                    "remove Take 91",
                    "192.0.2.20",
                ),
            )

    def test_reconciliation_fd_writes_the_inherited_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reconciliation.json"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                direct.write_reconciliation_fd(fd, {"result": "confirmed"})
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertEqual(
                    json.loads(os.read(fd, 4096)),
                    {"result": "confirmed"},
                )
            finally:
                os.close(fd)


class DirectMemberSelectionTests(unittest.TestCase):
    def plan(self) -> dict:
        return {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "cluster_mode": "cluster",
                "members": [
                    {"hostname": "member-a", "ip": "192.0.2.20"},
                    {"hostname": "member-b", "ip": "192.0.2.21"},
                ],
            },
        }

    def test_cluster_phase_requires_captured_state(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "must identify original active and standby"):
                direct.member_ips_for_phase(self.plan(), "first-member", Path(tmp))

    def test_captured_state_must_match_plan_members(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                json.dumps({"original_active_host": "192.0.2.99", "original_standby_host": "192.0.2.21"})
            )
            with self.assertRaisesRegex(SystemExit, "does not match"):
                direct.member_ips_for_phase(self.plan(), "second-member", reports)

    def test_phase_targets_follow_captured_state(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                json.dumps({"original_active_host": "192.0.2.20", "original_standby_host": "192.0.2.21"})
            )
            self.assertEqual(direct.member_ips_for_phase(self.plan(), "first-member", reports), ["192.0.2.21"])
            self.assertEqual(direct.member_ips_for_phase(self.plan(), "second-member", reports), ["192.0.2.20"])


    def test_semantically_duplicate_ipv6_members_fail_and_dispatch_is_preserved(
        self,
    ) -> None:
        duplicate = self.plan()
        duplicate["checkpoint"]["members"] = [
            {"hostname": "member-a", "ip": "2001:db8::1"},
            {"hostname": "member-b", "ip": "2001:0db8:0:0:0:0:0:1"},
        ]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            SystemExit, "must be distinct"
        ):
            direct.member_ips_for_phase(
                duplicate, "install-deployment-agent", Path(tmp)
            )

        plan = self.plan()
        plan["checkpoint"]["members"] = [
            {"hostname": "member-a", "ip": "2001:db8::1"},
            {"hostname": "member-b", "ip": "2001:db8::2"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            (reports / "cluster_initial_state_CHG_TEST.json").write_text(
                json.dumps(
                    {
                        "original_active_host": "2001:0db8:0:0:0:0:0:1",
                        "original_standby_host": "2001:0db8:0:0:0:0:0:2",
                    }
                )
            )
            self.assertEqual(
                direct.member_ips_for_phase(plan, "first-member", reports),
                ["2001:db8::2"],
            )
            self.assertEqual(
                direct.member_ips_for_phase(plan, "second-member", reports),
                ["2001:db8::1"],
            )


if __name__ == "__main__":
    unittest.main()
