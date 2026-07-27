# Check Point ServiceNow Automation: Architecture and Engineering Guide

Audience: automation engineers responsible for operating, extending, or productionizing the ServiceNow-driven Check Point firewall software automation workflow.

This document describes the full architecture: ServiceNow request governance, the local workers, the runner, Ansible playbooks, helper scripts, Check Point MDS and gateway interactions, evidence, logs, gates, remediation, and known operational constraints.

The SNOW-Lite Flask application is no longer the primary workflow. The intended operating model is ServiceNow first: a user submits a catalog request, ServiceNow governs the change, local automation validates and executes only after the correct ServiceNow gates are satisfied, and all meaningful phase status is written back into ServiceNow.

## 1. Executive Summary

The workflow automates Check Point firewall software activities while preserving ServiceNow governance.

At a high level:

1. A requester submits the Service Catalog item `CheckPoint FW Maintenance Activity`.
2. ServiceNow creates a REQ and RITM.
3. A ServiceNow business rule creates an automated readiness SCTASK.
4. The local readiness worker validates the request against Check Point MDS, gateways, CPUSE package files, package prerequisites, and cluster health.
5. If readiness passes, ServiceNow creates a governed CHG and the primary Implementation CTASK.
6. If readiness fails, ServiceNow creates a manual Firewall Deploy remediation SCTASK. A Firewall Deploy engineer can remediate and close it to trigger CHG creation.
7. The CHG follows normal approval, assessment, authorization, scheduling, and implement state handling.
8. When the CHG reaches Implement and is approved, the local implementation worker starts the runner.
9. The runner builds an activity plan, performs discovery, runs Ansible playbooks, uses CDT or direct CPUSE methods under the hood, posts phase notes, pauses at tester gates, and produces evidence.
10. On success, the worker creates and closes the final validation CTASK, closes the Implementation CTASK, and moves the CHG to Review.
11. On failure, the worker creates an Engineer Remediation CTASK and waits for a deliberate resume decision.

Every risky action is traceable to a ServiceNow request, a readiness decision, an approved change, a controlled implementation task, and a recoverable execution state.

## 2. Component Map

| Component | Location | Purpose |
| --- | --- | --- |
| Service Catalog item | ServiceNow | User-facing request entry point for Check Point maintenance. |
| REQ | ServiceNow | Parent request record. Contains high-level request tracking. |
| RITM | ServiceNow | Requested item. Carries detailed user-provided firewall activity data and attachments. |
| Automated readiness SCTASK | ServiceNow | System-created validation task picked up by the readiness worker. |
| Manual readiness SCTASK | ServiceNow | Firewall Deploy remediation task created when automated readiness fails. |
| CHG | ServiceNow | Governed change record. Automation executes only when it is approved and in Implement. |
| Implementation CTASK | ServiceNow | Primary implementation driver task. Automation phase notes are mirrored here. |
| Tester validation CTASK | ServiceNow | Human tester gate after first member/failover. Closing it Complete authorizes member 2. |
| Engineer remediation CTASK | ServiceNow | Failure recovery task created by the worker when execution fails. |
| Final validation CTASK | ServiceNow | Worker-created, worker-closed proof that postcheck succeeded. |
| Readiness worker | `servicenow_checkpoint_readiness_worker.py` | Polls automated readiness SCTASKs and performs pre-CHG validation. |
| Implementation worker | `servicenow_checkpoint_worker.py` | Polls approved Implement CHGs, launches/resumes the runner, handles gates and bookkeeping. |
| Runner | `servicenow_checkpoint_runner.py` | Executes one governed CHG through Ansible and Check Point automation phases. |
| Shared Check Point library | `checkpoint_cluster_upgrade.py` | SSH/Clish/Expert interactions, HA parsing, support capture, failover helpers, package helpers. |
| Ansible playbooks | `ansible/playbooks/*.yml` | Phase-level orchestration wrappers around helper scripts. |
| Helper scripts | `ansible/scripts/*.py` | CDT generation/execution, discovery, package validation, postcheck, major upgrade gates, direct CPUSE steps. |
| MDS | Check Point management host | Source of truth for CMA/domain, gateway objects, cluster objects, CDT, CPRID, policy install, package staging. |
| Firewalls | Check Point gateways | Targets for health checks, CPUSE inventory, package install/remove, failover, and support capture. |
| CDT | `/opt/CPcdt/CentralDeploymentTool` on MDS | Main controlled deployment engine for CPUSE package install/remove/upgrade. |
| CPRID | MDS to gateway transport | Used under the hood for MDS-mediated gateway file/log access and staging. |
| Worker state | `runs/worker_state.json` | Local idempotency and resume state for the implementation worker. |
| Run directories | `runs/<CHG>_<timestamp>/` | Per-run plans, vars, logs, attachments, resume state, and summary. |
| Ansible reports | `ansible/reports/` | Cluster state, support capture output, support diffs, and script-generated reports. |

