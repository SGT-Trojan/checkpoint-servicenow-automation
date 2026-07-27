#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import checkpoint_cluster_upgrade as c  # noqa: E402

NOT_FOUND, AMBIGUOUS, INCOMPLETE, INVALID, USAGE = 2, 3, 4, 5, 64
PAGE_SIZE = 500


class ResolverError(RuntimeError):
    def __init__(self, category: str, message: str, exit_code: int):
        super().__init__(message)
        self.category, self.exit_code = category, exit_code


class ResolverArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(USAGE, f"{self.prog}: error: {message}\n")


@dataclass(frozen=True)
class Domain:
    name: str
    cma_name: str
    cma_ip: str


@dataclass
class Candidate:
    domain: Domain
    name: str
    object_type: str
    mode: str
    addresses: set[str]
    members: list[dict[str, Any]]
    policy: str
    version: str


def ip(value: object) -> str:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def norm_ips(raw: str) -> list[str]:
    result: list[str] = []
    for value in re.split(r"[\s,;]+", raw or ""):
        if not value:
            continue
        normalized = ip(value)
        if not normalized:
            raise ResolverError("INVALID_INPUT", f"invalid target IP address: {value!r}", INVALID)
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ResolverError("INVALID_INPUT", "at least one target IP is required", INVALID)
    return result


def json_from_output(output: str) -> dict[str, Any]:
    for start, char in enumerate(output):
        if char != "{":
            continue
        candidate = output[start:].strip()
        for end in range(len(candidate), 0, -1):
            if candidate[end - 1] != "}":
                continue
            try:
                value = json.loads(candidate[:end])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no JSON object found")


def strict_query(session: Any, command: str, keys: Iterable[str] = (), timeout: int = 300) -> dict[str, Any]:
    try:
        result = session.run(command, timeout=timeout)
        print(f"===== MDS: {command} =====")
        print(result.output.rstrip())
        data = json_from_output(result.output)
    except Exception as exc:
        raise ResolverError("DISCOVERY_INCOMPLETE", f"Management API query failed: {command}: {exc}", INCOMPLETE) from exc
    if data.get("code") and data.get("message"):
        raise ResolverError("DISCOVERY_INCOMPLETE", f"Management API error: {data['code']}: {data['message']}", INCOMPLETE)
    expected = tuple(keys)
    if expected and not any(isinstance(data.get(key), list) for key in expected):
        raise ResolverError("DISCOVERY_INCOMPLETE", f"response omitted expected list {expected}: {command}", INCOMPLETE)
    return data


def paged(session: Any, base: str, keys: Iterable[str], timeout: int, size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    expected = tuple(keys)
    rows: list[dict[str, Any]] = []
    offset, total = 0, None
    signatures: set[tuple[str, ...]] = set()
    while total is None or len(rows) < total:
        data = strict_query(session, f"{base} limit {size} offset {offset} details-level full --format json", expected, timeout)
        page = next(([v for v in data[k] if isinstance(v, dict)] for k in expected if isinstance(data.get(k), list)), None)
        assert page is not None
        try:
            new_total = int(data.get("total"))
        except (TypeError, ValueError) as exc:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"invalid pagination total for {base}", INCOMPLETE) from exc
        if new_total < 0 or (total is not None and new_total != total):
            raise ResolverError("DISCOVERY_INCOMPLETE", f"pagination total changed for {base}", INCOMPLETE)
        total = new_total
        if not page:
            if len(rows) < total:
                raise ResolverError("DISCOVERY_INCOMPLETE", f"pagination stopped at {len(rows)} of {total}: {base}", INCOMPLETE)
            break
        signature = tuple(str(v.get("uid") or v.get("name") or "") for v in page)
        if signature in signatures:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"pagination repeated a page: {base}", INCOMPLETE)
        signatures.add(signature)
        rows.extend(page)
        offset += len(page)
    if total is None or len(rows) != total:
        raise ResolverError("DISCOVERY_INCOMPLETE", f"received {len(rows)} of {total}: {base}", INCOMPLETE)
    return rows


