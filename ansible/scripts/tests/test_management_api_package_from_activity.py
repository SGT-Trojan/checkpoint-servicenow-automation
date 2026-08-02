from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "management_api_package_from_activity.py"
SPEC = importlib.util.spec_from_file_location("management_api_package_from_activity", SCRIPT)
assert SPEC and SPEC.loader
api_backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api_backend
SPEC.loader.exec_module(api_backend)


def inventory(*member_packages: list[str]) -> dict:
    return {
        "targets": [{
            "cluster-members": [
                {"name": f"member-{index}", "packages": {"installed": [{"package-id": name} for name in packages]}}
                for index, packages in enumerate(member_packages, 1)
            ]
        }]
    }


class PackageIdentityTests(unittest.TestCase):
    def test_tar_source_maps_to_api_tgz_identity(self) -> None:
        step = {"package_name": "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tar"}
        self.assertEqual(
            api_backend.api_package_name(step),
            "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz",
        )

    def test_repository_parser_is_scoped_to_package_rows(self) -> None:
        response = {
            "name": "unrelated-root-name",
            "packages": [
                {"name": "one.tgz"},
                {"package-id": "two.tgz"},
                {"package-name": "three.tgz"},
            ],
        }
        self.assertEqual(api_backend.repository_package_ids(response), ["one.tgz", "two.tgz", "three.tgz"])
        task_response = {
            "tasks": [{
                "status": "succeeded",
                "task-details": [{"packages": [{"name": "nested.tgz"}]}],
            }],
        }
        self.assertEqual(api_backend.repository_package_ids(task_response), ["nested.tgz"])
        self.assertEqual(
            api_backend.repository_package_ids({"objects": [{"name": "object-form.tgz"}]}),
            ["object-form.tgz"],
        )

    def test_take_alias_resolves_only_current_release_on_every_member(self) -> None:
        r8120 = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        r82 = "Check_Point_R82_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        data = inventory([r8120, r82], [r8120, r82])
        for alias in ("Take 76", "T76"):
            with self.subTest(alias=alias):
                step = {"name": "remove_take", "action": "remove", "package_name": alias}
                self.assertEqual(
                    api_backend.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
                    r8120,
                )

    def test_explicit_identity_may_remain_on_only_one_member(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        data = inventory([package], [])
        step = {"action": "remove", "package_name": package}
        self.assertEqual(
            api_backend.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
            package,
        )


    def test_empty_api_inventory_uses_cprid_and_repository_cross_check(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"

        class Api:
            def call(self, domain: str, command: str) -> dict:
                return {"packages": [{"name": package}], "total": 1}

        with mock.patch.object(
            api_backend.cdt_candidates,
            "resolve_remove_package_ref",
            return_value=package,
        ) as resolver:
            selected = api_backend.resolve_remove_identity_via_cprid(
                Api(),
                mock.Mock(),
                "Global",
                {
                    "current_version": "R81.20",
                    "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
                },
                {"action": "remove", "package_name": "Take 76", "package_type": "jhf"},
                "remove_Take_76",
            )

        self.assertEqual(selected, package)
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(
            [call.args[1]["ip"] for call in resolver.call_args_list],
            ["192.0.2.20", "192.0.2.21"],
        )

    def test_cprid_and_repository_disagreement_fails_closed(self) -> None:
        repository_package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        history_package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz"

        class Api:
            def call(self, domain: str, command: str) -> dict:
                return {"packages": [{"name": repository_package}], "total": 1}

        with mock.patch.object(
            api_backend.cdt_candidates,
            "resolve_remove_package_ref",
            return_value=history_package,
        ):
            with self.assertRaisesRegex(RuntimeError, "identities disagree"):
                api_backend.resolve_remove_identity_via_cprid(
                    Api(),
                    mock.Mock(),
                    "Global",
                    {"current_version": "R81.20", "members": [{"ip": "192.0.2.20"}]},
                    {"action": "remove", "package_name": "Take 76", "package_type": "jhf"},
                    "remove_Take_76",
                )


    def test_repository_inventory_paginates_and_checks_total(self) -> None:
        class Api:
            def __init__(self) -> None:
                self.calls = []

            def call(self, domain: str, command: str) -> dict:
                self.calls.append(command)
                if "offset 0" in command:
                    return {"packages": [{"name": "one.tgz"}], "total": 2}
                return {"packages": [{"name": "two.tgz"}], "total": 2}

        api = Api()
        self.assertEqual(api_backend.repository_inventory(api, "Global"), ["one.tgz", "two.tgz"])
        self.assertEqual(len(api.calls), 2)


class WorkspaceAndReconciliationTests(unittest.TestCase):
    def test_repository_import_fails_when_var_log_cannot_hold_package_and_reserve(self) -> None:
        session = Session([
            str(10 * api_backend.GIB),
            str(8 * 1024 * 1024),  # 8 GiB free in KiB
        ])
        with self.assertRaisesRegex(RuntimeError, "insufficient /var/log space"):
            api_backend.validate_api_workspace(
                session,
                {"source_path": "/var/log/tmp/R82.tgz"},
                operation="repository",
                is_major=True,
            )

    def test_execute_requires_root_reserve_even_when_cache_is_populated(self) -> None:
        session = Session([
            str(1 * 1024 * 1024),  # 1 GiB free in KiB
            str(20 * 1024 * 1024),  # 20 GiB cache in KiB
        ])
        with self.assertRaisesRegex(RuntimeError, "root filesystem reserve"):
            api_backend.validate_api_workspace(
                session, {}, operation="execute", is_major=True
            )

    def test_execute_counts_existing_cache_as_effective_capacity(self) -> None:
        session = Session([
            str(3 * 1024 * 1024),  # 3 GiB free in KiB
            str(10 * 1024 * 1024),  # 10 GiB cache in KiB
        ])
        api_backend.validate_api_workspace(
            session, {}, operation="execute", is_major=True
        )

    def test_major_failure_reconciliation_requires_exact_phase_count_and_package(self) -> None:
        package = "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz"
        checkpoint = {
            "target_version": "R82",
            "members": [{"management_ip": "192.0.2.20"}, {"management_ip": "192.0.2.21"}],
        }
        step = {"package_name": package}
        old_version = "Product version Check Point Gaia R81.20"
        new_version = "Product version Check Point Gaia R82"
        installed = f"Blink Images\n{package} | Status: Installed"
        empty = "No installed packages match"

        with mock.patch.object(
            api_backend,
            "cprid_member_output",
            side_effect=[new_version, installed, old_version, empty],
        ):
            self.assertTrue(
                api_backend.major_upgrade_completed_despite_api_failure(
                    mock.Mock(), checkpoint, step, "first-member", 0
                )
            )

        with mock.patch.object(
            api_backend,
            "cprid_member_output",
            side_effect=[new_version, installed, new_version, installed],
        ):
            self.assertTrue(
                api_backend.major_upgrade_completed_despite_api_failure(
                    mock.Mock(), checkpoint, step, "second-member", 1
                )
            )

        with mock.patch.object(
            api_backend,
            "cprid_member_output",
            side_effect=[new_version, installed, new_version, installed],
        ):
            self.assertFalse(
                api_backend.major_upgrade_completed_despite_api_failure(
                    mock.Mock(), checkpoint, step, "second-member", 2
                )
            )

        with mock.patch.object(
            api_backend,
            "cprid_member_output",
            side_effect=[
                new_version,
                installed,
                new_version,
                "different-image.tgz | Status: Installed",
            ],
        ):
            self.assertFalse(
                api_backend.major_upgrade_completed_despite_api_failure(
                    mock.Mock(), checkpoint, step, "second-member", 1
                )
            )

    def test_major_reconciliation_rejects_untrusted_package_tables(self) -> None:
        package = "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz"
        checkpoint = {
            "target_version": "R82",
            "members": [{"management_ip": "192.0.2.20"}],
        }
        step = {"package_name": package}
        version = "Product version Check Point Gaia R82"
        installed = f"{package} | Status: Installed"
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
            f"{package} | Sta\x1b[31mtus: Installed",
            f"{installed}\n{installed}",
            f"{installed}\n{package.lower()} | Status: Installed",
        ):
            with (
                self.subTest(output=output),
                mock.patch.object(
                    api_backend,
                    "cprid_member_output",
                    side_effect=[version, output],
                ),
                self.assertRaises(RuntimeError),
            ):
                api_backend.major_upgrade_completed_count(
                    mock.Mock(), checkpoint, step
                )
        with mock.patch.object(
            api_backend,
            "cprid_member_output",
            side_effect=[version, "No installed packages match"],
        ):
            self.assertEqual(
                api_backend.major_upgrade_completed_count(
                    mock.Mock(), checkpoint, step
                )[0],
                0,
            )


class Result:
    def __init__(self, output: str):
        self.output = output


class Session:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.commands: list[str] = []
        self.closed = False

    def enter_expert(self, password: str) -> None:
        self.expert_password = password

    def run(self, command: str, timeout: int = 0) -> Result:
        self.commands.append(command)
        return Result(next(self.responses))

    def close(self) -> None:
        self.closed = True


class ApiExecutionContractTests(unittest.TestCase):
    def test_task_polling_recovers_from_bounded_transient_login_failure(self) -> None:
        session = Session([
            "Error: Failed to login to the management server",
            json.dumps({"tasks": [{"task-id": "task-1", "status": "in progress"}]}),
            json.dumps({"tasks": [{"task-id": "task-1", "status": "succeeded"}]}),
        ])
        api = api_backend.ManagementApi(session)
        with mock.patch.object(api_backend.time, "sleep"):
            result = api.wait("CMA-A", "task-1", 60)
        self.assertEqual(api_backend.task_status(result), "succeeded")
        self.assertEqual(len(session.commands), 3)

    def test_task_polling_fails_after_repeated_transport_errors(self) -> None:
        session = Session(["Error: Failed to login to the management server"] * 4)
        api = api_backend.ManagementApi(session)
        with (
            mock.patch.object(api_backend.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "4 consecutive transport/login errors"),
        ):
            api.wait("CMA-A", "task-1", 60)

    def test_read_calls_are_synchronous_and_only_mutations_are_async(self) -> None:
        read_session = Session([json.dumps({"packages": [], "total": 0})])
        api = api_backend.ManagementApi(read_session)
        api.call("Global", "show repository-packages limit 500 offset 0")
        self.assertNotIn("--sync false", read_session.commands[0])

        task_session = Session([
            json.dumps({"task-id": "task-1"}),
            json.dumps({"tasks": [{"task-id": "task-1", "status": "succeeded"}]}),
        ])
        api = api_backend.ManagementApi(task_session)
        api.task_call("CMA-A", "install-software-package name package.tgz targets.1 Cluster-A", 30)
        self.assertIn("--sync false", task_session.commands[0])
        self.assertNotIn("--sync false", task_session.commands[1])

    def run_phase(self, phase: str, activity: str = "Software Patch Activity", operation: str = "execute") -> Session:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        plan = {
            "change": {"activity_type": activity},
            "checkpoint": {
                "cluster_mode": "cluster",
                "current_version": "R81.20",
                "target_version": "R81.20",
                "mds_host": "192.0.2.10",
                "cma_name": "CMA_A_Server",
                "domain": "CMA-A",
                "cluster_name": "Cluster-A",
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            },
            "execution": {"deployment_backend": "api"},
            "package_steps": [{
                "name": "install_take_76",
                "action": "install",
                "package_type": "jhf",
                "package_name": package,
            }],
        }
        empty_inventory = json.dumps({"targets": []})
        responses = [
            "context set",
            empty_inventory,
        ]
        if operation == "execute":
            responses.extend([
                "20971520",  # 20 GiB free on root, reported in KiB
                "10485760",  # 10 GiB existing Central Deployment cache, in KiB
            ])
        responses.extend([
            json.dumps({"task-id": "task-1"}),
            json.dumps({"tasks": [{"task-id": "task-1", "status": "succeeded"}]}),
        ])
        if operation == "execute":
            responses.append(empty_inventory)
        session = Session(responses)
        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "plan.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "management_api_package_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--step", "install_take_76",
                "--phase", phase,
                "--operation", operation,
                *(["--execute"] if operation == "execute" else []),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, {"CP_PASSWORD": "login", "CP_EXPERT_PASSWORD": "expert"}, clear=False),
                mock.patch.object(api_backend.c, "connect", return_value=session),
                mock.patch.object(
                    api_backend,
                    "major_upgrade_completed_count",
                    return_value=(0 if phase == "first-member" else 1, []),
                ),
            ):
                self.assertEqual(api_backend.main(), 0)
        return session

    def test_first_phase_targets_cluster_and_requests_failover(self) -> None:
        session = self.run_phase("first-member")
        self.assertEqual(session.commands[0], "mdsenv CMA_A_Server")
        execute = next(command for command in session.commands if "install-software-package" in command)
        self.assertIn(" -d CMA-A ", execute)
        self.assertIn("targets.1 Cluster-A", execute)
        self.assertIn("cluster-strategy non-active-members-and-failover", execute)
        self.assertNotIn("CentralDeploymentTool", execute)
        self.assertTrue(session.closed)

    def test_second_phase_targets_only_remaining_non_active_member(self) -> None:
        session = self.run_phase("second-member")
        execute = next(command for command in session.commands if "install-software-package" in command)
        self.assertIn("cluster-strategy non-active-members-no-failover", execute)

    def test_install_verify_stages_package_from_central_repository(self) -> None:
        session = self.run_phase("first-member", operation="verify")
        verify = next(command for command in session.commands if "verify-software-package" in command)
        self.assertIn("download-package true", verify)
        self.assertIn("download-package-from central", verify)
        self.assertTrue(session.closed)

    def test_major_first_phase_defers_failover_to_explicit_workflow_gate(self) -> None:
        session = self.run_phase("first-member", "Major Version Upgrade")
        execute = next(command for command in session.commands if "install-software-package" in command)
        self.assertIn("cluster-strategy non-active-members-no-failover", execute)
        self.assertNotIn("cluster-strategy non-active-members-and-failover", execute)

    def test_terminal_major_api_failure_is_accepted_only_after_reconciliation(self) -> None:
        package = "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz"
        plan = {
            "change": {"activity_type": "Major Version Upgrade"},
            "checkpoint": {
                "cluster_mode": "cluster",
                "current_version": "R81.20",
                "target_version": "R82",
                "mds_host": "192.0.2.10",
                "cma_name": "CMA_A_Server",
                "domain": "CMA-A",
                "cluster_name": "Cluster-A",
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            },
            "execution": {"deployment_backend": "api"},
            "package_steps": [{
                "name": "upgrade_r82",
                "action": "upgrade",
                "package_type": "blink",
                "package_name": package,
            }],
        }
        session = Session(["context set", "20971520", "10485760"])
        api = mock.Mock()
        api.call.return_value = {"targets": []}
        api.task_call.side_effect = RuntimeError(
            "Management API task task-1 ended with status failed: {}"
        )
        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "plan.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "management_api_package_from_activity.py",
                "--activity-plan-file", str(plan_path),
                "--step", "upgrade_r82",
                "--phase", "second-member",
                "--operation", "execute",
                "--execute",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, {"CP_PASSWORD": "login", "CP_EXPERT_PASSWORD": "expert"}, clear=False),
                mock.patch.object(api_backend.c, "connect", return_value=session),
                mock.patch.object(api_backend, "ManagementApi", return_value=api),
                mock.patch.object(
                    api_backend,
                    "major_upgrade_completed_count",
                    return_value=(1, []),
                ),
                mock.patch.object(
                    api_backend,
                    "major_upgrade_completed_despite_api_failure",
                    return_value=True,
                ) as reconcile,
            ):
                self.assertEqual(api_backend.main(), 0)
        reconcile.assert_called_once()
        self.assertTrue(session.closed)


    def test_ambiguous_alias_fails_closed(self) -> None:
        first = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        second = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz"
        data = inventory([first, second], [first, second])
        step = {"action": "remove", "package_name": "Take 76"}
        with self.assertRaisesRegex(RuntimeError, "exactly one package"):
            api_backend.resolve_remove_identity(data, step, {"current_version": "R81.20"})

    def test_missing_release_or_take_fails_closed(self) -> None:
        package = "Check_Point_R82_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        data = inventory([package], [package])
        step = {"action": "remove", "package_name": "Take 76"}
        with self.assertRaisesRegex(RuntimeError, r"candidates=\[\]"):
            api_backend.resolve_remove_identity(data, step, {"current_version": "R81.20"})


    def test_remove_verify_uses_inventory_without_install_eligibility_call(self) -> None:
        package = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
        plan = {
            "checkpoint": {
                "cluster_mode": "cluster", "current_version": "R81.20", "target_version": "R81.20",
                "mds_host": "192.0.2.10", "cma_name": "CMA_A_Server", "domain": "CMA-A",
                "cluster_name": "Cluster-A", "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            },
            "execution": {"deployment_backend": "api"},
            "package_steps": [{"name": "remove_take_76", "action": "remove", "package_type": "jhf", "package_name": "Take 76"}],
        }
        session = Session(["context set", json.dumps(inventory([package], [package]))])
        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "plan.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "management_api_package_from_activity.py", "--activity-plan-file", str(plan_path),
                "--step", "remove_take_76", "--phase", "first-member", "--operation", "verify",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, {"CP_PASSWORD": "login", "CP_EXPERT_PASSWORD": "expert"}, clear=False),
                mock.patch.object(api_backend.c, "connect", return_value=session),
            ):
                self.assertEqual(api_backend.main(), 0)
        self.assertFalse(any("verify-software-package" in command for command in session.commands))
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
