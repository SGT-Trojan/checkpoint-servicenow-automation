# Standalone Python Workflow

This workflow changes a two-member Check Point cluster without ServiceNow and
without Ansible. It composes the same hardened Python helpers used behind the
governed workflow, but the operator owns approval, timing, tester validation,
and evidence retention.

It is not a shortcut around the safety gates. Each command advances one phase
in a protected journal. A failed or uncertain command leaves that phase open.

The coordinator accepts only `install` with `package_type: jhf`, `remove` with
`package_type: jhf`, and `upgrade` with `package_type: blink`. Deployment Agent
and other package operations must use their established governed paths.

## What it provides

- Standby-first install, removal, or major upgrade
- Exact release, Take, and package reconciliation after install or upgrade
- Exact package-absence reconciliation after removal
- A required tester stop backed by three healthy monitoring samples
- Mixed-version policy, MVC, and final policy phases for major upgrades
- Original ACTIVE-member restoration
- Atomic mode-0600 state, output logs, and reconciliation evidence
- Atomic mode-0600 mutation intents that prevent duplicate package operations
- Nonblocking locking to prevent two processes using the same run directory
- A stop-and-verify path for changed SSH host keys

It does not create ServiceNow records, approvals, tester tasks, or durable
identity for the person who approves a gate. The simulated gate is a local
self-attestation. Production use must keep an independent human tester.

## August 2026 live recertification

The standalone Python path was recertified on a two-member lab cluster without
ServiceNow and without Ansible. The live runs covered R81.20 Take 76 install and
removal, followed by a major upgrade from R81.20 to R82 build 777 with embedded
Take 60. The tester gates were simulated: they were operator self-attestation,
not independent human approval.

The major upgrade completed in this exact order:

```text
validate
capture-state
baseline-capture
stage-files
first-member
mixed-version-policy
mvc-on
failover-to-first
simulate-tester-gate
second-member
final-policy
mvc-off
restore-original-active
final-capture
postcheck
```

The run still exercised package verification, standby-first mutation, reboots,
exact target reconciliation, policy installation, MVC transitions, failover,
original-ACTIVE restoration, and final cluster health checks. Simulating the
tester gate did not weaken or replace any of those controls.

## Before you begin

1. Confirm the release, image, JHF, and upgrade path are supported by Check
   Point for your appliance and cluster.
2. Restore and validate a known lab baseline before using the examples.
3. Place the approved package on the MDS and obtain its published SHA-256.
4. Confirm backup and restore-point capacity.
5. Check policy installation, cluster health, PNOTEs, interfaces, licensing,
   and management connectivity.
6. Choose application and traffic tests that prove the upgraded member works.
7. Keep console access available. A Blink upgrade normally changes SSH host
   keys.

Do not put passwords in the activity plan or command line. Export
`CP_PASSWORD` and `CP_EXPERT_PASSWORD` in a protected shell.

## Files and state

Start from one of the sanitized plans under
`examples/standalone_python/`. The checked-in copies cannot run because their
hashes and identities are placeholders.

```bash
umask 077
export RUN_ID=STANDALONE_T76_INSTALL
export RUN_DIR="/var/tmp/checkpoint-standalone/$RUN_ID"
export PLAN="$RUN_DIR/activity-plan.json"
install -d -m 0700 "$RUN_DIR"
install -m 0600 examples/standalone_python/take76-install.json "$PLAN"
read -rsp "Gaia password: " CP_PASSWORD; echo
export CP_PASSWORD
read -rsp "Expert password: " CP_EXPERT_PASSWORD; echo
export CP_EXPERT_PASSWORD
```

The run directory contains:

| Path | Purpose |
|---|---|
| `workflow-state.json` | Schema-v9 checksummed phase journal, random run identity, source binding, completion ledger, at most one pending member operation, and exactly one bound reconciliation record per completed member phase |
| `activity-plan.locked.json` | Exact mode-0600 plan bytes captured atomically at `validate` |
| `workflow.lock` | Nonblocking process lock |
| `logs/<phase>.log` | Complete helper output and return context |
| `reports/cluster_initial_state_<RUN_ID>.json` | Original roles and interface baseline, hash- and identity-bound in the journal |
| `reconciliation/first-member.json` | Private descriptor-bound first-member result |
| `reconciliation/second-member.json` | Private descriptor-bound second-member result |
| `mutation-intents/first-member.json` | Schema-v3 durable first-member dispatch boundary |
| `mutation-intents/second-member.json` | Schema-v3 durable second-member dispatch boundary |

