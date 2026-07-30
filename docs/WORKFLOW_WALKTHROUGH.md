# Check Point Firewall Automation: Step-by-Step Walkthrough

This page follows one ServiceNow request from submission to closure. Read
[Start Here](START_HERE.md) first if the record names or Check Point terms are
new to you.

The names and addresses in the public version are examples.

## Phase 0 - Submit the request

1. The firewall engineer opens `CheckPoint FW Maintenance Activity` in the
   Service Catalog.
2. In **Activity Type**, the engineer selects Version Upgrade, Software Patch,
   or Deployment Agent Install.
3. The engineer enters **Environment**, **Target Firewall IPs**, **MDS Host/IP**,
   the current and target versions, the maintenance window, and the health-check
   options.
4. The engineer uploads the **CPUSE Package** CSV. It contains the action,
   package name, package type, checksums, and notes. The **CPUSE Dependency
   Checklist** CSV is optional.
5. The engineer submits the request. ServiceNow creates a REQ and RITM.

The requester does not choose CDT or Management API, a staging method, a CMA,
or credentials. The current ServiceNow path uses CDT for package work.

## Phase 1 - Create the readiness task

1. A ServiceNow business rule reads the open RITM.
2. It copies the request details into a structured description.
3. It creates one `Automated Check Point readiness validation` SCTASK for the
   Firewall Deploy group.

No CHG exists yet. ServiceNow creates a change only after readiness passes.

## Phase 2 - Check readiness

The readiness worker claims the SCTASK and downloads the attached files. It runs
read-only checks for:

- the MDS, CMA, cluster, members, and policy;
- one active and one standby cluster member;
- ClusterXL health, PNOTEs, interfaces, and ICAP when required;
- the Deployment Agent build;
- package presence and checksums on the MDS;
- package prerequisites; and
- free space and rollback capacity.

If every check passes, the worker sets readiness to `ready` and closes the
SCTASK as Closed Complete.

If a check fails, the worker closes the automated SCTASK as Closed Incomplete
and creates one manual readiness SCTASK. A Firewall Deploy engineer then chooses
one of two actions:

- fix the problem, set readiness to `ready`, and close the task Complete; or
- reject the request and close it Incomplete.

A rejected request closes without creating a CHG.

## Phase 3 - Create the change

When readiness is `ready`, ServiceNow creates the CHG. It includes the request,
target CI, implementation plan, test plan, and backout plan. It also creates:

- `Implementation - Check Point firewall automation workflow`; and
- `Tester validation gate - Check Point automation`.

The tester-task description says that the workflow pauses after failover to the
first changed member. Only Closed Complete lets the second-member work start.

## Phase 4 - Approve and schedule the change

The firewall engineer and CAB move the CHG through the normal change process:
Assess, Authorize, Scheduled, and Implement. The platform blocks unsupported
state jumps.

When the CHG enters Implement, the change model may create default tasks. The
automation relabels these tasks and leaves them open. Closing them early can
make ServiceNow move the CHG to Review before the firewall work starts.

## Phase 5 - Change the first member

The implementation worker starts only when the CHG has the automation marker,
is approved, is in Implement, has passed readiness, and has one open
implementation task. It checks these conditions again before launching the
runner. It also prevents two runs for the same CHG.

For a normal rolling JHF change, the runner then:

1. finds the exact cluster and members;
2. validates the activity plan;
3. runs health and package checks;
4. saves the original active member;
5. captures baseline support data;
6. changes the standby member;
7. waits for its reboot and health checks;
8. fails over traffic to that changed member; and
9. pauses at the tester gate.

A major upgrade adds mixed-version policy checks and turns MVC on before the
failover. After both members are upgraded, it installs final policy and turns
MVC off. Major upgrades require a two-member cluster.

A Deployment Agent update uses a shorter path. It updates both members without
failover or a tester gate, then checks the installed build on each member.

## Phase 6 - Test the changed member

The worker records `waiting_tester` and stops. A tester checks traffic, services,
policy behavior, and ICAP on the changed member.

The tester must close `Tester validation gate - Check Point automation` as
Closed Complete. Closed Skipped or Closed Incomplete does not open the gate.
A different task with a similar name also does not open it.

After approval, the runner changes the second member. It then runs final health,
package, support, and policy checks and restores the original active member when
requested.

## Phase 7 - Handle a failure

If any phase fails, the runner stops and saves the failed phase, playbook, step,
and log path. The CHG stays in Implement. The worker creates one
`Engineer remediation required` CTASK with the failure details.

The engineer fixes the cause, sets Checkpoint Resume Status to `approved`, and
closes the task Complete. The worker restarts at the failed phase. One approval
can start only one resume attempt.

Closing the task Incomplete or setting the status to rejected or not viable
keeps the change blocked. The worker never retries a failed change by itself.

## Phase 8 - Record success

After the final checks pass, the worker:

1. creates and closes the final validation CTASK;
2. closes the implementation CTASK;
3. closes the relabeled default change-model tasks; and
4. moves the CHG to Review.

The CHG and CTASK notes contain phase results. Detailed plans, logs, support
captures, diffs, and restart state stay in the protected run directory.

## Phase 9 - Close the change

The firewall engineer or change manager reviews the result and closes the CHG.
The RITM and REQ then complete through the normal catalog flow.

## What the Firewall Engineer Does Not Enter

The request does not ask for passwords, an execution backend, a package staging
method, a package directory, CMA or policy names, or CDT candidate settings.
The automation finds these values or reads them from protected configuration.
