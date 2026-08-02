# Governed ServiceNow Ticket Example

This is a sanitized, ticket-shaped companion to the
[ServiceNow build guide](SERVICENOW_BUILD_GUIDE.md). It shows what a requester
submits, which existing CSVs are attached, and how records move through the
implemented gates. It is not a mock ServiceNow instance and does not replace
the field definitions, ACLs, or business rules in the build guide.

All addresses are from the RFC 5737 documentation ranges. The example contains
no record numbers, sys_ids, credentials, customer names, or lab values.


## Validated Three-Request Sequence

The August 2026 recertification used three separate governed requests. Do not
combine them into one RITM.

| Request | Activity | Starting state | Result | Attachment |
|---|---|---|---|---|
| Major upgrade | `version_upgrade_activity` | R81.20 | R82 build 777 with embedded Take 60 | [Major-upgrade CSV](../test_inputs/cpuse_major_r8120_to_r82.csv) |
| Take 91 install | `software_patch_activity` | R82 Take 60 | R82 Take 91 | [Install CSV](../test_inputs/cpuse_install_take91.csv) |
| Take 91 removal | `software_patch_activity` | R82 Take 91 | R82 with Take 91 absent | [Removal CSV](../test_inputs/cpuse_remove_take91.csv) |

Each request gets its own readiness result, approval, CHG, implementation task,
tester boundary, evidence, and closure. The recertification performed live
firewall mutations, reboots, policy operations, failover, exact reconciliation,
and postchecks. Tester acceptance was simulated, so this run proves the
technical gate but does not recertify the human CTASK approval path.

## Catalog Submission

The JSON below represents the values a requester supplies on the RITM. An
actual ServiceNow attachment variable stores platform attachment metadata; the
filenames here are human-readable submission artifacts, not sys_ids.

```json
{
  "activity_type": "software_patch_activity",
  "environment": "lab",
  "icap_mode": "disabled",
  "target_ips": "192.0.2.20\n192.0.2.21",
  "mds_host": "192.0.2.10",
  "current_version": "R82",
  "target_version": "R82",
  "cpuse_package_upload": "CPUSE Package.csv",
  "preserve_original_active": "yes",
  "tester_gate": "yes",
  "scheduled_start": "<approved-start>",
  "scheduled_end": "<approved-end>",
  "cpuse_dependency_upload": "",
  "special_instructions": "Validate application traffic after failover and keep the tester pause open until evidence is attached."
}
```

