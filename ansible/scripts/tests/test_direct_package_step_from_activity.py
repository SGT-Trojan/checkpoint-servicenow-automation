from __future__ import annotations

import importlib.util
import sys
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
    def __init__(self, output: str):
        self.output = output

    def run(self, command: str, timeout: int = 0) -> Result:
        return Result(self.output)


class DirectRemoveIdentityTests(unittest.TestCase):
    def test_take_alias_resolves_from_local_cpinstlog(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        history = f"Install,CPUpdates,{package},BUNDLE_R81_20_JUMBO_HF_MAIN#76"
        self.assertEqual(
            direct.resolve_remove_package_name(
                Session(history),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            ),
            package,
        )

    def test_ambiguous_local_history_fails_closed(self) -> None:
        history = "\n".join([
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz",
        ])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            direct.resolve_remove_package_name(
                Session(history),
                {"name": "remove_Take_76", "action": "remove", "package_name": "Take 76", "package_type": "jhf"},
            )


class OutcomeSession:
    def __init__(self, output: str):
        self.output = output
        self.commands: list[str] = []
        self.connected = False
        self.expert_entered = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True

    def enter_expert(self, _password: str) -> None:
        self.expert_entered = True

    def run(self, command: str, timeout: int = 0) -> Result:
        self.commands.append(command)
        return Result(self.output)

    def close(self) -> None:
        self.closed = True


class DirectOutcomeTests(unittest.TestCase):
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
        self.assertIn("clish -c", session.commands[0])
        self.assertIn("__RC=%s", session.commands[0])

    def test_exact_package_presence_rejects_negative_and_lookalike_rows(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        present = self.fixture("package_still_installed.txt")
        absent = self.fixture("package_absent.txt")
        self.assertTrue(direct.package_identity_is_installed(present, package))
        tar_package = package.removesuffix(".tgz") + ".tar"
        self.assertTrue(direct.package_identity_is_installed(present, tar_package))
        self.assertFalse(
            direct.package_identity_is_installed(
                present.replace(".tgz", ".tgz.bak"), tar_package
            )
        )
        self.assertFalse(direct.package_identity_is_installed(absent, package))
        self.assertFalse(
            direct.package_identity_is_installed(
                f"{package} | Status: Not Installed", package
            )
        )
        self.assertFalse(
            direct.package_identity_is_installed(
                package.replace("_FULL.tgz", "_SPECIAL_FULL.tgz"), package
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


if __name__ == "__main__":
    unittest.main()
