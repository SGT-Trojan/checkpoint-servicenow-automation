from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import checkpoint_cluster_upgrade as cluster
import servicenow_checkpoint_runner as runner


def load(name: str):
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


candidates = load("generate_cdt_candidates_from_activity")
postcheck = load("postcheck_gateways")


class PackageSafetyTests(unittest.TestCase):
    def test_install_and_upgrade_require_published_checksum(self) -> None:
        for action in ("install", "upgrade"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "checksum"):
                runner.package_steps_from_rows([{"package_name": "package.tgz", "action": action}])

    def test_hash_format_is_validated_and_remove_does_not_require_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid SHA1"):
            runner.package_steps_from_rows([{"package_name": "package.tgz", "action": "install", "sha1": "bad"}])
        steps = runner.package_steps_from_rows([{"package_name": "Take 76", "action": "remove"}])
        self.assertEqual(steps[0]["checksum_sha1"], "")

    def test_valid_sha256_is_normalized(self) -> None:
        value = "A" * 64
        steps = runner.package_steps_from_rows([{"package_name": "package.tgz", "action": "install", "sha256": value}])
        self.assertEqual(steps[0]["checksum_sha256"], value.lower())

    def test_attachment_role_must_be_identifiable(self) -> None:
        context = {"attachments": [{"file_name": "input.csv", "local_path": "/tmp/input.csv"}]}
        with self.assertRaisesRegex(ValueError, "cannot identify package"):
            runner.choose_attachment(context, "package")
        named = {"attachments": [{"file_name": "CPUSE Package.csv", "local_path": "/tmp/package.csv"}]}
        self.assertEqual(runner.choose_attachment(named, "package"), Path("/tmp/package.csv"))

    def test_dependencies_are_not_uninstall_aliases_or_absent_expectations(self) -> None:
        step = {
            "name": "remove_take_76",
            "action": "remove",
            "package_name": "Take 76",
            "requires_present": ["Dependency-A"],
            "requires_absent": ["Conflict-B"],
        }
        aliases = candidates.package_aliases(step, step["name"])
        self.assertNotIn("Dependency-A", aliases)
        self.assertNotIn("Conflict-B", aliases)
        present, absent = postcheck.final_package_expectations({"package_steps": [step]})
        self.assertEqual(present, [])
        self.assertIn("Take 76", absent)
        self.assertIn("Conflict-B", absent)
        self.assertNotIn("Dependency-A", absent)

    def test_required_icap_needs_explicit_healthy_result(self) -> None:
        gateway = cluster.Gateway(host="192.0.2.20", local_state="STANDBY", pnotes_ok=True, interfaces_ok=True)
        self.assertFalse(cluster.gateway_ready(gateway, "required"))
        self.assertTrue(cluster.gateway_ready(gateway, "optional"))
        gateway.icap_ok = True
        self.assertTrue(cluster.gateway_ready(gateway, "required"))


if __name__ == "__main__":
    unittest.main()
