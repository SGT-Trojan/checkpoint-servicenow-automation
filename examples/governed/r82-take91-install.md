# Governed R82 Take 91 Installation

This walkthrough covers a two-member ServiceNow-governed installation of an
approved R82 Take 91 JHF package. It begins with both members at R82 build 777
and bundled Take 60, processes the originally standby member first, pauses for
testing, and then processes the other member.

It does not cover the later Take 91 removal workflow. The package names and
checksums below are deliberately invalid placeholders. Confirm the package and
prerequisites for your exact gateways with your Check Point support channel.

Read the [shared governed operating guide](README.md) first. Use
[Start Here](../../docs/START_HERE.md), the
[component reference](../../docs/COMPONENT_REFERENCE.md), and the
[ticket example](../../docs/SERVICENOW_TICKET_EXAMPLE.md) for material that is
not repeated here. The [runner CLI walkthrough](../runner_cli/README.md)
documents the separate ServiceNow-free path.

## 1. Prepare The Approved Attachment

The checked-in [CSV template](r82-take91-install.csv) must fail hash validation
as shipped. Create a mode-0600 working copy outside the checkout:

```bash
install -m 0600 \
  examples/governed/r82-take91-install.csv \
  /tmp/cpuse-r82-take91-approved.csv
```

Replace the package identity, SHA-1, and SHA-256 with the approved Take 91
artifact's published values and independently verify the staged MDS file. Keep
`action=install`, `package_type=jhf`, and `reboot_expected=true`. Add a
separate dependency attachment only when the approved package has explicit
prerequisite checks. Do not add an uninstall row to this request.

## 2. Submit One Catalog Request

Attach the protected working CSV with a filename containing `CPUSE Package`.
This JSON shows the catalog-variable shape, not an API payload:

```json
{
  "activity_type": "software_patch_activity",
  "environment": "lab",
  "icap_mode": "disabled",
  "target_ips": "192.0.2.20\n192.0.2.21",
  "mds_host": "192.0.2.10",
  "current_version": "R82",
  "target_version": "R82",
  "cpuse_package_upload": "CPUSE Package - R82 Take 91 Install.csv",
  "preserve_original_active": "yes",
  "tester_gate": "yes",
  "scheduled_start": "<approved-start>",
  "scheduled_end": "<approved-end>",
  "cpuse_dependency_upload": "",
  "special_instructions": "Install the approved R82 Take 91 package one member at a time and retain the tester pause."
}
```

Do not put credentials, package paths, or execution switches in the request.
The worker retrieves the attachment, stores it by attachment identity, builds
the activity plan, and validates the staged package before execution.

## 3. Complete Readiness And Change Approval

Start and monitor the workers with the commands in the
[shared guide](README.md#what-the-operator-runs). Before moving the CHG to
Implement, require all of the following:

- the readiness SCTASK is Closed Complete with automated status `ready`;
- discovery resolves the two requested addresses to one expected cluster;
- both members report R82 build 777, current Take 60, healthy pnotes, matching
  interface signatures, and the expected original roles;
- one `install` action identifies the approved Take 91 JHF, both hashes match,
  prerequisites pass, and reboot is expected;
- the generated plan shows CDT, MDS staging, preserved ownership, tester pause,
  and the chosen ICAP mode;
- the CHG is approved and in Implement with one open governed Implementation
  CTASK.

SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE

```text
Readiness: ready
Managed object: EXAMPLE-CLUSTER (two-member cluster)
Current state: R82 build 777, Take 60 on both members
Package action: install one approved R82 Take 91 JHF
Original roles: EXAMPLE-GW-A ACTIVE, EXAMPLE-GW-B STANDBY
ICAP mode: disabled
Evidence reference: <readiness-run>
```

## 4. Follow The First-Member Phases

The worker and runner execute these phases in order:

1. `discover-targets`
2. `validate-plan`
3. `init`
4. `deployment-agent-readiness`
5. `cluster-state-capture`
6. `baseline-capture`
7. `stage-files`
8. `first-member`
9. `failover-to-first`
10. `approve-testers`

The first-member phase validates prerequisites, generates CDT candidates, and
requires exactly one candidate for the originally standby member before guarded
execution. After reboot and trusted reconnect, package reconciliation must show
exact Take 91. A generic success line or a different Take is not sufficient.

The runner then fails traffic to the updated member and stops at
`approve-testers` with return code `20`. The worker records `waiting_tester`.
No second-member operation is authorized at this point.

SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE

```text
first-member: completed
first-member reconciliation: R82 build 777, exact Take 91
failover-to-first: updated member is ACTIVE
approve-testers: waiting for dedicated tester CTASK
Runner return code: 20
```

## 5. Complete The Tester Gate

Create new evidence for the active change. This is intentionally empty and is
not an approval artifact:

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
R82 build 777 and exact Take 91 reconciled: [ ]
Decision (leave blank until testing is complete):
```

Keep the tester CTASK open when evidence is incomplete or a check fails. Close
only the dedicated tester CTASK as Closed Complete after accountable testing.
Other terminal states, work notes, and close notes cannot authorize resume.

## 6. Resume And Verify Completion

After the valid tester transition, the worker invokes the internal runner with
`--start-at second-member`. The remaining phases are:

1. `second-member`
2. `restore-original-active`
3. `final-support-capture`
4. `support-diff`
5. `postcheck`

The remaining member must pass the same prerequisite, unique-candidate,
guarded-execution, reboot, reconnect, and exact-Take reconciliation checks.
Ownership then returns to the original active member when requested.

SYNTHETIC OUTPUT SHAPE - NOT LAB EVIDENCE

```text
second-member reconciliation: R82 build 777, exact Take 91
restore-original-active: original ownership restored
cluster health: pnotes and required interfaces healthy
postcheck: errors=[] package_state_errors=[]
summary: completed
Worker return code: 0
```

Inspect the final records before closure. Retain the generated plan, validated
hashes, original state, one selected CDT target per member, action and reboot
records, trusted reconnect evidence, exact Take 91 reconciliation on both
members, final support captures and diffs, restored ownership, postcheck member
records, empty error arrays, runner summary, and worker bookkeeping result.

The completed lab scenario used ICAP-disabled mode. That run cannot support an
ICAP-health claim. A site that requires ICAP must select and satisfy the
appropriate ICAP gate rather than copying the disabled setting from this page.
