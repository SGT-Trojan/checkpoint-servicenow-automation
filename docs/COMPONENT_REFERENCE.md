# Component and Integration Reference

Use this guide when you want one script or playbook instead of the full
ServiceNow workflow. If you are new to the project, read [Start Here](START_HERE.md)
first.

The tables list the exact command options, JSON keys, YAML variables, outputs,
and return codes. The examples use addresses reserved for documentation.
Replace every sample address, name, path, package, and checksum. Start with
read-only commands in a non-production environment.

## 1. Choose What to Reuse

| Goal | Use this | What you provide |
|---|---|---|
| Add Check Point discovery or validation to an existing program | Call a helper in `ansible/scripts/` | CLI arguments plus a JSON activity plan where required |
| Add repository phases to an existing Ansible project | Import or invoke a playbook in `ansible/playbooks/` | Ansible extra variables, normally YAML, plus credentials in environment variables |
| Replace the repository playbooks but keep their logic | Call helpers from your own tasks | Preserve the helper arguments, plan schema, output files, return codes, and execution gates |
| Keep the playbooks but construct plans elsewhere | Produce the documented activity-plan JSON | Pass both `activity_plan_file` and parsed `activity_plan` to plan-aware playbooks |
| Use the complete governed workflow | Run the worker and runner | See the ServiceNow build guide and workflow walkthrough |

Python helpers that accept `--activity-plan-file` call `json.loads`; the file
must therefore be JSON. Ansible variable files may be YAML or JSON. A playbook
that needs plan fields generally expects two variables:

- `activity_plan_file`: absolute path to the JSON plan on the controller.
- `activity_plan`: the same JSON decoded into an Ansible mapping.

You must pass both values. Supplying one does not create the other.

## 2. Where Commands Run and How They Get Credentials

Run the shipped playbooks on `localhost`; the helpers create their own SSH PTY
sessions to Gaia or the MDS. They do not use Ansible's SSH connection to manage
the gateways.

| Name | Meaning |
|---|---|
| `CP_PASSWORD` | Gaia/Clish password for the selected `--username` |
| `CP_EXPERT_PASSWORD` | Expert password used for expert-mode checks and management commands |
| `CP_SSH_PROXY` | Optional HTTP CONNECT proxy URL used by `checkpoint_cluster_upgrade.py` |
| `CP_SSH_PROXY_USER` | Optional proxy user |
| `CP_SSH_PROXY_PASSWORD` | Optional proxy password; select a different variable with `--ssh-proxy-password-env` |

Keep secrets in a vault or protected environment file. Never put them in an
extra-vars file, activity plan, command argument, Git history, or retained log.
The current reference implementation uses one Gaia and one Expert credential
across MDS and gateways. Split credential lookup by system in an environment
where those credentials differ.

## 3. Activity-Plan JSON

[`examples/activity_plans/read-only-validation.json`](../examples/activity_plans/read-only-validation.json)
is a runnable offline structure example.
[`examples/activity_plans/patch-install.json`](../examples/activity_plans/patch-install.json)
is a non-executable package template. Its package and checksum fields are placeholders
so it fails before a change until the operator supplies approved values.

### 3.1 Top-level keys

| Key | Required by | Meaning and accepted values |
|---|---|---|
| `schema_version` | Plan validation | Contract version; currently `1.0` |
| `generated_at` | Audit consumers | ISO-8601 generation timestamp |
| `change` | Plan validation and report names | Change metadata described below |
| `checkpoint` | All target-aware helpers | Management, cluster, member, version, and health expectations |
| `execution` | Backend and staging helpers | Backend, staging, gates, and support-capture settings |
| `package_steps` | Package helpers | Ordered list of package operations; names must be unique |
| `workflow_gates` | Plan validation/runner | Gate declarations; sequence may be empty for a custom caller |
| `evidence` | Plan validation/runner | Evidence requirements; `diff_required` must be defined |

### 3.2 `change`

| Key | Required | Meaning and accepted values |
|---|---|---|
| `number` | Yes | Stable run/change label. It must equal Ansible variable `chg_number` when `01_validate_activity_plan.yml` is used. |
| `activity_type` | Yes | `Software Patch Activity`, `Major Version Upgrade`, or `Deployment Agent Update` |
| `state` | No | Caller-owned lifecycle state; the runner normally writes `Implement` |
| `environment` | No | Caller label such as `lab`, `test`, or `production`; it does not weaken gates |

