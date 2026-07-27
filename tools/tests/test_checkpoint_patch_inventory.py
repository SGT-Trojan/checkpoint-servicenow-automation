#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "checkpoint_patch_inventory.py"
SPEC = importlib.util.spec_from_file_location("checkpoint_patch_inventory", MODULE_PATH)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


class InventoryTests(unittest.TestCase):
    def test_management_server_excludes_log_server(self):
        domain = {
            "servers": [
                {"name": "CLM", "type": "log server", "ipv4-address": "10.0.0.9", "active": True},
                {"name": "CMA", "type": "management server", "ipv4-address": "10.0.0.8", "active": True},
            ]
        }
        self.assertEqual(m.management_server(domain)["name"], "CMA")

    def test_gateway_records_associate_cluster_members(self):
        domain = m.DomainContext("D1", "duid", "CMA1", "10.0.0.8", True)
        objects = [
            {"name": "Cluster1", "type": "simple-cluster", "cluster-member-names": ["GW1", "GW2"]},
            {"uid": "1", "name": "GW1", "type": "cluster-member", "ipv4-address": "198.51.100.1", "version": "R82"},
            {"uid": "2", "name": "GW2", "type": "cluster-member", "ipv4-address": "198.51.100.20", "version": "R82"},
            {"uid": "3", "name": "Manager", "type": "checkpoint-host", "ipv4-address": "10.0.0.8"},
        ]
        gateways, errors = m.gateway_records(domain, objects, set())
        self.assertFalse(errors)
        self.assertEqual([g.name for g in gateways], ["GW1", "GW2"])
        self.assertTrue(all(g.cluster_name == "Cluster1" for g in gateways))

    def test_parse_cpuse_tables_and_status_enrichment(self):
        raw = """__CPINV_BEGIN_VERSION__
Product version Check Point Gaia R82
OS build 777
OS kernel version 4.18.0
__CPINV_END_VERSION__
__CPINV_BEGIN_INSTALLER_STATUS__
Agent: Enabled
Build number: 2771 (agent build is up to date)
Network connection: connected
__CPINV_END_INSTALLER_STATUS__
__CPINV_BEGIN_PACKAGES_INSTALLED__
**                                 Hotfixes                                   **
Display name                                                                                    Type
R82 Jumbo Hotfix Accumulator Recommended Jumbo Take 60                                          Hotfix
R82 Jumbo Hotfix Accumulator Recommended Jumbo Take 91                                          Hotfix
__CPINV_END_PACKAGES_INSTALLED__
__CPINV_BEGIN_PACKAGES_ALL__
**                                 Hotfixes                                   **
Display name                                                                                    Status
R82 Jumbo Hotfix Accumulator Recommended Jumbo Take 60                                          Installed as part of
R82 Jumbo Hotfix Accumulator Recommended Jumbo Take 91                                          Installed
__CPINV_END_PACKAGES_ALL__
"""
        gateway = m.Gateway("D1", "duid", "CMA1", "10.0.0.8", "Cluster1", "GW1", "1", "198.51.100.1", "cluster-member")
        result = m.parse_gateway_output(gateway, raw)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.version, "R82")
        self.assertEqual(result.da_build, "2771")
        self.assertEqual([p.inferred_take for p in result.packages], ["60", "91"])
        self.assertEqual(result.packages[0].status, "Installed as part of")
        self.assertEqual(result.packages[1].status, "Installed")
        self.assertEqual(m.summary_row(result)["current_jhf_take"], "91")

    def test_blink_filename_prefers_jhf_take_over_image_build(self):
        release, take, family = m.infer_package_fields(
            "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz",
            "Blink Images",
            "Blink Version",
        )
        self.assertEqual((release, take, family), ("R82", "60", "blink"))

    def test_connection_warning_is_preserved(self):
        rows, warnings = m.package_table(
            """** Hotfixes **
** Connection error. Packages list might be incomplete **
Display name    Type
Package_A.tgz   Hotfix
""",
            "Type",
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
