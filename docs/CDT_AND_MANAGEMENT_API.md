# CDT and Management API Deployment

This guide explains the two package-deployment backends. Read [Start Here](START_HERE.md)
first if the terms CDT, MDS, or Management API are new to you. Always check the
Check Point documentation for your installed management release.

## Which Backend Should I Use?

Management API Central Deployment does not invoke CDT. They are separate tools.
They use different commands, credentials, logs, and error handling.

- Use Management API Central Deployment for an API-native package operation.
- Use CDT through a restricted MDS execution channel when CDT behavior is
  required.
- Let the runner control readiness, target selection, member order, failover,
  tester approval, logs, and restart state. Changing the backend must not skip
  these checks.
- Do not use a generic script API to make one backend call the other.

This repository keeps the two backends separate. Each one has its own runner
path, playbooks, execution flags, logs, and restart phases.

## Interfaces

| Mechanism | Interface and transport | Execution location | Main artifacts |
|---|---|---|---|
| CDT | `$CDTDIR/CentralDeploymentTool` over restricted SSH | Expert mode on the Management Server or MDS, in the applicable CMA context | Deployment plan XML, candidates CSV, CDT logs |
| Management API Central Deployment | Management Web API, commonly through `mgmt_cli` | Management API server | Repository package identity, task ID, task result |
| This repository's API backend | `mgmt_cli` over restricted SSH plus Expert context | MDS | Activity plan, API task output, phase evidence |
| Management API `run-script` | Management Web API operation against named targets | Target selected through the Management Server | Script task and output |
| Gaia REST API `run-script` | Gaia REST API on a specific Gaia system | The Gaia system receiving the call | Gaia task and output |
| Direct CPUSE | Gaia Clish through the guarded direct helper | Selected gateway member | CPUSE package state and operation logs |

The API helper runs the same Management API commands used by an HTTPS client or
the `check_point.mgmt` Ansible collection. The connection is different. This
helper opens restricted SSH to the MDS and runs `mgmt_cli -r true`. Ansible
modules normally connect to the Web API over HTTPS. They may need different
credentials, session settings, and firewall rules.

## Management API Package Steps

The following syntax matches the Management API behavior tested by this
repository on its documented R81.20/R82 management path. Confirm the command
schema with `mgmt_cli` or the Management API reference for the installed
management release before using it elsewhere.

### 1. Import a local package

On an MDS, the repository import is performed in the Global domain. `name` is
the package file name and `path` is the directory that already contains it.

```bash
mgmt_cli -r true -d Global add repository-package \
  name "<PACKAGE.tar>" \
  path "/var/log/tmp" \
  source local \
  --sync false \
  --format json
```

Poll the returned task ID, then read the repository and use the exact identity
it exposes:

```bash
mgmt_cli -r true -d Global show repository-packages \
  limit 500 \
  offset 0 \
  --format json
```

Repository listings must be paginated until complete. Do not select a package
by a partial Take or release match when multiple identities remain.

### 2. Inspect target inventory

```bash
mgmt_cli -r true -d "<DOMAIN>" show-software-packages-per-targets \
  targets.1 "<CLUSTER-OBJECT>" \
  display.installed any \
  --format json
```

### 3. Verify the package

```bash
mgmt_cli -r true -d "<DOMAIN>" verify-software-package \
  name "<EXACT-REPOSITORY-NAME>" \
  targets.1 "<CLUSTER-OBJECT>" \
  download-package true \
  download-package-from central \
  --sync false \
  --format json
```

The target is the complete cluster object, not an individual cluster member.
Verification does not change policy or package state, but it is an asynchronous
operation and its returned task must still be polled.

### 4. Install a JHF with API-managed failover

```bash
mgmt_cli -r true -d "<DOMAIN>" install-software-package \
  name "<EXACT-REPOSITORY-NAME>" \
  targets.1 "<CLUSTER-OBJECT>" \
  method install \
  package-location central \
  cluster-installation-settings.cluster-delay 0 \
  cluster-installation-settings.cluster-strategy \
    non-active-members-and-failover \
  --sync false \
  --format json
```

`cluster-delay 0` is the setting exercised by this repository. It is not a
universal recommendation. Select delay and strategy from the maintenance
requirements and the installed Management API version.

### 5. Poll an asynchronous task

```bash
mgmt_cli -r true -d "<DOMAIN>" show-task \
  task-id "<TASK-ID>" \
  details-level full \
  --format json
```

Poll until a documented terminal state and fail closed on an absent task ID,
empty response, API error object, timeout, or unsuccessful terminal state.

## Synchronization Rules

- Keep inventory and other ordinary read commands synchronous.
- Use `--sync false` only when the command is expected to return an asynchronous
  task.
- Poll asynchronous repository, verification, installation, upgrade, and
  uninstall operations with synchronous `show-task`.
- Do not add `--sync false` indiscriminately to show commands. During the
  repository's read-only probes, the tested API returned task envelopes for
  show commands when asynchronous mode was forced, including `show domains`
  and `show repository-packages`.

The implementation of these rules is in
`../ansible/scripts/management_api_package_from_activity.py`. The tested outcomes
and limits are recorded in `CERTIFIED_SCENARIOS.md`.

## Cluster Strategies

| Strategy | Intended behavior in this repository |
|---|---|
| `non-active-members-and-failover` | Patch the non-active member and let Central Deployment fail over |
| `non-active-members-no-failover` | Patch or upgrade the non-active member without API-managed failover |

These strategies control cluster behavior; they do not make an individual
cluster member a valid API target. The repository requires the authoritative
full cluster object and currently rejects topologies other than the supported
two-member workflow.