## 3. ServiceNow Flow

### 3.1 Catalog Item

The user-facing catalog item is `CheckPoint FW Maintenance Activity`.

The catalog item is intentionally simple. Users should provide what they want done, when it should happen, and the required package/dependency evidence. The currently certified catalog path does not expose an execution-engine choice and continues to use CDT for software patch and major-version package execution. A separately implemented Management Web API backend can be selected only from the lower-level runner during independent certification. A future catalog field may offer CDT or API after that backend is certified; until then it must not alter the existing ServiceNow behavior. Direct SSH/CPRID remains an internal health, evidence, and support mechanism rather than an end-user package-deployment choice.

Current request model:

- Activity category: `Version upgrade activity`, `Software patch activity`, or `Deployment Agent install`.
- Environment.
- MDS host or management context.
- Target firewall IPs.
- Cluster or standalone information when known.
- Requested maintenance start and end.
- CPUSE Package attachment.
- Dependency checklist attachment.
- Special instructions.

Removed from the normal requester experience:

- Execution method selection.
- Package staging method selection.
- Package source directory on MDS.
- Separate target take field.
- Blink-specific fields.
- Advanced JSON/YAML as the normal path.

The backend assumes packages are available on the MDS under `/var/log/tmp`; Firewall Deploy/readiness validation proves that before the CHG is created.

### 3.2 CPUSE Package Attachment

The CPUSE package attachment is CSV or XLSX.

Typical install example:

```csv
sequence_number,action,package_name,sha1,sha256,reboot_expected
1,install,Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar,53c6bcf11729a009c1b87ef90c0548ae481412a1,,true
```

Typical uninstall example:

```csv
sequence_number,action,package_name,sha1,sha256,reboot_expected
1,remove,Take 91,,,true
```

For install, a full package file name is preferred because the file must be validated on MDS. For uninstall, the runner and CDT helper can resolve common user intent such as `Take 91`, `T91`, or `JHF_T91` to the actual installed CPUSE package reference using gateway CPUSE history and `/opt/CPInstLog` accessed through MDS/CPRID.

### 3.3 Dependency Attachment

The dependency checklist is CSV or XLSX.

Example:

```csv
sequence_number,condition,package_name
1,Present,Check_Point_R82_jumbo_hf_main_Bundle_T60_FULL
1,Not Present,Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL
```

This lets the request or Firewall Deploy validation declare expected preconditions. The validator normalizes `.tar`, `.tgz`, and extensionless tokens where possible. For JHF/take inputs, it also normalizes `Take 91`, `T91`, and `JHF_T91` variants.

### 3.4 REQ and RITM

REQ is the request container. It tracks that a Check Point maintenance activity was requested.

RITM is the detailed technical request record. It should contain the activity details, target IPs, MDS host, schedule, package/dependency attachments, special instructions, and readiness fields. The runner resolves the CHG back to the parent RITM and downloads the original attachments so the implementation is tied to the approved request.

### 3.5 Automated Readiness SCTASK

After RITM creation, a business rule creates an automated readiness SCTASK. The readiness worker picks it up and validates the request before a CHG exists.

Readiness validates:

- CPUSE package attachment exists and parses.
- Dependency attachment parses if present.
- Target firewall IPs exist.
- MDS host exists.
- MDS/CMA/cluster/member/policy context can be discovered.
- Gateway precheck passes.
- Deployment Agent readiness is acceptable.
- Required packages exist on MDS.
- Required checksums match if supplied.
- Package prerequisites are satisfied.

Readiness success:

- Automated SCTASK closes Complete.
- `u_checkpoint_readiness_status=ready`.
- `u_checkpoint_readiness_source=automated`.
- Summary/evidence fields are populated.
- The readiness business rule creates the governed CHG.

Readiness failure:

- Automated SCTASK closes Incomplete.
- `u_checkpoint_readiness_status=failed`.
- A manual Firewall Deploy remediation SCTASK is created.
- No CHG is created yet.

### 3.6 Manual Readiness Remediation SCTASK

This task exists only when automated readiness cannot prove the request is executable.

Typical causes:

- Package missing from `/var/log/tmp` on MDS.
- Checksum mismatch.
- Discovery cannot identify the correct CMA.
- Gateway precheck fails.
- CPUSE inventory does not satisfy dependency rules.
- Deployment Agent is not ready.

Firewall Deploy remediates the issue, marks the task ready, and closes it Complete. That closure triggers CHG creation. If the task is rejected or closed incomplete, the request should not create a CHG.

### 3.7 CHG and CTASKs

