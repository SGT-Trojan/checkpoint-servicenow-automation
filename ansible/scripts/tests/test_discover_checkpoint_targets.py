#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
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


def cluster(uid: str, name: str, members: list[str], policy: str = "Policy") -> dict:
    return {
        "uid": uid,
        "name": name,
        "type": "CpmiGatewayCluster",
        "cluster-member-names": members,
        "policy": {"access-policy-name": policy},
        "version": "R82",
    }


def member(uid: str, name: str, address: str) -> dict:
    return {"uid": uid, "name": name, "type": "cluster-member", "ipv4-address": address}


def detail(uid: str, name: str, rows: list[tuple[str, str]]) -> dict:
    return {
        "uid": uid,
        "name": name,
        "type": "simple-cluster",
        "cluster-members": [
            {"name": member_name, "ip-address": address, "interfaces": []}
            for member_name, address in rows
        ],
    }


class FixtureSession:
    def __init__(self, domains: dict[str, list[dict]] | None = None, details: dict[str, dict] | None = None):
        self.domains = domains or {}
        self.details = details or {}
        self.commands: list[str] = []
        self.fail_domain = ""

    def run(self, command: str, timeout: int = 300):
        self.commands.append(command)
        if "show simple-cluster uid" in command:
            uid = command.split("show simple-cluster uid ", 1)[1].split()[0].strip("'")
            data = self.details[uid]
        elif "show gateways-and-servers" in command:
            domain = command.split(" -d ", 1)[1].split(" show gateways", 1)[0].strip("'")
            if domain == self.fail_domain:
                data = {"code": "generic_err", "message": "permission denied"}
            else:
                rows = self.domains.get(domain, [])
                offset = int(command.split(" offset ", 1)[1].split()[0])
                limit = int(command.split(" limit ", 1)[1].split()[0])
                data = {"objects": rows[offset:offset + limit], "total": len(rows)}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return SimpleNamespace(output="command echo\n" + json.dumps(data) + "\n[Expert]#")