def management_servers(row: dict[str, Any]) -> list[dict[str, Any]]:
    found, seen = [], set()
    for key in ("servers", "domain-servers"):
        for server in row.get(key) or []:
            if not isinstance(server, dict):
                continue
            kind = str(server.get("type") or "").lower()
            address = ip(server.get("ipv4-address") or server.get("ipv6-address") or server.get("ip-address"))
            name = str(server.get("name") or "").strip()
            identity = (name.casefold(), address)
            if "management server" in kind and "log" not in kind and name and address and identity not in seen:
                seen.add(identity)
                found.append(server)
    return found


def management_server(domain: str, row: dict[str, Any]) -> dict[str, Any]:
    servers = management_servers(row)
    active = [v for v in servers if v.get("active") is True]
    unknown = [v for v in servers if "active" not in v]
    if len(active) == 1:
        return active[0]
    if len(servers) == 1 and unknown:
        return servers[0]
    detail = "multiple active servers" if len(active) > 1 else "no authoritative active non-logging management server"
    raise ResolverError("DISCOVERY_INCOMPLETE", f"domain {domain}: {detail}", INCOMPLETE)


def discover_domains(session: Any, preferred: str = "") -> list[Domain]:
    rows = paged(session, "mgmt_cli -r true show domains", ("objects", "domains"), 300)
    domains, names = [], set()
    for row in rows:
        if "global" in str(row.get("domain-type") or "").lower():
            continue
        name = str(row.get("name") or row.get("domain-name") or "").strip()
        if not name or name.casefold() in names:
            raise ResolverError("DISCOVERY_INCOMPLETE", "unnamed or duplicate regular domain returned", INCOMPLETE)
        server = management_server(name, row)
        domains.append(Domain(name, str(server["name"]).strip(), ip(server.get("ipv4-address") or server.get("ipv6-address") or server.get("ip-address"))))
        names.add(name.casefold())
    if not domains:
        raise ResolverError("DISCOVERY_INCOMPLETE", "MDS returned no regular domains", INCOMPLETE)
    if preferred:
        wanted = preferred.strip().casefold()
        matches = [d for d in domains if wanted in {d.name.casefold(), d.cma_name.casefold()}]
        if len(matches) != 1:
            raise ResolverError("INVALID_INPUT", f"preferred domain/CMA {preferred!r} was not uniquely discovered", INVALID)
    return sorted(domains, key=lambda d: d.name.casefold())


def kind(obj: dict[str, Any]) -> str:
    return str(obj.get("type") or "").lower().replace("_", "-")


def is_cluster(obj: dict[str, Any]) -> bool:
    return "cluster" in kind(obj) and "member" not in kind(obj)


def is_member(obj: dict[str, Any]) -> bool:
    return "cluster" in kind(obj) and "member" in kind(obj)


def is_gateway(obj: dict[str, Any]) -> bool:
    value = kind(obj)
    if not value or is_cluster(obj) or is_member(obj):
        return False
    if value in {"simple-gateway", "cpmigatewayplain", "gateway-plain", "vsx-gateway", "maestro-gateway"}:
        return True
    return "gateway" in value and not any(x in value for x in ("management", "log", "host", "interoperable", "cluster"))


ADDRESS_KEYS = ("ipv4-address", "ipv6-address", "ip-address", "main-ip", "ipv4-addresses", "ipv6-addresses")