### 3.3 `checkpoint`

| Key | Required | Meaning and accepted values |
|---|---|---|
| `current_version` | For package/version checks | Exact source release, for example `R81.20` |
| `target_version` | For package/version checks | Exact intended release; equals current release for a JHF |
| `target_take` | Postcheck for installs | Expected JHF Take as a string or number; removal-only plans may leave it empty when `package_steps` defines expected absence |
| `cluster_name` | CDT/API/policy operations | Authoritative management object name, never an individual member name |
| `cluster_mode` | Backend selection | `cluster` or `standalone`; API and major workflows currently require `cluster` with exactly two members |
| `mds_host` | MDS helpers | SSH-reachable MDS address or name |
| `cma_name` | MDS shell context | Authoritative Multi-Domain Server/CMA server name used by `mdsenv` |
| `domain` | Management API calls | Management domain name passed to `mgmt_cli -d`; this is distinct from `cma_name` |
| `cma_ip` | CDT generation | Authoritative active CMA address |
| `target_ips` | Discovery/audit | Requested gateway/member addresses |
| `policy_package` | Major policy gates | Existing policy package installed on the matched cluster |
| `members` | All cluster helpers | One or two member mappings described below; certified rolling flows require exactly two |
| `preserve_original_active` | Runner | Boolean controlling final ownership restoration |
| `original_active_member` | State consumers | Normally empty in the plan and established by captured state |
| `require_one_active_member` | Health logic | Boolean; normally true for a cluster |
| `icap_mode` | Health helpers | `required`, `optional`, or `disabled` |

Each `members` entry accepts these keys:

| Key | Required | Meaning |
|---|---|---|
| `slot` | Recommended | Stable label such as `member_a` or `member_b` |
| `hostname` | Yes | Real gateway object/host name; never fabricate one |
| `ip` | Yes | Address used for gateway SSH unless `access_ip` is supplied |
| `management_ip` | Recommended | Address recorded on the management object |
| `access_ip` | Recommended | Address reachable from the automation host |

### 3.4 `execution`

| Key | Required | Meaning and accepted values |
|---|---|---|
| `method` | Some staging logic/reports | Descriptive method: `CDT (Central Deployment Tool)`, `Management Web API Central Deployment`, or `Direct CPUSE/Clish` |
| `deployment_backend` | Backend playbooks and standalone workflow | `cdt`, `api`, `direct`, or `standalone`; selecting a value does not itself authorize execution |
| `staging_method` | Plan validation/staging | `cprid_from_mds` for automatic MDS-to-gateway copy, or a caller-defined manual method paired with explicit staging confirmation |
| `package_source_dir` | Plan construction | MDS directory containing approved packages, normally `/var/log/tmp` |
| `tester_pause` | Runner | Boolean controlling the first-member tester gate |
| `support_capture_script` | Support capture | Absolute controller path to the reviewed read-only command script |
| `minimum_deployment_agent_build` | DA validation, optional | Minimum numeric Deployment Agent build |
| `deployment_agent_package_path` | DA validation, optional | Approved offline DA package path on MDS |
| `deployment_agent_package_build` | DA validation, optional | Build represented by that offline package |

### 3.5 `package_steps[]`

| Key | Required | Meaning and accepted values |
|---|---|---|
| `order` | Recommended | Integer execution order |
| `name` | Yes | Unique identifier passed as `--step`; use letters, numbers, `.`, `_`, or `-` |
| `action` | Recommended | `install`, `upgrade`, `remove`, or `uninstall`; blank defaults to `install`, while any unknown non-empty value is rejected |
| `package_name` | Yes | Approved package filename. Ticket CSV values may use letters, numbers, `.`, `_`, `+`, or `-`; whitespace and shell/clish metacharacters are rejected |
| `package_type` | Recommended | `jhf`, `blink`, `deployment_agent`, or the locally supported wrapper type |
| `target_build` | Major/Blink | Exact positive OS build expected after a major upgrade; ticket CSVs may declare it explicitly and approved Blink filenames may carry it as `R<release>_T<build>` |
| `source_path` | Install/upgrade | Absolute package path on MDS. Ticket CSV values use the same filename characters plus `/`; relative paths and `.` or `..` segments are rejected |
| `dest_path` | Direct staging, optional | Gateway destination directory; normally `/var/log/tmp` |
| `checksum_sha1` | Install/upgrade | Published 40-character lowercase hexadecimal SHA-1; at least one approved hash is mandatory |
| `checksum_sha256` | Install/upgrade | Published 64-character lowercase hexadecimal SHA-256; preferred when available |
| `requires_present` | Optional | Package identities that must already be installed; this is not an uninstall list |
| `requires_absent` | Optional | Package identities that must not be installed before this step |
| `reboot_expected` | Recommended | Boolean describing expected reboot behavior |
| `requested_build` | Deployment Agent | Positive integer minimum compatibility floor. An equal or higher observed build is an authorized idempotent no-op; only a lower unique build is updated, and the workflow never downgrades |
| `notes` | Optional | Non-secret operator context |

