# Governed R82 Take 91 Removal

This walkthrough covers a separate ServiceNow-governed request that removes a
separately installed R82 Take 91 JHF from a two-member cluster. It begins after
the completed Take 91 installation, processes the originally standby member,
pauses at the real tester task, resumes the same change for the other member,
and restores the original ownership.

Removal must be its own approved catalog submission. Do not append it to the
installation request, reuse the installation CHG, or treat it as an automatic
rollback. Confirm the removal path and fallback package state for your exact
gateways with your Check Point support channel.

Read the [shared governed operating guide](README.md) first. Use
[Start Here](../../docs/START_HERE.md), the
[component reference](../../docs/COMPONENT_REFERENCE.md), the
[ticket example](../../docs/SERVICENOW_TICKET_EXAMPLE.md), and the
[runner CLI walkthrough](../runner_cli/README.md) for material that is not
repeated here.

## 1. Prepare A Separate Removal Attachment

The checked-in [CSV template](r82-take91-remove.csv) contains one `remove` row,
blank hashes, and a placeholder `.tar` identity. Removal has no package file to
stage or hash. The submitted name is an alias for discovery, not authority to
remove a similarly named package.

Create a protected working copy outside the checkout:

```bash
install -m 0600 \
  examples/governed/r82-take91-remove.csv \
  /tmp/cpuse-r82-take91-remove-approved.csv
```

Replace only the placeholder package identity with the approved Take 91 alias.
Keep `action=remove`, both hash fields blank, and
`reboot_expected=true`. A second engineer should compare the alias with the
installed package records before submission.

## 2. Submit A New Catalog Request

Attach the removal CSV to a new RITM using a filename containing
`CPUSE Package`. This JSON shows the catalog-variable shape; it is not an API
payload and contains no live record identifiers:

```json
{
  "activity_type": "software_patch_activity",
  "environment": "lab",
  "icap_mode": "disabled",
  "target_ips": "192.0.2.20\n192.0.2.21",
  "mds_host": "192.0.2.10",
  "current_version": "R82",
  "target_version": "R82",
  "cpuse_package_upload": "CPUSE Package - R82 Take 91 Remove.csv",
  "preserve_original_active": "yes",
  "tester_gate": "yes",
  "scheduled_start": "<approved-start>",
  "scheduled_end": "<approved-end>",
  "cpuse_dependency_upload": "",
  "special_instructions": "Remove the separately installed R82 Take 91 package one member at a time and retain the tester pause."
}
```

Do not add package hashes, credentials, execution switches, or an install row.
Do not reuse the installation request's readiness, approval, tester decision, or
resume state.

## 3. Complete Readiness

