from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import servicenow_checkpoint_runner as runner


SCRIPT = Path(__file__).resolve().parents[1] / "major_policy_gate_from_activity.py"
SPEC = importlib.util.spec_from_file_location("major_policy_gate_from_activity", SCRIPT)
assert SPEC and SPEC.loader
policy_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy_gate
SPEC.loader.exec_module(policy_gate)


class Result:
    def __init__(self, output: str):
        self.output = output


class Session:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.commands: list[str] = []
        self.expert_password = ""
        self.closed = False

    def enter_expert(self, password: str) -> None:
        self.expert_password = password

    def run(self, command: str, timeout: int = 0) -> Result:
        self.commands.append(command)
        return Result(next(self.responses))

    def close(self) -> None:
        self.closed = True


class MajorPolicyGateTests(unittest.TestCase):
    def test_api_json_fails_closed_on_empty_and_api_error_output(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "empty output"):
            policy_gate.api_json(Session([""]), "mgmt_cli show domains")

        error = '{"code":"generic_err_invalid_parameter","message":"bad domain"}'
        with self.assertRaisesRegex(RuntimeError, "bad domain"):
            policy_gate.api_json(Session([error]), "mgmt_cli show domains")

    def test_main_separates_runtime_cma_from_api_domain(self) -> None:
        plan = {
            "checkpoint": {
                "mds_host": "192.0.2.10",
                "cma_name": "CMA_A_Server",
                "domain": "CMA-A",
                "cma_ip": "192.0.2.11",
                "cluster_name": "Cluster-A",
                "policy_package": "Policy-A",
                "current_version": "R81.20",
                "target_version": "R82",
                "members": [{"ip": "192.0.2.20"}, {"ip": "192.0.2.21"}],
            }
        }
        success = '{"task-id":"task-1","status":"succeeded"}'
        task = '{"tasks":[{"task-id":"task-1","status":"succeeded"}]}'
        session = Session([success, success, task, success, task, success, task])

        with tempfile.TemporaryDirectory() as td:
            plan_path = Path(td) / "plan.json"
            plan_path.write_text(json.dumps(plan))
            argv = [
                "major_policy_gate_from_activity.py",
                "--activity-plan-file",
                str(plan_path),
                "--phase",
                "final-policy-install",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.dict(os.environ, {"CP_PASSWORD": "login", "CP_EXPERT_PASSWORD": "expert"}, clear=False),
                mock.patch.object(policy_gate.c, "connect", return_value=session),
            ):
                self.assertEqual(policy_gate.main(), 0)

        self.assertEqual(session.commands[0], "mdsenv CMA_A_Server")
        api_commands = session.commands[1:]
        self.assertTrue(api_commands)
        self.assertTrue(all(" -d CMA-A " in command for command in api_commands))
        self.assertTrue(all("CMA_A_Server" not in command for command in api_commands))
        self.assertTrue(session.closed)

    def test_main_requires_both_cma_runtime_name_and_domain(self) -> None:
        base = {"mds_host": "192.0.2.10", "cma_name": "CMA_A_Server", "domain": "CMA-A", "cluster_name": "Cluster-A", "policy_package": "Policy-A"}
        for missing in ("cma_name", "domain"):
            checkpoint = dict(base)
            checkpoint[missing] = ""
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as td:
                plan_path = Path(td) / "plan.json"
                plan_path.write_text(json.dumps({"checkpoint": checkpoint}))
                argv = ["major_policy_gate_from_activity.py", "--activity-plan-file", str(plan_path), "--phase", "final-policy-install"]
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaisesRegex(SystemExit, missing):
                        policy_gate.main()


    def test_runner_persists_discovered_domain_in_plan_and_vars(self) -> None:
        args = argparse.Namespace(
            activity_type="Version Upgrade Activity", target_ips="192.0.2.20,192.0.2.21",
            mds_host="192.0.2.10", current_version="R81.20", target_version="R82",
            icap_mode="required", preserve_original_active="true", tester_gate="true",
            package_source_dir="/var/log/tmp", cma_name="", cma_ip="", target_take="60",
            cluster_name="", policy_package="", chg_number="CHGTEST", change_number="",
            environment="test", support_capture_script="/tmp/capture.sh",
        )
        discovered = {
            "domain": "CMA-A", "cma_name": "CMA_A_Server", "cma_ip": "192.0.2.11",
            "cluster_name": "Cluster-A", "cluster_mode": "cluster", "policy_package": "Policy-A",
            "members": [{"name": "GW-A", "ip": "192.0.2.20"}, {"name": "GW-B", "ip": "192.0.2.21"}],
        }
        plan = runner.build_base_plan(args, [], discovered, {})
        self.assertEqual(plan["checkpoint"]["cma_name"], "CMA_A_Server")
        self.assertEqual(plan["checkpoint"]["domain"], "CMA-A")
        variables = runner.runner_vars(plan, Path("plan.json"))
        self.assertEqual(variables["cma_name"], "CMA_A_Server")
        self.assertEqual(variables["domain"], "CMA-A")


if __name__ == "__main__":
    unittest.main()
