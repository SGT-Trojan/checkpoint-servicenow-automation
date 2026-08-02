# Build the ServiceNow Integration

Status: sanitized reference implementation. Replace all example values and validate every component in your own estate.
Audience: ServiceNow developers (Sections 4-7) and automation engineers
(Sections 8-13). Both groups should read Sections 1-3 and 14-17.

New to the project? Read [Start Here](START_HERE.md) first. This guide is a detailed
build manual. You can follow it in order, or use the section numbers as a
reference while you build one part.

This guide shows how to build the complete ServiceNow integration. ServiceNow
controls the request and approval. Ansible runs the firewall steps. Fields store
automation decisions, while notes explain them to people. A failure creates a
task with the information an engineer needs to continue.

The steps come from a tested build. You must still replace every example and
validate the result in your own environment.

---

## 1. Architecture: what you are building

### 1.1 The four layers

| Layer | Components | Owner | Responsibility |
|---|---|---|---|
| ServiceNow | 1 catalog item, 6 business rules, 16 custom fields, change model | ServiceNow developer | Request intake, record chain, approvals, human gates, audit trail |
| Automation host (workers) | `servicenow_checkpoint_readiness_worker.py`, `servicenow_checkpoint_worker.py` (systemd) | Automation engineer | Poll ServiceNow, validate readiness pre-CHG, launch/govern execution, remediation tasks, bookkeeping |
| Orchestration | `servicenow_checkpoint_runner.py` + 21 Ansible playbooks + 14 helper scripts | Automation engineer | Phase sequencing, guarded CDT execution, health checks, evidence capture |
| Check Point estate | MDS/CMA, CDT, CPRID, CPUSE/Deployment Agent, ClusterXL gateways | Firewall team | The actual firewall work and the ground truth for every validation |

### 1.2 The record chain

Every activity produces this ServiceNow chain, in this order:

```text
REQ (request)
 └─ RITM (requested item, carries all variables + attachments)
     └─ SCTASK "Automated Check Point readiness validation - <activity>"   (readiness worker)
     └─ SCTASK "Firewall Deploy manual readiness remediation - <task>"     (only on readiness failure)
 └─ CHG (change_request, created by BR only when readiness fields say ready)
     ├─ CTASK "Implementation - Check Point firewall automation workflow"  (primary execution record)
     ├─ CTASK "Tester validation gate - Check Point automation"            (human gate before member 2)
     ├─ CTASK "Engineer remediation required - Check Point automation"     (only on mid-flight failure)
     ├─ CTASK "Final validation - Check Point post-implementation checks"  (created on success)
     └─ CTASK "Change-model default: ... (auto-managed, no action needed)" (relabeled model tasks)
```

### 1.3 Design principles (why the build looks like this)

1. **A CHG only exists for validated, actionable work.** Readiness is proven (automated, or by a Firewall Deploy engineer) *before* change governance starts.
2. **Decisions are field-driven, never text-driven.** `u_checkpoint_readiness_*` and `u_checkpoint_resume_*` fields carry every machine decision; work notes are for humans. Close-note markers exist for audit only.
3. **Defense in depth.** The worker validates governance before launching; the runner independently re-validates before touching anything.
4. **Every failure produces exactly one actionable human task.** Failures are deduplicated and include evidence paths and explicit instructions.
5. **The gateways are air-gapped.** Only the automation host talks to ServiceNow (outbound HTTPS). Packages are manually staged to the MDS; gateways receive them MDS-side (CPRID / CDT).

### 1.4 Activity types and their execution paths

| Catalog choice (value) | Runner activity | Execution method | Workflow shape |
|---|---|---|---|
| `version_upgrade_activity` | Major Version Upgrade | CDT | Rolling two-member + mixed-version policy gates + MVC on/off. Standalone rejected. |
| `software_patch_activity` | JHF/wrapper patch | CDT | Rolling one-member-at-a-time; install and/or uninstall; standalone = first member only |
| `deployment_agent_install` | Deployment Agent Update | Direct CPUSE/clish over SSH | Short path, ALL members in parallel, no failover/tester gate |

The firewall-side execution phases and decisions are documented in Section 10.

---

## 2. Prerequisites

### 2.1 Check Point estate (reference lab values)

| Component | Lab value | Notes |
|---|---|---|
| MDS host | 192.0.2.10 | Multi-Domain Server; CDT installed at `/opt/CPcdt/CentralDeploymentTool`; package staging dir `/var/log/tmp` |
| CMA | `CMA_A_Server` @ 192.0.2.11 | Domain management server owning the cluster |
| Cluster | `CP-FW-Cluster` = CP-FW-A 192.0.2.20 + CP-FW-B 192.0.2.21 | R82 ClusterXL HA pair |
| Credentials | `admin` + expert password | Same on MDS and members in the lab; production: per-device vaulted credentials |
| Version/take | R82, target take 91 | Example values; carried in the inventory defaults |

Requirements: SSH (22) from the automation host to MDS and both members; `mgmt_cli -r true` usable on the MDS; CDT ≥ the version matching your gateways; CPUSE Deployment Agent on every gateway (build currency: see `tools/DEPLOYMENT_AGENT_CURRENCY.md`).

### 2.2 ServiceNow instance

- Any Washington-or-later instance works; the build was done on a free Personal Developer Instance.
- Required platform capabilities are Service Catalog, Request Management, Change Management, CMDB, Table API, and Attachment API. The validated official MID Server is optional for the current REST-worker transport; IntegrationHub and the Ansible Spoke are required only for the alternative platform-initiated model.
- An integration account (Section 4).

### 2.3 Automation host

Ubuntu 24.04 (any systemd Linux works):

```bash
sudo apt install python3 python3-venv openssh-client
python3 -m venv /opt/checkpoint-automation/.venv-ansible
/opt/checkpoint-automation/.venv-ansible/bin/pip install ansible-core paramiko
# Workers/runner use only the Python stdlib (urllib, json, subprocess) — no pip packages needed.
```

Network: outbound HTTPS (443) to the ServiceNow instance; SSH to MDS + gateways. Inbound: none — every connection is initiated by the automation host, which is the fact that makes the no-MID-server design possible.

Directory layout to replicate (everything lives under one root):

```text
checkpoint-servicenow-automation/
├── servicenow_checkpoint_readiness_worker.py   # worker 1: pre-CHG validation
├── servicenow_checkpoint_worker.py             # worker 2: execution governor
├── servicenow_checkpoint_runner.py             # phase engine
├── checkpoint_cluster_upgrade.py               # shared SSH/clish library + precheck/capture phases
├── ansible/
│   ├── inventory/hosts.yml                     # estate definition
│   ├── playbooks/  (24 x *.yml)                # thin single-phase wrappers
│   ├── scripts/    (14 x *.py)                 # the actual logic (Section 11)
│   └── reports/                                # JSON reports per playbook
├── runs/                                       # per-CHG run dirs, worker state, readiness evidence
└── tools/                                      # currency, inventory, and hygiene utilities
```

---

## 3. Integration transport: how ServiceNow and Ansible talk

### 3.1 Reference pattern: outbound REST polling (no MID server)

This build uses no MID server. Two long-running Python workers on the automation host poll the ServiceNow REST Table API (`/api/now/table/...`) every 60 seconds over outbound HTTPS with basic authentication:

- The readiness worker polls `sc_task` for open tasks whose short description starts with `Automated Check Point readiness validation`.
- The implementation worker polls `change_request` for records carrying the `[CHECKPOINT_AUTOMATION]` marker in state Implement with approval `approved`.

Writes go the same way: `PATCH` on task/CHG records (states, `u_checkpoint_*` fields, work notes) and `POST /api/now/attachment/file` for evidence uploads.

Why this is the right shape for this system:

1. **All connections are outbound from the automation host.** ServiceNow never needs to reach into the network, so nothing needs to be exposed or brokered — which is exactly the problem a MID server exists to solve.
2. **The pollers are also the state machines.** The implementation worker is not a dumb executor; it re-validates governance on every poll, tracks per-CHG state (`runs/worker_state.json`), and refuses to double-launch (singleton `flock` + per-CHG status). Moving execution triggering into ServiceNow (Flow Designer → MID → Ansible) would split that state machine across two systems.
3. **Failure semantics stay simple.** If the host is down, requests simply queue in ServiceNow; nothing is lost.

### 3.2 ServiceNow components to activate for this pattern

Nothing needs to be *installed*; you need to *configure*:

| Component | What to do |
|---|---|
| REST Table API | Enabled by default. Verify `GET /api/now/table/sc_task?sysparm_limit=1` returns 200 with the integration account. |
| Attachment API | Enabled by default; used for log/evidence upload (`POST /api/now/attachment/file`). |
| Integration account | Section 4. Basic auth. For production, prefer OAuth 2.0 (client credentials) — the workers' auth layer is a single method to swap. |
| Custom fields | Section 5. Created once via `sys_dictionary` (REST or UI). |
| Business rules | Section 7. These are the ServiceNow-side "integration logic" — the workers contain no record-chain knowledge beyond markers and field names. |

### 3.3 The MID server variant (when you need it)

Use a MID server only if your security model forbids storing ServiceNow credentials outside the platform, or you want ServiceNow (Flow Designer/IntegrationHub) to *initiate* actions instead of being polled. Steps, for completeness:

1. Activate plugins: MID Server (`com.snc.mid.server`); IntegrationHub starter or better if you want the Ansible spoke (`sn_ansible_spoke`) — subscription required.
2. Create a MID user: role `mid_server`, non-interactive.
3. Install the MID server on a host in the same network zone as the automation host (`agent/` package from the instance: MID Server → Downloads), configure `config.xml` with instance URL + MID user, start the service, then Validate it in MID Server → Servers.
4. Wire execution: either (a) Ansible spoke pointed at an AWX/Automation Platform API running these playbooks, or (b) a custom IntegrationHub action invoking `servicenow_checkpoint_runner.py` via the MID server's script/SSH step.
5. Keep the governance model: even in this variant, keep the readiness worker and the runner's independent governance gate. The MID server replaces the *transport*, not the *validation logic* — the double-check (worker validates, runner re-validates) is what saved CHG_EXAMPLE when a ServiceNow change-model rule misbehaved (Section 15.3).

Decision record: the as-built system chose polling over MID because the automation host must hold Check Point credentials and SSH reachability anyway — adding a MID server would add a component without removing a trust requirement.

### 3.4 Official MID Server installation and validation

The reference host also runs a validated official MID Server named `checkpoint-local-mid`. It coexists with the REST workers but does not launch them, invoke Ansible, or consume this workflow through ECC Queue. Installing it does not replace either systemd worker.

#### ServiceNow preparation

1. Create a dedicated non-interactive user such as `mid.checkpoint.automation` under User Administration > Users.
2. Grant the `mid_server` role only. Do not reuse the REST integration account or grant `admin`.
3. Store a long random password in the approved vault.
4. Download the Linux x86-64 MID distribution from MID Server > Downloads for the same ServiceNow release family.

#### Host and network preparation

1. Provide a supported Linux VM with DNS, NTP, at least 2 vCPU, 4 GB RAM, and 10 GB free disk. Increase capacity for Discovery workloads.
2. Permit outbound TCP 443 to the ServiceNow instance. No inbound Internet port is required; do not expose the host publicly.
3. Create a service account and directory:

```bash
sudo useradd --system --home /opt/servicenow-mid --shell /usr/sbin/nologin snowmid
sudo install -d -o snowmid -g snowmid -m 0750 /opt/servicenow-mid/checkpoint-local-mid
```

4. Extract the archive beneath that directory, retain the bundled JRE, and own the complete `agent/` tree with `snowmid:snowmid`.

#### Agent configuration

Configure `agent/conf/config.xml` with the instance URL, dedicated user, vaulted secret, and unique name:

```xml
<parameter name="url" value="https://INSTANCE.service-now.com/"/>
<parameter name="mid.instance.username" value="mid.checkpoint.automation"/>
<parameter name="mid.instance.password" value="VAULTED_SECRET"/>
<parameter name="name" value="checkpoint-local-mid"/>
```

Protect `config.xml` and the agent keystore. Never commit them.

#### systemd service

```ini
[Unit]
Description=ServiceNow MID Server - checkpoint-local-mid
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=snowmid
Group=snowmid
WorkingDirectory=/opt/servicenow-mid/checkpoint-local-mid/agent/bin
ExecStart=/opt/servicenow-mid/checkpoint-local-mid/agent/bin/mid.sh start
ExecStop=/opt/servicenow-mid/checkpoint-local-mid/agent/bin/mid.sh stop
ExecReload=/opt/servicenow-mid/checkpoint-local-mid/agent/bin/mid.sh restart
PIDFile=/opt/servicenow-mid/checkpoint-local-mid/agent/work/mid.pid
Restart=on-failure
RestartSec=15
TimeoutStartSec=180
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now servicenow-mid-checkpoint-local
sudo systemctl status servicenow-mid-checkpoint-local --no-pager
```

