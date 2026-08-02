# Completed Governed Walkthroughs

These walkthroughs show how an operator runs three completed lab scenarios through
the ServiceNow-governed workflow:

- [R81.20 to R82 build 777 with bundled Take 60](r8120-to-r82-t60.md)
- [R82 Take 91 installation](r82-take91-install.md)
- [R82 Take 91 removal](r82-take91-remove.md)

They are sanitized operating examples, not proof that the same packages or
settings are suitable for another estate. Start in a non-production environment
and use your approved package records.

For background and field definitions, use these existing guides instead of
relying on this directory as a reference manual:

- [Start Here](../../docs/START_HERE.md) for terms and the overall flow.
- [Component reference](../../docs/COMPONENT_REFERENCE.md) for flags, variables,
  return codes, and playbook contracts.
- [Governed ticket example](../../docs/SERVICENOW_TICKET_EXAMPLE.md) for the full
  REQ/RITM/SCTASK/CHG/CTASK lifecycle.
- [Runner CLI walkthrough](../runner_cli/README.md) for the separate path that
  does not use ServiceNow.

## What The Operator Runs

The operator starts and monitors the two persistent workers. The implementation
worker validates the approved change and launches the runner. Run these commands
on the automation host after its protected environment source has supplied the
ServiceNow and Check Point credentials:

```bash
sudo systemctl enable --now \
  snow-checkpoint-readiness-worker \
  snow-checkpoint-worker

systemctl is-active \
  snow-checkpoint-readiness-worker \
  snow-checkpoint-worker

journalctl -u snow-checkpoint-readiness-worker -f
journalctl -u snow-checkpoint-worker -f
```

Do not add passwords to these commands, ticket variables, attachments, or
shell history. The units load credentials from their protected environment.

## What The Worker Runs Internally

The following commands explain the handoff. They are not operator commands and
should not be run beside an active worker:

```bash
python3 servicenow_checkpoint_runner.py \
  --chg-sys-id '<change-sys-id>'
```

At the tester boundary the runner exits with return code `20`. After the exact
tester CTASK reaches Closed Complete, the worker starts a new runner process for
the same change:

```bash
python3 servicenow_checkpoint_runner.py \
  --chg-sys-id '<same-change-sys-id>' \
  --start-at second-member
```

The worker invokes the governed playbooks, supplies the internal phase
authorization, validates the immutable artifact chain, and owns the run lock.
Do not invoke a mutating playbook directly. Use the
[catalog and worker entry point](../../docs/SERVICENOW_BUILD_GUIDE.md) so the
change approval, member selection, tester gate, and resume checks stay in one
workflow. Starting the runner manually while the worker is active creates a
collision and bypasses the intended worker state machine.

## Shared Gate Rules

1. Submit one request with one package-action attachment.
2. Wait for automated readiness to close its SCTASK as Closed Complete with
   `u_checkpoint_readiness_status=ready`.
3. Complete normal change approval and move the CHG to Implement with the
   governed Implementation CTASK open.
4. Let the worker process the originally non-active member and fail traffic to
   it. The runner must stop at `approve-testers` with return code `20`.
5. Test the updated active member. Keep the tester CTASK open if evidence is
   missing or any check fails.
6. Close only the dedicated tester CTASK as Closed Complete to authorize the
   `second-member` resume. Skipped, Closed Incomplete, Canceled, work notes, and
   close notes do not authorize it.
7. Confirm the second member, ownership restoration, final captures, diff, and
   postcheck before closing the change.

`--simulate-gates` is lab-only and is intentionally absent from these governed
commands. The walkthroughs preserve the real ServiceNow tester boundary.

## Evidence To Retain

Retain the local evidence reference attached to the records, plus the ticket
history. At minimum, verify these fields rather than relying on a final success
sentence:

- readiness status, resolved object type, cluster mode, exact target set,
  current version, member mapping, policy package, package action and type,
  prerequisites, reboot expectation, and evidence reference;
- activity-plan schema, activity type, environment, current and target
  version/Take, backend, staging method, ownership policy, ICAP mode, tester
  gate, package identity and hashes, workflow gates, and evidence flags;
- original ACTIVE/STANDBY identities, pnotes, required interfaces, monitored
  interface signature, and virtual-interface signature;
- for each member, prerequisite result, exactly one CDT candidate, pre-action
  version/Take/state, action result, reboot and trusted reconnect, and reconciled
  version/Take;
- restored ownership, final support captures, per-member diffs, postcheck member
  records, empty `errors` and `package_state_errors`, summary status, worker
  return code, and successful ServiceNow bookkeeping.

The three scenario pages add the checks unique to each activity. Expected output
is synthetic and deliberately contains no reusable passing evidence.