The CHG is created only after readiness is ready. It carries the `[CHECKPOINT_AUTOMATION]` marker and links back to the parent RITM.

Key CTASKs:

- `Implementation - Check Point firewall automation workflow`: primary driver task. Phase notes are mirrored here. Closed by automation after final validation.
- `Tester validation gate - Check Point automation`: human tester gate after first member/failover. Closing Complete or Skipped authorizes member 2.
- `Engineer remediation required - Check Point automation at <phase>`: created on execution failure. Closing Complete with approved resume status allows automatic resume.
- `Final validation - Check Point post-implementation checks`: created and closed by automation after final postcheck succeeds.

ServiceNow change-model default tasks such as `Implement` and `Post implementation testing` are not the automation-driving tasks. They are relabeled as model-managed to avoid operator confusion and to avoid breaking the change model.

### 3.8 Approval and Implement

Automation starts only when a governed CHG is approved and moved to Implement. The UI action moves the CHG to Implement; it does not directly run automation. The local worker sees the approved Implement CHG on its next poll and starts the runner.

The runner performs its own governance recheck before touching firewalls. This defense is intentional and already prevented a bad execution when a CHG was accidentally advanced to Review by ServiceNow model-task behavior.

## 4. Local Worker Architecture

### 4.1 Readiness Worker

File: `servicenow_checkpoint_readiness_worker.py`

Systemd unit: `systemd/snow-checkpoint-readiness-worker.service`

Responsibilities:

- Poll ServiceNow for automated readiness SCTASKs.
- Download RITM attachments.
- Parse CPUSE package and dependency files.
- Build a preliminary activity plan.
- Discover Check Point target context.
- Run read-only validation playbooks.
- Close readiness SCTASK ready or failed.
- Create manual remediation SCTASK on failure.

It does not install packages, remove packages, fail over gateways, move CHGs, or execute CDT.

Readiness playbooks currently run:

1. `02_discover_targets.yml`
2. `01_validate_activity_plan.yml`
3. `00_precheck.yml`
4. `07_validate_deployment_agent.yml`
5. `06_validate_mds_package.yml`
6. `08_validate_package_prerequisites.yml` per package step.

### 4.2 Implementation Worker

File: `servicenow_checkpoint_worker.py`

Systemd unit: `systemd/snow-checkpoint-worker.service`

Responsibilities:

- Poll ServiceNow for governed approved CHGs in Implement.
- Enforce idempotency with `runs/worker_state.json`.
- Launch the runner.
- Detect tester gate return code.
- Resume after tester CTASK closure.
- Detect failure return codes.
- Create Engineer Remediation CTASK.
- Resume after approved engineer remediation.
- Perform success bookkeeping.

Worker states include `running`, `waiting_tester`, `waiting_engineer_remediation`, `completed`, `failed`, and `remediation_rejected`.

The worker uses a singleton lock so duplicate service instances do not launch duplicate firewall runs.

### 4.3 Worker State and Logs

State file:

```text
runs/worker_state.json
```

Worker logs:

```text
runs/worker_logs/
```

Per-run logs:

```text
runs/<CHG>_<timestamp>/logs/
```

The state file prevents accidental duplicate execution and records run status, return code, failed phase, final validation task, and bookkeeping outcome.

Security note: persisted state/logs are redacted, but a production hardening gap remains: the live runner command can expose the ServiceNow password in process argv while the runner is active. Move credentials out of argv before production.

## 5. Runner Architecture

File: `servicenow_checkpoint_runner.py`

The runner executes one governed CHG. Normal kickoff is through the implementation worker, not by manually invoking the runner.

### 5.1 Governance Checks

Before parsing packages or touching firewalls, the runner validates:

- CHG exists.
- CHG contains `[CHECKPOINT_AUTOMATION]`.
- CHG state is Implement.
- CHG approval is approved.
- Parent RITM exists.
- Firewall Deploy readiness SCTASK is closed ready.
- Exactly one governed Implementation CTASK exists.
- Duplicate CHG number ambiguity is resolved.

If any check fails, the runner exits before firewall activity.

### 5.2 Attachments and Activity Plan

After governance passes, the runner downloads RITM/CHG attachments into:

```text
runs/<CHG>_<timestamp>/attachments/
```

It parses the CPUSE package file and dependency file, then writes:

```text
runs/<CHG>_<timestamp>/<CHG>_activity_plan.json
runs/<CHG>_<timestamp>/<CHG>_vars.json
```

The activity plan includes change metadata, target context, MDS/CMA/domain, members, package steps, workflow profile, gates, and evidence requirements.

### 5.3 Discovery

Unless skipped, the runner executes `02_discover_targets.yml` to resolve:

- CMA/domain name.
- CMA IP.
- Cluster object.
- Member objects.
- Member management IPs.
- Access/NAT SSH IPs when provided.
- Policy package.

