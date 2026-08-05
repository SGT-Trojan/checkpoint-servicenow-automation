#!/usr/bin/env python3
"""Regression tests for Management API package identity resolution.

Covers rolling-uninstall identity resolution, divergent member inventories,
absent packages, and JHF Take parsing precedence.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "management_api_package_from_activity.py"
SPEC = importlib.util.spec_from_file_location("management_api_package_from_activity", SCRIPT)
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


def inventory(*member_packages: list[str]) -> dict:
    return {
        "targets": [{
            "cluster-members": [
                {"name": f"member-{i}", "packages": {"installed": [{"package-id": n} for n in pkgs]}}
                for i, pkgs in enumerate(member_packages, 1)
            ]
        }]
    }


T76 = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_FULL.tgz"
T76_SPECIAL = "Check_Point_R81_20_JUMBO_HF_MAIN_Bundle_T76_SPECIAL_FULL.tgz"


class UnequalInventoryUninstallTests(unittest.TestCase):
    def test_second_member_uninstall_must_resolve_unique_union_identity(self):
        """Resolve the remaining package after the first member is clean.

        Rolling removal needs the unique identity from the union of both member
        inventories so it can complete the second-member phase.
        """
        data = inventory([], [T76])  # member 1 already cleaned
        step = {"action": "remove", "package_name": "Take 76"}
        self.assertEqual(
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
            T76,
        )

    def test_second_member_uninstall_explicit_full_identity(self):
        """Accept an exact package identity when only the second member has it."""
        data = inventory([], [T76])
        step = {"action": "remove", "package_name": T76}
        self.assertEqual(
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
            T76,
        )

    def test_divergent_member_inventories_still_fail_closed(self):
        """Reject two distinct Take 76 identities as ambiguous."""
        data = inventory([T76], [T76_SPECIAL])
        step = {"action": "remove", "package_name": "Take 76"}
        with self.assertRaises(RuntimeError):
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"})

    def test_package_absent_everywhere_fails_closed(self):
        data = inventory([], [])
        step = {"action": "remove", "package_name": "Take 76"}
        with self.assertRaises(RuntimeError):
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"})


class TakeNumberPrecedenceTests(unittest.TestCase):
    def test_take_number_jhf_token_precedence(self):
        """Prefer the JHF token over a Blink image build token.

        T777 identifies the base image build, while JHF_T60 identifies the
        bundled hotfix level used for post-upgrade verification.
        """
        value = "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz"
        self.assertEqual(api.take_number(value), "60")


if __name__ == "__main__":
    unittest.main()
