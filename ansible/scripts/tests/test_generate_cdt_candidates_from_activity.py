from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "generate_cdt_candidates_from_activity.py"
SPEC = importlib.util.spec_from_file_location("generate_cdt_candidates_from_activity", SCRIPT)
assert SPEC and SPEC.loader
candidate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = candidate
SPEC.loader.exec_module(candidate)


class MandatoryRemovalIdentityTests(unittest.TestCase):
    checkpoint = {
        "members": [
            {"management_ip": "192.0.2.20"},
            {"management_ip": "192.0.2.21"},
        ]
    }
    package = {
        "action": "remove",
        "package_name": "Check_Point_R82_T91_FULL.tar",
        "source_path": "/var/log/tmp/Check_Point_R82_T91_FULL.tar",
        "package_type": "jhf",
    }

    @staticmethod
    def fake_run(_command: str, timeout: int = 300) -> str:
        del timeout
        return ""

    def test_explicit_tar_never_becomes_a_fallback_execution_identity(self) -> None:
        def run(command: str, timeout: int = 300) -> str:
            del timeout
            if "snowlite_installed" in command and "python3 -c" in command:
                return "Check_Point_R82_T91_FULL.tgz | Status: Installed\n"
            return ""

        with mock.patch.object(candidate, "package_candidates_from_history", return_value=[]):
            with self.assertRaisesRegex(SystemExit, "uniquely alias-resolved"):
                candidate.resolve_remove_package_ref(
                    run,
                    self.checkpoint["members"][0],
                    self.package,
                    "remove_take_91",
                    self.package["source_path"],
                )

    def test_cpinstlog_tar_identity_is_not_rewritten_to_tgz(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tar"
        self.assertEqual(
            candidate.package_candidates_from_history(
                f"Installed package {identity}\n", [identity], "jhf"
            ),
            [identity],
        )

    def test_multiple_current_history_intersections_fail_closed(self) -> None:
        history = "\n".join([
            "Install Take91_A.tgz",
            "Install Take91_B.tgz",
        ])
        table = "\n".join([
            "Take91_A.tgz | Status: Installed",
            "Take91_B.tgz | Status: Installed",
        ])
        with self.assertRaisesRegex(RuntimeError, "matches=.*Take91_A"):
            candidate.resolve_current_remove_identity(
                history, table, ["Take91"], "jhf"
            )

    def test_missing_members_cannot_bypass_cpinstlog(self) -> None:
        with self.assertRaisesRegex(SystemExit, "management address"):
            candidate.resolve_remove_package_ref(
                self.fake_run,
                {},
                self.package,
                "remove_take_91",
                self.package["source_path"],
            )

    def test_timestamp_inversion_never_determines_current_state(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        histories = (
            (
                f"2026-01-02T00:00:00Z Uninstalled {identity}\n"
                f"2026-01-01T00:00:00Z Installed {identity}"
            ),
            (
                f"2026-01-01T00:00:00Z Installed {identity}\n"
                f"2026-01-02T00:00:00Z Uninstalled {identity}"
            ),
        )
        for history in histories:
            with self.subTest(history=history), self.assertRaisesRegex(
                RuntimeError, r"matches=\[\]"
            ):
                candidate.resolve_current_remove_identity(
                    history,
                    f"{identity} | Status: Not Installed",
                    ["Take 91"],
                    "jhf",
                )

        inverse_histories = (
            (
                f"2026-01-02T00:00:00Z Installed {identity}\n"
                f"2026-01-01T00:00:00Z Uninstalled {identity}"
            ),
            (
                f"2026-01-01T00:00:00Z Uninstalled {identity}\n"
                f"2026-01-02T00:00:00Z Installed {identity}"
            ),
        )
        for history in inverse_histories:
            with self.subTest(history=history):
                self.assertEqual(
                    candidate.resolve_current_remove_identity(
                        history,
                        f"{identity} | Status: Installed",
                        ["Take 91"],
                        "jhf",
                    ),
                    identity,
                )

    def test_equal_malformed_or_missing_history_timestamps_are_not_state(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        histories = (
            f"same-time Installed {identity}\nsame-time Uninstalled {identity}",
            f"not-a-time Installed {identity}",
            f"Installed {identity}",
        )
        for history in histories:
            with self.subTest(history=history), self.assertRaisesRegex(
                RuntimeError, r"matches=\[\]"
            ):
                candidate.resolve_current_remove_identity(
                    history,
                    f"{identity} | Status: Removed",
                    ["Take 91"],
                    "jhf",
                )

    def test_fixed_pathname_order_inversion_cannot_change_verdict(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        path_order = (
            f"/opt/CPInstLog/z-new.log: Uninstalled {identity}\n"
            f"/opt/CPInstLog/a-old.log: Installed {identity}"
        )
        inverse_path_order = "\n".join(reversed(path_order.splitlines()))
        for history in (path_order, inverse_path_order):
            with self.subTest(history=history):
                self.assertEqual(
                    candidate.resolve_current_remove_identity(
                        history,
                        f"Name: {identity}\nStatus: Installed",
                        ["Take 91"],
                        "jhf",
                    ),
                    identity,
                )

    def test_current_table_disagreement_and_malformed_status_fail_closed(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        other = "Check_Point_R82_T92_FULL.tgz"
        with self.assertRaisesRegex(RuntimeError, r"matches=\[\]"):
            candidate.resolve_current_remove_identity(
                f"Installed {identity}",
                f"{other} | Status: Installed",
                ["Take 91"],
                "jhf",
            )
        with self.assertRaisesRegex(RuntimeError, "unknown or malformed line"):
            candidate.installed_package_identities(f"{identity} | 2026-01-01")
        with self.assertRaisesRegex(RuntimeError, "duplicate normalized identity"):
            candidate.installed_package_identities(
                f"{identity} | Status: Installed\n"
                f"{identity} | Status: Removed"
            )

    def test_negative_status_and_take_lookalike_never_qualify(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        lookalike = "Check_Point_R82_T910_FULL.tgz"
        lines = [f"Installed {identity}", f"Installed {lookalike}"]
        for history in ("\n".join(lines), "\n".join(reversed(lines))):
            with self.subTest(history=history), self.assertRaisesRegex(
                RuntimeError, r"matches=\[\]"
            ):
                candidate.resolve_current_remove_identity(
                    history,
                    f"{identity} | Status: Uninstalled\n"
                    f"{lookalike} | Status: Installed",
                    ["Take 91"],
                    "jhf",
                )

    def test_extension_carrying_lookalikes_are_never_package_tokens(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        for suffix in (".backup", ".old"):
            with self.subTest(source="history", suffix=suffix), self.assertRaisesRegex(
                RuntimeError, r"history_candidates=\[\]"
            ):
                candidate.resolve_current_remove_identity(
                    f"Installed {identity}{suffix}",
                    f"{identity} | Status: Installed",
                    ["Take 91"],
                    "jhf",
                )
            with self.subTest(source="table", suffix=suffix), self.assertRaisesRegex(
                RuntimeError, "extension lookalike"
            ):
                candidate.resolve_current_remove_identity(
                    f"Installed {identity}",
                    f"{identity}{suffix} | Status: Installed",
                    ["Take 91"],
                    "jhf",
                )

    def test_authoritative_table_requires_rows_or_exact_empty_marker(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        installed = f"{identity} | Status: Installed"
        malformed = (
            "",
            "   \n",
            "Error: database unavailable",
            "arbitrary output",
            f"{identity} | State unknown",
            f"{installed}\nmalformed output",
            "No installed packages match\nmalformed output",
            "Installed Packages\nmalformed output",
            f"No installed packages match\n{installed}",
            "No installed packages match\nNo installed packages match",
        )
        for table in malformed:
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                candidate.installed_package_identities(table)
        self.assertEqual(
            candidate.installed_package_identities("No installed packages match"),
            [],
        )

    def test_authoritative_table_accepts_crlf_but_rejects_terminal_controls(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        table = (
            "Installed Packages\r\n"
            "------------------\r\n"
            f"{identity} | Status: Installed\r\n"
            "__RC=0\r\n"
        )
        self.assertEqual(candidate.installed_package_identities(table), [identity])
        hostile = (
            f"{identity} | Sta\x1b[31mtus: Installed",
            "Check_Point_R82_T91_FU\x1b[31mLL.tgz | Status: Installed",
            "Installed\x1b[31m Packages\nNo installed packages match",
            f"{identity} | Status: Installed\x1b]0;unsafe\x07",
            f"{identity} | Status: Installed\rmalformed",
        )
        for value in hostile:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                candidate.installed_package_identities(value)

    def test_duplicate_normalized_identities_always_fail_closed(self) -> None:
        identity = "Check_Point_R82_T91_FULL.tgz"
        variants = (
            f"{identity} | Status: Installed\n{identity} | Status: Installed",
            f"{identity} | Status: Removed\n{identity.lower()} | Status: Removed",
            f"{identity} | Status: Installed\n"
            f"Name: {identity.lower()}\nStatus: Installed",
        )
        for table in variants:
            with self.subTest(table=table), self.assertRaisesRegex(
                RuntimeError, "duplicate normalized identity"
            ):
                candidate.installed_package_identities(table)

    def test_selected_member_management_ip_is_the_only_cprid_target(self) -> None:
        commands: list[str] = []

        def run(command: str, timeout: int = 300) -> str:
            del timeout
            commands.append(command)
            if "python3 -c" in command:
                if "snowlite_cpinstlog" in command:
                    return "Installed Check_Point_R82_T91_FULL.tgz\n"
                if "snowlite_installed" in command:
                    return (
                        "Check_Point_R82_T91_FULL.tgz | Status: Installed\n"
                    )
            return ""

        selected = {
            "management_ip": "192.0.2.20",
            "access_ip": "198.51.100.20",
        }
        self.assertEqual(
            candidate.resolve_remove_package_ref(
                run,
                selected,
                self.package,
                "remove_take_91",
                self.package["source_path"],
            ),
            "Check_Point_R82_T91_FULL.tgz",
        )
        cprid_commands = [command for command in commands if "cprid_util" in command]
        self.assertTrue(cprid_commands)
        self.assertTrue(all("192.0.2.20" in command for command in cprid_commands))
        self.assertTrue(all("198.51.100.20" not in command for command in cprid_commands))
        self.assertTrue(all("192.0.2.21" not in command for command in cprid_commands))

    def test_peer_only_identity_cannot_authorize_selected_member(self) -> None:
        commands: list[str] = []

        def run(command: str, timeout: int = 300) -> str:
            del timeout
            commands.append(command)
            if "snowlite_installed" in command and "python3 -c" in command:
                return "No installed packages match\n"
            return ""

        with self.assertRaisesRegex(SystemExit, "uniquely alias-resolved"):
            candidate.resolve_remove_package_ref(
                run,
                self.checkpoint["members"][0],
                self.package,
                "remove_take_91",
                self.package["source_path"],
            )
        self.assertFalse(any("192.0.2.21" in command for command in commands))

    def test_policy_selection_preserves_management_and_access_addresses(self) -> None:
        members = [
            {
                "management_ip": "192.0.2.20",
                "access_ip": "198.51.100.20",
            },
            {
                "management_ip": "192.0.2.21",
                "access_ip": "198.51.100.21",
            },
        ]

        def run(command: str, timeout: int = 300) -> str:
            del timeout
            if "python3 -c" not in command:
                return ""
            state = "ACTIVE" if "_1.out" in command else "STANDBY"
            return (
                "Cluster Mode: High Availability\n"
                f"1 (local)  192.0.2.99  100%  {state}  EXAMPLE\n"
                "Active PNOTEs: 0\n"
            )

        selected = candidate.select_member_for_removal(
            members, "standby", None, run, "remove_take_91"
        )
        self.assertEqual(selected["management_ip"], "192.0.2.21")
        self.assertEqual(selected["access_ip"], "198.51.100.21")


if __name__ == "__main__":
    unittest.main()