The tested API did not provide safe per-member semantics for rolling cluster
uninstall. The API path therefore resolves package identity through Management
API inventory or CPRID/CPInstLog and uses the separately guarded direct CPUSE
fallback. It does not invoke CDT.

## Major Upgrades

Check Point documents native Central Deployment as capable of coordinating a
cluster upgrade, including policy preparation, standby upgrade, MVC, failover,
the remaining member, and final state verification.

This repository deliberately decomposes its Management API major upgrade into
governed phases:

1. Execute member one with `non-active-members-no-failover`.
2. Run the mixed-version policy gate.
3. Enable MVC.
4. Perform explicit failover.
5. Stop for tester approval.
6. Execute member two with `non-active-members-no-failover`.
7. Install final policy and disable MVC.
8. Restore ownership when required and run final validation.

A single `install-software-package` call must not be assumed to complete a
major upgrade unless the installed API version, cluster type, package, strategy,
and native Central Deployment behavior have been verified. The repository's
certified API major-upgrade row is in `CERTIFIED_SCENARIOS.md`.

## CDT Workflow

When CDT behavior is required, this repository uses this path:

```text
orchestrator
    -> restricted SSH to MDS
    -> Expert mode
    -> mdsenv applicable CMA
    -> create deployment plan
    -> generate CDT candidates
    -> validate cluster and member identity
    -> enable exactly one intended member
    -> execute CDT
    -> collect logs and status
```

Candidate generation can return unrelated gateways from the CMA. The helper
therefore validates the expected cluster and both members, enables exactly one
member for the current phase, and disables its peer before execution.

Relevant components:

- `../ansible/playbooks/10_cdt_generate_candidates.yml`
- `../ansible/playbooks/20_cdt_execute_guarded.yml`
- `../ansible/scripts/generate_cdt_candidates_from_activity.py`
- `../ansible/scripts/execute_cdt_from_activity.py`

## Ansible Module Mapping

For new Ansible integrations, pin `check_point.mgmt>=2.9` or a later collection
version validated by the environment. The following modules map to the
Management API command family:

| Purpose | Module |
|---|---|
| Import package | `check_point.mgmt.cp_mgmt_add_repository_package` |
| Read repository | `check_point.mgmt.cp_mgmt_repository_package_facts` |
| Read target inventory | `check_point.mgmt.cp_mgmt_show_software_packages_per_targets` |
| Verify package | `check_point.mgmt.cp_mgmt_verify_software_package` |
| Install or upgrade package | `check_point.mgmt.cp_mgmt_install_software_package` |
| Uninstall package | `check_point.mgmt.cp_mgmt_uninstall_software_package` |
| Read task | `check_point.mgmt.cp_mgmt_task_facts` |

`check_point.mgmt.cp_mgmt_show_task` still appears in some collection releases
but is deprecated in favor of `cp_mgmt_task_facts`. Do not use it for a new
playbook.

Module availability does not certify an operation for every topology. In
particular, the repository does not use the uninstall module for its rolling
two-member workflow because the tested Management API rejected the required
safe per-member behavior.

## Script APIs Are Not CDT APIs

Three names must not be conflated:

1. Management API `run-script` executes a supplied script against selected
   managed targets. Its Ansible mapping is
   `check_point.mgmt.cp_mgmt_run_script`.
2. Gaia REST API `run-script` executes on the Gaia system receiving that API
   call. The separate `check_point.gaia` collection exposes
   `check_point.gaia.cp_gaia_run_script`.
3. `expert_api_runscript` is the Gaia role-feature identifier that controls
   access to the Gaia REST `run-script` feature; it is not an API command name.

Check Point does not document any of these as a CDT API. This repository has not
implemented or certified CDT through either script interface.

A future design could expose a fixed local wrapper that accepts only an
approved server-side job ID or profile. Such a design would require a separate
threat model, authorization contract, single-execution lock, target and package
resolution, controlled logging, status interface, and live certification. It
must not accept arbitrary shell, package paths, XML, candidate files, filters,
or CDT arguments from an API caller.

No Gaia or Management API `run-script` proof of concept is included in this
repository.

## Backend Selection

The ServiceNow-governed path currently uses CDT. The lower-level runner can
select the separate API backend with `--deployment-backend api`. A future
ServiceNow field may select one backend only after the field, propagation,
authorization, evidence, and resume contracts receive their own governed
certification.

Changing the backend must not change or bypass target resolution, readiness,
member order, tester approval, remediation, evidence retention, or final
validation.

## Vendor References

- [Installing Software Packages on Gaia](https://sc1.checkpoint.com/documents/R82/WebAdminGuides/EN/CP_R82_Installation_and_Upgrade_Guide/Content/Topics-IUG/Installing-Software-Packages-on-Gaia.htm)
- [Central Deployment of Hotfixes and Version Upgrades](https://sc1.checkpoint.com/documents/R82/WebAdminGuides/EN/CP_R82_SecurityManagement_AdminGuide/Content/Topics-SECMG/Central-Deployment-of-Software-Packages.htm)
- [Central Deployment Tool Administration Guide](https://sc1.checkpoint.com/documents/CDT/Unified/Topics/Introduction-to-CDT.htm)
- [Gaia role features](https://sc1.checkpoint.com/documents/R82/WebAdminGuides/EN/CP_R82_Gaia_AdminGuide/Content/Topics-GAG/Roles-Available-Features.htm)
- [Running scripts](https://sc1.checkpoint.com/documents/R82/WebAdminGuides/EN/CP_R82_SecurityManagement_AdminGuide/Content/Topics-SECMG/Running_Scripts.htm)
- [Check Point Management API Ansible collection](https://docs.ansible.com/projects/ansible/latest/collections/check_point/mgmt/index.html)