For removal, put the approved filename or alias in `package_name` with
`action: remove`. That ticket value is a search input only. Before CDT
candidate generation, CPRID reads gateway CPInstLog data and must resolve
exactly one installed package identity; zero or multiple identities stop the
workflow. Only that resolved identity can become the uninstall selector. Do
not put the removal target in `requires_present`; that list represents true
prerequisites.

## 4. Python Helper CLI Reference

Run `python3 <path> --help` for parser-generated usage. The table below explains
how the switches combine and which operations can change systems.

| Program | Required interface | Optional switches and accepted values | Change scope |
|---|---|---|---|
| `checkpoint_cluster_upgrade.py` | `--phase`; `--members GW1 GW2` except support diff/analyze | `--phase precheck|download-verify|support-capture|support-diff|support-analyze|failover-test|rolling`; `--package`; `--target standby|HOST`; rolling also requires `--target-version` and `--target-take`; `--icap-mode required|optional|disabled`; timeouts, proxy, support and diff paths | `download-verify`, `failover-test`, and `rolling` require `--execute`; always pass an explicit `--package` for package phases |
| `checkpoint_standalone_workflow.py` | phase, `--activity-plan-file`, `--run-dir` | Phases: `validate`, `capture-state`, `baseline-capture`, `stage-files`, `first-member`, `mixed-version-policy`, `mvc-on`, `failover-to-first`, `simulate-tester-gate`, `second-member`, `final-policy`, `mvc-off`, `restore-original-active`, `final-capture`, `postcheck`, `show-state`; `--username`; gate `--evidence`; member retry `--host-key-evidence` | Runs Python helpers only; every phase except local validation/state display requires `--execute`, exact journal order, and a nonblocking run lock; member phases create private `mutation-intents/<phase>.json` evidence and become reconciliation-only after intent publication |
| `discover_checkpoint_targets.py` | `--mds-host`, `--target-ips` | `--username`; `--preferred-domain`; `--output`; persistence requires both `--db-path` and positive `--change-id` | Read-only unless persistence is requested; scans all domains before returning |
| `validate_mds_packages.py` | `--activity-plan-file` | `--username` | Read-only |
| `validate_deployment_agent.py` | `--activity-plan-file` | `--username`; `--minimum-build`; `--offline-package-path`; `--offline-package-build` | Read-only |
| `validate_package_prerequisites.py` | `--activity-plan-file`, `--reports-dir`, `--phase`, `--step` | `--username` | Read-only |
| `generate_cdt_candidates_from_activity.py` | `--activity-plan-file`, `--phase`, `--resolution-output`, `--operation-id` | `--step`; `--plan-path`; `--candidates-path`; `--target-policy standby|active`; `--target-ip`; `--username` | Generates/re-writes plan and candidate files on MDS and writes a private plan/phase/member-bound context; removals require one CPInstLog/CPRID identity |
| `execute_cdt_from_activity.py` | `--activity-plan-file`, `--plan-path`, `--candidates-path` | `--step`; `--username`; `--timeout` | Prints plan and returns 3 without `--execute`; deploys with `--execute` |
| `governed_cdt_artifacts.py` | Imported library; no CLI | None | Shared owner/mode/symlink-safe atomic JSON read/write and digest helpers for governed CDT artifacts |
| `record_cdt_mutation.py` | `--activity-plan-file`, `--context-file`, `--operation-id`, `--phase`, `--step`; `--receipt-file` unless validating only | `--validate-only` | Records successful CDT return against the exact protected candidate context; does not inspect or change gateways |
| `reconcile_cdt_member.py` | `--activity-plan-file`, `--context-file`, `--receipt-file`, `--evidence-file`, `--operation-id`, `--phase`, `--step` | `--username`; `--timeout` | Read-only exact-member release/Take/build or resolved-package-absence gate; fails on missing, stale, or mismatched artifacts |
| `direct_package_step_from_activity.py` | `--activity-plan-file`, one of `--reports-dir` or `--state-file`, `--phase`, `--step` | `--username`; `--timeout`; `--auto-reboot-grace`; `--explicit-reboot-fallback`; mutually exclusive `--reconciliation-file PATH` or `--reconciliation-fd FD`; standalone coordinator options `--mutation-intent-file`, `--operation-id`, `--mutation-intent-dir`, `--standalone-run-id`, `--standalone-plan-sha256`, `--standalone-phase`, `--standalone-operation-id`, `--standalone-completion-id`, `--standalone-event-nonce`, and `--standalone-reconciliation-only` | Requires `--execute`; non-interactive commands require a captured exit status, install/upgrade requires exact release/Take/package reconciliation, and interactive removal requires exact package absence after reboot; a persisted mutation intent is plan-bound and permanently prohibits redispatch |
| `cluster_phase_control.py` | action, `--members GW1 GW2`, `--state-file` | Actions: `capture-state`, `prepare-candidates`, `failover-to`, `restore-original-active`, `assert-member-take`; `assert-member-take` requires `--target-host` and `--target-take`; other action-specific options are `--mds-host`, `--source-candidates`, `--dest-candidates`, `--phase phase1|phase2`, `--failover-wait-seconds`, and `--icap-mode` | Capture/assert are read-only; prepare writes files; failover/restore change ClusterXL state |
| `major_policy_gate_from_activity.py` | `--activity-plan-file`, `--phase mixed-version-policy-gate|final-policy-install` | `--username` | Changes cluster version/policy state |
| `major_mvc_from_activity.py` | `--activity-plan-file`, one of `--reports-dir` or `--state-file`, `--phase mvc-on|mvc-off` | `--username` | Changes MVC state only after exact command RC and `cphaprob mvc` readback agree |
| `management_api_package_from_activity.py` | `--activity-plan-file`, `--step`, `--phase stage-files|first-member|second-member`, `--operation repository|verify|execute` | `--global-domain`; `--username`; `--timeout` | Repository import mutates management repository; package execution additionally requires `--execute`; verify is read-only |
| `monitor_gateways.py` | `--members GW1 GW2` | `--interval`; `--samples`; `--icap-mode required|optional|disabled`; `--include-take`; `--output-jsonl`; `--username` | Read-only; returns 2 if any sample is unhealthy or unreadable |
| `postcheck_gateways.py` | `--members GW1 GW2`, `--target-take` | `--absent-take`; `--icap-mode`; `--state-file`; `--activity-plan-file`; `--username` | Read-only final verdict |
| `stage_packages_cprid.py` | `--activity-plan-file` | `--username` | Copies approved package files from MDS to plan members when `staging_method` is `cprid_from_mds` |