#### Validate

1. Open MID Server > Servers, wait for `checkpoint-local-mid`, and select Validate.
2. Resolve certificate, credential, DNS, or proxy findings until status is Up and validation succeeds.
3. Confirm recurring heartbeats and review `agent/logs/agent0.log`, `wrapper.log`, and `service-wrapper.log`.
4. Assign only required capabilities. Stop/start once and prove ServiceNow detects Down and Up states.

| Symptom | Check | Corrective action |
|---|---|---|
| Never registers | DNS, TCP 443, URL, proxy, `agent0.log` | Correct routing and verify the account. |
| Authentication rejected | MID role and secret | Reset the dedicated secret. |
| Registered, not validated | Certificate chain | Correct TLS trust; never disable verification. |
| MID Up, Ansible idle | Expected in this design | Start the two REST worker services. |

For a future platform-initiated design, activate licensed IntegrationHub/Ansible Spoke components and dispatch through AWX job templates or a tightly scoped custom action. Preserve the same idempotency, tester wait, engineer remediation, phase resume, and independent runner governance checks, then re-certify all paths.

---

## 4. ServiceNow build — integration account

1. Create user `checkpoint.automation` (User Administration → Users): Web service access only = true.
2. Roles: `itil` (task/CHG read-write), `catalog` (RITM variable read), `rest_api_explorer` for testing. The lab build used `admin` — do not replicate that in production; Section 17 lists the minimum ACL set.
3. Store the credentials on the automation host only, in `/etc/snow-checkpoint-worker.env` (Section 8.2). They are never passed via argv and never appear in ServiceNow records or logs.

## 5. ServiceNow build — custom fields (dictionary)

Create these 16 fields (System Definition → Dictionary → New, or REST to `sys_dictionary`). All are Type String. These fields ARE the integration contract: every machine decision flows through them.

| Table | Field | Length | Purpose |
|---|---|---|---|
| `sc_task` | `u_checkpoint_readiness_status` | 4000 | `pending` / `ready` / `failed` / `rejected` / `not_viable` |
| `sc_task` | `u_checkpoint_readiness_source` | 4000 | `automated` / `manual` |
| `sc_task` | `u_checkpoint_readiness_summary` | 4000 | Human-readable readiness result |
| `sc_task` | `u_checkpoint_readiness_evidence` | 4000 | Path to `runs/readiness/<SCTASK>_<ts>/` |
| `sc_req_item` | same four `u_checkpoint_readiness_*` fields | 4000 | Copy stamped onto the RITM (drives the CHG-creation BR) |
| `change_task` | `u_checkpoint_resume_status` | 4000 | `approved` / `ready` / `resume_approved` / `rejected` / `abort` |
| `change_task` | `u_checkpoint_resume_phase` | 4000 | Phase to resume from (defaults to the failed phase) |
| `change_task` | `u_checkpoint_resume_summary` | 4000 | Engineer's remediation summary |
| `change_task` | `u_checkpoint_resume_evidence` | 4000 | Engineer's evidence pointer |
| `change_request` | `u_checkpoint_workflow_state` | 255 | Worker's last known state for the CHG |
| `change_request` | `u_checkpoint_failed_phase` | 255 | Last failed phase |
| `change_request` | `u_checkpoint_failed_step` | 255 | Last failed package step |
| `change_request` | `u_checkpoint_resume_token` | 255 | Monotonic token so one approval resumes exactly one time |

Gotcha (verified live): REST writes to some brand-new `u_` fields can be *silently discarded* until the dictionary entry has fully propagated. After creating fields, prove writability with a PATCH + GET readback before wiring anything to them.

## 6. ServiceNow build — the catalog item

### 6.1 Create the item

Service Catalog → Catalog Definitions → Maintain Items → New:

- Name: `CheckPoint FW Maintenance Activity`
- Catalog: Service Catalog; any category (lab used Hardware).
- Delivery workflow: none needed — the business rules drive everything after submission.

### 6.2 Variables (item_option_new) — exact replication table

Create these 14 variables. Type numbers are ServiceNow's internal `type` values as exported from the live item: `5` = Select Box, `6` = Single Line Text (with reference qualifier where noted), `2` = Multi Line Text, `33` = Attachment, `10` = Date/Time.

| Order | Name | Type | Mandatory | Label / notes |
|---|---|---|---|---|
| 10 | `activity_type` | Select Box | yes | Activity Type — no default: requester must actively choose. Choices: `version_upgrade_activity` "Version Upgrade Activity", `software_patch_activity` "Software Patch Activity", `deployment_agent_install` "Deployment Agent Install" |
| 30 | `environment` | Select Box | yes | Environment: `lab`, `qa`, `production` |
| 50 | `icap_mode` | Select Box | yes | ICAP Check Mode: `required`, `optional`, `disabled` |
| 60 | `target_ips` | Multi Line Text | yes | Target Firewall IPs (comma/newline separated; validated against MDS discovery) |
| 70 | `mds_host` | Single Line | yes | MDS Host/IP |
| 100 | `current_version` | Single Line | no | Current Check Point Version |
| 110 | `target_version` | Single Line | yes | Target Check Point Version |
| 125 | `cpuse_package_upload` | Attachment | yes | CPUSE Package — CSV/XLSX defining ordered package steps (format: Section 16.1) |
| 170 | `preserve_original_active` | Select Box | yes | `yes` / `no` — fail traffic back to the original active member at the end |
| 180 | `tester_gate` | Select Box | yes | `yes` / `no` — pause for tester validation before member 2 |
| 190 | `scheduled_start` | Date/Time | no | Requested maintenance start |
| 200 | `scheduled_end` | Date/Time | no | Requested maintenance end |
| 205 | `cpuse_dependency_upload` | Attachment | no | CPUSE Dependency Checklist — optional CSV/XLSX (Section 16.2) |
| 900 | `special_instructions` | Multi Line Text | no | Free text to the Firewall Deploy engineer |

Deliberate absences (learned the hard way — do NOT add): no MDS API key variable (production sources credentials from CyberArk, never from the requester); no Target Take (always derived from the uploaded package names); no staging method / package source dir / Blink fields / execution method (all removed in the upload-only simplification — the parser and the runner own those decisions). If you see variables like `package_sequence_input_method` or `mds_api_key_alias` in an older export, they are inactive leftovers; leave them inactive or delete them.

For a sanitized submission JSON, upload links, record-state map, and CLI phase
comparison, see the [governed ticket example](SERVICENOW_TICKET_EXAMPLE.md).

### 6.3 Catalog client script (exactly one active)

One onLoad script (`Check Point FW Maintenance - upload only`, UI Type: All / applies to item view) pins the upload-only model:

```javascript
function onLoad() {
    // Upload-only catalog model. These variables intentionally remain simple:
    // requester uploads CPUSE package and optional dependency checklist files;
    // Firewall Deploy validates staging and details in SCTASK.
    g_form.setDisplay('checkpoint_package_sequence_rows', false);
    g_form.setDisplay('checkpoint_dependency_check_rows', false);
    g_form.setDisplay('cpuse_package_upload', true);
    g_form.setDisplay('cpuse_dependency_upload', true);
    g_form.setMandatory('cpuse_package_upload', true);
    g_form.setMandatory('cpuse_dependency_upload', false);
}
```

Earlier iterations had five onLoad/onChange scripts implementing a three-way input-method selector (table rows / CSV upload / JSON-YAML fallback). They are inactive on the live item and superseded — replicate only the script above.

### 6.4 Downloadable sample documents

Attach two sample files to the catalog item so requesters can download, fill, and re-upload: a sample CPUSE Package CSV and a sample Dependency Checklist CSV (contents in Section 16). This replaced all in-form package/dependency data entry.

---

## 7. ServiceNow build — the four business rules

Create these under System Definition → Business Rules. Full script bodies are in Appendix A — this section explains what each does and why, so a developer can maintain them, not just paste them. All four are scoped global, no condition field (the scripts self-filter), and carry `gs.error` wrapping so failures are visible in the system log.

### 7.1 `Check Point FW Maintenance - create readiness task` (intake)

Table `sc_req_item` · When after · Insert yes · Update yes · Order 100

What it does, in order:

1. Self-filter: exits unless `cat_item` is one of the known CheckPoint item sys_ids. (When replicating: update these three constants to YOUR item's sys_id.)
2. Skips closed/inactive RITMs (state 3/4/7 or `active=false`) — this is what prevents retro-creating readiness tasks on legacy records when someone touches an old RITM (a live incident before this guard existed).
3. Dedupes across ALL readiness task prefixes (`Automated Check Point readiness validation`, legacy human-readiness, and manual remediation) — any prior readiness task means intake already ran; exit.
4. Builds a normalized summary from the RITM variables (activity label, environment, target IPs, versions, MDS host, ICAP, preserve/tester flags, attachment inventory) and rewrites the RITM/REQ short description + description with it, tagging the description with the `[CHECKPOINT_AUTOMATION_INTAKE]` marker.
5. Creates the `Automated Check Point readiness validation - <activity>` SCTASK under the RITM, assigned to the Firewall Deploy group, with `u_checkpoint_readiness_status=pending`. This SCTASK is what the readiness worker polls for.

Replication warning (the most expensive lesson of the build): business rule compile failures are completely silent — a script with a syntax error simply never runs; nothing appears in any log. When creating this BR via API, one literal `\n` inside a string was mangled into a real newline ("unterminated string literal") and every catalog submission silently produced nothing. After creating any BR programmatically: (a) compile-test it server-side — background script: `new Function('current','previous', grBR.script);` throws on syntax errors — and (b) prove it with a real catalog submission before moving on.

### 7.2 `Check Point FW Maintenance - readiness SCTASK to CHG`

Table `sc_task` · When after · Update yes · Order 100

The governance heart. Fires when a readiness SCTASK is updated, and decides using fields, not text:

- If the task closes with `u_checkpoint_readiness_status` = `rejected` / `not_viable` (or closes Incomplete with a failed status): stamps the status onto the parent RITM and closes the RITM Incomplete (state 4). No CHG. The requester sees why on the RITM.
- If the task closes Complete with `u_checkpoint_readiness_status=ready`: stamps `ready` + source + summary + evidence onto the RITM, then:
  1. Duplicate guard: queries `change_request` for an existing open CHG carrying this RITM's number in its description — if found, exits (this closed a live defect where re-closing a readiness task minted a second CHG).
  2. Creates the CHG (state `-5` Assess → normal approval flow), description tagged `[CHECKPOINT_AUTOMATION]` + the full request summary, CI list resolved from `target_ips` via `cmdb_ci` IP match, affected-CI records added.
  3. Creates two governed CTASKs under it: `Implementation - Check Point firewall automation workflow` (the record the worker and mirror BR treat as primary) and the post-implementation placeholder. Assignment group/assignee constants must be updated to your Firewall Deploy group when replicating.
- Re-entrancy guard: exits early when neither state nor readiness status actually changed (previous-vs-current comparison), so unrelated field updates on closed tasks never re-trigger CHG creation.

The manual-remediation path uses the same rule: a `Firewall Deploy manual readiness remediation - <task>` SCTASK closed Complete with readiness fields set to `ready`/`manual` triggers the same CHG creation; closed with `rejected` closes the RITM. One rule, one decision surface.

### 7.3 `Check Point FW Maintenance - relabel default CTASKs`

Table `change_task` · When before · Insert yes · Order 10

When the change model auto-creates its default phase tasks ("Implement", "Post implementation testing") on a `[CHECKPOINT_AUTOMATION]` CHG, this rule relabels them to `Change-model default: <name> (auto-managed, no action needed)` and rewrites the description to point humans at the governed CTASKs.

It matches only: unassigned tasks (`assignment_group` and `assigned_to` empty) with those exact short descriptions, on CHGs whose description carries the automation marker — so human-created tasks are never touched.

Why relabel and never close (verified live, CHG_EXAMPLE): an earlier version closed these tasks at birth. The change model interpreted its phase tasks being closed as "Implement phase finished" and auto-advanced the CHG Implement → Review seconds after it reached Implement, yanking it away from the worker mid-validation and producing a bogus remediation task "failed at unknown". The relabeled tasks stay open; the worker closes them during success bookkeeping, which lets the change model advance to Review naturally at the end. Do not "improve" this rule back into closing tasks.

### 7.4 `Check Point FW Maintenance - mirror CHG notes`

Table `change_request` · When after · Update yes · Order 200

Copies every new CHG work note (the runner posts one per phase) to the Implementation CTASK, prefixed `[Mirrored from CHG automation notes]`, with a marker check to prevent mirror loops. Filters on `work_notes.changes()` + the `[CHECKPOINT_AUTOMATION]` marker. Result: the Implementation CTASK is a complete, self-contained execution log for people who only have task-level visibility.

### 7.5 Change model notes

- The default change model is used unmodified — the relabel BR (7.3) is the only accommodation it needs.
- CHG flow: Assess (`-5`) → approvals → Scheduled → Implement (worker only acts here, and only when `approval=approved`) → Review (reached naturally when the worker's success bookkeeping closes the phase tasks) → Closed (human closes with close code after reviewing evidence).
- If you must move a stuck CHG programmatically, use a background script with `setWorkflow(false)` and set both `state` and the model's phase fields — REST PATCH on `state` alone is silently rearranged by the change model.

### 7.6 ServiceNow testing methodology (do these for every BR change)

1. Server-side compile test (`new Function(...)`) — catches what the UI won't tell you.
2. One real end-to-end record: submit the catalog item, verify the SCTASK appears within seconds, check `sys_updated_on` actually moved.
3. `gs.info` may never reach the syslog on PDIs — instrument with `gs.error` during bring-up and verify by side effects (fields changed, records created).
4. After API-driven BR edits, GET the script back and diff — REST occasionally normalizes whitespace in ways that matter to string literals.


### 7.7 Catalog-chain completion and default fulfillment-task retirement

The four rules above are the core execution contract. A complete production build also needs two lifecycle rules so a successfully closed CHG reconciles its parent records and unused out-of-box catalog tasks do not remain open.

- `CP FW - complete catalog chain`: table `change_request`, after update, order 900, update=true. It self-filters on the automation marker and terminal CHG states. Closed Successful maps the parent RITM to Closed Complete; an unsuccessful/canceled CHG maps it to Closed Incomplete. After closing default delivery tasks, it reasserts the CHG-derived RITM outcome so a successfully remediated readiness failure cannot be recalculated as incomplete. When every RITM under the REQ is terminal, the rule closes the REQ consistently.
- `CP FW - retire default catalog tasks`: table `sc_task`, before insert/update. It only handles out-of-box delivery tasks named `Assess or Scope Task` or `Provide requested service` for governed Check Point RITMs whose CHG chain is already terminal. It does not touch the automated readiness or manual remediation SCTASKs.

The source of record for these two rules is `tools/servicenow_checkpoint_catalog_completion.py`. It defaults to dry-run. Set `SN_INSTANCE`, `SN_USERNAME`, and `SN_PASSWORD`, review the diff, then use `--apply`. On a fresh instance, prefer an application/update set containing the verified records; use the tool as a reconciler, not as an undocumented installer.

### 7.8 CMDB and form configuration

1. Create a CI for each physical or virtual firewall member in the appropriate Network Gear/Network Device class. Store management IP, hostname, serial, asset, model, software version, and ownership as available.
2. Do not create cluster, CMA, or MDS records merely to satisfy this workflow. For a cluster change, set Configuration Item to one member and create Affected CI links for both members.
3. Put the required CHG fields in the top two-column form section, not a separate automation tab. Preserve native Change Management fields and explanatory annotations according to enterprise UX standards.
4. Add the readiness fields to RITM/SCTASK views and resume fields to the Engineer Remediation CTASK view. Restrict write ACLs: the integration identity and Firewall Deploy engineers may update decision fields; requesters and testers may not.
5. Assign the Implementation CTASK and engineer-remediation CTASK to Firewall Deploy. Assign tester tasks to the intended validation group. The final-validation CTASK inherits implementation assignment but is automation-created and automation-closed.

### 7.9 UI actions and change-model behavior

Use the native Implement action when the chosen change model exposes it. A custom `Move to Implement - Check Point Automation` action may validate marker, approval, and Implementation CTASK before setting state, but it is redundant when the native action works and should be hidden to avoid two apparently equivalent buttons. Neither button starts Ansible directly; the implementation worker detects the approved Implement CHG on its next poll.

Never close the model-created `Implement` or `Post implementation testing` CTASKs at insert time. The relabel rule keeps them open and clearly model-managed. Closing them immediately causes the change model to advance Implement to Review before the worker can run.

### 7.10 Promotion and provisioning approach

1. Build in a scoped application or dedicated update set.
2. Include catalog item, variables, choices, client script, dictionary fields, form layouts, ACLs, groups/roles, six business rules, any UI action, and sample CSV/XLSX attachments.
3. Keep environment-specific sys_ids, group IDs, assignee IDs, item IDs, and CI references in documented deployment properties or a post-clone mapping worksheet. The Appendix scripts contain reference-instance IDs and must not be pasted unchanged.
4. Run server-side JavaScript compile checks, retrieve each script after promotion, and compare it byte-for-byte with the approved source.
5. Submit real catalog orders in the target sub-production instance. Business-rule syntax failures can be silent; record side effects are the acceptance test.
6. The helper tools under `tools/servicenow_*.py` default to dry-run where implemented. Some are historical patch tools tied to reference sys_ids; inspect and parameterize them before use. Do not reactivate the superseded corrupt intake rule documented in the project progress history.

---

## 8. Automation host build

### 8.1 Files

Install the repository at `/opt/checkpoint-automation`. Create a dedicated service account and Python environment before installing the systemd units:

```bash
sudo useradd --system --home /opt/checkpoint-automation --shell /usr/sbin/nologin checkpoint-auto
sudo mkdir -p /opt/checkpoint-automation
sudo chown checkpoint-auto:checkpoint-auto /opt/checkpoint-automation
sudo -u checkpoint-auto python3 -m venv /opt/checkpoint-automation/.venv
sudo -u checkpoint-auto /opt/checkpoint-automation/.venv/bin/pip install -r /opt/checkpoint-automation/requirements.txt
```

### 8.2 Credentials

`/etc/snow-checkpoint-worker.env`, root:root 0600:

```bash
SN_INSTANCE=https://<instance>.service-now.com
SN_USERNAME=checkpoint.automation
SN_PASSWORD=<secret>
SN_FIREWALL_DEPLOY_GROUP_SYS_ID=<sys_id>
CP_PASSWORD=<gaia admin password>
CP_EXPERT_PASSWORD=<gaia expert password>
```

Rules enforced by the code: credentials are read from the environment only (both workers exit at startup if any are missing — a running service is proof the env is complete); never passed as argv; never logged; never written to ServiceNow. Production: source this env file from CyberArk/vault at service start.

### 8.3 Reference systemd units

`/etc/systemd/system/snow-checkpoint-readiness-worker.service` and `/etc/systemd/system/snow-checkpoint-worker.service`:

```ini
[Unit]
Description=ServiceNow Check Point Automation Worker   # (or "... Automated Readiness Worker")
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/checkpoint-automation
EnvironmentFile=/etc/snow-checkpoint-worker.env
ExecStart=/opt/checkpoint-automation/.venv/bin/python /opt/checkpoint-automation/servicenow_checkpoint_worker.py --poll-interval 60
Restart=always
RestartSec=15
User=checkpoint-auto
Group=checkpoint-auto
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ReadWritePaths=/opt/checkpoint-automation/runs

[Install]
WantedBy=multi-user.target
```

(The readiness unit is identical except `ExecStart` points at `servicenow_checkpoint_readiness_worker.py`.)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now snow-checkpoint-readiness-worker snow-checkpoint-worker
journalctl -u snow-checkpoint-worker -f    # watch it poll
```

Operational rule: workers load code at start — after ANY edit to worker files, `sudo systemctl restart <unit>` or the fix is not live. (Runner and helper scripts are launched as fresh subprocesses per execution and need no restart.)

### 8.4 Ansible inventory (`ansible/inventory/hosts.yml`)

```yaml
---
all:
  children:
    checkpoint_lab:
      hosts:
        localhost:
          ansible_connection: local
      vars:
        checkpoint_mds_host: 192.0.2.10
        checkpoint_cma_env: CMA_A_Server
        checkpoint_cma_server_ip: 192.0.2.11
        checkpoint_cluster_name: CP-FW-Cluster
        checkpoint_cluster_members:
          - 192.0.2.20
          - 192.0.2.21
        checkpoint_target_version: R82
        checkpoint_target_take: 91
```

Note the shape: Ansible runs on localhost. Playbooks are thin wrappers that invoke helper scripts, which open their own SSH-PTY sessions to Gaia clish (Check Point boxes are not generic Linux targets; `raw`/`command` modules against clish are brittle). Ansible provides phase structure, extra-vars plumbing, JSON report capture — not transport.

---

## 9. The two workers in depth

### 9.1 Readiness worker (`servicenow_checkpoint_readiness_worker.py`)

Poll target: open `sc_task` records whose short description starts with `Automated Check Point readiness validation`, 60s interval.

Per task, `validate_request()` runs the read-only readiness pipeline into an evidence directory `runs/readiness/<SCTASK>_<ts>/`:

1. Parse the RITM variables and the uploaded CPUSE Package CSV/XLSX (and optional dependency checklist) into package steps — any parse error is a readiness failure, not an execution failure.
2. Discovery against the MDS (`discover_checkpoint_targets.py`): resolve every requested target IP to a managed gateway/cluster in some CMA; unresolvable IP = fail.
3. Precheck (cluster health), MDS package presence + SHA1/SHA256, Deployment Agent readiness — the same helper scripts the runner uses (Section 11), invoked with a `READINESS_<RITM>` pseudo-change so reports are traceable.

Outcomes (all field-driven):

- Pass: SCTASK Closed Complete with `u_checkpoint_readiness_status=ready`, `source=automated`, summary carrying resolved domain/cluster/policy + evidence dir. The readiness-to-CHG BR then mints the CHG.
- Fail: SCTASK Closed Incomplete with `status=failed`, and the worker creates ONE `Firewall Deploy manual readiness remediation - <task>` SCTASK (deduped by prefix) containing the failure summary, evidence dir, and explicit instructions: fix the underlying issue, set readiness fields to `ready`/`manual`, close Complete → CHG; or set `rejected` and close → RITM closed Incomplete. No CHG either way until a human or the validator says ready.
- Close-note markers `[CHECKPOINT_READINESS_READY]` / `[CHECKPOINT_READINESS_FAILED]` are written for human audit but never parsed for decisions.

### 9.2 Implementation worker (`servicenow_checkpoint_worker.py`)

Poll target: `change_request` in state Implement, `approval=approved`, description containing `[CHECKPOINT_AUTOMATION]`.

Governance gate re-validated on every poll (defense in depth — the BR already guaranteed most of this once, but records can be edited): marker present; state Implement; approved; parent RITM has a Closed Complete readiness SCTASK with status `ready`; an open Implementation CTASK exists. Any miss = skip with a work note, not a crash.

Per-CHG state machine (persisted in `runs/worker_state.json`, singleton `flock` on `worker_state.lock`; never double-launches, never auto-retries):

| State | Meaning | Exit condition |
|---|---|---|
| `running` | Runner subprocess live (mode `start` or `resume --start-at <phase>`) | Runner exit code |
| `waiting_tester` | Runner exited rc 20 at the tester gate | `Tester validation gate - Check Point automation` CTASK (prefix-matched) closed Complete → resume from `second-member`. Suppressed/relabeled/born-closed tasks can NOT satisfy this gate — only Closed Complete on the exact prefix counts. |
| `waiting_engineer` | Runner failed; remediation CTASK open | See remediation loop below |
| `completed` | rc 0 + bookkeeping done | terminal |

Failure → remediation loop: on non-zero exit, the worker reads the runner's `resume_state.json` (failed phase/playbook/step/log — e.g. `{"failed_phase": "postcheck", "failed_playbook": "60_postcheck.yml", "failed_log": ".../logs/postcheck_none_60_postcheck.yml.log"}`), stamps `u_checkpoint_failed_phase/step` on the CHG, and creates ONE `Engineer remediation required - Check Point automation` CTASK (deduped on open-prefix) carrying those details. The engineer fixes the estate, sets `u_checkpoint_resume_status=approved` (optionally `u_checkpoint_resume_phase` to override the restart point) and closes the CTASK Complete → worker resumes the runner `--start-at <phase>`, consuming a monotonic `u_checkpoint_resume_token` so one approval resumes exactly once. Closing Incomplete or setting `rejected`/`abort` = no resume.

Success bookkeeping (wrapped in try/except so a bookkeeping hiccup cannot mark a successful run failed): create the `Final validation - Check Point post-implementation checks` CTASK, close the Implementation CTASK with an evidence note, close the relabeled change-model default tasks (letting the model advance the CHG to Review), upload the phase logs.

## 10. The runner (`servicenow_checkpoint_runner.py`)

Launched by the worker as `--chg-sys-id <sys_id>` (sys_id, not number — numbers can duplicate). Can be run manually with the same args for lab work.

1. Independent governance gate — re-checks marker/state/approval/readiness/CTASK itself. The worker being convinced is not enough.
2. Activity plan build: RITM variables + parsed package CSV + MDS discovery → one JSON contract (Appendix B) passed to every playbook as `--extra-vars @<run>/CHG_vars.json`. Key mappings: `ACTIVITY_MAP` normalizes catalog values (`version_upgrade_activity`→"Major Version Upgrade", `software_patch_activity`→patch, `deployment_agent_install`→"Deployment Agent Update"); execution method = `Direct CPUSE/Clish` for DA activity, else CDT; target take inferred from package filenames; package types inferred (`deployment_agent` when the name carries deployment+agent; `.tar`→`.tgz` aliasing recorded).
3. Workflow selection (`workflow_steps()`): three branches — DA short path / major upgrade with policy+MVC phases / generic rolling patch. Standalone major → hard `ValueError`. The phase matrix in Section 11 is the reference for all three.
4. Execution loop: each phase = one `ansible-playbook` invocation, logged to `runs/<CHG>_<ts>/logs/<phase>_<step>_<playbook>.log`, one work note posted to the CHG (mirror BR copies it), log uploaded to the Implementation CTASK.
5. Exit codes: `0` success · `20` stopped at tester gate · `21` explicit `--stop-after` boundary · anything else = failure with `resume_state.json` written. Flags: `--start-at <phase>` (resume), `--stop-after`, `--dry-run`, `--simulate-gates` (auto-approve tester gate, lab only), `--tester-gate false` (suppress the gate when the catalog said no).

## 11. Playbook catalog and helper scripts in depth

### 11.1 Playbook → helper → activity matrix

Playbooks are thin (assert credentials → run helper → save JSON report → print). All logic lives in the helpers. "M/P/D" = used by Major / Patch / DA-install.

| Playbook | Helper script | M | P | D | Phase purpose |
|---|---|---|---|---|---|
| `01_validate_activity_plan.yml` | (inline assertions) | M | P | D | Contract sanity: members, steps, checksums declared |
| `02_discover_targets.yml` | `discover_checkpoint_targets.py` | M | P | D | Resolve CMA/cluster/members/policy from MDS |
| `00_precheck.yml` | `checkpoint_cluster_upgrade.py --phase precheck` | M | P | D | Cluster/member health gate |
| `07_validate_deployment_agent.yml` | `validate_deployment_agent.py` | M | P | D | DA build adequacy (+ post-install verify for DA activity) |
| `11_capture_cluster_state.yml` | `checkpoint_cluster_upgrade.py` | M | P | – | Record original ACTIVE/STANDBY |
| `12_support_capture.yml` | `checkpoint_cluster_upgrade.py --phase support` | M | P | – | Evidence snapshot (baseline + final) |
| `06_validate_mds_package.yml` | `validate_mds_packages.py` | M | P | D | Package presence + SHA on MDS |
| `05_airgap_package_gate.yml` | (inline + report) | M | P | D | Staging acknowledgement |
| `08_validate_package_prerequisites.yml` | `validate_package_prerequisites.py` | M | P | D | Present/absent prerequisites, capacity |
| `10_cdt_generate_candidates.yml` | `generate_cdt_candidates_from_activity.py` | M | P | – | Controlled candidate file (one member enabled) |
| `20_cdt_execute_guarded.yml` | `execute_cdt_from_activity.py` | M | P | – | Guarded CDT execution |
| `30_direct_package_step.yml` | `direct_package_step_from_activity.py` | – | (P)* | D | Direct CPUSE/clish path (*patch only when method=Direct) |
| `23_failover_to_member.yml` | `cluster_phase_control.py failover` | M | P | – | ClusterXL traffic move |
| `31_major_policy_gate.yml` | `major_policy_gate_from_activity.py` | M | – | – | Mixed-version + final policy installs |
| `32_major_mvc.yml` | `major_mvc_from_activity.py` | M | – | – | `cphaconf mvc on/off` |
| `61_restore_original_active.yml` | `cluster_phase_control.py restore` | M | P | – | Fail traffic back |
| `62_support_diff.yml` | (report diff) | M | P | – | Baseline-vs-final delta |
| `60_postcheck.yml` | `postcheck_gateways.py` | M | P | – | Final package/health verdict |
| `22_monitor_gateways.yml` | `monitor_gateways.py` | opt | opt | – | Sampling monitor during long waits |
| `25_check_jhf_installed.yml` | (query helper) | opt | opt | – | Ad-hoc take verification |
| `site_preexecute.yml` | `stage_packages_cprid.py` | opt | opt | opt | MDS→gateway CPRID staging when used |

### 11.2 Shared plumbing: `checkpoint_cluster_upgrade.py`

The foundation library every helper imports (`import checkpoint_cluster_upgrade as c`):

- `SshPty` — paramiko-based interactive PTY that treats Gaia clish as a stateful shell: prompt detection, `run(command, timeout)`, `sendline`/`drain_pending` for interactive dialogs (CPUSE uninstall confirmations), `enter_expert(password)` to drop to expert mode. This wrapper exists because Check Point CLIs are menu-driven and pager-prone; naive exec channels hang.
- Phases when run directly: `precheck` (per member: `cphaprob state` — exactly one ACTIVE + one STANDBY, no active PNOTEs; monitored interfaces vs required count; core processes via `cpwd_admin list`; ICAP per `icap_mode`) and `support` capture (fixed command battery incl. `show installer status all`, `show installer policy/packages`, routes, ARP, interfaces — same battery both times so `62_support_diff` is meaningful).
- Sample precheck summary: `192.0.2.20 CP-FW-A: state=STANDBY, pnotes=ok, interfaces=ok (required=3, monitored=3, virtual=2), icap=skipped` — the summary line is what the playbook asserts on.

### 11.3 `discover_checkpoint_targets.py` — target resolution

- Input: `--mds-host`, `--target-ips` (comma/newline list), `--preferred-domain`, optional `--output` JSON path. Env: `CP_PASSWORD`, `CP_EXPERT_PASSWORD` (hard exit if missing — every helper enforces this).
- How it works: SSH to the MDS → `mgmt_cli -r true` (keyless, root-trust on the management) → paginate all regular domains and each domain's `show gateways-and-servers` response. Address matching reads only structured address fields on gateway, cluster, member, and interface objects; comments and arbitrary text cannot match. A matched cluster is expanded by UID with `show simple-cluster`, and the policy package comes from the matched object's policy data.
- Decisions: every query, response shape, and pagination total is required to be complete. Transport, authentication, API, malformed-response, repeated-page, or incomplete-page errors fail the whole scan. All requested IPs must belong to one object after every domain has been scanned; not-found and cross-object/cross-domain ambiguity are distinct failures. A preferred domain only filters among domains actually discovered and cannot inject a missing domain.
- Output: `discovered` JSON (domain, cluster name/mode, members with roles, policy package) consumed by the runner's activity-plan build; optionally written into a SQLite db (`--db-path/--change-id`) for the readiness evidence trail.

### 11.4 `validate_mds_packages.py` — package presence + integrity

- Reads the activity plan; for every step with a `source_path`, SSH to the MDS, expert mode, then per file: exists (`ls`), size, `sha1sum`/`sha256sum`.
- Decisions: missing `mds_host`/members = rc 2 (config error); file missing = rc 2 `package not found on MDS: <path>`; declared checksum mismatch = rc 2 naming the exact hash kind. Steps with no declared checksum log the computed hashes (evidence) and pass — checksum declaration is enforced upstream at plan-validation for install steps.
- Log sample / decision: `ERROR: SHA256 mismatch for /var/log/tmp/Check_Point_R82_JHF_T91.tgz` → phase fails → readiness (if pre-CHG) or engineer remediation (if mid-run). A tampered or truncated upload can never reach a gateway.

### 11.5 `validate_deployment_agent.py` — DA readiness contract

- Determines `required_build`: explicit plan value, or inferred from the offline package filename (`DeploymentAgent_000002771_1.tgz` → 2771) when the activity installs a DA.
- Per member: `show installer status all` → parse `Build number: (\d+)`. Unparseable build = failure (rc 2). Below-required with NO offline package declared/found on MDS (path + optional SHA verify, via expert `remote_file_metadata`) = failure with the exact remediation message. Below-required WITH a valid offline package = pass-with-instruction: "Run the install-deployment-agent step before CDT/CPUSE package execution."
- No `required_build` at all → informational only (rc 0 with WARNING) — readiness does not block patch activities on DA currency, it blocks on *parseability* (a gateway whose DA can't answer is a real risk).
- Real log (CHG_EXAMPLE): `Required minimum DA build: not declared` … `Build number: 2771 (agent build is up to date)` → rc 0.
- Production note: `Update from cloud` status is explicitly treated as informational — air-gapped gateways rely on the offline package path. Pair with `tools/cpuse_da_fetch.py` (sk92449 fetch + checksum verify) to keep the offline package current.

### 11.6 `validate_package_prerequisites.py` — per-step CPUSE gate

- For the step under execution, selects the intended member from the captured cluster state, pulls CPUSE inventory with `show installer packages`, and evaluates the plan's `requires_present` / `requires_absent` lists. First-member and second-member phases fail closed when the captured original ACTIVE/STANDBY identities are missing or differ from the plan.
- Alias normalization covers `.tar`↔`.tgz`, basename and path forms, underscore/hyphen spacing, and common `Take 91`/`JHF_T91`/`T91` variants. CPUSE may display a `.tgz` identity when the approved source used `.tar`, so every present/absent comparison goes through `token_variants`.
- Major upgrades additionally run `show snapshots`: parses snapshot names, verifies restore-point capacity, and prints stale Blink/upgrade snapshot cleanup candidates (informational unless capacity is actually blocking).
- Decisions: required-present missing or required-absent found returns rc 2 with the exact token and member. Major-upgrade restore-point capacity that cannot be parsed or is below the configured floor also returns rc 2. Removal requires an explicit package name or supported Take/JHF alias; prerequisite keys are not treated as a general uninstall list.

### 11.7 `generate_cdt_candidates_from_activity.py` — controlled candidate file

- On the MDS: runs `/opt/CPcdt/CentralDeploymentTool -generate -candidates=<csv> -deploymentplan=<xml> -server=<CMA-IP>` to produce CDT's own candidate list plus the deployment plan XML for the package step (real log: `The generated candidates list is: /var/log/tmp/CHG_EXAMPLE_..._cdt_candidates.csv`).
- Then rewrites the candidate CSV: the intended member gets `upgrade_order=1`, every other row is forced to `-` (disabled), preserving CDT's exact CSV dialect (`render_preserving_cdt_format`).
- Self-audit before returning: re-reads the file and asserts exactly one enabled row matching the intended target IP and exactly one disabled peer — `ERROR: controlled candidate file does not enable exactly the selected target` = rc 2, and execution never happens. This guard is the single most important safety property of the CDT path: CDT can *never* see a candidate file that would let it touch the wrong member.

### 11.8 `execute_cdt_from_activity.py` — guarded execution

- Re-parses the controlled candidate file again (trust nothing on disk): exactly 2 rows, exactly 1 enabled + 1 disabled, else rc 2. Prints the selected target (`Selected execution target: CP-FW-A 192.0.2.20 (STANDBY)`).
- Without `--execute`: prints the planned command and returns rc 3 ("planned, not executed") — the playbook maps this to a controlled stop. With it: runs `CentralDeploymentTool -execute -candidates=... -deploymentplan=... -server=<CMA>` and waits (CDT itself manages transfer → CPUSE install → reboot → reconnect).
- Output classification: fatal markers (`candidate list error has occurred`, `an error has occurred in stage`, `installation failed`, `execution finished with errors`, `entity: `) → rc 2; any other `error` occurrence → rc 2 as "unclassified" — EXCEPT known-benign mail-notification failures (`failed to send mail`, `email server is not configured`, ...) which are tolerated. Unclassified-fails-closed is deliberate: a new CDT error string halts the run for a human rather than being guessed at.

### 11.9 `direct_package_step_from_activity.py` — direct CPUSE path

- Target selection per phase: `first-member` = original standby, `second-member` = original active (from the captured cluster state), `install-deployment-agent` = all members.
- Command synthesis per step: DA → `installer agent install <path>` + `show installer status all`; install/upgrade → `installer import local` → `installer verify` → `installer install` → status + packages; remove → interactive uninstall.
- DA parallelism: deployment_agent install/upgrade with >1 target runs members concurrently (ThreadPoolExecutor), aggregating per-member failures into one error. Everything else stays strictly sequential.
- Uninstall choreography (the subtle part): `run_interactive_uninstall` drives CPUSE's confirmation dialog over the PTY; `blocked_hotfixes_from_uninstall` parses "Uninstall the hotfix(es) X and try again" into an ordered dependency list; after success it waits for CPUSE's automatic reboot (`wait_for_auto_reboot_start`), only falling back to an explicit `reboot` if `--explicit-reboot-fallback` was set. After SSH returns, a fresh authenticated query must confirm that the exact resolved package identity is absent before cluster-readiness checks may proceed.
- Non-interactive commands run through `clish -c` with a captured exit status. A missing or nonzero status fails the phase. Output markers remain a secondary failure signal and can never be the sole success verdict.

### 11.10 `cluster_phase_control.py` — cluster state + traffic movement

Sub-commands used by playbooks 11/23/61: `collect/write_state` (who is ACTIVE/STANDBY → `cluster_initial_state_<CHG>.json` — the file every later phase reads for member ordering); `failover_to <host>` (clusterXL admin down/up sequence then `wait_for_target_active` polling `cphaprob state` until the target owns traffic, timeout = rc 2); `restore_original_active` (same, toward the recorded original); `assert_member_take` (member reports expected take, else rc 2 `reported take X, expected Y`); plus the candidate-file helpers shared with 11.7.

### 11.11 `major_policy_gate_from_activity.py` + `major_mvc_from_activity.py`

- Policy gate, `mixed-version` mode: `mgmt_cli set simple-cluster ... version <target>` + `publish`, then `install-policy` targeting the upgraded member with `allow_partial=true`; a fully clean install at this stage is suspicious — the summary explicitly warns if the expected partial/warning markers are absent. `final` mode: full install-policy, `allow_partial=false`, ANY failure/warning marker = rc≠0. Task completion is watched via `show-task` polling (`wait_task`).
- MVC: phase `mvc-on` → `cphaconf mvc on` on the required member(s) before failover to the upgraded member; `mvc-off` after both members match; each verified by `cphaprob mvc` readback plus `wait_cluster` health polling.

### 11.12 `postcheck_gateways.py` — the final verdict

- Builds final expectations from the WHOLE plan (`final_package_expectations`): net effect of ordered install/remove steps → `expected_present` + `expected_absent` token lists, deduped, alias-normalized (same `token_variants` machinery as 11.6).
- Per member: CPUSE inventory + take + health; every expected-present token must match, every expected-absent must not. `.tar`/`.tgz` identity tolerated (a live defect fixed after the first E2E: the validator compared the MDS `.tar` path against CPUSE's `.tgz` display name and failed a successful run).
- rc 2 on any mismatch → engineer remediation with the exact token and member named.

### 11.13 `monitor_gateways.py` + `stage_packages_cprid.py`

- Monitor: N samples at interval over both members (cphaprob state, PNOTEs, optional take, ICAP per mode) → JSONL; used during long converge windows.
- CPRID staging: for flows where gateways must receive files from the MDS without SSH-from-host: `cprid_util ... -server <member-ip> putfile` from the MDS to `/var/log/tmp/` on each member (mkdir → putfile → stat), then verifies size/hash over SSH (`verification_script` emits one JSON line parsed by `parse_json_line`). Missing source on MDS / member without a CPRID-reachable IP = rc 2 before any copy.


### 11.14 `gateway_support_commands.example.sh` and support evidence

The support script is transferred/executed by the support-capture phase and provides a repeatable command battery before and after maintenance. Its path comes from `execution.support_capture_script` in the activity plan, defaulted by the runner. The capture wrapper records the member, phase label, command output, timeout, and local evidence path. Use the same script revision at baseline and final capture; otherwise a diff can reflect script drift instead of firewall drift.

Expected capture areas include Gaia version/build, CPUSE package and agent status, ClusterXL state/PNOTEs/interfaces, process watchdog status, routes, interfaces, ARP, policy context, and other appliance diagnostics implemented by the script. Individual command failures remain visible in the capture and must not be silently discarded. `62_support_diff.yml` compares normalized baseline and final files and records additions/removals; the postcheck remains the authoritative pass/fail gate because some support deltas are expected.

### 11.15 `tools/cpuse_da_fetch.py` and Deployment Agent currency

This optional supply-chain helper authenticates to Check Point UserCenter, follows the supported Deployment Agent download workflow, downloads the offline agent package, and validates recorded metadata/checksum. It is not called by the current catalog runner because UserCenter credentials and TOTP are separate privileged supply-chain credentials and must not be supplied by a requester.

Recommended production use:

1. Run it in a separate scheduled artifact-acquisition pipeline with UserCenter credentials and TOTP seed sourced from a vault.
2. Malware-scan and checksum the downloaded package, publish it to the internal repository, and stage the approved file on MDS `/var/log/tmp`.
3. Update the internal DA currency record documented in `tools/DEPLOYMENT_AGENT_CURRENCY.md`.
4. Supply the approved DA package/build in a Deployment Agent activity or as a pre-upgrade remediation when `07_validate_deployment_agent.yml` proves the gateways are below the required build.
5. Never treat "latest available" as proof that a specific JHF or Blink image requires that build. A package-specific minimum must come from Check Point release metadata, package contents, support guidance, or a validated failure signature.

### 11.16 Environment and secret contract by component

| Component | Required environment | Optional environment/arguments | Secret handling |
|---|---|---|---|
| Readiness worker | `SN_INSTANCE`, `SN_USERNAME`, `SN_PASSWORD`, `CP_PASSWORD`, `CP_EXPERT_PASSWORD` | poll interval, Ansible path, dry-run/once | Environment file or vault injection; never argv. |
| Implementation worker | same | state file, log dir, poll interval, dry-run/once | Command logging is redacted; protect worker state and journal. |
| Runner | ServiceNow and Check Point variables | CHG sys_id, start/stop phase, tester settings | Normal entry is worker-launched; lab bypass flags forbidden in production. |
| Check Point helpers | `CP_PASSWORD`, `CP_EXPERT_PASSWORD` | username, timeouts, ICAP mode, proxy | Production should split MDS and gateway Expert credentials and retrieve per target. |
| DA acquisition | UserCenter account and TOTP secret | destination, checksum metadata | Separate vault namespace and execution identity. |

A production implementation must remove the current assumption that one Expert secret works on MDS and gateways. Extend the credential resolver to return MDS Gaia/Expert credentials and per-gateway Gaia/Expert credentials independently, then inject only the credentials needed by each subprocess.

### 11.17 Return codes, decisions, and remediation ownership

| Return/result | Produced by | Worker interpretation | Required action |
|---|---|---|---|
| rc 0 | playbook/runner | phase/run succeeded | Continue; on final success perform bookkeeping. |
| rc 2 | most validation/execution helpers | failed phase | Create/dedupe Engineer Remediation CTASK and preserve resume state. |
| rc 3 | guarded direct/CDT helper without execute approval | planned but not executed | Stop; correct invocation/governance, do not mark success. |
| rc 20 | runner at tester gate | intentional pause | Wait for the exact Tester validation gate CTASK to reach Closed Complete. |
| rc 130 | interrupted runner | interrupted failure | Investigate process/host interruption; resume only after engineer approval. |
| readiness exception/rc nonzero | readiness worker | pre-CHG failure | Close automated SCTASK Incomplete; create manual Firewall Deploy SCTASK; no CHG. |
| ServiceNow bookkeeping exception after rc 0 | implementation worker | firewall work succeeded, bookkeeping incomplete | Persist completed state, post warning, finish record updates manually without rerunning firewall phases. |

Resume is phase-oriented, not command-oriented. The runner writes `resume_state.json` with failed phase, playbook, step, and log. The engineer addresses the cause, documents evidence in `u_checkpoint_resume_*`, and closes the remediation CTASK Complete. The worker launches `--start-at <failed_phase>`; each phase must therefore remain idempotent or fail closed when its desired end state already exists.

---

## 12. Sanitized example scenarios

### 12.1 Scenario A — governed JHF Take 91 install, end to end (CHG_EXAMPLE/CHG_EXAMPLE pattern)

1. Requester orders CheckPoint FW Maintenance Activity: Software Patch Activity, lab, targets `192.0.2.20, 192.0.2.21`, MDS `192.0.2.10`, target version R82, uploads `cpuse_packages.csv` (one install row for `Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar` with SHA256), tester gate = yes.
2. Intake BR fires on RITM insert → `Automated Check Point readiness validation - Software Patch Activity` SCTASK within seconds.
3. Readiness worker (≤60s): discovery resolves both IPs to `CP-FW-Cluster` in `CMA_A_Server`; precheck summary:
   ```text
   192.0.2.20 CP-FW-A: state=STANDBY, pnotes=ok, interfaces=ok (required=3, monitored=3, virtual=2), icap=skipped
   192.0.2.21 CP-FW-B: state=ACTIVE,  pnotes=ok, interfaces=ok (required=3, monitored=3, virtual=2), icap=skipped
   ```
   MDS package + SHA verified; DA build 2771 parsed on both. SCTASK → Closed Complete, `readiness_status=ready/automated`.
4. Readiness-to-CHG BR mints the CHG (+ Implementation & post-implementation CTASKs); relabel BR neutralizes the change-model default tasks. Approvals happen; CHG reaches Implement.
5. Implementation worker validates the full gate, launches the runner. Phases post one CHG work note each (mirrored to the CTASK). CDT candidate generation log (real):
   ```text
   ===== MDS: /opt/CPcdt/CentralDeploymentTool -generate -candidates=/var/log/tmp/CHG_EXAMPLE_install_..._cdt_candidates.csv
         -deploymentplan=/var/log/tmp/CHG_EXAMPLE_install_..._cdt_plan.xml -server=192.0.2.11 =====
   *N* [Main]: Please wait while generating the installation candidates list...
   *N* [Main]: The generated candidates list is: /var/log/tmp/CHG_EXAMPLE_..._cdt_candidates.csv
   ```
   Controlled rewrite enables only CP-FW-A (standby); guarded execute installs T91; member reboots and rejoins; failover moves traffic to it.
6. Runner exits rc 20; worker → `waiting_tester`, tester CTASK is the only actionable record. Tester validates and closes it Complete → worker resumes `--start-at second-member`.
7. Member 2 same loop; original active restored (catalog said preserve=yes); final capture + support diff + postcheck (T91 present on both, healthy) → rc 0.
8. Bookkeeping: Final-validation CTASK created, Implementation CTASK closed with evidence, default tasks closed → change model advances CHG to Review. Human reviews and closes.

Outcome vs decision summary: every green transition above was a *field* (readiness_status, approval, CTASK state, resume token) — at no point did any component parse prose to decide anything.

### 12.2 Scenario B — readiness failure → manual remediation

Requester typo: `target_ips = 192.0.2.99`. Discovery: `ERROR: target IPs ['192.0.2.99'] were not found in any queried gateway/cluster object` → readiness SCTASK Closed Incomplete (`status=failed`), remediation SCTASK created with the evidence dir. Firewall Deploy engineer corrects course: confirms the intended target really is `.7`, decides the request as submitted is wrong → sets `u_checkpoint_readiness_status=rejected`, closes the task → BR closes the RITM Incomplete with the reason. No CHG ever existed. (Had the engineer instead fixed the environment — e.g. staged a missing package — they would set `ready`/`manual` and close Complete → CHG.)

### 12.3 Scenario C — mid-flight failure → engineer remediation → resume (the CHG_EXAMPLE class)

Postcheck failure example (real `resume_state.json`):
```json
{"failed_phase": "postcheck", "failed_playbook": "60_postcheck.yml", "failed_step": "",
 "failed_log": ".../runs/CHG_EXAMPLE_20260712131818/logs/postcheck_none_60_postcheck.yml.log",
 "time": "2026-07-12T..."}
```
Worker stamps `u_checkpoint_failed_phase=postcheck`, creates the engineer remediation CTASK pointing at the exact log. Engineer reads the log (in this historical case: `.tar` vs `.tgz` naming defect — fixed in `postcheck_gateways.py`), fixes the cause, sets `u_checkpoint_resume_status=approved` (resume phase defaults to `postcheck`), closes Complete → worker resumes exactly that phase; rc 0 → normal bookkeeping. The firewalls were never touched during remediation — postcheck is read-only, and resume re-ran only the check.

Two hard-won rules encoded here: never close change-model default tasks early (auto-advances the CHG out from under the worker — that was the actual CHG_EXAMPLE incident), and retire legacy readiness SCTASKs as Closed Incomplete, never Complete/Skipped (a Complete close with ready fields is a CHG-minting event).

### 12.4 Scenario D — Deployment Agent install

CPUSE Package CSV: `1,install,DeploymentAgent_000002771_1.tgz,,<sha256>,deployment_agent,Install offline DA`. Readiness passes (build inferred = 2771); CHG governance identical; runner selects the short direct branch (every step is `deployment_agent`); both members get `installer agent install /var/log/tmp/DeploymentAgent_000002771_1.tgz` in parallel, `show installer status all` confirms `Build number: 2771 (agent build is up to date)` on each; post-readiness asserts installed==expected on every member. No failover, no tester gate, no traffic movement; typical wall time minutes, not hours.

## 13. Requester input file formats

### 13.1 CPUSE Package CSV/XLSX (`cpuse_package_upload`, mandatory)

```csv
sequence_number,action,package_name,sha1,sha256,package_type,notes
1,uninstall,Check_Point_R82_JHF_T89.tgz,,,jhf,Remove old take first
2,install,Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar,,<sha256>,jhf,Install Take 91
```

- `action`: install / upgrade / uninstall (aliases remove/removal/delete accepted). A blank value keeps the install default; any other value is rejected rather than treated as an install.
- `package_type`: `jhf` / `wrapper` / `blink` / `deployment_agent` / `other`; if omitted it is inferred (deployment+agent in the name → `deployment_agent`).
- `sha1`/`sha256`: strongly recommended; enforced whenever declared. Source path defaults to `/var/log/tmp/<package_name>` on the MDS.
- Steps execute in `sequence_number` order per member.

Uploaded ticket files must be `.csv` or `.xlsx`. The workers reject empty,
absolute, traversal-shaped, and unsupported filenames before download. They
store accepted files as `<attachment_sys_id>.csv` or `.xlsx` under the run's
attachment directory; the original filename is metadata used only to identify
the package or dependency role. XLSX parsing follows the workbook relationship
for the first sheet and uses cell coordinates, so omitted or reordered cells do
not shift values into another column.

### 13.2 CPUSE Dependency Checklist CSV/XLSX (`cpuse_dependency_upload`, optional)

```csv
package_name,requires_present,requires_absent,notes
Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar,,Check_Point_R82_JHF_T89,T89 must be removed first
```

Feeds `requires_present`/`requires_absent` in the activity plan, evaluated by `validate_package_prerequisites.py` with full alias normalization.

## 14. Build verification checklist

Run through in order; each line was a real failure mode during the original build.

1. `GET /api/now/table/sys_user?sysparm_limit=1` with the integration account → 200.
2. All 16 `u_` fields: PATCH a value, GET it back (silent-discard check).
3. BR compile test server-side; then submit a real catalog order → readiness SCTASK exists in <60s.
4. Break `target_ips` → readiness fails, remediation SCTASK appears, RITM survives; reject → RITM Closed Incomplete, no CHG.
5. Clean submission → CHG appears only after readiness Complete; exactly ONE CHG (close/reopen the readiness task — no second CHG).
6. Change-model default tasks appear relabeled and open; CHG stays in Implement (watch ≥2 min — the auto-advance defect struck within seconds).
7. `systemctl status` both workers; `journalctl -f` shows polling; kill -9 a worker mid-poll → restarts, no duplicate launch (state file + lock).
8. Dry-run the runner manually: `python3 servicenow_checkpoint_runner.py --chg-sys-id <sys_id> --dry-run`.
9. Lab E2E with `--simulate-gates` off: verify rc 20 park, tester close → resume; verify a forced phase failure → remediation CTASK → approve → resume.
10. Confirm every phase log exists under `runs/<CHG>_*/logs/` and was uploaded to the Implementation CTASK.

## 15. Troubleshooting matrix

| Symptom | Likely cause | Fix |
|---|---|---|
| Catalog order → no readiness SCTASK | Intake BR compile failure (silent) or wrong `cat_item` sys_ids | Compile-test; fix constants; resubmit |
| Readiness SCTASK never picked up | Readiness worker down / wrong short-description prefix | `systemctl status`; prefix must start exactly `Automated Check Point readiness validation` |
| Two CHGs for one RITM | Duplicate guard removed, or readiness task re-closed Complete | Restore guard (7.2); retire legacy tasks Closed Incomplete only |
| CHG jumps Implement→Review in seconds | Default CTASKs being closed at creation | Relabel BR must NOT close (7.3); recover via `setWorkflow(false)` push back to Implement |
| Worker sees CHG but won't launch | Governance gate miss — read the work note it posted | Fix the failing leg (approval, readiness, CTASK) |
| Runner refuses: "not in Implement" | Change model moved the CHG | See auto-advance row above |
| CDT "candidate list error" | Controlled file drifted / wrong CMA IP | Regenerate 10; verify `-server=<CMA-IP>` not MDS IP |
| Postcheck fails though install worked | Package name alias gap | Extend `token_variants`; `.tar`/`.tgz`/extensionless/Take forms |
| Tester close doesn't resume | Task closed Incomplete, or prefix mismatch | Must be exact prefix + Closed Complete |
| Fixed worker code, behavior unchanged | Workers load at start | `sudo systemctl restart ...` (runner/helpers need no restart) |
| BR edits via REST don't stick | Field-level ACL or silent normalization | GET-and-diff after PATCH; use background script fallback with `sysparm_ck` for stubborn fields |

## 16. Production hardening (delta from lab)

1. Dedicated integration account with minimum ACLs (read RITM/variables/attachments; write sc_task state+`u_checkpoint_*`+notes; write change_request notes+`u_checkpoint_*`; write change_task; attach files) — the lab's `admin` is a shortcut, not a pattern. OAuth over basic auth.
2. CyberArk/vault for `/etc/snow-checkpoint-worker.env` and Check Point credentials; TOTP seed for the UserCenter fetch account in the same vault; rotation on schedule.
3. Field ACLs on `u_checkpoint_readiness_*`/`u_checkpoint_resume_*`: writable only by the integration account and the Firewall Deploy group — these fields ARE authorization.
4. Per-device Check Point credentials; readiness worker + runner already take them from env only.
5. Optional MID server per Section 3.3 if platform policy requires ServiceNow-initiated flows — keep both validation layers regardless.
6. Monitoring: systemd unit alerts, plus a synthetic weekly catalog order in a test category exercising readiness end-to-end.
7. Deliberate failure drill: schedule the Scenario C loop (forced postcheck failure) quarterly — the remediation path must stay exercised, not just designed.


## 17. End-to-end replication runbook

### 17.1 Ownership and handoff

| Workstream | ServiceNow developer | Automation engineer | Firewall engineer/security |
|---|---|---|---|
| Catalog, fields, forms, ACLs, BRs, change model | Accountable | Consulted on contract | Consulted |
| Integration/MID accounts and OAuth | Accountable | Consumes credentials | Security approves |
| Workers, runner, Ansible, systemd | Consulted | Accountable | Consulted |
| MDS/CDT/CPRID and gateway credentials | Informed | Implements interfaces | Accountable |
| Package acquisition/checksum/staging | Informed | Automates validation | Accountable for approval |
| E2E certification and rollback | Joint | Joint | Joint |

### 17.2 Build order

1. Freeze contracts: copy the repository, pin a commit/release, record supported ServiceNow and Check Point versions, and agree the three catalog activity values.
2. Prepare Check Point: install/validate CDT on MDS, prove CPRID to each gateway, stage test packages under `/var/log/tmp`, verify SSH/Expert access, and create member CIs.
3. Prepare ServiceNow identities: create Firewall Deploy/tester groups, REST integration account, optional MID account, roles, and vault entries.
4. Build ServiceNow schema: dictionary fields and choices first; PATCH/GET every field to prove writes persist.
5. Build catalog: item, variables, choices, upload-only client behavior, sample attachments, REQ/RITM descriptions.
6. Build rules: intake, readiness-to-CHG, model-task relabel, note mirror, catalog-chain completion, and default fulfillment retirement. Compile and side-effect test each independently.
7. Build forms/ACLs: RITM/SCTASK readiness fields, remediation CTASK resume fields, CHG layout, CI/Affected CI related list, approvals and task related lists.
8. Install optional MID: follow Section 3.4 and validate it, while keeping it outside the execution path unless the alternative design is explicitly selected.
9. Build automation host: Python/Ansible environment, repository paths, inventory, reports/runs ownership, protected env file, two systemd units.
10. Static verification: Python compile, Ansible syntax check for every playbook, unit tests for parsers/state decisions, secret scan, and configuration lint.
11. Read-only integration verification: REST dry-runs, MDS discovery, firewall precheck, DA parse, package/checksum validation, prerequisites, and no mutation.
12. ServiceNow record-chain test: real order through readiness success and failure/manual remediation, but hold CHG before Implement.
13. Controlled E2E tests: Deployment Agent idempotent path, software patch install/remove, and two-member major upgrade. Use real tester and engineer gates; do not use `--simulate-gates` or governance bypass.
14. Failure drills: missing package, checksum mismatch, wrong target, unhealthy interface, ambiguous uninstall, CDT candidate contamination, tester rejection, worker restart, transient ServiceNow outage, and deliberate postcheck failure/resume.
15. Production acceptance: evidence review, firewall state reconciliation, ServiceNow chain closure, monitoring alerts, credential rotation test, operational sign-off, and rollback plan.

### 17.3 Network and port matrix

| Source | Destination | Port | Purpose | Inbound exposure |
|---|---|---|---|---|
| REST worker host | ServiceNow instance | TCP 443 | Table/Attachment APIs | None on worker |
| MID host | ServiceNow instance | TCP 443 | MID heartbeat/ECC transport | None on MID |
| Automation host | MDS | TCP 22 | MDS CLI, CDT, mgmt_cli, CPRID orchestration | Internal only |
| Automation host | Gateways/NAT access IPs | TCP 22 | Health, CPUSE, ClusterXL, evidence | Internal only |
| MDS | Gateway management IPs | Check Point CPRID channels per estate policy | Package/log mediation | Internal management plane |
| MDS/CMA | Gateways | Check Point management/SIC ports per supported design | Policy/CDT management | Existing management plane |
| Automation/MID host | AWX | TCP 443 | Optional platform-initiated variant | Not used in reference flow |

Firewall access IP and management IP are separate fields in the activity plan. In NAT environments, SSH health checks use `access_ip`; MDS/CDT candidate matching uses `management_ip`. Never substitute one globally for the other.

### 17.4 Static verification commands

Run from the repository root with the intended virtual environment:

```bash
python3 -m py_compile \
  checkpoint-servicenow-automation/servicenow_checkpoint_runner.py \
  checkpoint-servicenow-automation/servicenow_checkpoint_worker.py \
  checkpoint-servicenow-automation/servicenow_checkpoint_readiness_worker.py

for p in checkpoint-servicenow-automation/ansible/playbooks/*.yml; do
  .venv-ansible/bin/ansible-playbook --syntax-check "$p" \
    -i checkpoint-servicenow-automation/ansible/inventory/hosts.yml
done

systemctl is-enabled snow-checkpoint-readiness-worker snow-checkpoint-worker
systemctl is-active snow-checkpoint-readiness-worker snow-checkpoint-worker
```

Also validate that the environment file is root-owned mode 0600, services have zero restart loops, the state file is writable only by the worker identity, and no secret appears under `runs/`, journals, ServiceNow work notes, or process argv.

### 17.5 Activity acceptance matrix

| Activity | Required certification scenario | Must prove |
|---|---|---|
| Software patch install | Install one JHF/wrapper on a two-member cluster | Standby selected first, one controlled CDT candidate, tester pause, second member, original active restoration, package present on both. |
| Software patch remove | Remove using full name and aliases such as `Take 91` | Version-aware resolver finds installed CPUSE identity via inventory/history, removal only if present, package absent on both, reboot observed rather than assumed. |
| Version upgrade | Supported two-member major upgrade | Blink/package validated, mixed-version policy and MVC gates, tester gate, second member, final policy, MVC off, versions equal and healthy. |
| Deployment Agent | Install/verify offline DA on both members | Members handled concurrently, no failover/support-capture choreography; the requested build is enforced as a minimum compatibility floor on every member, with equal/higher builds retained and no downgrade. |
| Readiness remediation | Missing package | Automated task fails, one manual task appears, no CHG until field-driven ready closure. |
| Execution remediation | Deliberate read-only postcheck failure | One engineer task appears with phase/evidence; approved closure resumes exactly failed phase; rejected closure never resumes. |
| Worker resilience | Restart worker during idle and gate wait | No duplicate launch, durable state survives, completed CHG never reruns. |
| ServiceNow lifecycle | Close CHG successful/unsuccessful | Implementation/final tasks correct, CHG Review/Close behavior correct, RITM and REQ reconcile to matching terminal state. |

### 17.6 Evidence required for go-live

- ServiceNow update-set/application manifest and exported record inventory.
- Business-rule compile results and real record side-effect tests.
- ACL test results for requester, tester, Firewall Deploy, integration, MID, and administrator roles.
- Python/Ansible static-test output and parser/state-machine unit tests.
- Three activity E2E reports plus readiness and engineer-remediation drills.
- CDT raw and controlled candidate evidence proving one enabled target.
- Baseline/final support captures and diff.
- Security review of credentials, OAuth/vault integration, logs, file permissions, and network rules.
- Operational runbook, on-call ownership, ServiceNow/MDS/worker monitoring, backup/restore, upgrade procedure, and rollback criteria.

The sanitized scenarios in this guide illustrate expected behavior; they are not certification of a new estate.

---

## Appendix A — Business rule script bodies (as deployed)

Replace every environment-specific item, group, and assignee sys_id before activation. In A.1, use the current consolidated catalog item sys_id and remove unused legacy item constants.

### A.1 Intake: create readiness task (`sc_req_item`, after insert+update)

```javascript
(function executeRule(current, previous) {
    try {
        var PATCH_ITEM = '<CATALOG_ITEM_SYS_ID_1>';
        var UPGRADE_ITEM = '<CATALOG_ITEM_SYS_ID_2>';
        var LEGACY_ITEM = '<CATALOG_ITEM_SYS_ID_3>';
        var cat = current.cat_item ? current.cat_item.toString() : '';
        if (cat != PATCH_ITEM && cat != UPGRADE_ITEM && cat != LEGACY_ITEM)
            return;

        var marker = '[CHECKPOINT_AUTOMATION_INTAKE]';

        // Only act on open RITMs; never retro-create readiness tasks on closed/legacy requests.
        var ritmState = current.state ? current.state.toString() : '';
        if (current.active.toString() == 'false' || ritmState == '3' || ritmState == '4' || ritmState == '7')
            return;

        // Any prior readiness task (automated, legacy human, or manual remediation) means intake already ran.
        var existingTask = new GlideRecord('sc_task');
        existingTask.addQuery('request_item', current.sys_id);
        var qc = existingTask.addQuery('short_description', 'STARTSWITH', 'Automated Check Point readiness validation');
        qc.addOrCondition('short_description', 'STARTSWITH', 'Firewall Deploy readiness validation');
        qc.addOrCondition('short_description', 'STARTSWITH', 'Firewall Deploy manual readiness remediation');
        existingTask.setLimit(1);
        existingTask.query();
        if (existingTask.next())
            return;

        var vars = current.variables;
        function val(name) { try { return vars[name] ? vars[name].toString() : ''; } catch (e) { return ''; } }
        function label(name) { try { return vars[name] ? vars[name].getDisplayValue() : ''; } catch (e) { return val(name); } }
        function line(label, value) { return label + ': ' + (value || '-') + '\n'; }
        function yesNo(name) { var v = (label(name) || val(name) || '').toString(); return v || '-'; }
        function attachmentSummary(record) {
            var out = [];
            var att = new GlideRecord('sys_attachment');
            att.addQuery('table_sys_id', record.getUniqueValue());
            att.orderBy('file_name');
            att.query();
            while (att.next())
                out.push(att.getValue('file_name') + ' (' + att.getDisplayValue('size_bytes') + ' bytes)');
            return out.join('\n') || '-';
        }
        function activityLabel() {
            var activity = label('activity_type') || val('activity_type');
            if (activity == 'version_upgrade_activity') return 'Version Upgrade Activity';
            if (activity == 'software_patch_activity') return 'Software Patch Activity';
            if (activity == 'deployment_agent_install') return 'Deployment Agent Install';
            return activity;
        }

        var activity = activityLabel();
        var env = label('environment') || val('environment');
        var targetIps = val('target_ips');
        var currentVersion = val('current_version');
        var targetVersion = val('target_version');
        var mdsHost = val('mds_host');
        var icap = label('icap_mode') || val('icap_mode');
        var preserve = yesNo('preserve_original_active');
        var tester = yesNo('tester_gate');
        var pkgUpload = label('cpuse_package_upload') || val('cpuse_package_upload') || 'Attached CPUSE package CSV/XLSX required';
        var depUpload = label('cpuse_dependency_upload') || val('cpuse_dependency_upload') || 'Optional dependency checklist attachment';
        var special = val('special_instructions');
        var attachments = attachmentSummary(current);

        var basic = marker + '\n' +
            line('Activity Type', activity) +
            line('Environment', env) +
            line('Firewall IPs', targetIps) +
            line('MDS Host', mdsHost) +
            line('Current Version', currentVersion) +
            line('Target Version', targetVersion) +
            line('CPUSE Package Upload', pkgUpload);

        var detail = marker + '\n' +
            line('Requested Item', current.number) +
            line('Activity Type', activity) +
            line('Environment', env) +
            line('Target IPs', targetIps) +
            line('MDS Host', mdsHost) +
            line('Current Version', currentVersion) +
            line('Target Version', targetVersion) +
            line('ICAP Mode', icap) +
            line('Preserve Original Active', preserve) +
            line('Tester Gate', tester) +
            line('CPUSE Package Upload', pkgUpload) +
            line('CPUSE Dependency Checklist Upload', depUpload) +
            line('All Request Attachments', attachments) +
            line('Package Staging Policy', 'Backend enforced: CPRID from MDS') +
            line('MDS Package Repository', 'Backend/readiness enforced: /var/log/tmp on the MDS') +
            line('Automation Engine Policy', 'Backend selected: CDT for controlled package execution/major upgrade where suitable; SSH/CLI for health checks, discovery, CPRID verification, and remediation') +
            line('Readiness Flow', 'Automated readiness SCTASK validates resources first; failed automation creates a manual Firewall Deploy remediation SCTASK; CHG is created only after readiness is explicitly ready') +
            line('Special Instructions', special);

        current.approval = 'approved';
        current.stage = 'fulfillment';
        current.u_checkpoint_readiness_status = 'pending';
        current.u_checkpoint_readiness_source = 'automated';
        current.u_checkpoint_readiness_summary = 'Automated readiness SCTASK created; waiting for local worker validation.';
        current.u_checkpoint_readiness_evidence = '';
        current.short_description = 'Check Point Firewall Maintenance - ' + (activity || 'Firewall activity');
        current.description = detail;
        current.work_notes = 'RITM captured requester inputs. Automated Check Point readiness validation SCTASK will validate MDS/CMA/FW reachability, target resolution, package CSV/XLSX, package presence/checksums in /var/log/tmp on the MDS, Deployment Agent readiness, package dependencies/removal identity, CPRID readiness, ICAP/tester requirements, and cluster health. CHG creation is blocked until readiness is explicitly ready.';
        current.setWorkflow(false);
        current.update();

        if (current.request) {
            var req = new GlideRecord('sc_request');
            if (req.get(current.request.toString())) {
                req.short_description = 'Check Point Firewall Maintenance - ' + (activity || 'Firewall activity');
                req.description = basic;
                req.work_notes = 'REQ contains the basic request summary. Detailed requester input and attachments are stored on ' + current.number + '. Automated readiness must pass or be manually remediated before CHG creation.';
                req.setWorkflow(false);
                req.update();
            }
        }

        var task = new GlideRecord('sc_task');
        task.initialize();
        task.request_item = current.sys_id;
        task.assignment_group = '<FIREWALL_DEPLOY_GROUP_SYS_ID>';
        task.u_checkpoint_readiness_status = 'pending';
        task.u_checkpoint_readiness_source = 'automated';
        task.u_checkpoint_readiness_summary = 'Automated readiness is pending local worker validation.';
        task.u_checkpoint_readiness_evidence = '';
        task.short_description = 'Automated Check Point readiness validation - ' + (activity || 'Check Point firewall activity');
        task.description = 'Automation-owned readiness SCTASK. The local ServiceNow Check Point readiness worker validates the request and resources before CHG creation. On success, it closes this SCTASK with [CHECKPOINT_READINESS_READY], which allows CHG creation. On failure, it closes this SCTASK incomplete/canceled and creates a manual Firewall Deploy remediation SCTASK. Do not close this task manually unless intentionally bypassing automation with the readiness marker.';
        task.work_notes = 'Automated readiness validation SCTASK created from ' + current.number + '. Waiting for the local ServiceNow Check Point readiness worker.';
        task.insert();
    } catch (ex) {
        gs.error('Check Point automated readiness SCTASK rule failed: ' + ex);
    }
})(current, previous);
```

### A.2 Readiness SCTASK to CHG (`sc_task`, after update)

```javascript

(function executeRule(current, previous) {
    try {
        if (!current.request_item)
            return;
        var shortDesc = current.short_description ? current.short_description.toString() : '';
        var isAutomatedReadiness = shortDesc.indexOf('Automated Check Point readiness validation') === 0;
        var isManualReadiness = shortDesc.indexOf('Firewall Deploy manual readiness remediation') === 0 || shortDesc.indexOf('Firewall Deploy readiness validation') === 0;
        if (!isAutomatedReadiness && !isManualReadiness)
            return;

        var ritm = new GlideRecord('sc_req_item');
        if (!ritm.get(current.request_item.toString()))
            return;

        var taskState = current.state ? current.state.toString() : '';
        var readinessStatus = current.u_checkpoint_readiness_status ? current.u_checkpoint_readiness_status.toString() : '';
        var readinessSource = current.u_checkpoint_readiness_source ? current.u_checkpoint_readiness_source.toString() : '';
        var readinessSummary = current.u_checkpoint_readiness_summary ? current.u_checkpoint_readiness_summary.toString() : '';
        var readinessEvidence = current.u_checkpoint_readiness_evidence ? current.u_checkpoint_readiness_evidence.toString() : '';

        if (isManualReadiness && (taskState == '4' || readinessStatus == 'rejected' || readinessStatus == 'not_viable')) {
            ritm.u_checkpoint_readiness_status = readinessStatus || 'rejected';
            ritm.u_checkpoint_readiness_source = readinessSource || 'manual';
            ritm.u_checkpoint_readiness_summary = readinessSummary || 'Manual readiness rejected; request is not viable for CHG creation.';
            ritm.u_checkpoint_readiness_evidence = readinessEvidence;
            ritm.active = false;
            ritm.state = '4';
            ritm.work_notes = 'Manual readiness rejected or closed incomplete. RITM closed incomplete; no CHG will be created. Summary: ' + ritm.u_checkpoint_readiness_summary;
            ritm.update();
            return;
        }

        if (taskState != '3' && taskState != '7')
            return;
        if (readinessStatus != 'ready')
            return;

        if (previous && previous.state && previous.state.toString() == current.state.toString() && previous.u_checkpoint_readiness_status && previous.u_checkpoint_readiness_status.toString() == readinessStatus)
            return;

        ritm.u_checkpoint_readiness_status = 'ready';
        ritm.u_checkpoint_readiness_source = readinessSource || (isAutomatedReadiness ? 'automated' : 'manual');
        ritm.u_checkpoint_readiness_summary = readinessSummary || 'Readiness validated and approved for CHG creation.';
        ritm.u_checkpoint_readiness_evidence = readinessEvidence;
        ritm.update();

        var existing = new GlideRecord('change_request');
        existing.addQuery('parent', ritm.sys_id);
        existing.addQuery('description', 'CONTAINS', '[CHECKPOINT_AUTOMATION]');
        existing.setLimit(1);
        existing.query();
        if (existing.next())
            return;

        var vars = ritm.variables;
        function val(name) { try { return vars[name] ? vars[name].toString() : ''; } catch (e) { return ''; } }
        function label(name) { try { return vars[name] ? vars[name].getDisplayValue() : ''; } catch (e) { return val(name); } }
        function line(label, value) { return label + ': ' + (value || '-') + '\n'; }
        function yesNo(name) { var v = (label(name) || val(name) || '').toString(); return v || '-'; }
        function attachmentSummary(record) {
            var out = [];
            var att = new GlideRecord('sys_attachment');
            att.addQuery('table_sys_id', record.getUniqueValue());
            att.orderBy('file_name');
            att.query();
            while (att.next())
                out.push(att.getValue('file_name') + ' (' + att.getDisplayValue('size_bytes') + ' bytes)');
            return out.join('\n') || '-';
        }
        function activityLabel() {
            var activity = label('activity_type') || val('activity_type');
            if (activity == 'version_upgrade_activity') return 'Version Upgrade Activity';
            if (activity == 'software_patch_activity') return 'Software Patch Activity';
            if (activity == 'deployment_agent_install') return 'Deployment Agent Install';
            return activity;
        }
        function findCiByIpList(ipText) {
            var ips = (ipText || '').split(/[,\n\r\t ]+/);
            for (var i = 0; i < ips.length; i++) {
                var ip = ips[i].replace(/^\s+|\s+$/g, '');
                if (!ip) continue;
                var ci = new GlideRecord('cmdb_ci');
                ci.addQuery('ip_address', ip);
                ci.addQuery('name', 'NOT LIKE', 'Cluster');
                ci.setLimit(1);
                ci.query();
                if (ci.next()) return ci.sys_id.toString();
            }
            return '';
        }
        function addAffected(chgId, ciId) {
            if (!ciId) return;
            var rel = new GlideRecord('task_ci');
            rel.addQuery('task', chgId);
            rel.addQuery('ci_item', ciId);
            rel.setLimit(1);
            rel.query();
            if (rel.next()) return;
            rel.initialize(); rel.task = chgId; rel.ci_item = ciId; rel.insert();
        }

        var activity = activityLabel();
        var env = label('environment') || val('environment');
        var targetVersion = val('target_version');
        var targetIps = val('target_ips');
        var primaryCi = findCiByIpList(targetIps);
        var pkgUpload = label('cpuse_package_upload') || val('cpuse_package_upload') || 'Attached CPUSE package CSV/XLSX required';
        var depUpload = label('cpuse_dependency_upload') || val('cpuse_dependency_upload') || 'Optional dependency checklist attachment';
        var attachments = attachmentSummary(ritm);

        var chg = new GlideRecord('change_request');
        chg.initialize();
        chg.type = 'normal';
        chg.state = '-5';
        chg.parent = ritm.sys_id;
        chg.requested_by = '<FIREWALL_ENGINEER_SYS_ID>';
        chg.opened_by = '<FIREWALL_ENGINEER_SYS_ID>';
        chg.assignment_group = '<FIREWALL_DEPLOY_GROUP_SYS_ID>';
        chg.assigned_to = '<FIREWALL_ENGINEER_SYS_ID>';
        chg.u_change_submitter = '<FIREWALL_ENGINEER_SYS_ID>';
        chg.u_change_submitter_group = '<FIREWALL_DEPLOY_GROUP_SYS_ID>';
        chg.u_ci_category = 'Network Device';
        chg.u_environment = env;
        chg.category = 'Network';
        chg.risk = '3';
        if (primaryCi) chg.cmdb_ci = primaryCi;
        chg.short_description = 'Check Point FW Maintenance - ' + activity + (targetVersion ? ' - ' + targetVersion : '');
        chg.description = '[CHECKPOINT_AUTOMATION]\nSource RITM: ' + ritm.number + '\nSource Readiness SCTASK: ' + current.number + '\n' +
            line('Activity Type', activity) +
            line('Environment', env) +
            line('Target IPs', targetIps) +
            line('MDS Host', val('mds_host')) +
            line('Current Version', val('current_version')) +
            line('Target Version', targetVersion) +
            line('ICAP Mode', label('icap_mode') || val('icap_mode')) +
            line('Preserve Original Active', yesNo('preserve_original_active')) +
            line('Tester Gate', yesNo('tester_gate')) +
            line('CPUSE Package Upload', pkgUpload) +
            line('CPUSE Dependency Checklist Upload', depUpload) +
            line('All Request Attachments', attachments) +
            line('Package Staging Policy', 'Backend enforced: CPRID from MDS') +
            line('MDS Package Repository', 'Backend/readiness enforced: /var/log/tmp on the MDS') +
            line('Automation Engine Policy', 'Backend selected: CDT for controlled package execution/major upgrade where suitable; SSH/CLI for health checks, discovery, CPRID verification, and remediation') +
            line('Special Instructions', val('special_instructions')) +
            line('Readiness Source', ritm.u_checkpoint_readiness_source) +
            line('Readiness Summary', ritm.u_checkpoint_readiness_summary) +
            line('Readiness Evidence', ritm.u_checkpoint_readiness_evidence);
        chg.justification = 'Readiness validation completed (' + ritm.u_checkpoint_readiness_source + ') and confirmed the requested Check Point firewall maintenance is ready for change governance.';
        chg.implementation_plan = 'Implementation CTASK is the primary execution driver. Automation phase status and evidence will be written to the Implementation CTASK work notes and summarized on the CHG. Package sequence is sourced from the uploaded CPUSE Package CSV/XLSX. Dependency requirements are sourced from the optional CPUSE Dependency Checklist CSV/XLSX. Package staging is performed from the MDS using CPRID, with SSH/CLI used under the hood for health checks and remediation.';
        chg.test_plan = 'Tester CTASKs validate pre-activity, post-first-failover, post-second-member, and final postcheck gates. Post implementation testing is assigned to the change submitter and submitter group.';
        chg.backout_plan = 'Pause workflow at failed gate, create engineer intervention CTASK if required, preserve at least one active cluster member, and follow approved package uninstall/backout or snapshot restore procedure.';
        chg.work_notes = 'CHG created after Firewall Deploy readiness SCTASK ' + current.number + ' was closed. CI Category set to Network Device; Configuration Item set to a firewall member CI, not a cluster CI. Requester-facing catalog is simplified; execution method and staging are backend workflow decisions.';
        var chgId = chg.insert();

        addAffected(chgId, primaryCi);
        var ipParts = (targetIps || '').split(/[,\n\r\t ]+/);
        for (var j = 0; j < ipParts.length; j++) {
            var ip2 = ipParts[j].replace(/^\s+|\s+$/g, '');
            if (!ip2) continue;
            var ci2 = new GlideRecord('cmdb_ci'); ci2.addQuery('ip_address', ip2); ci2.addQuery('name', 'NOT LIKE', 'Cluster'); ci2.query();
            while (ci2.next()) addAffected(chgId, ci2.sys_id.toString());
        }

        var impl = new GlideRecord('change_task');
        impl.initialize(); impl.change_request = chgId; impl.assignment_group = '<FIREWALL_DEPLOY_GROUP_SYS_ID>'; impl.assigned_to = '<FIREWALL_ENGINEER_SYS_ID>';
        impl.short_description = 'Implementation - Check Point firewall automation workflow';
        impl.description = 'Primary driver CTASK for Check Point firewall automation. MID/Ansible status updates should be written to this CTASK work notes. The execution plan is derived from the uploaded CPUSE Package and optional Dependency Checklist documents, with CPRID staging from MDS and backend-selected CDT/SSH execution.';
        impl.work_notes = 'Implementation CTASK created. Automation should append phase status here, with summarized updates copied to the CHG activity stream.';
        impl.insert();

        if (activity != 'deployment_agent_install' && activity != 'Deployment Agent Install') {
        var post = new GlideRecord('change_task');
        post.initialize(); post.change_request = chgId; post.assignment_group = '<FIREWALL_DEPLOY_GROUP_SYS_ID>'; post.assigned_to = '<FIREWALL_ENGINEER_SYS_ID>';
        post.short_description = 'Tester validation gate - Check Point automation';
        post.description = 'Tester validation gate for Check Point firewall automation. Automation pauses after the FIRST member is upgraded and traffic has failed over to it. Validate application traffic, business service health, firewall policy behavior, and ICAP (if required) on the upgraded member. Closing this task Closed Complete is the approval signal that authorizes automation to continue to the SECOND member. Close it Closed Incomplete to keep automation blocked if validation fails.';
        post.insert();
        }

        ritm.work_notes = 'Firewall Deploy readiness SCTASK ' + current.number + ' closed. Created CHG ' + chg.number + ' with Implementation CTASK as the automation driver.';
        ritm.update();
    } catch (ex) {
        gs.error('Check Point readiness SCTASK to CHG rule failed: ' + ex);
    }
})(current, previous);
```

### A.3 Relabel default CTASKs (`change_task`, before insert)

```javascript
(function executeRule(current, previous) {
    try {
        var chgId = current.getValue('change_request');
        if (!chgId)
            return;

        var shortDesc = current.getValue('short_description') || '';
        if (shortDesc != 'Implement' && shortDesc != 'Post implementation testing')
            return;

        var group = current.getValue('assignment_group') || '';
        var assignee = current.getValue('assigned_to') || '';
        if (group || assignee)
            return;

        var chg = new GlideRecord('change_request');
        if (!chg.get(chgId))
            return;
        var desc = chg.getValue('description') || '';
        if (desc.indexOf('[CHECKPOINT_AUTOMATION]') < 0)
            return;

        // IMPORTANT: do NOT close these tasks. They are the change model's own
        // phase tasks; closing them makes the change model treat the Implement
        // phase as finished and auto-advance the CHG to Review seconds after it
        // reaches Implement (observed live on CHG_EXAMPLE), which yanks the CHG
        // away from the automation worker. Instead, relabel them so requesters
        // and engineers know they are not actionable; the automation worker
        // closes them during success bookkeeping, which lets the change model
        // advance to Review naturally at the end of the run.
        current.setValue('short_description', 'Change-model default: ' + shortDesc + ' (auto-managed, no action needed)');
        current.setValue('description', 'Default ServiceNow change-model task. For Check Point automation CHGs the governed Implementation, Tester validation gate, and Final validation CTASKs are the operational records. This task requires no human action; the automation worker closes it automatically when the workflow completes successfully.');
    } catch (ex) {
        gs.error('Check Point default CTASK relabel failed: ' + ex);
    }
})(current, previous);
```

### A.4 Mirror CHG notes (`change_request`, after update)

```javascript

(function executeRule(current, previous) {
    try {
        if (!current.work_notes.changes())
            return;
        var desc = current.description ? current.description.toString() : '';
        if (desc.indexOf('[CHECKPOINT_AUTOMATION]') < 0)
            return;
        var note = current.work_notes.getJournalEntry(1);
        if (!note || note.indexOf('[Mirrored to Implementation CTASK]') >= 0)
            return;
        var impl = new GlideRecord('change_task');
        impl.addQuery('change_request', current.sys_id);
        impl.addQuery('short_description', 'STARTSWITH', 'Implementation - Check Point firewall automation workflow');
        impl.setLimit(1);
        impl.query();
        if (!impl.next())
            return;
        impl.work_notes = '[Mirrored from CHG automation notes]\n' + note;
        impl.update();
    } catch (ex) {
        gs.error('Check Point mirror CHG notes to Implementation CTASK failed: ' + ex);
    }
})(current, previous);
```

## Appendix B — Activity plan JSON (abridged real example)

```json
{
  "change": {"number": "CHG_EXAMPLE", "activity_type": "JHF/Hotfix Patch",
             "state": "Implement", "environment": "lab"},
  "checkpoint": {
    "mds_host": "192.0.2.10", "cma_env": "CMA_A_Server", "cma_server_ip": "192.0.2.11",
    "cluster_name": "CP-FW-Cluster", "cluster_mode": "cluster",
    "members": [{"name": "CP-FW-A", "ip": "192.0.2.20"}, {"name": "CP-FW-B", "ip": "192.0.2.21"}],
    "current_version": "R82", "target_version": "R82", "target_take": "91",
    "icap_mode": "optional", "preserve_original_active": true
  },
  "execution": {"method": "CDT (Central Deployment Tool)", "tester_pause": true,
                "gates": [{"name": "tester_validation_after_first_member", "enabled": true,
                           "after_phase": "failover-to-first",
                           "decision_source": "servicenow_ctask_or_simulated"}]},
  "package_steps": [{
    "name": "install_Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL",
    "action": "install", "package_type": "jhf",
    "package_name": "Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar",
    "source_path": "/var/log/tmp/Check_Point_R82_jumbo_hf_main_Bundle_T91_FULL.tar",
    "checksum_sha256": "<sha256>", "requires_absent": ["Check_Point_R82_JHF_T89"]
  }]
}
```

End of build guide. See `ARCHITECTURE_AND_ENGINEERING_GUIDE.md`, `WORKFLOW_WALKTHROUGH.md`, and `../tools/DEPLOYMENT_AGENT_CURRENCY.md`.