Discovery must select the CMA that owns the firewall objects, not a CLM/logging domain.

### 5.4 Workflow Order

Current high-level runner sequence:

1. `discover-targets` via `02_discover_targets.yml`.
2. `validate-plan` via `01_validate_activity_plan.yml`.
3. `init` via `00_precheck.yml`.
4. `deployment-agent-readiness` via `07_validate_deployment_agent.yml`.
5. `cluster-state-capture` via `11_capture_cluster_state.yml`.
6. `baseline-capture` via `12_support_capture.yml`.
7. For install/upgrade actions: `06_validate_mds_package.yml` and `05_airgap_package_gate.yml`.
8. First member package steps: prerequisites, then CDT generate/execute or direct package step.
9. `failover-to-first` via `23_failover_to_member.yml` for clusters.
10. Tester gate if enabled.
11. Second member package steps.
12. `restore-original-active` via `61_restore_original_active.yml` when configured.
13. `final-support-capture` via `12_support_capture.yml`.
14. `support-diff` via `62_support_diff.yml`.
15. `postcheck` via `60_postcheck.yml`.

For standalone targets, the runner uses only first-member package processing and skips cluster failover/restoration.

Deployment Agent Install is a separate direct-maintenance path, not a rolling firewall package workflow. Its runner sequence is: discovery, validate plan, precheck, DA readiness, MDS package/checksum validation, air-gap/staging acknowledgement, package prerequisite validation for the `install-deployment-agent` phase, direct `installer agent install <path>` against all target members, and DA readiness again. It skips cluster-state capture, support capture, support diff, failover, tester gate, restore-original-active, and final JHF/package postcheck because a DA update can be applied to all gateways without moving traffic ownership.

Major Version Upgrade also uses its own dedicated branch rather than the generic rolling flow. It follows the same readiness/baseline/staging steps, then per package step on the first member: prerequisites, CDT candidate generation, guarded CDT execution. After the first member it runs `mixed-version-policy-gate` (`31_major_policy_gate.yml`) and `mvc-on` (`32_major_mvc.yml`) before `failover-to-first` and the tester gate. After the second member's package steps it runs `final-policy-install` (`31_major_policy_gate.yml`) and `mvc-off` (`32_major_mvc.yml`), then restore-original-active (when configured), final support capture, support diff, and postcheck. Standalone targets are hard-rejected for this activity: the runner raises `Major Version Upgrade currently requires a two-member cluster workflow` at plan-build time. Standalone software patching remains supported via the generic flow (first-member only).

### 5.5 Notes, Failure, Resume, and Success

For each phase, the runner posts a concise CHG note. A ServiceNow mirror business rule copies the authoritative automation notes to the Implementation CTASK.

At tester gate, the runner stops intentionally. The worker records `waiting_tester` and resumes from `second-member` only after the dedicated tester CTASK is closed Complete or Skipped.

On failure, the runner writes:

```text
runs/<CHG>_<timestamp>/resume_state.json
```

The worker uses that to create the Engineer Remediation CTASK and resume from the failed phase after approval.

On success, the runner writes:

```text
runs/<CHG>_<timestamp>/summary.json
```

The worker then creates/closes the final validation CTASK, closes the Implementation CTASK, and moves the CHG to Review.

## 6. Ansible Playbook Reference

### `02_discover_targets.yml`

Discovers MDS/CMA/domain, cluster, members, management IPs, access IPs, and policy package. Primary helper: `ansible/scripts/discover_checkpoint_targets.py`.

### `01_validate_activity_plan.yml`

Validates activity plan structure and required fields before execution proceeds.

### `00_precheck.yml`

Runs gateway health checks through `checkpoint_cluster_upgrade.py --phase precheck`.

Checks include SSH, hostname, ClusterXL state, one-active-member requirement, pnotes, monitored interface state, required interface counts, virtual cluster interfaces, and ICAP when configured.

Interface parser behavior matters: annotations such as `(S)` or `(S-LS)` are metadata, not failures. Actual failures are statuses such as `Inbound: DOWN` or `Outbound: DOWN`.

### `07_validate_deployment_agent.yml`

Validates Deployment Agent readiness and DA package metadata through `ansible/scripts/validate_deployment_agent.py`.

### `08_validate_package_prerequisites.yml`

Validates step-specific `requires_present` and `requires_absent` checks through `ansible/scripts/validate_package_prerequisites.py`. It normalizes `.tar`, `.tgz`, extensionless names, and take-token variants.

### `11_capture_cluster_state.yml`

Captures initial active/standby state, member mapping, and interface signature using `ansible/scripts/cluster_phase_control.py`. This is used later for failover, restore, and postcheck comparison.

### `12_support_capture.yml`