Start and monitor the persistent workers with the commands in the
[shared guide](README.md#what-the-operator-runs). Automated readiness must close
its SCTASK as Closed Complete with status `ready`. Before change approval,
inspect the readiness evidence for all of these facts:

- both TEST-NET targets resolve to the intended two-member cluster;
- both members are R82 build 777 with separately installed Take 91;
- original A ACTIVE / B STANDBY ownership, healthy pnotes, and monitored and
  virtual interface signatures are captured;
- the activity plan contains one JHF `remove` action, CDT backend, no staging
  action, expected reboot, tester pause, ownership restoration, and ICAP
  disabled;
- prerequisite fields are checks only and are not removal identities.

See [synthetic readiness output](expected/r82-take91-remove/01-readiness.txt).

## 4. Require Unique CPInstLog Resolution

The submitted placeholder follows the observed `.tar` alias shape, while the
installed CPUSE identity can use `.tgz`. Before any candidate or shell command
is generated, the resolver reads CPInstLog and compares the approved alias with
the installed package aliases from `source_path`, `package_name`,
`display_name`, `name`, and the step name.

Resolution must produce exactly one installed Take 91 identity. Zero matches or
more than one match fails closed. The exact resolved `.tgz` identity, not the
submitted `.tar` alias and never `requires_present` or `requires_absent`,
becomes the removal selector.

See [synthetic identity-resolution output](expected/r82-take91-remove/02-identity-resolution.txt).

## 5. Approve The Change And Select The Standby Member

Complete normal change approval and move the CHG to Implement with exactly one
open governed Implementation CTASK. The worker revalidates the marker, approval,
Implement state, readiness result, and task uniqueness before launching the
internal runner.

The first CDT candidate set must contain only the originally standby member B.
An empty set, both members, the active member, or an identity that differs from
the unique CPInstLog result must stop before mutation.

See [synthetic candidate output](expected/r82-take91-remove/03-standby-candidate.txt).

## 6. Run The First-Member Removal

The initial runner follows this exact phase order:

1. `discover-targets`
2. `validate-plan`
3. `init`
4. `deployment-agent-readiness`
5. `cluster-state-capture`
6. `baseline-capture`
7. `first-member`
8. `failover-to-first`
9. `approve-testers`

There is no `stage-files` phase for removal. Inside `first-member`, the runner
validates prerequisites, regenerates the standby-only candidate, and invokes
guarded CDT removal. Member B reboots, reconnects with its trusted host identity,
and completes the required stabilization interval before the package-absence
gate and cluster checks are accepted.

See [synthetic first-member output](expected/r82-take91-remove/04-first-member.txt).

After reconciliation, the runner fails traffic to updated member B. It stops at
`approve-testers` with return code `20`; this is an intentional pause, not a
successful final return. The implementation worker records `waiting_tester`
and waits for the existing dedicated tester CTASK created by the ServiceNow
business rule.

See [synthetic tester-gate output](expected/r82-take91-remove/05-tester-gate.txt).

## 7. Test Through The Dedicated Task

Do not use `--simulate-gates`. Test application traffic, policy behavior,
cluster state, pnotes, required interfaces, and package absence while member B
is ACTIVE. Create fresh evidence for this request:

```text
Tester evidence checklist - EMPTY TEMPLATE
Change reference:
Tester name or accountable identity:
Test start and end time:
Updated ACTIVE member identity:
Current ACTIVE/STANDBY output attached: [ ]
Required interfaces and pnotes healthy: [ ]
Application and traffic tests listed: [ ]
Application and traffic results attached: [ ]
Policy behavior validated for the approved scope: [ ]
Exact Take 91 package absence reconciled: [ ]
Decision (leave blank until testing is complete):
```

Only Closed Complete on that dedicated tester CTASK authorizes the worker to
resume. Skipped, Closed Incomplete, Canceled, work notes, and close notes do not.
The worker consumes that lifecycle transition once and launches the same CHG
with the internal `--start-at second-member` boundary.

## 8. Resume The Same Change And Finish

On resume, discovery refreshes both members before mutation. The completed run
first observed Take 60 on member B during this resume discovery; no earlier
first-member summary recorded that fallback Take.

The remaining phase order is:

1. `second-member`
2. `restore-original-active`
3. `final-support-capture`
4. `support-diff`
5. `postcheck`

Member A is now standby. It must pass the same prerequisite, unique CPInstLog
identity, standby-only candidate, guarded CDT removal, reboot, trusted
reconnect, stabilization, and package-absence checks. The runner then restores
A ACTIVE / B STANDBY, captures final support data, generates the support diff,
and runs postcheck.

See [synthetic second-member output](expected/r82-take91-remove/06-second-member.txt).

## 9. Verify Records And Evidence Boundaries

Require a completed runner summary, return code `0`, restored original
ownership, healthy final cluster state, and empty `errors` and
`package_state_errors`. The worker closes the governed implementation and
final-validation tasks, moves the CHG to Review, and completes normal
bookkeeping. After the CHG is closed successfully, the catalog reconciliation
moves the RITM and REQ to their successful terminal states.

See [synthetic terminal-state output](expected/r82-take91-remove/07-terminal-records.txt).

Interpret the completed evidence within these exact limits:

- ICAP was disabled, so this run makes no ICAP-health claim.
- There is no first-member summary.
- The first Take 60 observation appears only in resume discovery.
- Postcheck enforces Take 91 absence; it does not enforce that the fallback Take
  equals 60.
- The activity plan requests an intermediate support capture, but the workflow
  has no intermediate support-capture phase despite that plan flag.
- Local evidence lacks a sanitized final ServiceNow snapshot. Confirm terminal
  record states from the governed ticket history.
- Normal expected output omits the lab mail-notification error. That message was
  not used as the package-outcome verdict and does not belong in reusable output
  examples.
