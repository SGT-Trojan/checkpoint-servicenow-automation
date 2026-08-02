from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "major_mvc_from_activity.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "major_mvc"
SPEC = importlib.util.spec_from_file_location("major_mvc_from_activity", SCRIPT)
assert SPEC and SPEC.loader
mvc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mvc)


class Result:
    def __init__(self, output: str):
        self.output = output


class Session:
    def __init__(self, outputs: list[str]):
        self.outputs = iter(outputs)
        self.commands: list[str] = []
        self.closed = False

    def connect(self) -> None:
        pass

    def enter_expert(self, _password: str) -> None:
        pass

    def run(self, command: str, timeout: int = 0) -> Result:
        self.commands.append(command)
        return Result(next(self.outputs))

    def close(self) -> None:
        self.closed = True


class MajorMvcTests(unittest.TestCase):
    def fixture(self, name: str) -> str:
        return (FIXTURES / name).read_text()

    def run_host(self, outputs: list[str], expected_state: str = "enabled") -> Session:
        session = Session(outputs)
        command = "cphaconf mvc on" if expected_state == "enabled" else "cphaconf mvc off"
        with mock.patch.object(mvc.c, "SshPty", return_value=session):
            mvc.run_host(
                "192.0.2.21",
                "admin",
                "password",
                "expert",
                command,
                expected_state,
            )
        return session

    def test_command_failure_rc_fails_before_readback(self) -> None:
        session = Session([self.fixture("command_failure.txt")])
        with (
            mock.patch.object(mvc.c, "SshPty", return_value=session),
            self.assertRaisesRegex(RuntimeError, "exit status 1"),
        ):
            mvc.run_host(
                "192.0.2.21",
                "admin",
                "password",
                "expert",
                "cphaconf mvc on",
                "enabled",
            )
        self.assertEqual(len(session.commands), 1)
        self.assertTrue(session.closed)

    def test_missing_command_rc_fails_before_readback(self) -> None:
        session = Session([self.fixture("rc_missing.txt")])
        with (
            mock.patch.object(mvc.c, "SshPty", return_value=session),
            self.assertRaisesRegex(RuntimeError, "did not return an exit status"),
        ):
            mvc.run_host(
                "192.0.2.21",
                "admin",
                "password",
                "expert",
                "cphaconf mvc on",
                "enabled",
            )
        self.assertEqual(len(session.commands), 1)

    def test_wrong_mvc_state_fails_even_if_cluster_health_text_is_present(self) -> None:
        wrong = self.fixture("mvc_disabled.txt") + "ACTIVE\nActive PNOTEs: None\n"
        with self.assertRaisesRegex(RuntimeError, "reported disabled; expected enabled"):
            self.run_host([self.fixture("command_success.txt"), wrong])

    def test_success_requires_explicit_readback_for_requested_state(self) -> None:
        for expected, fixture in (
            ("enabled", "mvc_enabled.txt"),
            ("disabled", "mvc_disabled.txt"),
        ):
            with self.subTest(expected=expected):
                session = self.run_host(
                    [self.fixture("command_success.txt"), self.fixture(fixture)],
                    expected,
                )
                self.assertIn("rc=$?", session.commands[0])
                self.assertIn("__RC=%s", session.commands[0])
                self.assertEqual(session.commands[1], "cphaprob mvc")
                self.assertNotIn("cphaprob state", session.commands)

    def test_generic_cluster_health_is_not_mvc_state_proof(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unambiguous MVC state"):
            mvc.parse_mvc_state("ACTIVE\nActive PNOTEs: None\n")

    def test_conflicting_mvc_readback_is_ambiguous(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unambiguous MVC state"):
            mvc.parse_mvc_state("cphaprob mvc\nON\nOFF\n[Expert@gateway-a:0]#\n")

    def test_explicit_state_file_controls_target(self) -> None:
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
                '{"original_standby_host":"192.0.2.20"}'
            )
            inherited = root / "inherited-state.json"
            inherited.write_text('{"original_standby_host":"192.0.2.21"}')
            self.assertEqual(
                mvc.members_for_phase(plan, "mvc-on", reports, inherited),
                ["192.0.2.21"],
            )

    def test_mvc_on_target_must_be_captured_plan_member(self) -> None:
        plan = {
            "change": {"number": "CHG_TEST"},
            "checkpoint": {
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            reports = Path(temporary)
            state = reports / "cluster_initial_state_CHG_TEST.json"
            state.write_text('{"original_standby_host":"192.0.2.99"}')
            with self.assertRaisesRegex(SystemExit, "does not match"):
                mvc.members_for_phase(plan, "mvc-on", reports)
            state.write_text('{"original_standby_host":"192.0.2.21"}')
            self.assertEqual(
                mvc.members_for_phase(plan, "mvc-on", reports), ["192.0.2.21"]
            )
            self.assertEqual(
                mvc.members_for_phase(plan, "mvc-off", reports),
                ["192.0.2.20", "192.0.2.21"],
            )


if __name__ == "__main__":
    unittest.main()