def addresses(obj: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    containers = [obj]
    interfaces = obj.get("interfaces") or []
    if isinstance(interfaces, dict):
        interfaces = interfaces.get("objects") or interfaces.get("interfaces") or []
    containers.extend(v for v in interfaces if isinstance(v, dict))
    for container in containers:
        for key in ADDRESS_KEYS:
            value = container.get(key)
            for candidate in value if isinstance(value, list) else [value]:
                normalized = ip(candidate)
                if normalized:
                    found.add(normalized)
    return found


def primary(obj: dict[str, Any], found: set[str]) -> str:
    for key in ("ipv4-address", "ip-address", "main-ip", "ipv6-address"):
        value = ip(obj.get(key))
        if value:
            return value
    return sorted(found)[0] if found else ""


def policy(obj: dict[str, Any]) -> str:
    value = obj.get("policy")
    return str(value.get("access-policy-name") or "").strip() if isinstance(value, dict) else ""


def member_names(obj: dict[str, Any]) -> set[str]:
    found = {v.strip() for v in obj.get("cluster-member-names") or [] if isinstance(v, str) and v.strip()}
    for key in ("cluster-members", "members"):
        for member in obj.get(key) or []:
            if isinstance(member, dict):
                name = str(member.get("name") or member.get("member-name") or "").strip()
                if name:
                    found.add(name)
    return found


def members(detail: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for key in ("cluster-members", "members"):
        for member in detail.get(key) or []:
            if not isinstance(member, dict):
                continue
            member_ips = addresses(member)
            name = str(member.get("name") or member.get("member-name") or "").strip()
            member_ip = primary(member, member_ips)
            if not name or not member_ip:
                raise ResolverError("DISCOVERY_INCOMPLETE", f"cluster {detail.get('name') or detail.get('uid')} has incomplete member data", INCOMPLETE)
            result.append({"hostname": name, "ip": member_ip, "all_ips": sorted(member_ips)})
    return result


def domain_objects(session: Any, domain: Domain) -> list[dict[str, Any]]:
    return paged(session, f"mgmt_cli -r true -d {shlex.quote(domain.name)} show gateways-and-servers", ("objects", "gateways-and-servers"), 600)


def expand_cluster(session: Any, domain: Domain, cluster: dict[str, Any]) -> dict[str, Any]:
    uid = str(cluster.get("uid") or "").strip()
    if not uid:
        raise ResolverError("DISCOVERY_INCOMPLETE", f"cluster {cluster.get('name')} in {domain.name} has no UID", INCOMPLETE)
    return strict_query(
        session,
        f"mgmt_cli -r true -d {shlex.quote(domain.name)} show simple-cluster uid {shlex.quote(uid)} details-level full --format json",
        timeout=300,
    )


def domain_candidates(session: Any, domain: Domain, objects: list[dict[str, Any]], targets: set[str]) -> list[Candidate]:
    clusters = [v for v in objects if is_cluster(v)]
    top_members = [v for v in objects if is_member(v)]
    matched_names = {str(v.get("name") or "").strip() for v in top_members if targets & addresses(v)}
    relevant = [v for v in clusters if targets & addresses(v) or matched_names & member_names(v)]
    if matched_names and not relevant:
        relevant = clusters
    result: list[Candidate] = []
    for cluster in relevant:
        detail = expand_cluster(session, domain, cluster)
        member_rows = members(detail)
        if not member_rows:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"cluster {cluster.get('name')} returned no members", INCOMPLETE)
        found = addresses(cluster)
        for member in member_rows:
            found.update(member["all_ips"])
        name = str(cluster.get("name") or detail.get("name") or "").strip()
        if not name:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"cluster UID {cluster.get('uid')} has no name", INCOMPLETE)
        result.append(Candidate(domain, name, str(cluster.get("type") or detail.get("type") or ""), "cluster", found, member_rows, policy(cluster) or policy(detail), str(cluster.get("version") or detail.get("version") or cluster.get("os-name") or "")))
    for obj in objects:
        if not is_gateway(obj):
            continue
        found = addresses(obj)
        if not targets & found:
            continue
        name, gateway_ip = str(obj.get("name") or "").strip(), primary(obj, found)
        if not name or not gateway_ip:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"standalone gateway in {domain.name} has incomplete identity", INCOMPLETE)
        result.append(Candidate(domain, name, str(obj.get("type") or ""), "standalone", found, [{"hostname": name, "ip": gateway_ip, "all_ips": sorted(found)}], policy(obj), str(obj.get("version") or obj.get("os-name") or "")))
    return result


def resolve(session: Any, domains: list[Domain], target_ips: list[str], preferred: str = "") -> Candidate:
    targets, covered, candidates = set(target_ips), set(), []
    for domain in domains:
        print(f"===== Searching domain {domain.name} =====")
        current = domain_candidates(session, domain, domain_objects(session, domain), targets)
        candidates.extend(current)
        for candidate in current:
            covered.update(targets & candidate.addresses)
    complete = [v for v in candidates if targets <= v.addresses]
    if preferred:
        wanted = preferred.strip().casefold()
        complete = [v for v in complete if wanted in {v.domain.name.casefold(), v.domain.cma_name.casefold()}]
    if len(complete) == 1:
        return complete[0]
    if len(complete) > 1:
        identities = ", ".join(f"{v.domain.name}/{v.name}" for v in complete)
        raise ResolverError("AMBIGUOUS", f"target IPs resolve to multiple managed objects: {identities}", AMBIGUOUS)
    if targets <= covered:
        identities = ", ".join(f"{v.domain.name}/{v.name}" for v in candidates if targets & v.addresses)
        raise ResolverError("AMBIGUOUS", f"target IPs span multiple managed objects: {identities}", AMBIGUOUS)
    raise ResolverError("NOT_FOUND", f"unresolved target IPs: {sorted(targets - covered)}", NOT_FOUND)