Runs `ansible/scripts/gateway_support_commands.example.sh` against gateways and stores command output as baseline/final evidence.

### `06_validate_mds_package.yml`

Confirms package files exist on MDS and match supplied checksums. Primary helper: `ansible/scripts/validate_mds_packages.py`.

### `05_airgap_package_gate.yml`

Confirms or stages package availability according to backend staging policy. In the ServiceNow model, users do not choose staging; backend expects MDS-mediated staging with CPRID as the standard path.

### `10_cdt_generate_candidates.yml`

Generates CDT plan XML, runs CDT `-generate`, parses all candidates, and creates a controlled candidate file. Primary helper: `ansible/scripts/generate_cdt_candidates_from_activity.py`.

Important safety property: CDT may return every gateway in the CMA/domain. The helper must select exactly one intended target for the current member phase and disable all other rows.

### `20_cdt_execute_guarded.yml`

Executes CDT with the controlled candidate file. Primary helper: `ansible/scripts/execute_cdt_from_activity.py`. Execution requires explicit approval variable and controlled candidate validation.

### `23_failover_to_member.yml`

Fails over to the first changed member so testers can validate traffic on it before member 2 is touched. Uses `ansible/scripts/cluster_phase_control.py`.

### `22_monitor_gateways.yml`

Samples gateway state/take during operations where reboot or reconnect behavior matters. Primary helper: `ansible/scripts/monitor_gateways.py`.

### `25_check_jhf_installed.yml`

Validates JHF take state for flows that require explicit JHF confirmation. Final package state is primarily enforced by `60_postcheck.yml`.

### `30_direct_package_step.yml`

Runs direct CPUSE/Clish package actions when CDT is not the right backend. Primary helper: `ansible/scripts/direct_package_step_from_activity.py`.

### `31_major_policy_gate.yml`

Handles major upgrade policy gate operations such as cluster version setting and policy install. Primary helper: `ansible/scripts/major_policy_gate_from_activity.py`.

### `32_major_mvc.yml`

Handles major-version upgrade mechanics where MVC is involved. Primary helper: `ansible/scripts/major_mvc_from_activity.py`.

### `61_restore_original_active.yml`

Restores original active member after both members are complete, if configured. Uses `cluster_phase_control.py`.

### `62_support_diff.yml`

Compares baseline and final support captures using `checkpoint_cluster_upgrade.py --phase support-diff`.

### `60_postcheck.yml`

Final post-implementation validation. Primary helper: `ansible/scripts/postcheck_gateways.py`.

Checks include connectivity, cluster state, pnotes, interface health, ICAP, target take, package present/absent expectations, and baseline interface signature consistency.

Key lesson: do not validate uninstall success by searching for the original MDS `.tar` path in CPUSE inventory. CPUSE reports installed/imported display names, often `.tgz`. Postcheck must validate normalized CPUSE display tokens and final-state expectations.

## 7. Helper Script Reference

### `checkpoint_cluster_upgrade.py`

Common Check Point SSH/Clish/Expert utility. It handles SSH PTY connections, cluster state parsing, `cphaprob -a if` parsing, pnotes, ICAP checks, support capture, support diff, failover helpers, and direct CPUSE package helpers.

### `discover_checkpoint_targets.py`

Discovers domains, gateways, clusters, members, and policy package from an MDS. It paginates and scans every regular domain, selects an authoritative active CMA while excluding logging servers, and requires all requested addresses to resolve to exactly one managed object. A standalone Security Management Server is intentionally not accepted by this MDS resolver: when `show domains` returns no regular domains, discovery fails closed. Standalone-SMS support requires a separate explicit resolver mode and must not be emulated with a preferred-domain fallback.

### `generate_cdt_candidates_from_activity.py`

Builds CDT deployment plan XML, resolves install/remove package references, reads CPUSE history and `/opt/CPInstLog` through MDS/CPRID where needed, parses CDT candidates, selects the target, and disables all non-target rows.

### `execute_cdt_from_activity.py`

Runs CDT execute using the controlled plan and candidate file.

### `cluster_phase_control.py`

Parses/renders CDT candidate files, collects/writes cluster state, prepares candidates, performs failover, restores original active, and asserts member take.

### `validate_mds_packages.py`

Validates MDS package file existence and checksums.

### `validate_package_prerequisites.py`

Validates required-present and required-absent package dependencies against gateway CPUSE inventory with token normalization.

### `postcheck_gateways.py`

Builds final package expectations and verifies installed/removed package state after the workflow.

### `direct_package_step_from_activity.py`

Runs direct CPUSE package install/remove, handles interactive uninstall, detects blocked hotfixes, waits for reboot/reconnect, and waits for cluster readiness.

### `validate_deployment_agent.py`

