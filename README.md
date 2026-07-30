<div align="center">

# Check Point + ServiceNow Firewall Automation

**Ansible automation for Check Point firewall maintenance, with optional ServiceNow change control.**

[![Validate](https://github.com/SGT-Trojan/checkpoint-servicenow-automation/actions/workflows/validate.yml/badge.svg)](https://github.com/SGT-Trojan/checkpoint-servicenow-automation/actions/workflows/validate.yml)

<a href="#requirements">
  <img src="docs/diagrams/project-metadata-badges.svg" width="396" alt="Apache-2.0 license; Python 3.10 or later; Ansible Core 2.16 through 2.21">
</a>

[Start here](docs/START_HERE.md) | [Architecture](docs/ARCHITECTURE_AND_ENGINEERING_GUIDE.md) | [Components](docs/COMPONENT_REFERENCE.md) | [Tested scenarios](docs/CERTIFIED_SCENARIOS.md) | [ServiceNow setup](docs/SERVICENOW_BUILD_GUIDE.md) | [Security](SECURITY.md)

</div>

## Contents

- [What This Project Does](#what-this-project-does)
- [Requirements](#requirements)
- [What We Tested](#what-we-tested)
- [Quick Start](#quick-start)
- [Documentation](#documentation)
- [Execution Architecture](#execution-architecture)
- [Repository Map](#repository-map)
- [Run the Checks](#run-the-checks)
- [Limits](#limits)
- [License](#license)

## What This Project Does

This project helps you install, remove, or upgrade Check Point software on a
cluster. You can run it from an automation host or connect it to ServiceNow.
Both options use the same safety checks and change one cluster member at a time.

New to the project? Read [Start here](docs/START_HERE.md) before the detailed
guides.

| Area | What it does |
|---|---|
| Find the target | Finds the MDS, CMA, cluster, members, and policy. Stops if the result is missing or unclear. |
| Check readiness | Checks cluster health, packages, hashes, free space, rollback capacity, and other prerequisites. |
| Install or remove a JHF | Updates the standby member first, checks it, fails over, pauses for a tester, and then updates the other member. |
| Run a major upgrade | Adds mixed-version, policy, MVC, failover, and final health checks to the rolling workflow. |
| Update the Deployment Agent | Finds the required build, checks the package, and updates both members through a separate path. |
| Use ServiceNow | Tracks the request, approval, change, tester task, recovery task, and final result. |
| Recover from a failure | Saves the failed phase and lets an engineer restart there after fixing the problem. |
| Find JHF packages | Lists current and older Recommended Takes, downloads a selected package, and verifies both published hashes. |

## Requirements

| Item | Supported value |
|---|---|
| License | [Apache License 2.0](LICENSE) |
| Python | 3.10 or later |
| Ansible Core | 2.16 through 2.21 (`>=2.16,<2.22`) |
| Check Point releases and Takes | See [What we tested](docs/CERTIFIED_SCENARIOS.md) |

## What We Tested

| How it was run | What completed |
|---|---|
| ServiceNow and CDT | Installed and removed R81.20 Take 76. Upgraded R81.20 build 634, with no separately installed JHF, to R82 build 777 with Take 60. Used real approvals, tester tasks, failure recovery, and closure. |
| Command line and CDT | Ran the same R81.20 work with runner safety gates. Also installed R82 Recommended Take 107 over Take 60, one member at a time. |
| Command line and Management API | Installed R81.20 Take 76 and ran the R81.20-to-R82 upgrade. Removed Take 76 with the guarded direct CPUSE fallback because the tested API could not safely remove it one member at a time. |
| Deployment Agent | Confirmed that build 2771 was already installed and current on both members through the separate Deployment Agent workflow. |

These tests used one two-member lab cluster. They do not prove that another
version, cluster, or environment will work. The major upgrade started on
R81.20 build 634 after Take 76 had been removed. We did not separately test a
direct upgrade from R81.20 Take 76 to R82. See [What we tested](docs/CERTIFIED_SCENARIOS.md)
for dates, checks, limits, and combinations we have not tested.

## Quick Start

> [!CAUTION]
> This software can change packages, policy, and which cluster member is active.
> Start in a non-production environment. Run the offline tests and read-only
> checks first. Confirm the target and package hashes. Do not skip tester or
> recovery tasks.

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
failover, or upgrade.

### JHF discovery example

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --list
python3 tools/cpuse_jhf_fetch.py --version R82 --menu --dest /srv/checkpoint/packages
```

The menu labels each Take as Recommended, Latest, or previously Recommended.
It checks the selected package against Check Point metadata and both published
hashes. Choosing a package does not install it.

## Documentation

| Goal | Start here |
|---|---|
| Learn the basic flow and terms | [Start here](docs/START_HERE.md) |
| Run directly from a controlled automation host | [Architecture and engineering guide](docs/ARCHITECTURE_AND_ENGINEERING_GUIDE.md) |
| Choose between CDT and Management API deployment | [CDT and Management API deployment](docs/CDT_AND_MANAGEMENT_API.md) |
| Follow copy-ready helper and playbook scenarios | [Practical examples](examples/README.md) |
| Reuse individual scripts or playbooks | [Component and integration reference](docs/COMPONENT_REFERENCE.md) |
| Reproduce the full ServiceNow-governed implementation | [ServiceNow build guide](docs/SERVICENOW_BUILD_GUIDE.md) |
| Understand the request-to-completion lifecycle | [Workflow walkthrough](docs/WORKFLOW_WALKTHROUGH.md) |
| Review exactly which versions and paths were exercised live | [Certified scenarios](docs/CERTIFIED_SCENARIOS.md) |
| Inventory installed patches across managed gateways | [Patch inventory guide](tools/CHECKPOINT_PATCH_INVENTORY.md) |
| Discover or securely download a JHF | [JHF currency and download guide](tools/JHF_CURRENCY_AND_DOWNLOAD.md) |
| Assess Deployment Agent currency | [Deployment Agent currency guide](tools/DEPLOYMENT_AGENT_CURRENCY.md) |

ServiceNow is optional. The command-line runner can use the same maintenance
steps without a ServiceNow instance.

## Execution Architecture

<a href="docs/diagrams/execution-architecture.svg">
  <img src="docs/diagrams/execution-architecture.svg" width="100%" alt="Execution architecture showing optional ServiceNow governance, the automation host, Check Point management, standby-first member sequencing, a Closed Complete human gate, final validation, and the evidence and resume loop.">
</a>

<p align="center"><sub><a href="docs/diagrams/execution-architecture.svg">Open the architecture diagram at full size</a></sub></p>

The backend only sends the deployment command. The runner still controls target
checks, member order, failover, tester approval, logs, and restart state.

## Repository Map

```text
ansible/
  inventory/                 Example controller inventory
  playbooks/                 Readiness, execution, failover, and postcheck phases
  scripts/                   Target resolution and backend helper programs
  scripts/tests/             Offline orchestration and adversarial tests
docs/                        Architecture, workflow, and ServiceNow build guides
systemd/                     Long-running worker service definitions
test_inputs/                 Sanitized activity-plan examples
examples/                    Standalone helper and playbook examples
tools/                       JHF, Deployment Agent, inventory, and hygiene utilities
checkpoint_cluster_upgrade.py
servicenow_checkpoint_runner.py
servicenow_checkpoint_worker.py
```

## Run the Checks

```bash
python3 -m unittest discover -s ansible/scripts/tests -v
python3 -m unittest discover -s tools/tests -v
for playbook in ansible/playbooks/*.yml $(find examples -path '*/playbooks/*.yml' -print); do
  ansible-playbook --syntax-check "$playbook" -i ansible/inventory/hosts.yml
done
python3 tools/scan_public_repository.py .
```

GitHub Actions runs the protected `test` and `secrets` checks for every pull
request. Public exports are generated from an allowlisted source tree and carry
a SHA-256 manifest.

## Limits

See [What we tested](docs/CERTIFIED_SCENARIOS.md) before using this code. Read
[Security](SECURITY.md) before connecting it to an environment. This repository
does not include vendor packages, passwords, snapshots, or raw test evidence.

## License

Original source code is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for trademark and independence statements.