At `validate`, the coordinator generates an internal run identity in the strict
form `run_` plus 64 lowercase hexadecimal characters using Python's `secrets`
module. Every completed phase binds that identity with the phase name, sequence,
UTC completion timestamp, and plan SHA-256 under its own digest. The tester-gate
record and its failover-completion reference carry the same identity. This stops
an internally valid completion ledger or tester record from an independently
initialized run directory being reused by another run, even when both runs use
identical plan bytes.

For each member phase, the coordinator generates a cryptographically random
64-hex event nonce and uses it to derive a unique operation ID and completion
ID. Before the helper can start, it durably journals that pending operation,
including the dispatch target and package context. It passes the run ID,
locked-plan SHA-256, phase, event nonce, operation ID, and completion ID through
the helper CLI. A same-run retry must reuse the pending identity and is invoked
with `--standalone-reconciliation-only`; it cannot redispatch a mutation, even
when no mutation-intent artifact was published before the earlier interruption.
Two independently initialized attempts for the same run, plan, and phase
therefore have different identities.

The helper binds the complete standalone context and the canonical schema-v3
mutation-intent SHA-256 into its schema-v3 reconciliation payload. Standalone
orchestration creates the owner-only reconciliation file and passes its
already-open descriptor with `--reconciliation-fd`; the helper never
re-resolves that output pathname. After validating the inherited descriptor,
protected intent, host, package outcome, and every binding, the coordinator
journals exactly one reconciliation and its matching phase completion while
atomically consuming the pending operation.

Every journal read requires exactly one reconciliation record for each completed
member phase and none for incomplete or unknown phases. It also revalidates the
run, plan, phase, event nonce, operation, intent, completion, normalized host,
nested payload hash, consumed pending-operation proof, and completion-record
proof. Exactly one pending operation is allowed only for the next incomplete
member phase. Missing, extra, replayed, or edited records fail closed even when
the outer journal hash alone has been recomputed.

The journal reader verifies the complete ledger on every invocation. Older
journal schemas, missing or extra completion records, malformed identities or
timestamps, non-increasing or future completion chronology, mixed-run records,
and structurally inconsistent records fail closed. These are unkeyed SHA-256
integrity checks, not cryptographic proof against an operator who controls the
journal bytes and can recompute the hashes, including all run-identity fields.
Preserve an incompatible run as evidence and start a new run directory; never
upgrade a journal by hand.

At `validate`, the coordinator reads the source plan once, atomically writes those
exact bytes to `activity-plan.locked.json`, and hash-binds both records in the
journal. Before each later phase, the coordinator opens the locked plan with
`O_NOFOLLOW`, verifies its recorded identity, owner, mode, size, and hash, then
keeps that descriptor open through helper completion. Plan-consuming helpers
receive only `/proc/self/fd/N`, inherited with `pass_fds`; they never reopen the
locked pathname. Replacing the pathname after verification therefore cannot
change the bytes seen by a dispatched helper. The coordinator also rechecks the
source path, file identity, size, and hash against the validation record, so a
changed or replaced source stops the next phase.

After `capture-state`, the same descriptor-bound rule applies to the recorded
cluster-state file. The coordinator verifies its identity, owner, mode, size,
hash, and original member roles, then passes an inherited `/proc/self/fd/N` path
to every helper that uses the state to select or validate a member. Replacing
the state pathname after verification cannot redirect the in-flight phase.

"Locked" is a workflow integrity contract, not an OS immutable flag. The file is
mode 0600 inside the mode-0700 run directory; do not edit, chmod, unlink, or
replace either plan after `validate`. Start a new run directory for every plan
change.

Before a package install, upgrade, or removal is dispatched, the member helper
atomically records a private mutation intent. It binds the exact plan SHA-256,
semantic host identity, action, step, requested package name, source path,
package type, resolved package identity, target release/Take where applicable,
and the run, phase, event nonce, operation, and completion identities.