Validates Deployment Agent build and package metadata.

### `monitor_gateways.py`

Samples gateway version/take and cluster state.

### `major_policy_gate_from_activity.py`

Handles policy install and cluster version gates during major upgrades.

### `major_mvc_from_activity.py`

Handles major-version upgrade mechanics for MVC-related flows.

## 8. Check Point MDS Role

The MDS is the management-side automation anchor.

It is used for:

- Domain/CMA discovery.
- Gateway and cluster object discovery.
- Policy package discovery.
- CDT plan generation and execution.
- CDT candidate generation.
- Package presence validation under `/var/log/tmp`.
- CPRID-based gateway access.
- Gateway log/history access without direct user login.
- Policy gate operations during major upgrades.

The MDS matters because it knows management object identities, CDT runs from MDS/CMA context, and production firewall management IPs may differ from direct SSH/NAT access IPs.

### NAT/Access IP vs Management IP

The activity plan supports both MDS management IP and SSH access/NAT IP.

Example:

- SSH access IP: `10.33.120.48`
- MDS management IP: `20.0.0.4`

Health checks can use access IP, while CDT candidate selection uses management IP.

## 9. Firewall Role

Firewalls provide ground truth for:

- HA state.
- Interface state.
- Pnotes.
- ICAP state.
- CPUSE installed/imported package inventory.
- CPUSE history.
- `/opt/CPInstLog`.
- Reboot/reconnect behavior.

Common commands and sources:

- `show version all`
- `show installer packages`
- `show installer packages installed`
- `cphaprob state`
- `cphaprob -a if`
- `cpwd_admin list`
- CPUSE command output
- `/opt/CPInstLog`

### CPUSE Inventory Lesson

`show installer packages installed` often reports `.tgz` display names even when the source file used on MDS was `.tar`. Therefore install/remove validation must normalize package names and should not rely on original source path alone.

### `/opt/CPInstLog`

`/opt/CPInstLog` contains package history useful for resolving uninstall references. This is more production-reliable than `messages`, which can rotate quickly.

## 10. CDT Methodology

CDT is the preferred backend for controlled Check Point package deployment/removal when applicable.

CDT workflow components:

- Deployment plan XML.
- Candidates CSV.
- Controlled candidates CSV.
- MDS/CMA context.
- Package reference.
- CDT logs under `/var/log/CPcdt/logs_<timestamp>/`.

For install, the plan references the package file. For uninstall, the plan references the installed CPUSE package identity, often `.tgz`.

Candidate generation can return every gateway in a domain. The automation parses all candidates and writes a controlled candidate file where exactly one intended gateway has upgrade order `1`; every other row is disabled. This is how the workflow prevents accidental execution on unrelated gateways.

First member selection generally targets the standby member. After failover, second member selection targets the remaining standby member.

Operational observations from controlled testing:

- Wrapper install/remove may not trigger reboot even when `reboot_expected=true`.
- JHF removal is usually expected to reboot, but behavior must be observed rather than assumed.
- Final validation must be based on actual package/take state and cluster health.

## 10A. Separate Management Web API Methodology

The lower-level runner also has a separately certified Management Web API backend selected with `--deployment-backend api`. This is not currently exposed by the ServiceNow catalog; CDT remains the governed default. The API backend and CDT backend have separate runner branches and playbooks.

API components:

- `39_api_repository_package.yml`: imports a local MDS package into the Global Central Deployment repository.
- `40_api_verify_package.yml`: asks the API to verify eligibility against the full cluster object.
- `41_api_execute_package.yml`: issues the authorized install/upgrade task and polls its task ID.
- `management_api_package_from_activity.py`: handles repository pagination/envelopes, identity resolution, strategy selection, task polling, MDS capacity gates, and fail-closed major-upgrade reconciliation.

Read operations are synchronous. Mutations use `--sync false`, require the API execution approval variable and helper `--execute`, and are followed by synchronous `show-task` polling. Patch installs use the cluster strategy `non-active-members-and-failover`. Major member one and member two use `non-active-members-no-failover`; the existing policy/MVC/failover phases control the mixed-version interval explicitly.

The tested API cannot safely select one member for cluster uninstall. The API workflow therefore performs request/identity validation through API and CPRID but uses guarded direct CPUSE for the actual rolling removal. It never falls back to CDT.

Before repository import, `/var/log` must have package-sized conversion workspace plus reserve. Before package execution, root must retain a reserve and root-free plus existing Central Deployment cache must satisfy the package workspace requirement. A major API task that reports terminal failure after reboot is not blindly retried: CPRID checks both gateways in CMA context before execution and after failure. The phase must start with the expected prior completed-member count and advance by exactly one member to the requested Gaia release and exact Blink identity.