`--reconciliation-fd` is the standalone orchestration transport. The coordinator
creates the private mode-0600 regular file itself, keeps that exact file
descriptor open, inherits it into the member helper, and validates the same
descriptor after the helper returns. This prevents a pathname replacement from
redirecting the helper's result. `--reconciliation-file` remains available for
governed callers that own and validate a pathname; the two switches cannot be
combined. The standalone-only `--standalone-event-nonce` binds one random
dispatch attempt end to end. On a retry, the coordinator reuses that pending
identity and adds `--standalone-reconciliation-only`, which permits exact
target-state reconciliation but prohibits a second mutation dispatch even if
the earlier helper stopped before publishing its protected intent.

Supply `CP_PASSWORD` and `CP_EXPERT_PASSWORD` to Check Point helpers unless the
selected offline phase does not connect. Argument-parser usage errors commonly
return 2; resolver-specific exits are 2 not found, 3 ambiguous, 4 incomplete or
transport/auth failure, and 64 invalid invocation.

### 4.1 `checkpoint_cluster_upgrade.py` complete option set

| Switch | Meaning |
|---|---|
| `--members GW1 GW2` | Exactly two member addresses for connected phases |
| `--username` | Gaia login; default `admin` |
| `--password-env` | Environment variable containing the Gaia password; default `CP_PASSWORD` |
| `--expert-password-env` | Environment variable containing the Expert password; default `CP_EXPERT_PASSWORD` |
| `--package` | Explicit CPUSE package identity; do not rely on the historical default |
| `--target-version` | Expected Gaia release; required for `rolling` and checked before failover |
| `--target-take` | Expected installed JHF Take; required for `rolling` and checked with the exact package before failover |
| `--phase` | `precheck`, `download-verify`, `support-capture`, `support-diff`, `support-analyze`, `failover-test`, or `rolling` |
| `--target` | `standby` or a member address/name for `download-verify` |
| `--execute` | Allows a change-capable phase to proceed |
| `--create-backup` | Requests configured backup behavior |
| `--verbose` | Prints additional command output |
| `--strict-host-key-checking` | SSH host-key policy; default `accept-new` |
| `--ssh-proxy` | HTTP CONNECT proxy URL |
| `--ssh-proxy-user` | Proxy username |
| `--ssh-proxy-password-env` | Environment variable containing proxy password; default `CP_SSH_PROXY_PASSWORD` |
| `--download-timeout` | Download timeout seconds; default 3600 |
| `--verify-timeout` | Package verification timeout seconds; default 1200 |
| `--install-timeout` | Install timeout seconds; default 7200 |
| `--backup-wait-seconds` | Backup wait seconds; default 600 |
| `--reconnect-timeout` | Post-reboot reconnect timeout seconds; default 1800 |
| `--support-script` | Controller path to support command script |
| `--support-output-dir` | Support capture/diff directory |
| `--support-label` | Stable capture label |
| `--support-command-timeout` | Per support-command timeout seconds; default 300 |
| `--diff-before`, `--diff-after` | Capture files to compare |
| `--diff-output` | Explicit diff output path |
| `--capture-files` | Files consumed by support analysis |
| `--failover-wait-seconds` | ClusterXL transition timeout seconds; default 120 |
| `--icap-mode` | `required`, `optional`, or `disabled`; default `required` |