During initial reconciliation and every later journal read, the coordinator
opens `mutation-intents/<phase>.json` with mandatory `O_NOFOLLOW` through its
owner-owned mode-0700 real parent. It requires an owner-owned mode-0600 regular
file, stable pathname/descriptor/inode/metadata/contents, the exact supported
schema, the same canonical intent digest as the helper, and exact equality with
the journaled protected-file evidence. The member completion proof also binds
the artifact's raw-byte hash and protected path, owner, mode, device, inode,
size, and timestamps to the run, plan, phase, operation, completion, and event
identities. Recomputing only the outer journal checksum cannot legitimize a
replacement. Missing, extra, replaced, altered, or stale-schema artifacts fail
closed. A retry can reconcile only and cannot
dispatch the package operation again. For a removal retry, the helper opens a
fresh read-only expert session and requires the same approved step to resolve to
that exact identity from local CPInstLog history before it checks package
absence.

## Validate

Replace all example values in the protected copy, including:

- both member addresses;
- MDS, CMA, domain, cluster, and policy names;
- current and target releases;
- target Take;
- exact package filename and MDS path;
- published 64-character SHA-256;
- ICAP mode.

Then run:

```bash
python3 checkpoint_standalone_workflow.py validate \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
```

Validation is local and non-mutating. It requires two distinct members, a
single package step, the standalone backend, a tester pause, ownership
restoration, safe package/path characters, and a published SHA-256 for install
or upgrade. Install and upgrade also require CPRID-from-MDS staging and an MDS
host. Major upgrades additionally require the CMA, API domain, cluster, and
policy package identities used by the policy phases.

## Take 76 install

Run each command separately and inspect its log before continuing:

```bash
python3 checkpoint_standalone_workflow.py capture-state --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py baseline-capture --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py stage-files --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py first-member --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py failover-to-first --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
```

`first-member` imports and verifies the package, runs `installer install`,
waits through the expected disconnect/reboot, then requires exact R81.20,
Take 76, and package identity. A disconnect alone never completes the phase.

After failover, run real application and traffic tests. Then collect evidence:

```bash
umask 077
python3 ansible/scripts/monitor_gateways.py \
  --members MEMBER_A_IP MEMBER_B_IP --include-take \
  --icap-mode disabled --samples 3 --interval 20 \
  --output-jsonl "$RUN_DIR/reports/tester-gate.jsonl"

python3 checkpoint_standalone_workflow.py simulate-tester-gate \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" \
  --evidence "$RUN_DIR/reports/tester-gate.jsonl" --execute
```

The gate accepts only a regular file owned by the executing user with mode `0600`.
It opens the file once with `O_NOFOLLOW` and hashes and parses only the first byte
snapshot read from that descriptor. After the path check, a confirmation read
from the same descriptor must match those bytes. Device, inode, size, mode,
owner, nanosecond modification time, and nanosecond change time must also remain
identical across the open, reads, path check, and final descriptor check. This is
a finite snapshot boundary: it detects changes during validation but does not
make the file immutable after the gate returns. At least three rows are required.
Each row needs a positive integer
`sample` value and a timezone-aware `timestamp`; both must be strictly increasing.
Every timestamp must be newer than the validated `failover-to-first` completion
and no later than the single UTC ceiling captured when gate validation starts.
The health checks still require the upgraded first member to be active at the
target Take. These checks protect technical evidence only; the independent human
tester, application results, and continue/stop decision remain required.

Finish the run:

```bash
python3 checkpoint_standalone_workflow.py second-member --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py restore-original-active --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py final-capture --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py postcheck --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
```

## Take 76 removal

Create a new run directory from `take76-remove.json`. Confirm both members
start on Take 76. The removal order is:

```text
validate
capture-state
baseline-capture
first-member
failover-to-first
simulate-tester-gate
second-member
restore-original-active
final-capture
postcheck
```

There is no staging phase. Before uninstalling, the helper searches local
CPInstLog history and requires exactly one installed package identity for the
`Take76` alias. The interactive CPUSE dialog remains interactive. After
reboot, the package inventory must prove the exact package is absent.

The tester evidence must show that the first member no longer reports Take 76.
The final postcheck requires Take 76 to be absent from both members. Describe
the result as "no separately installed JHF remains," not as a product Take
numbered zero.

## R81.20 to R82 major upgrade

Restore a clean R81.20 baseline and use `r8120-to-r82.json`. Confirm that the
approved Blink image contains the intended R82 build and Take 60.

Run through `first-member` as in the install flow. The helper uses
`installer upgrade`, not `installer install`, and requires exact R82,
Take 60, and Blink identity after reconnect.

