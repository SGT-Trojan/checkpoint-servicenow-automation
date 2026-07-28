from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "execute_cdt_from_activity.py"
SPEC = importlib.util.spec_from_file_location("execute_cdt_from_activity", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class CandidateIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.members = [
            {"hostname": "GW-A", "management_ip": "192.0.2.20"},
            {"hostname": "GW-B", "management_ip": "192.0.2.21"},
        ]
        self.rows = [
            {"object_name": "GW-A", "cluster_name": "Cluster-A", "ip_address": "192.0.2.20", "state": "standby", "upgrade_order": "1"},
            {"object_name": "GW-B", "cluster_name": "Cluster-A", "ip_address": "192.0.2.21", "state": "active", "upgrade_order": "-"},
        ]

    def test_exact_plan_identity_is_accepted(self) -> None:
        module.validate_candidate_identity(self.rows, self.members, "Cluster-A")

    def test_wrong_ip_name_or_cluster_fails_closed(self) -> None:
        for field, value in (("ip_address", "192.0.2.99"), ("object_name", "GW-X"), ("cluster_name", "Cluster-X")):
            rows = [dict(row) for row in self.rows]
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.validate_candidate_identity(rows, self.members, "Cluster-A")

    def test_enabled_active_member_fails_closed(self) -> None:
        rows = [dict(row) for row in self.rows]
        rows[0]["state"] = "active"
        rows[1]["state"] = "standby"
        with self.assertRaisesRegex(ValueError, "standby"):
            module.validate_candidate_identity(rows, self.members, "Cluster-A")


if __name__ == "__main__":
    unittest.main()