def record(candidate: Candidate, target_ips: list[str]) -> dict[str, Any]:
    return {
        "input_ips": target_ips,
        "matched_ips": sorted(set(target_ips) & candidate.addresses),
        "domain": candidate.domain.name,
        "cma_name": candidate.domain.cma_name,
        "cma_ip": candidate.domain.cma_ip,
        "cluster_name": candidate.name if candidate.mode == "cluster" else "",
        "cluster_mode": candidate.mode,
        "policy_package": candidate.policy,
        "members": candidate.members,
        "matched_object_type": candidate.object_type,
        "current_version": candidate.version,
    }


def write_db(db_path: str, change_id: int, discovered: dict[str, Any]) -> None:
    if not db_path or not change_id:
        return
    db = sqlite3.connect(db_path)
    try:
        rows = discovered.get("members") or []
        a, b = (rows[0] if rows else {}), (rows[1] if len(rows) > 1 else {})
        db.execute(
            """UPDATE change_requests SET cluster_name=?, cluster_mode=?, cma_name=?, cma_ip=?,
policy_package=?, member_a_hostname=?, member_a_ip=?, member_b_hostname=?, member_b_ip=?,
checkpoint_version=COALESCE(NULLIF(checkpoint_version,''),?), updated_at=datetime('now') WHERE id=?""",
            (discovered.get("cluster_name", ""), discovered.get("cluster_mode", "cluster"), discovered.get("cma_name", ""),
             discovered.get("cma_ip", ""), discovered.get("policy_package", ""), a.get("hostname", ""), a.get("ip", ""),
             b.get("hostname", ""), b.get("ip", ""), discovered.get("current_version", ""), change_id),
        )
        db.execute("INSERT INTO work_notes (change_id,author,note,note_type) VALUES (?,?,?,?)",
                   (change_id, "ansible.svc", "Target discovery resolved: " + json.dumps(discovered, sort_keys=True), "discovery"))
        db.commit()
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = ResolverArgumentParser()
    parser.add_argument("--mds-host", required=True)
    parser.add_argument("--target-ips", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--preferred-domain", default="")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--change-id", type=int, default=0)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    target_ips = norm_ips(args.target_ips)
    if not os.environ.get("CP_PASSWORD") or not os.environ.get("CP_EXPERT_PASSWORD"):
        raise ResolverError("INVALID_INPUT", "CP_PASSWORD and CP_EXPERT_PASSWORD are required", INVALID)
    cp_args = c.parse_args(["--members", args.mds_host, args.mds_host, "--username", args.username, "--phase", "precheck"])
    try:
        session = c.connect(cp_args, args.mds_host)
    except Exception as exc:
        raise ResolverError("DISCOVERY_INCOMPLETE", f"could not connect to MDS {args.mds_host}: {exc}", INCOMPLETE) from exc
    try:
        try:
            session.enter_expert(os.environ["CP_EXPERT_PASSWORD"])
        except Exception as exc:
            raise ResolverError("DISCOVERY_INCOMPLETE", f"could not enter expert mode: {exc}", INCOMPLETE) from exc
        candidate = resolve(session, discover_domains(session, args.preferred_domain), target_ips, args.preferred_domain)
        discovered = record(candidate, target_ips)
        print("===== Discovery Result =====")
        print(json.dumps(discovered, indent=2, sort_keys=True))
        if args.output:
            Path(args.output).write_text(json.dumps(discovered, indent=2, sort_keys=True) + "\n")
        write_db(args.db_path, args.change_id, discovered)
        return 0
    finally:
        session.close()


def main() -> int:
    try:
        return run(parse_args())
    except ResolverError as exc:
        print(f"ERROR[{exc.category}]: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