Standalone rolling execution fails if the installer returns a nonzero status. If
the reboot closes SSH before a status is returned, execution may continue only
to reconnect and target reconciliation. The upgraded member must report the
requested version and exact installed package/Take before failover.


### 4.2 Other Python entry points

| Program group | Reference |
|---|---|
| `servicenow_checkpoint_runner.py` and worker programs | ServiceNow build guide; use `--help` and do not bypass governance flags outside an authorized lab |
| `tools/cpuse_jhf_fetch.py` | `../tools/JHF_CURRENCY_AND_DOWNLOAD.md` |
| `tools/checkpoint_patch_inventory.py` | `../tools/CHECKPOINT_PATCH_INVENTORY.md` |
| `tools/cpuse_da_fetch.py` | `../tools/DEPLOYMENT_AGENT_CURRENCY.md` |
| Repository/evidence scanners | Positional paths shown by each tool's `--help` |

## 5. Ansible Playbook Variable Reference

For the distinction between CDT, Management API Central Deployment, this
repository's SSH-carried `mgmt_cli` backend, and the corresponding
`check_point.mgmt` modules, see `CDT_AND_MANAGEMENT_API.md`.

All playbooks target `localhost`. `CP_PASSWORD` and `CP_EXPERT_PASSWORD` are
required in the environment unless the row says offline. `chg_number` is
optional for most report names but should always be supplied for traceability.

