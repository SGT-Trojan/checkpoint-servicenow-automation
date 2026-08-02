from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import checkpoint_cluster_upgrade as upgrade


FIXTURES = Path(__file__).parent / "fixtures" / "cluster_upgrade"
PACKAGE = "Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tgz"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class ParserFixtureTests(unittest.TestCase):
    def test_cluster_state_pass_failure_and_malformed(self) -> None:
        local, peers, pnotes = upgrade.parse_cluster_state(
            fixture("cluster_state_healthy.txt")
        )
        self.assertEqual(local, "ACTIVE")
        self.assertEqual(peers["EXAMPLE-GW-B"], "STANDBY")
        self.assertTrue(pnotes)

        local, _peers, pnotes = upgrade.parse_cluster_state(
            fixture("cluster_state_degraded.txt")
        )
        self.assertEqual(local, "DOWN")
        self.assertFalse(pnotes)
        self.assertEqual(
            upgrade.parse_cluster_state(fixture("cluster_state_malformed.txt")),
            ("UNKNOWN", {}, False),
        )

    def test_cluster_interfaces_pass_failure_and_malformed(self) -> None:
        healthy = upgrade.parse_cluster_interfaces(fixture("interfaces_healthy.txt"))
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["required_interfaces"], 2)
        self.assertEqual(len(healthy["interfaces"]), 2)
        self.assertEqual(healthy["virtual_interfaces"][0]["ip"], "192.0.2.100")

        degraded = upgrade.parse_cluster_interfaces(
            fixture("interfaces_degraded.txt")
        )
        self.assertFalse(degraded["ok"])
        self.assertFalse(
            upgrade.parse_cluster_interfaces(fixture("interfaces_malformed.txt"))["ok"]
        )

    def test_package_table_requires_affirmative_status(self) -> None:
        self.assertTrue(
            upgrade.package_table_has_ready_package(
                fixture("packages_ready.txt"), PACKAGE
            )
        )
        self.assertTrue(
            upgrade.package_table_has_ready_package(
                fixture("packages_installed.txt"), PACKAGE
            )
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                fixture("packages_negative.txt"), PACKAGE
            )
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                fixture("packages_malformed.txt"), PACKAGE
            )
        )
        wrapper = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar"
        imported = (
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
            " | Status: Imported"
        )
        self.assertTrue(
            upgrade.package_table_has_ready_package(imported, wrapper)
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                imported.replace(".tgz", ".tgz.bak"), wrapper
            )
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                imported.replace(".tgz", ".tgz.bak"),
                imported.split(" |", 1)[0],
            )
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                imported.replace(".tgz", ".tgz.bak"),
                "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL",
            )
        )
        self.assertTrue(
            upgrade.package_table_has_ready_package(
                "Take 76 | Status: Imported", wrapper
            )
        )
        self.assertFalse(
            upgrade.package_table_has_ready_package(
                imported.replace("Imported", "Not Imported"), wrapper
            )
        )

    def test_installed_target_requires_exact_package_take_and_positive_state(self) -> None:
        installed = fixture("packages_installed.txt")
        self.assertTrue(
            upgrade.package_table_has_installed_target(installed, PACKAGE, "91")
        )
        self.assertFalse(
            upgrade.package_table_has_installed_target(installed, PACKAGE, "92")
        )
        self.assertFalse(
            upgrade.package_table_has_installed_target(
                fixture("packages_negative.txt"), PACKAGE, "91"
            )
        )

    def test_icap_pass_failure_and_malformed(self) -> None:
        rows = [
            ("icap_healthy.json", True),
            ("icap_listener_missing.json", False),
            ("icap_malformed.json", False),
        ]
        for name, expected in rows:
            with self.subTest(name=name):
                data = json.loads(fixture(name))
                self.assertEqual(
                    upgrade.parse_icap_status(
                        data["cpwd"], data["listener"], data["process"]
                    ),
                    expected,
                )


class FakeSession:
    def __init__(self, outputs: list[str] | None = None, error: Exception | None = None):
        self.outputs = list(outputs or [])
        self.error = error
        self.commands: list[str] = []
        self.expert_entered = False
        self.closed = False

    def enter_expert(self, _password: str) -> None:
        self.expert_entered = True

    def run(self, command: str, timeout: int = 0) -> upgrade.CommandResult:
        self.commands.append(command)
        if self.error:
            raise self.error
        return upgrade.CommandResult(command, self.outputs.pop(0))

    def close(self) -> None:
        self.closed = True