class ResolverTests(unittest.TestCase):
    def test_pagination_collects_every_page(self):
        session = FixtureSession({"D": [{"uid": "1"}, {"uid": "2"}, {"uid": "3"}]})
        rows = m.paged(
            session,
            "mgmt_cli -r true -d D show gateways-and-servers",
            ("objects",),
            30,
            size=1,
        )
        self.assertEqual([row["uid"] for row in rows], ["1", "2", "3"])
        self.assertEqual(len(session.commands), 3)

    def test_truncated_pagination_fails_closed(self):
        class Truncated:
            def run(self, command, timeout=300):
                return SimpleNamespace(output=json.dumps({"objects": [], "total": 2}))

        with self.assertRaises(m.ResolverError) as caught:
            m.paged(Truncated(), "show domains", ("objects",), 30)
        self.assertEqual(caught.exception.exit_code, m.INCOMPLETE)

    def test_cma_selection_excludes_clm_and_uses_active_management_server(self):
        row = {
            "servers": [
                {"name": "CLM", "type": "log server", "ipv4-address": "10.0.0.9", "active": True},
                {"name": "CMA-B", "type": "management server", "ipv4-address": "10.0.0.8", "active": False},
                {"name": "CMA-A", "type": "management server", "ipv4-address": "10.0.0.7", "active": True},
            ]
        }
        self.assertEqual(m.management_server("D", row)["name"], "CMA-A")

    def test_multiple_active_cmas_fail_closed(self):
        row = {"servers": [
            {"name": "A", "type": "management server", "ipv4-address": "198.51.100.1", "active": True},
            {"name": "B", "type": "management server", "ipv4-address": "198.51.100.20", "active": True},
        ]}
        with self.assertRaises(m.ResolverError) as caught:
            m.management_server("D", row)
        self.assertEqual(caught.exception.exit_code, m.INCOMPLETE)

    def test_structured_addresses_ignore_unrelated_text_and_include_ipv6(self):
        obj = {
            "ipv4-address": "198.51.100.1",
            "comments": "old address 192.0.2.44 must not match",
            "interfaces": [{"ipv6-address": "2001:db8::1"}],
        }
        self.assertEqual(m.addresses(obj), {"198.51.100.1", "2001:db8::1"})

    def test_cluster_resolves_only_when_all_ips_share_one_object(self):
        domain = m.Domain("D", "CMA", "198.51.100.10")
        objects = [
            cluster("c1", "Cluster1", ["GW1", "GW2"], "Authoritative-Policy"),
            member("m1", "GW1", "198.51.100.1"),
            member("m2", "GW2", "198.51.100.20"),
            {"uid": "mgr", "name": "Manager", "type": "checkpoint-host", "ipv4-address": "198.51.100.10"},
        ]
        session = FixtureSession({"D": objects}, {"c1": detail("c1", "Cluster1", [("GW1", "198.51.100.1"), ("GW2", "198.51.100.20")])})
        found = m.resolve(session, [domain], ["198.51.100.1", "198.51.100.20"])
        self.assertEqual(found.name, "Cluster1")
        self.assertEqual(found.policy, "Authoritative-Policy")
        self.assertIn("show simple-cluster uid c1", "\n".join(session.commands))
        self.assertNotIn("show packages", "\n".join(session.commands))

    def test_ips_from_different_clusters_are_ambiguous(self):
        domain = m.Domain("D", "CMA", "198.51.100.10")
        objects = [
            cluster("c1", "Cluster1", ["GW1"]),
            member("m1", "GW1", "198.51.100.1"),
            cluster("c2", "Cluster2", ["GW2"]),
            member("m2", "GW2", "198.51.100.20"),
        ]
        session = FixtureSession(
            {"D": objects},
            {
                "c1": detail("c1", "Cluster1", [("GW1", "198.51.100.1")]),
                "c2": detail("c2", "Cluster2", [("GW2", "198.51.100.20")]),
            },
        )
        with self.assertRaises(m.ResolverError) as caught:
            m.resolve(session, [domain], ["198.51.100.1", "198.51.100.20"])
        self.assertEqual(caught.exception.exit_code, m.AMBIGUOUS)

    def test_duplicate_ip_across_domains_is_ambiguous(self):
        domains = [m.Domain("D1", "CMA1", "198.51.100.10"), m.Domain("D2", "CMA2", "203.0.113.10")]
        gateway = lambda name: {
            "uid": name,
            "name": name,
            "type": "simple-gateway",
            "ipv4-address": "192.0.2.1",
            "policy": {"access-policy-name": name + "-Policy"},
        }
        session = FixtureSession({"D1": [gateway("GW1")], "D2": [gateway("GW2")]})
        with self.assertRaises(m.ResolverError) as caught:
            m.resolve(session, domains, ["192.0.2.1"])
        self.assertEqual(caught.exception.exit_code, m.AMBIGUOUS)

    def test_any_domain_query_failure_invalidates_whole_scan(self):
        domains = [m.Domain("D1", "CMA1", "198.51.100.10"), m.Domain("D2", "CMA2", "203.0.113.10")]
        session = FixtureSession({"D1": [{"uid": "g1", "name": "GW", "type": "simple-gateway", "ipv4-address": "192.0.2.1"}]})
        session.fail_domain = "D2"
        with self.assertRaises(m.ResolverError) as caught:
            m.resolve(session, domains, ["192.0.2.1"])
        self.assertEqual(caught.exception.exit_code, m.INCOMPLETE)

    def test_unresolved_address_is_not_found(self):
        domain = m.Domain("D", "CMA", "198.51.100.10")
        session = FixtureSession({"D": []})
        with self.assertRaises(m.ResolverError) as caught:
            m.resolve(session, [domain], ["192.0.2.99"])
        self.assertEqual(caught.exception.exit_code, m.NOT_FOUND)

    def test_invalid_address_has_distinct_exit_code(self):
        with self.assertRaises(m.ResolverError) as caught:
            m.norm_ips("Take 91")
        self.assertEqual(caught.exception.exit_code, m.INVALID)


if __name__ == "__main__":
    unittest.main()