| Playbook | Required extra variables | Optional variables and accepted values | Effect |
|---|---|---|---|
| `01_validate_activity_plan.yml` | `chg_number`, `activity_plan_file`, `activity_plan` mapping | None | Offline validation and plan snapshot |
| `02_discover_targets.yml` | `mds_host`, `target_ips` comma/newline string | `cma_name` preferred domain; `change_id` non-negative persistence id; `chg_number` | Read-only MDS discovery; writes report and optional local DB record |
| `00_precheck.yml` | `member_a_ip`, `member_b_ip` | `icap_mode required|optional|disabled`; `chg_number` | Read-only health gate |
| `05_airgap_package_gate.yml` | `activity_plan_file`, `activity_plan` | `staging_method`; `execution_method`; `package_stage_required`; `package_stage_confirmed`; `chg_number` | Validates MDS files; may copy with CPRID |
| `06_validate_mds_package.yml` | `activity_plan_file` | `chg_number` | Read-only MDS file/hash validation |
| `07_validate_deployment_agent.yml` | `activity_plan_file`, `activity_plan` | `minimum_deployment_agent_build`; `deployment_agent_package_path`; `deployment_agent_package_build`; `chg_number` | Read-only DA and offline-package validation |
| `08_validate_package_prerequisites.yml` | `activity_plan_file`, `phase`, `step` | `chg_number` | Read-only per-member package/capacity gate |
| `10_cdt_generate_candidates.yml` | `activity_plan_file`, `activity_plan`, `operation_id`, `phase`, `cdt_context_file` | `step`; `cdt_target_policy standby|active`; `chg_number` | Generates one-member controlled candidates and protected reconciliation context |
| `11_capture_cluster_state.yml` | `member_a_ip`, `member_b_ip` | `icap_mode`; `chg_number` | Read-only state file required by rolling phases |
| `12_support_capture.yml` | `activity_plan` with members and support script | `phase`; `support_capture_script`; `support_command_timeout`; `chg_number` | Runs reviewed read-only support commands and writes evidence |
| `20_cdt_execute_guarded.yml` | `activity_plan_file`, `activity_plan`, `checkpoint_execute_upgrade=true`, `operation_id`, `phase`, `step`, `cdt_context_file`, `cdt_mutation_receipt_file` | `checkpoint_cdt_execute_timeout`; `chg_number` | Mutating CDT deployment followed by a protected receipt bound to its candidate context |
| `21_cdt_reconcile_member.yml` | `activity_plan_file`, `operation_id`, `phase`, `step`, and the three `cdt_*` artifact paths | `chg_number` | Read-only exact selected-member reconciliation; must immediately follow each governed CDT mutation |
| `22_monitor_gateways.yml` | `member_a_ip`, `member_b_ip` | `icap_mode`; `chg_number` | Three read-only samples at ten-second intervals; writes JSONL |
| `23_failover_to_member.yml` | `member_a_ip`, `member_b_ip`; prior state file for same `chg_number` | `failover_target`; `icap_mode`; `chg_number` | Mutating ClusterXL failover |
| `25_check_jhf_installed.yml` | `member_a_ip`, `member_b_ip`, `target_take` | `icap_mode`; `chg_number`; `scripts_dir` | Read-only Take assertion |
| `30_direct_package_step.yml` | `activity_plan_file`, `step`, `phase`, `checkpoint_execute_direct=true` | `checkpoint_direct_execute_timeout`; `chg_number` | Mutating direct CPUSE/Clish action |
| `31_major_policy_gate.yml` | `activity_plan_file`, `phase mixed-version-policy-gate|final-policy-install` | `chg_number` | Mutating management policy/version gate |
| `32_major_mvc.yml` | `activity_plan_file`, `phase mvc-on|mvc-off` | `chg_number` | Mutating MVC control |
| `39_api_repository_package.yml` | `activity_plan_file`, `activity_plan`, `step`; backend must be `api` | `chg_number` | Mutating management repository import/confirmation |
| `40_api_verify_package.yml` | `activity_plan_file`, `activity_plan`, `step`; backend must be `api` | `chg_number` | Read-only package verification |
| `41_api_execute_package.yml` | `activity_plan_file`, `activity_plan`, `step`, `phase first-member|second-member`, `checkpoint_execute_api=true` | `chg_number` | Mutating Management API deployment |
| `60_postcheck.yml` | `member_a_ip`, `member_b_ip` | `target_take`; `activity_plan_file`; `icap_mode`; `chg_number` | Read-only final health/package verdict; supply plan for net package expectations |
| `61_restore_original_active.yml` | `member_a_ip`, `member_b_ip`; prior state file for same `chg_number` | `icap_mode`; `chg_number` | Mutating ownership restoration when required |
| `62_support_diff.yml` | `activity_plan` with members; matching baseline/final captures | `chg_number` | Offline evidence diff |
| `site_preexecute.yml` | Union of imported phase variables | Same as imported phases | Convenience composite; inspect its imports before use rather than treating it as a complete workflow |

Boolean execution variables are intentionally separate from the plan. A plan
that says `deployment_backend: api` does not satisfy
`checkpoint_execute_api=true`; both are required by the API execution playbook.
The same rule applies to CDT and direct execution.