Blink presents a two-choice yes/no confirmation. A JHF install can instead
offer an additional choice that suppresses reboot. Never answer the Blink
prompt as though that suppress-reboot choice exists. If exact Blink target
reconciliation succeeds while ClusterXL reports `HA module not started`, treat
that state only as a handoff to the mandatory new-version policy phase. It is
not a successful cluster-health verdict, and failover remains blocked until the
policy phase and its checks complete.

Before failover:

```bash
python3 checkpoint_standalone_workflow.py mixed-version-policy --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py mvc-on --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py failover-to-first --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
```

Perform application and traffic tests, collect three monitor samples, and
record the simulated gate exactly as shown in the install flow. Then:

```bash
python3 checkpoint_standalone_workflow.py second-member --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py final-policy --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py mvc-off --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py restore-original-active --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py final-capture --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
python3 checkpoint_standalone_workflow.py postcheck --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" --execute
```

The mixed-version phase changes the management object version in a controlled
sequence and validates partial policy results. The final phase sets the target
version and requires successful Access Control and Threat Prevention policy
tasks. MVC is enabled only for the upgraded first member and disabled on both
members after the second upgrade. Each target must return an explicit zero exit
status from `cphaconf mvc on/off`, followed by an unambiguous matching
`cphaprob mvc` state. The later ClusterXL settle check is supplemental health
evidence and cannot substitute for MVC state proof.

## Changed SSH host key

Never bypass or globally disable host-key checking. If a member phase stops on
a changed key, bounded SSH keepalives ensure that a stale PTY terminates instead
of waiting indefinitely. Keepalive termination is only a transport result; it
does not prove that the package operation succeeded.

To recover:

1. Read the phase log and note the member.
2. Obtain the new fingerprint from the gateway console or another trusted
   channel.
3. Compare it with the fingerprint presented by SSH.
4. Remove only the old entry and reconnect interactively to store the verified
   key.
5. Record what was checked in a protected evidence file.
6. Retry the same phase with `--host-key-evidence`.

```bash
install -m 0600 /dev/null "$RUN_DIR/reports/host-key-verification.txt"
printf '%s\n' 'Fingerprint verified through the gateway console.' \
  > "$RUN_DIR/reports/host-key-verification.txt"

python3 checkpoint_standalone_workflow.py first-member \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR" \
  --host-key-evidence "$RUN_DIR/reports/host-key-verification.txt" --execute
```

Retry only the same phase after trusted fingerprint verification. The pending
operation forces a reconciliation-only retry and cannot redispatch the package
mutation. If the upgrade already completed, exact target reconciliation records
an idempotent result; otherwise the phase remains open and fails closed.

## Resume and recovery

Show the current state at any time:

```bash
python3 checkpoint_standalone_workflow.py show-state \
  --activity-plan-file "$PLAN" --run-dir "$RUN_DIR"
```

| Condition | Result | Recovery |
|---|---|---|
| Wrong phase requested | No helper runs | Run the next phase shown in the error |
| Source plan changed or was replaced | Source binding mismatch; no helper runs | Preserve the run and start a new run directory |
| Locked plan changed, replaced, linked, or re-permissioned | Snapshot integrity failure; no helper runs | Preserve evidence and start a new run directory |
| Journal edited, damaged, or from an older schema | Integrity or schema failure | Preserve evidence; start a new run; never reconstruct completion manually |
| Installer RC missing/nonzero | Member phase remains open | Diagnose CPUSE; retry only after correction |
| Installer disconnect | Provisional only | Wait for exact target reconciliation |
| Persisted intent cannot reconcile | Dispatch timing is uncertain; no retry is allowed | Preserve the complete run directory, restore the authorized clean snapshot or baseline, and start a new run directory with a new protected plan instance |
| Wrong release, Take, or package | Member phase remains open | Restore or remediate before retry |
| Gate evidence unhealthy | Second member remains blocked | Correct the fault and collect three new samples |
| Changed host key | Member phase remains open | Verify fingerprint and provide evidence |
| Another process holds the lock | Second process exits | Find the active operator; never delete a live lock |

Do not mark phases complete by editing the journal. The journal records only
successful helper return, required reconciliation, and accepted evidence.
Do not delete, clear, rename, or reuse a mutation intent. A process can stop
after the intent is durable but before the installer command reaches CPUSE, and
there is no safe local test that distinguishes that case from a command whose
result was lost. Reusing the same run could duplicate a real mutation. The
supported recovery therefore keeps the uncertain run as evidence, restores the
authorized clean baseline, and begins again with a new run directory and plan
instance.
