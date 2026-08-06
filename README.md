<div align="center">

# Check Point + ServiceNow Firewall Automation

**Ansible automation for Check Point firewall maintenance, with optional ServiceNow change control.**

[![Validate](https://github.com/SGT-Trojan/checkpoint-servicenow-automation/actions/workflows/validate.yml/badge.svg)](https://github.com/SGT-Trojan/checkpoint-servicenow-automation/actions/workflows/validate.yml)

[![License](https://img.shields.io/badge/License-Apache%202.0-2f855a)](LICENSE) [![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](#requirements) [![Ansible Core](https://img.shields.io/badge/Ansible%20Core-2.16--2.21-ee0000)](#requirements)

[Start here](docs/START_HERE.md) | [Examples](examples/README.md) | [Documentation](docs/README.md) | [Tested scenarios](docs/CERTIFIED_SCENARIOS.md) | [Security](SECURITY.md)

</div>

## What this project does

This project helps you install, remove, or upgrade Check Point software on a
cluster. You can run it from an automation host or connect it to ServiceNow.
Both options use the same safety checks and change one cluster member at a time.

- Resolves the management server, management domain, cluster, members, and
  policy from the requested target addresses. It stops when the result is
  missing or ambiguous.
- Checks cluster health, packages, hashes, free space, rollback capacity, and
  other prerequisites before making a change.
- Changes the standby member first, verifies the result, fails over, and pauses
  for a tester before changing the second member.
- Adds mixed-version cluster, policy, ownership, and final health checks for
  major upgrades.
- Uses the same technical safety checks with or without ServiceNow. ServiceNow
  adds request, approval, tester, remediation, and closure records.
- Saves failed-phase state so an engineer can fix the cause and resume the same
  authorized operation.

<a href="docs/diagrams/execution-architecture.svg">
  <img src="docs/diagrams/execution-architecture.svg" width="100%" alt="Execution architecture showing optional ServiceNow governance, the automation host, Check Point management, standby-first member sequencing, a Closed Complete human gate, final validation, and the evidence and resume loop.">
</a>

<p align="center"><sub><a href="docs/diagrams/execution-architecture.svg">Open the architecture diagram at full size</a></sub></p>

The deployment backend sends the package operation. The runner still controls
target checks, member order, failover, tester approval, logs, and resume state.

## What we tested

- **ServiceNow and CDT:** installed and removed R81.20 Take 76, then upgraded
  R81.20 build 634 with no separately installed JHF to R82 build 777 with
  embedded Take 60. A later recertification installed and removed R82 Take 91
  as separate requests. The original run used a real tester task; the later run
  simulated tester acceptance.
- **Standalone Python:** completed the Take 76 install and removal without
  ServiceNow or Ansible, then upgraded R81.20 to R82 build 777 with embedded
  Take 60. Tester gates were simulated.
- **Command-line runner:** exercised both CDT and Management API deployment.
  The tested API path used the guarded direct CPUSE fallback when it could not
  safely remove Take 76 one member at a time. CDT also installed R82 Recommended
  Take 107 over Take 60.
- **Deployment Agent:** confirmed that build 2771 was already current on both
  members through the separate Deployment Agent workflow.

These results came from one two-member lab cluster. They do not establish
support for other versions, clusters, or environments. The major upgrade began
after Take 76 had been removed; a direct upgrade from R81.20 Take 76 to R82 was
not tested separately. See [Tested scenarios](docs/CERTIFIED_SCENARIOS.md) for
the dates, checks, exact combinations, and remaining limits.

## Safety and limitations

> [!CAUTION]
> This software can change packages, policy, and which cluster member is active.
> Start in a non-production environment. Run the offline tests and read-only
> checks first. Confirm the target and package hashes. Do not skip tester or
> recovery tasks.

The repository does not include vendor packages, passwords, snapshots, or raw
lab output. Package approval, compatibility decisions, staging to the management
server, maintenance approval, tester acceptance, and rollback decisions remain
external responsibilities. Read [Security](SECURITY.md) before connecting the
automation to an environment.

## Quick start

### Requirements

Use Python 3.10 or later. The Ansible workflows support Ansible Core 2.16
through 2.21 (`>=2.16,<2.22`); the standalone Python workflow does not require
Ansible. Check the [tested scenarios](docs/CERTIFIED_SCENARIOS.md) before
selecting a firewall release or Take.

```bash
git clone https://github.com/SGT-Trojan/checkpoint-servicenow-automation.git
cd checkpoint-servicenow-automation
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Copy the example inventory and replace its sample values. Keep passwords out of
Git. Load them from protected environment variables or a secrets vault.

```bash
cp ansible/inventory/hosts.yml ansible/inventory/hosts.local.yml
python3 checkpoint_cluster_upgrade.py --help
python3 servicenow_checkpoint_runner.py --help
```

Run discovery and readiness checks first. Do not start with an install, removal,
failover, or upgrade. The [examples](examples/README.md) show safe starting
points and complete workflows.

### Find a JHF package

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --list
python3 tools/cpuse_jhf_fetch.py --version R82 --menu --dest /srv/checkpoint/packages
```

The menu labels each Take as Recommended, Latest, or previously Recommended.
It checks the selected package against Check Point metadata and both published
hashes. Choosing a package does not install it.

## Documentation

- [Start here](docs/START_HERE.md) explains the terms and the basic workflow.
- [Practical examples](examples/README.md) provides safe, copy-ready scenarios.
- [Runner CLI walkthrough](examples/runner_cli/README.md) covers the complete
  workflow without ServiceNow.
- [Standalone Python workflow](docs/STANDALONE_PYTHON_WORKFLOW.md) covers the
  path without ServiceNow or Ansible.
- [ServiceNow build guide](docs/SERVICENOW_BUILD_GUIDE.md) documents the full
  governed implementation.
- [Documentation index](docs/README.md) organizes every guide by audience and
  task.

## License

Original source code is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for trademark and independence statements.
