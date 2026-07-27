#!/usr/bin/env python3
"""Kimi adversarial review fixtures for codex/resolver-hardening f453b14.

test_member_without_name_must_fail_closed is a REPRODUCTION test for blocking
finding K1: it encodes the required fail-closed behavior and FAILS against the
reviewed commit until the implementer removes the fabricated member name.
The remaining tests are gap-fill coverage and pass against the reviewed commit.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[1] / "discover_checkpoint_targets.py"
SPEC = importlib.util.spec_from_file_location("discover_checkpoint_targets", MODULE)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def cluster(uid: str, name: str, members: list[str]) -> dict:
    return {
        "uid": uid,
        "name": name,
        "type": "CpmiGatewayCluster",
        "cluster-member-names": members,
        "policy": {"access-policy-name": "Policy"},
    }


def member(uid: str, name: str, address: str) -> dict:
    return {"uid": uid, "name": name, "type": "cluster-member", "ipv4-address": address}


def detail(uid: str, name: str, rows: list[dict]) -> dict:
    return {"uid": uid, "name": name, "type": "simple-cluster", "cluster-members": rows}


class FixtureSession:
    def __init__(self, domains: dict[str, list[dict]], details: dict[str, dict] | None = None):
        self.domains = domains
        self.details = details or {}

    def run(self, command: str, timeout: int = 300):
        if "show simple-cluster uid" in command:
            uid = command.split("show simple-cluster uid ", 1)[1].split()[0].strip("'")
            data = self.details[uid]
        elif "show gateways-and-servers" in command:
            domain = command.split(" -d ", 1)[1].split(" show gateways", 1)[0].strip("'")
            rows = self.domains.get(domain, [])
            offset = int(command.split(" offset ", 1)[1].split()[0])
            limit = int(command.split(" limit ", 1)[1].split()[0])
            data = {"objects": rows[offset:offset + limit], "total": len(rows)}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return SimpleNamespace(output=json.dumps(data))


class AdversarialResolverTests(unittest.TestCase):
    def test_member_without_name_must_fail_closed(self):
        """K1 REPRODUCTION: a cluster member lacking a name must raise INCOMPLETE.

        Reviewed commit instead fabricates 'member-N' (discover_checkpoint_targets.py,
        members(): name defaults to f'member-{index}', so the 'if not name' guard can
        never fire) and persists the invented hostname to the change-request record.
        """
        domain = m.Domain("D", "CMA", "198.51.100.10")
        objects = [
            cluster("c1", "Cluster1", ["GW1", "GW2"]),
            member("m1", "GW1", "198.51.100.1"),
            member("m2", "GW2", "198.51.100.20"),
        ]
        session = FixtureSession(
            {"D": objects},
            {"c1": detail("c1", "Cluster1", [
                {"name": "GW1", "ip-address": "198.51.100.1", "interfaces": []},
                {"ip-address": "198.51.100.20", "interfaces": []},  # no name field
            ])},
        )
        with self.assertRaises(m.ResolverError) as caught:
            m.resolve(session, [domain], ["198.51.100.1", "198.51.100.20"])
        self.assertEqual(caught.exception.exit_code, m.INCOMPLETE)

    def test_ipv6_gateway_resolves_end_to_end(self):
        domain = m.Domain("D", "CMA", "198.51.100.10")
        gateway = {
            "uid": "g1",
            "name": "GW6",
            "type": "simple-gateway",
            "ipv6-address": "2001:db8::5",
            "policy": {"access-policy-name": "V6-Policy"},
        }
        session = FixtureSession({"D": [gateway]})
        found = m.resolve(session, [domain], ["2001:db8::5"])
        self.assertEqual(found.name, "GW6")
        self.assertEqual(found.policy, "V6-Policy")

    def test_preferred_domain_disambiguates_cross_domain_duplicate(self):
        domains = [m.Domain("D1", "CMA1", "198.51.100.10"), m.Domain("D2", "CMA2", "203.0.113.10")]
        gateway = lambda name: {
            "uid": name, "name": name, "type": "simple-gateway",
            "ipv4-address": "192.0.2.1",
            "policy": {"access-policy-name": name + "-Policy"},
        }
        session = FixtureSession({"D1": [gateway("GW1")], "D2": [gateway("GW2")]})
        found = m.resolve(session, domains, ["192.0.2.1"], preferred="D2")
        self.assertEqual(found.domain.name, "D2")
        self.assertEqual(found.name, "GW2")

    def test_unknown_preferred_domain_is_invalid_input(self):
        class DomainSession:
            def run(self, command, timeout=300):
                row = {"name": "D1", "servers": [
                    {"name": "CMA1", "type": "management server",
                     "ipv4-address": "198.51.100.10", "active": True}]}
                return SimpleNamespace(output=json.dumps({"objects": [row], "total": 1}))

        with self.assertRaises(m.ResolverError) as caught:
            m.discover_domains(DomainSession(), preferred="no-such-domain")
        self.assertEqual(caught.exception.exit_code, m.INVALID)

    def test_usage_error_has_distinct_exit_code(self):
        """K3: malformed invocation must not be reported as target-not-found."""
        proc = subprocess.run(
            [sys.executable, str(MODULE)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, m.USAGE)
        self.assertNotEqual(proc.returncode, m.NOT_FOUND)
        self.assertIn("usage:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