Use the exact variable choices configured in the
[catalog variable table](SERVICENOW_BUILD_GUIDE.md#62-variables-item_option_new--exact-replication-table).
Do not add credentials, package paths, execution switches, or a requested Take
to the ticket. The runner derives and validates those values through the
documented controls.


## Major Upgrade Catalog Submission

Use a separate request for the major upgrade:

```json
{
  "activity_type": "version_upgrade_activity",
  "environment": "lab",
  "icap_mode": "disabled",
  "target_ips": "192.0.2.20\n192.0.2.21",
  "mds_host": "192.0.2.10",
  "current_version": "R81.20",
  "target_version": "R82",
  "cpuse_package_upload": "CPUSE Major Upgrade.csv",
  "preserve_original_active": "yes",
  "tester_gate": "yes",
  "scheduled_start": "<approved-start>",
  "scheduled_end": "<approved-end>",
  "cpuse_dependency_upload": "",
  "special_instructions": "Install the mixed-version policy before failover and keep the tester pause open until evidence is attached."
}
```

The major-upgrade CSV uses `action=upgrade` and `package_type=blink`. Its
phase order is first-member upgrade, mixed-version policy, MVC on, failover,
tester gate, second-member upgrade, final policy, MVC off, and original-active
restoration. A changed SSH host key stops the member phase until the replacement
fingerprint is verified through a trusted channel; it is never accepted
automatically.

## Upload Artifacts

Use the existing sanitized files as the three package-action examples. They are
linked instead of copied so their schema and safety corrections cannot drift:

- [Major-upgrade CSV](../test_inputs/cpuse_major_r8120_to_r82.csv)
- [JHF install CSV](../test_inputs/cpuse_install_take91.csv)
- [JHF removal CSV](../test_inputs/cpuse_remove_take91.csv)

For the catalog submission above, upload the install CSV with a filename that
contains `CPUSE Package`, for example `CPUSE Package.csv`. The removal CSV is
the alternate artifact for a separately approved removal request; do not attach
install and removal examples to the same RITM. Add the optional dependency CSV
only when the package has explicit prerequisite checks. See the
[requester file formats](SERVICENOW_BUILD_GUIDE.md#13-requester-input-file-formats)
for the authoritative columns and attachment naming rules.

## Record Lifecycle And Gates

The state labels below are display values. The build guide owns the exact
choice values and business-rule implementation.

| Point | REQ and RITM | Readiness SCTASK | CHG | Operational CTASKs | Authorization fields |
|---|---|---|---|---|---|
| Catalog submitted | REQ and RITM open; RITM readiness is pending | Automated readiness task Open | Not created | None | `u_checkpoint_readiness_status=pending`, source `automated` |
| Automated readiness passes | RITM remains open and is marked ready | Closed Complete with readiness evidence | Created by the guarded rule and enters the normal approval model | Change-model tasks may exist; no runner execution yet | Readiness status `ready`, source `automated`, summary and evidence populated |
| Readiness fails | RITM remains open for a decision | Automated task Closed Incomplete; manual readiness SCTASK Open | Not created | None | Automated task and RITM status `failed`; manual task starts `pending` with source `manual` |
| Manual readiness accepted | RITM is marked ready | Manual readiness SCTASK Closed Complete | Created once by the guarded rule | Normal change-model tasks | Status `ready`, source `manual`, summary and evidence populated |
| Manual readiness rejected | RITM Closed Incomplete; REQ follows catalog reconciliation | Manual readiness SCTASK Closed Incomplete | Never created | None | Status `rejected` or `not_viable`; no execution authorization |
| Approved maintenance starts | REQ/RITM remain linked and open | Passed task remains Closed Complete | Approved and in Implement | Governed Implementation CTASK Open | Marker, approval, Implement state, readiness, and unique implementation-task gates all pass |
| First member and failover complete | No catalog-state change | No change | Remains Implement | Implementation CTASK Open; tester validation CTASK Open | Worker state is `waiting_tester`; no resume occurs from notes or task names alone |
| Tester accepts | No catalog-state change | No change | Remains Implement | Tester CTASK Closed Complete | Closed Complete on the dedicated tester task authorizes `second-member`; Skipped, Incomplete, or Canceled does not |
| Execution phase fails | No automatic catalog closure | No change | Remains Implement | Implementation CTASK Open; engineer-remediation CTASK Open | `u_checkpoint_resume_status=pending`; phase, summary, and evidence identify the failed boundary |
| Remediation accepted | No catalog-state change | No change | Remains Implement | Remediation CTASK Closed Complete | `u_checkpoint_resume_status=approved`; `u_checkpoint_resume_phase` selects the exact restart boundary |
| Remediation rejected | No automatic success closure | No change | Remains Implement for manual handling | Remediation CTASK closed without approval | Rejected status blocks resume; prose and close notes cannot override it |
| Automation succeeds | REQ/RITM remain linked until normal change/catalog closure | No change | Moves to Review | Final-validation, Implementation, and relabeled default CTASKs Closed Complete | Final evidence and summary are attached before bookkeeping completes |
| Change closes successfully | REQ and RITM reconcile to their successful terminal states | Closed | Closed Successful | Closed | Catalog-chain reconciliation follows the CHG result |
| Change closes unsuccessfully | REQ and RITM reconcile to incomplete/unsuccessful terminal states | Closed | Closed Unsuccessful | Closed or manually resolved | No success state is inferred from partial execution |

Readiness fields live on the readiness SCTASK and RITM:
`u_checkpoint_readiness_status`, `u_checkpoint_readiness_source`,
`u_checkpoint_readiness_summary`, and `u_checkpoint_readiness_evidence`.
Execution-remediation fields live on the remediation CTASK:
`u_checkpoint_resume_status`, `u_checkpoint_resume_phase`,
`u_checkpoint_resume_summary`, and `u_checkpoint_resume_evidence`. Field ACLs
matter because these values authorize transitions; work notes are evidence,
not control inputs.

## Governed Versus CLI Phase Map

Both paths use `servicenow_checkpoint_runner.py` for firewall-side execution.
The difference is who establishes and records each authorization boundary. See
the [workflow walkthrough](WORKFLOW_WALKTHROUGH.md) for phase detail and the
[runner CLI example](../examples/runner_cli/README.md) for the full manual
command sequence.

| Phase | Governed ticket path | ServiceNow-free operator path |
|---|---|---|
| Request intake | Readiness worker reads RITM variables and named attachments | Operator creates and independently verifies a protected CPUSE CSV |
| Readiness | Readiness SCTASK runs discovery, health, package, checksum, dependency, and identity checks before CHG creation | Operator performs the same prerequisites under an external approved change; there is no RITM gate |
| Change authorization | Worker requires the automation marker, approved CHG in Implement, passed readiness, and one open Implementation CTASK | Operator confirms the external change approval and invokes the runner with no ServiceNow credentials |
| Discover targets | Runner discovers the exact CMA, cluster, and members; evidence is posted through the governed records | Same runner discovery; evidence remains in the local run and reports directories |
| Validate and capture | Runner validates the plan, prechecks health, validates Deployment Agent readiness, and captures original roles and baseline support data | Same runner phases and local evidence |
| Stage package | Runner validates MDS package presence, declared hashes, air-gap state, dependencies, and exact package identity | Same runner phases using the operator-approved working CSV |
| First member | Worker records phase progress while the runner processes the originally non-active member | Runner processes the originally non-active member; operator monitors the local logs |
| Failover | Runner fails traffic to the updated member and records evidence on the governed change | Same runner failover; operator records evidence in the external change system |
| Tester gate | Worker parks on return code 20 and waits for the dedicated tester CTASK to be Closed Complete | Runner exits 20; human testing and approval are self-attested outside the runner |
| Resume | Worker launches the runner at `second-member` only after the tester CTASK authorizes it | Operator reuses the same manual ID and command inputs with `--start-at second-member` |
| Second member | Runner revalidates prerequisites and processes the remaining member | Same runner phases and reconciliation |
| Restore and postcheck | Runner optionally restores original ownership, captures final support data, diffs evidence, and runs postcheck; worker updates CTASK/CHG records | Same runner phases; operator archives local evidence and updates the external change record |
| Completion | Worker closes operational tasks and moves the CHG to Review; later closure reconciles RITM and REQ | Operator reviews the summary and closes the external change under local process |

The CLI path is not a shortcut around checks. It removes ServiceNow as the
system enforcing the human decisions, so the operator must supply equivalent
change control without weakening package, identity, cluster, tester, resume, or
postcheck gates.