Controlled certification demonstrated Take 76 install/removal and R81.20-to-R82 completion. The removal used the documented direct fallback. Final R82 policy, MVC-off, ownership restoration, support evidence, PNOTEs, interfaces, ICAP, version, and Take checks passed. ServiceNow exposure remains future work requiring a catalog choice, worker propagation, governance testing, and independent review.

## 11. Direct CPUSE and SSH Methodology

Direct CPUSE/SSH is available under the hood for cases where CDT is not appropriate, especially Deployment Agent installation and controlled troubleshooting flows. Users should not select the execution engine from the catalog; the backend chooses this path for Deployment Agent Install.

For Deployment Agent Install, `direct_package_step_from_activity.py` runs `installer agent install <path>` and then `show installer status all`. When multiple members are targeted, DA install/upgrade actions are launched concurrently in the single `install-deployment-agent` phase. Other direct package actions retain sequential behavior.

Direct flow handles:

- CPUSE command execution.
- Interactive uninstall prompts.
- Blocking hotfix detection.
- Reboot wait.
- SSH reconnect wait.
- Cluster readiness wait.
- Final state validation.

## 12. Package Resolution and Validation

Package actions:

- `install`
- `remove`
- `upgrade`

Package types:

- `jhf`
- `wrapper`
- `blink`
- `deployment_agent`
- `other`

JHF uninstall should accept reasonable operator input such as `Take 91`, `T91`, `JHF_T91`, full package name, `.tar`, `.tgz`, or extensionless package name. Wrapper removal should preferably use the full wrapper name because wrapper naming is specific.

Before a step, prerequisites validate required-present and required-absent tokens. After all steps, postcheck validates final present/absent expectations from package actions and dependency rows.

## 13. Evidence and Logs

ServiceNow evidence:

- RITM details and attachments.
- Readiness SCTASK status and notes.
- Manual readiness remediation notes if applicable.
- CHG approvals and state history.
- Implementation CTASK phase notes.
- Tester validation CTASK decision.
- Engineer remediation CTASK decision if applicable.
- Final validation CTASK.
- CHG work notes.

Local run evidence:

```text
runs/<CHG>_<timestamp>/
```

Common contents:

- `attachments/`
- `logs/`
- `<CHG>_activity_plan.json`
- `<CHG>_vars.json`
- `summary.json`
- `resume_state.json` on failure

Ansible reports:

```text
ansible/reports/
```

MDS logs:

```text
/var/log/CPcdt/logs_<timestamp>/
/var/log/tmp/<CHG>_*_cdt_plan.xml
/var/log/tmp/<CHG>_*_cdt_candidates.csv
```

Gateway evidence:

- CPUSE inventory.
- `/opt/CPInstLog`.
- Support capture command output.
- Cluster command output.

## 14. Remediation Model

### Readiness Remediation

Happens before CHG creation. Failed automated readiness creates manual Firewall Deploy SCTASK and blocks CHG creation until remediated.

### Implementation Remediation

Happens after CHG reaches Implement and runner execution fails. The worker creates an Engineer Remediation CTASK, records failed phase/playbook/step/log, and waits. Resume requires approved resume status plus Closed Complete/Skipped state.

Auto-retry is intentionally avoided because a failed firewall change may indicate real cluster, package, or management drift.

## 15. Success Criteria

A successful run should show:

- Automated readiness SCTASK Closed Complete.
- CHG created with automation marker.
- CHG approved and moved to Implement.
- Implementation CTASK open during execution.
- Phase notes posted.
- First member package action complete.
- Failover complete.
- Tester validation CTASK Closed Complete or Skipped.
- Second member package action complete.
- Original active restored when configured.
- Final support capture complete.
- Support diff complete.
- Postcheck complete.
- Final validation CTASK created and Closed Complete.
- Implementation CTASK Closed Complete.
- CHG moved to Review.
- Worker state `completed` with rc `0`.

The operator still performs normal change closure from Review to Closed with a successful close code.

## 16. Known Live Validation Milestones

### CHG_EXAMPLE

Proved the governed install path: catalog intake, automated readiness, BR-created CHG, real approvals, Implement worker pickup, CDT execution, tester gate pause/resume, and both members completed. It exposed a postcheck issue where the validator compared the MDS `.tar` path against CPUSE inventory display names; that was corrected.

### CHG_EXAMPLE

Proved the governed uninstall path and automated success bookkeeping: REQ/RITM intake, automated readiness, CHG approval chain, Implement transition, worker pickup, CDT Take 91 uninstall, tester gate, final postcheck, final validation CTASK creation/closure, Implementation CTASK closure, and move to Review.

It also exposed the default CTASK suppression bug. The runner refused to execute while the CHG was incorrectly in Review, proving the governance recheck is meaningful.

## 17. Known Pitfalls

