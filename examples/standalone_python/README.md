# Standalone Python Workflow

This example runs the rolling workflow without ServiceNow and without
`ansible-playbook`. It uses the repository's Python helpers directly. It is a
lab structure example, not a certified combination.

Only JHF install, JHF removal, and Blink major upgrade plans are accepted.
Deployment Agent and other package types are rejected during local validation,
before a helper command can run.

The checked-in plans are intentionally disabled. They contain TEST-NET
addresses, generic object names, and invalid checksum placeholders. Do not edit
them in place. Create a protected working copy outside the repository and have a
second engineer check every identity and published hash.

The complete operating guide is
[Standalone Python Workflow](../../docs/STANDALONE_PYTHON_WORKFLOW.md).

## Prepare a run

```bash
umask 077
export CP_PASSWORD
export CP_EXPERT_PASSWORD
export RUN_ID=STANDALONE_T76_INSTALL
export RUN_DIR="/var/tmp/checkpoint-standalone/$RUN_ID"
export PLAN="$RUN_DIR/activity-plan.json"

install -d -m 0700 "$RUN_DIR"
install -m 0600 examples/standalone_python/take76-install.json "$PLAN"
```

Replace every example address, object, package path, and checksum in
`$PLAN`. The package must already be present on the MDS at the exact
`source_path`. Install and upgrade plans require `cprid_from_mds` staging
and an MDS host; major upgrades also require the CMA, API domain, cluster, and
policy package identities.

Validate without touching a gateway:

```bash
python3 checkpoint_standalone_workflow.py validate \
  --activity-plan-file "$PLAN" \
  --run-dir "$RUN_DIR"
```

The placeholder plan must fail with `requires a valid published SHA256`.
Continue only after the protected copy validates. A successful validation creates
`$RUN_DIR/activity-plan.locked.json` from the exact source bytes. Keep passing
`$PLAN` so the coordinator can verify its journaled source binding; every helper
that consumes a plan receives only the locked snapshot. Do not modify, replace,
link, or re-permission either file. The lock is a workflow contract, not an OS
immutable flag.

## Run the phases

The commands below validate the protected inputs and show the common opening.
They deliberately omit mutation authorization and cannot change a firewall as
copied. Follow the controlled execution procedure in
[Start Here](../../docs/START_HERE.md) after a second engineer has verified the
plan, hashes, target identities, and rollback point. During an authorized run,
each invocation completes one phase; the journal refuses skips and repeats.
Stop after `first-member` and choose the correct scenario sequence below. A
major upgrade must run its policy and MVC phases before failover.

```bash
python3 checkpoint_standalone_workflow.py capture-state --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
python3 checkpoint_standalone_workflow.py baseline-capture --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
python3 checkpoint_standalone_workflow.py stage-files --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
python3 checkpoint_standalone_workflow.py first-member --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
```

For each member phase, the coordinator creates the private intent directory and
passes `$RUN_DIR/mutation-intents/<phase>.json` to the package helper as
`--mutation-intent-file`. The helper atomically creates the intent file, bound
to the exact plan and package identity, immediately before CPUSE is called.
Once present, that phase is
reconciliation-only and can never dispatch the mutation again.
A removal retry must also resolve the approved alias from fresh local CPInstLog
history to the same exact package stored in the intent before absence can be
accepted.

## Live-validated phase order

Use a fresh protected plan copy and a new run directory for every scenario.
The August 2026 lab pass exercised all three sequences below with the Python
helpers directly, without ServiceNow and without `ansible-playbook`.

Take 76 install:

```text
capture-state -> baseline-capture -> stage-files -> first-member
failover-to-first -> simulate-tester-gate -> second-member
restore-original-active -> final-capture -> postcheck
```

Take 76 removal follows the same order without `stage-files`. The submitted
alias must resolve to one exact installed identity before either uninstall.

R81.20 to R82 build 777 with embedded Take 60:

```text
capture-state -> baseline-capture -> stage-files -> first-member
mixed-version-policy -> mvc-on -> failover-to-first
simulate-tester-gate -> second-member -> final-policy -> mvc-off
restore-original-active -> final-capture -> postcheck
```

The major sequence is strict. After the first member reconciles to the exact
R82 release, Take, and Blink identity, the mixed-version policy phase starts
ClusterXL for that member and MVC must read back `ON` before failover. After the
second member reconciles, final Access Control and Threat Prevention policy
must succeed on both members before MVC can be turned off.

A Blink upgrade uses a two-choice `yes/no` confirmation. A JHF prompt may
offer a separate suppress-reboot choice; never answer Blink as if that choice
exists.

The live runs used three healthy monitoring samples and the simulated tester
gate. That proves the technical stop, evidence validation, failover, and resume
path; it is self-attestation and does not prove independent human approval.

Collect three real monitoring samples after application and traffic validation:

```bash
umask 077
python3 ansible/scripts/monitor_gateways.py \
  --members MEMBER_A_IP MEMBER_B_IP \
  --include-take --icap-mode disabled \
  --samples 3 --interval 20 \
  --output-jsonl "$RUN_DIR/reports/tester-gate.jsonl"
```

The simulated gate is still a real stop. It records self-attestation only after
the technical evidence is healthy. Keep the JSONL owned by the operator with
mode `0600`. The gate reads and hashes the same protected bytes, rejects links
and changes detected during its finite open/read/path-check/confirmation-read/
final-descriptor snapshot, and requires at least three positive increasing sample IDs with
timezone-aware increasing timestamps. Every sample must be newer than the
validated `failover-to-first` completion for the journal's random internal run
identity and no later than the single UTC ceiling captured when gate validation
starts. Completion records, the tester record, and its failover reference all
carry that identity, which prevents accidental or cross-directory reuse between
independently initialized runs with the same plan. The journal uses unkeyed
integrity hashes for structural consistency; the identity is not cryptographic
proof against an operator who can rewrite the journal and recompute those
hashes:

```bash
python3 checkpoint_standalone_workflow.py simulate-tester-gate \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" \
  --evidence "$RUN_DIR/reports/tester-gate.jsonl"
```

Finish a Take 76 install or removal with this order:

```text
second-member
restore-original-active
final-capture
postcheck
```

Finish a major upgrade with this order. Do not restore ownership before the
final target-version policy succeeds and MVC is off:

```text
second-member
final-policy
mvc-off
restore-original-active
final-capture
postcheck
```

Use `take76-remove.json` in a fresh run directory for removal. Removal omits
`stage-files`; the journal selects the correct phase order from the action.
The local CPInstLog resolver must turn `Take76` into exactly one installed
package identity before CPUSE is contacted.

After verifying a replacement fingerprint through a trusted channel, retry the
same stopped member phase with the protected evidence record:
[host-key stop](expected/host-key-stop.txt) output shapes.

After verifying a replacement fingerprint through a trusted channel, retry the
stopped member phase with the protected evidence record:

```bash
python3 checkpoint_standalone_workflow.py first-member \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" \
  --host-key-evidence "$RUN_DIR/reports/host-key-verification.txt"
```

If an intent exists but exact reconciliation fails, do not remove or edit the
intent and do not reuse that run directory. Preserve the run as evidence,
restore the authorized clean snapshot or baseline, make a new protected plan
copy, and start with a new run directory. This is required because a stop after
the intent write but before CPUSE dispatch cannot be distinguished safely from
a dispatched operation whose result was lost.