## 6. Copy-ready Examples
For complete read-only, fail-closed, CDT, API, Deployment Agent, and composition
examples, start with [`examples/README.md`](../examples/README.md).
For the journaled Python-only sequence, use
[`examples/standalone_python/README.md`](../examples/standalone_python/README.md).

### 6.1 Call read-only helpers directly

```bash
export CP_PASSWORD="$(vault-command checkpoint/gaia-password)"
export CP_EXPERT_PASSWORD="$(vault-command checkpoint/expert-password)"

python3 ansible/scripts/discover_checkpoint_targets.py \
  --mds-host 192.0.2.10 \
  --target-ips 192.0.2.20,192.0.2.21 \
  --output /tmp/discovery.json

python3 checkpoint_cluster_upgrade.py \
  --members 192.0.2.20 192.0.2.21 \
  --phase precheck \
  --icap-mode required

python3 ansible/scripts/monitor_gateways.py \
  --members 192.0.2.20 192.0.2.21 \
  --samples 3 \
  --interval 10 \
  --icap-mode required \
  --include-take \
  --output-jsonl /tmp/cluster-monitor.jsonl
```

Replace `vault-command` with the environment's approved secret retrieval. It is
a placeholder, not a program shipped by this repository.

### 6.2 Run shipped read-only playbooks with your YAML variables

```bash
ansible-playbook \
  -i ansible/inventory/hosts.yml \
  examples/playbooks/read_only_cluster_checks.yml \
  -e @examples/common/vars.yml
```

The example wrapper imports `00_precheck.yml` and `22_monitor_gateways.yml`.
Edit the variable file locally; do not commit real addresses or credentials.

### 6.3 Validate a custom activity plan

```bash
PLAN="$PWD/examples/activity_plans/read-only-validation.json"
ansible-playbook \
  -i ansible/inventory/hosts.yml \
  examples/playbooks/validate_activity_plan.yml \
  -e "activity_plan_source=$PLAN" \
  -e "chg_number=MANUAL_EXAMPLE"
```

The read-only example validates the plan structure without contacting a remote
system. The separate patch-install template contains non-operational package and
checksum placeholders that must be replaced before use.

### 6.4 Call a helper from your own playbook

```yaml
- name: Use repository package validation in another project
  hosts: localhost
  gather_facts: false
  vars:
    plan_file: /srv/change-inputs/activity-plan.json
  tasks:
    - name: Validate packages and published hashes on MDS
      ansible.builtin.command:
        argv:
          - python3
          - /opt/checkpoint-automation/ansible/scripts/validate_mds_packages.py
          - --activity-plan-file
          - "{{ plan_file }}"
          - --username
          - admin
      environment:
        CP_PASSWORD: "{{ lookup('env', 'CP_PASSWORD') }}"
        CP_EXPERT_PASSWORD: "{{ lookup('env', 'CP_EXPERT_PASSWORD') }}"
      changed_when: false
```

For a mutating helper, copy the corresponding shipped playbook's assertions as
well as its command. Do not replace the explicit execution boolean and helper
`--execute` pair with a single unchecked variable.

## 7. Outputs, Return Codes, and Composition Rules

- Helpers write human-readable evidence to stdout/stderr; playbooks persist it
  under `ansible/reports/`.
- Discovery can write structured JSON with `--output`.
- Monitoring writes one JSON object per sample with `--output-jsonl`.
- Cluster state is a JSON file created by `capture-state`; failover, member order,
  direct/API rolling phases, and restoration must consume the same file.
- CDT generation produces an XML deployment plan and controlled candidate CSV.
  Execute only the exact files generated and reviewed for that step.
- Exit 0 means the helper completed its contract. Exit 2 normally means a failed
  validation or operation. Exit 3 from guarded CDT/direct helpers means planned
  but not executed. Do not translate a nonzero return into success.
- Never resume from an arbitrary phase name. The complete runner validates phase
  boundaries and refuses a zero-phase success; custom orchestration must do the
  same.

A standalone component is reusable only with its prerequisites. For example,
`20_cdt_execute_guarded.yml` is not a safe replacement for the workflow unless
the caller has already completed target resolution, package/hash validation,
cluster-state capture, candidate generation and identity review, approval, and
post-execution health checks.