- Do not close default ServiceNow change-model CTASKs on insert; relabel them instead.
- Tester gate matching must use the dedicated `Tester validation gate` task, not loose words like `validation`.
- CPUSE inventory is not the MDS source filename.
- Use `/opt/CPInstLog`, not only `messages`, for package history.
- CHG Implement is the start signal; CTASK closure alone should not start firewall changes.
- Restart systemd workers after code changes.

## 18. Production Hardening Checklist

Before production, address:

- Remove ServiceNow password from live runner argv.
- Use a dedicated ServiceNow integration account instead of admin.
- Use CyberArk or equivalent vault for MDS and firewall credentials.
- Support separate MDS Expert and gateway Expert passwords.
- Add ACLs around readiness/resume fields.
- Run a deliberate live failure drill for engineer remediation and resume.
- Define operational ownership, escalation, and change closure procedure.

Protected fields should include:

- `u_checkpoint_readiness_status`
- `u_checkpoint_readiness_source`
- `u_checkpoint_readiness_summary`
- `u_checkpoint_readiness_evidence`
- `u_checkpoint_resume_status`
- `u_checkpoint_resume_phase`
- `u_checkpoint_resume_summary`
- `u_checkpoint_resume_evidence`

## 19. Operational Runbook

Normal path:

1. User submits catalog request and uploads CPUSE/dependency files.
2. Automated readiness SCTASK is created.
3. Readiness worker validates request.
4. If ready, CHG is created. If not ready, manual readiness remediation SCTASK is created.
5. CHG is reviewed, assessed, authorized, scheduled, and approved.
6. Authorized user moves CHG to Implement.
7. Worker launches runner.
8. Runner executes first member and failover.
9. Tester closes tester gate CTASK Complete.
10. Worker resumes second member.
11. Runner performs final support capture, support diff, and postcheck.
12. Worker creates final validation CTASK, closes Implementation CTASK, and moves CHG to Review.
13. Change manager closes CHG successful after review.

Failure path:

1. Runner fails at a phase.
2. Runner writes `resume_state.json`.
3. Worker creates Engineer Remediation CTASK.
4. Engineer fixes issue.
5. Engineer sets resume status approved and closes Complete.
6. Worker resumes from failed phase.

## 20. Troubleshooting

No readiness SCTASK:

- Check catalog item business rule.
- Check RITM active state.
- Check business rule compile health.
- Use a real test request; ServiceNow may silently skip broken BR scripts.

Readiness stuck:

- Check readiness worker service.
- Check env credentials.
- Check worker logs.
- Check RITM attachments.
- Check MDS connectivity.

CHG not created:

- Check readiness fields.
- Check automated or manual readiness SCTASK closure state.
- Check readiness CHG creation business rule.

CHG not starting:

- Check marker, approval, Implement state, Implementation CTASK, worker service, and worker state.

Paused at tester gate:

- Close `Tester validation gate - Check Point automation` Complete or Skipped after validation.

Engineer remediation created:

- Review failed phase/playbook/step/log.
- Remediate.
- Set resume status approved.
- Close Complete.

## 21. Glossary

| Term | Meaning |
| --- | --- |
| CDT | Check Point Central Deployment Tool. |
| CPUSE | Check Point upgrade and package management system. |
| MDS | Multi-Domain Server. |
| CMA | Customer Management Add-on/domain management server. |
| CLM | Customer Log Module/logging domain. Should not be selected as firewall management CMA. |
| CPRID | Check Point remote installation/communication mechanism used from management to gateways. |
| JHF | Jumbo Hotfix. |
| Wrapper | Check Point hotfix wrapper package. |
| DA | Deployment Agent. |
| RITM | ServiceNow Requested Item. |
| SCTASK | ServiceNow Catalog Task. |
| CHG | ServiceNow Change Request. |
| CTASK | ServiceNow Change Task. |
| Pnotes | Check Point problem notification status. |
| ICAP | Internet Content Adaptation Protocol process/listener check used as an environment-specific health gate. |

## 22. Current Confidence

Proven live:

- Governed ServiceNow catalog intake.
- Automated readiness success path.
- CHG creation from readiness.
- Approval-driven Implement start.
- Worker pickup.
- CDT install path.
- CDT uninstall path.
- Tester gate pause/resume.
- Final postcheck.
- Final validation CTASK creation.
- Implementation CTASK closure.
- Move to Review.

Implemented and unit-tested, but still needs a deliberate live drill:

- Engineer remediation CTASK failure/resume loop.

Still required before production:

- Remove credentials from runner argv.
- Use dedicated integration account.
- Integrate CyberArk or equivalent vault.
- Lock down readiness/resume fields with ACLs.
- Exercise live failure/resume.
- Document production operational ownership and escalation.
