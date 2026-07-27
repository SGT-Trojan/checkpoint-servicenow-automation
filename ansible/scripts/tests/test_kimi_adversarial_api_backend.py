#!/usr/bin/env python3
"""Kimi adversarial fixtures for codex/api-deployment-backend 62c84e0.

test_second_member_uninstall_must_resolve_unique_union_identity is a
REPRODUCTION test for blocking finding A1: it encodes the required behavior
and FAILS against the reviewed commit. test_take_number_jhf_token_precedence
documents finding A3 (also failing against the reviewed commit).
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
        """A1 REPRODUCTION: after the first-member removal, the package remains
        on exactly one member. Resolution must still succeed because the
        identity is unique across the cluster inventory (union), otherwise the
        API rolling uninstall can never complete its second member.

        Reviewed commit requires the package in the INTERSECTION (installed on
        every member), so the second-member phase always raises."""
        data = inventory([], [T76])  # member 1 already cleaned
        step = {"action": "remove", "package_name": "Take 76"}
        self.assertEqual(
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
            T76,
        )

    def test_second_member_uninstall_explicit_full_identity(self):
        """A1 companion: explicit full package identity in the phase-2
        (unequal) inventory must also resolve."""
        data = inventory([], [T76])
        step = {"action": "remove", "package_name": T76}
        self.assertEqual(
            api.resolve_remove_identity(data, step, {"current_version": "R81.20"}),
            T76,
        )

    def test_divergent_member_inventories_still_fail_closed(self):
        """A1 guard: uniqueness must not be weakened — two DIFFERENT take-76
        identities across members is ambiguity and must raise."""
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
        """A3: the blink image name contains build token T777 and JHF token
        T60. Project rule (PROGRESS.md history): the JHF_T## token must take
        precedence over the image build token. Reviewed commit's leftmost
        regex match returns 777."""
        value = "blink_image_1.1_Check_Point_R82_T777_JHF_T60_SecurityGateway.tgz"
        self.assertEqual(api.take_number(value), "60")


if __name__ == "__main__":
    unittest.main()