class RollingOutcomeTests(unittest.TestCase):
    @staticmethod
    def args() -> SimpleNamespace:
        return SimpleNamespace(
            execute=True,
            expert_password="expert-secret",
            package=PACKAGE,
            install_timeout=60,
            create_backup=False,
            target_version="R82",
            target_take="91",
        )

    def test_installer_return_code_is_required_and_checked(self) -> None:
        success = FakeSession(["installation started\n__RC=0\n"])
        with mock.patch.object(upgrade, "connect", return_value=success):
            self.assertIsNone(upgrade.install_package(self.args(), "192.0.2.20"))
        self.assertTrue(success.expert_entered)
        self.assertIn("clish -c", success.commands[0])

        for output, message in (("__RC=7", "exit status 7"), ("done", "did not return")):
            with self.subTest(output=output):
                session = FakeSession([output])
                with mock.patch.object(upgrade, "connect", return_value=session):
                    with self.assertRaisesRegex(upgrade.CheckPointError, message):
                        upgrade.install_package(self.args(), "192.0.2.20")

    def test_install_disconnect_requires_later_reconciliation(self) -> None:
        session = FakeSession(error=upgrade.CheckPointError("connection closed"))
        with mock.patch.object(upgrade, "connect", return_value=session):
            self.assertIsNone(upgrade.install_package(self.args(), "192.0.2.20"))

    def test_post_install_reconciliation_checks_version_and_take(self) -> None:
        good = FakeSession(
            ["Product version Check Point Gaia R82", fixture("packages_installed.txt")]
        )
        with mock.patch.object(upgrade, "connect", return_value=good):
            upgrade.verify_rolling_target(self.args(), "192.0.2.20")

        wrong_version = FakeSession(
            ["Product version Check Point Gaia R81.20", fixture("packages_installed.txt")]
        )
        with mock.patch.object(upgrade, "connect", return_value=wrong_version):
            with self.assertRaisesRegex(upgrade.CheckPointError, "target version"):
                upgrade.verify_rolling_target(self.args(), "192.0.2.20")

        wrong_take = FakeSession(
            ["Product version Check Point Gaia R82", fixture("packages_negative.txt")]
        )
        with mock.patch.object(upgrade, "connect", return_value=wrong_take):
            with self.assertRaisesRegex(upgrade.CheckPointError, "target Take"):
                upgrade.verify_rolling_target(self.args(), "192.0.2.20")

    def test_rolling_reconciles_first_member_before_failover(self) -> None:
        standby = upgrade.Gateway(host="192.0.2.20", local_state="STANDBY")
        active = upgrade.Gateway(host="192.0.2.21", local_state="ACTIVE")
        after_failover = [
            upgrade.Gateway(host=standby.host, local_state="ACTIVE", icap_ok=True),
            upgrade.Gateway(host=active.host, local_state="DOWN", icap_ok=True),
        ]
        events: list[str] = []

        with (
            mock.patch.object(upgrade, "run_precheck", side_effect=[[standby, active], after_failover]),
            mock.patch.object(upgrade, "download_and_verify"),
            mock.patch.object(upgrade, "install_package"),
            mock.patch.object(upgrade, "wait_for_reconnect"),
            mock.patch.object(
                upgrade,
                "verify_rolling_target",
                side_effect=lambda _args, host: events.append(f"verify:{host}"),
            ),
            mock.patch.object(
                upgrade,
                "clusterxl_admin",
                side_effect=lambda _args, host, _action: events.append(f"failover:{host}"),
            ),
            mock.patch.object(upgrade, "wait_for_cluster_condition", return_value=after_failover),
        ):
            upgrade.run_rolling(self.args())

        self.assertLess(
            events.index(f"verify:{standby.host}"),
            events.index(f"failover:{active.host}"),
        )
        self.assertEqual(events[-1], f"verify:{active.host}")

    def test_version_match_does_not_accept_longer_release(self) -> None:
        self.assertTrue(upgrade.version_output_matches_target("Gaia R82", "R82"))
        self.assertFalse(upgrade.version_output_matches_target("Gaia R82.10", "R82"))

    def test_rolling_cli_requires_explicit_targets(self) -> None:
        with self.assertRaises(SystemExit):
            upgrade.parse_args(["--phase", "rolling"])

        env = {
            "CP_PASSWORD": "synthetic-password",
            "CP_EXPERT_PASSWORD": "synthetic-expert-password",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            args = upgrade.parse_args(
                [
                    "--phase",
                    "rolling",
                    "--target-version",
                    "R82",
                    "--target-take",
                    "91",
                ]
            )
        self.assertEqual(args.target_version, "R82")
        self.assertEqual(args.target_take, "91")


if __name__ == "__main__":
    unittest.main()
