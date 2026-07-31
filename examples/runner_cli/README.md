# Run One Cluster Workflow Without ServiceNow

This example shows the complete lower-level runner path: prepare one CPUSE
package CSV, start the first member, stop at the tester gate, and resume at the
second member. It does not create or update ServiceNow records.

Without ServiceNow, every human gate is self-attested. The operator is
responsible for recording the approval, test evidence, and resume decision in
the organization's change system. Production use must keep the manual tester
pause between members.

> **Lab only:** `--simulate-gates` bypasses the manual tester stop. Never use it
> for a production change. The commands below intentionally do not use it.

For option details, prerequisites, and tested boundaries, use the
[component reference](../../docs/COMPONENT_REFERENCE.md),
[Start Here](../../docs/START_HERE.md), and
[tested scenarios](../../docs/CERTIFIED_SCENARIOS.md). The ServiceNow-managed
equivalent is in the [governed ticket example](../../docs/SERVICENOW_TICKET_EXAMPLE.md).

## Safety Of The Checked-In Files

[`cpuse-package.csv`](cpuse-package.csv) contains a placeholder package name
and invalid placeholder checksums. It must fail package validation as shipped.
The expected-output files are synthetic text; they are not captured firewall
output. Nothing in this directory authorizes a package change.

Do not edit the checked-in fixture into a live plan. Create a protected working
copy outside the repository and have another engineer verify its package name,
source file, and hashes before use.

## 1. Prepare The CPUSE CSV

Run commands from the repository root. The existing sanitized install and
remove samples show the accepted column layout:

```bash
sed -n '1,2p' test_inputs/cpuse_install_take91.csv
sed -n '1,2p' test_inputs/cpuse_remove_take91.csv
```

Create a mode-0600 working copy from this example's fail-closed template:

```bash
install -m 0600 examples/runner_cli/cpuse-package.csv /tmp/cpuse-package.approved.csv
```

In `/tmp/cpuse-package.approved.csv`, replace the placeholder package name and
both checksum placeholders with values from the approved package staged on the
MDS. Keep the action as `install` for this walkthrough. Do not continue until
the file has been independently checked against the staged artifact.

```bash
CPUSE_CSV=/tmp/cpuse-package.approved.csv
test -s "$CPUSE_CSV"
```

## 2. Set The ServiceNow-Free Context

Use TEST-NET addresses here only as a shape example. Replace them with the
approved non-production MDS and both managed cluster-member addresses. Load
Check Point credentials from the normal protected source; do not place them in
the CSV or command history.

```bash
unset SN_INSTANCE SN_USERNAME SN_PASSWORD
export RUN_ID=MANUAL_TEST_NET_PATCH
export CPUSE_CSV=/tmp/cpuse-package.approved.csv
export CP_PASSWORD
export CP_EXPERT_PASSWORD
```

The same `RUN_ID` must be used for the initial run and resume. The runner stores
the original member roles in
`ansible/reports/cluster_initial_state_${RUN_ID}.json`; changing the ID would
break the resume's state lineage and the second-member checks must fail closed.

## 3. Start The Workflow

This is a connected, mutating command after the placeholders have been
replaced. Use it only inside an approved maintenance window in a non-production
environment first.

```bash
python3 servicenow_checkpoint_runner.py \
  --chg-number "$RUN_ID" \
  --package-file "$CPUSE_CSV" \
  --target-ips '192.0.2.20,192.0.2.21' \
  --mds-host 192.0.2.10 \
  --activity-type software_patch_activity \
  --environment lab \
  --current-version R82 \
  --target-version R82 \
  --icap-mode disabled \
  --preserve-original-active true \
  --tester-gate true
```

The runner discovers the managed object, validates the plan and staged package,
captures the original cluster roles, processes the non-active member, fails
traffic to that updated member, and exits with return code `20` at
`approve-testers`. See the
[synthetic gate-stop output](expected/gate-stop.txt) for the console shape.

## 4. Validate Before Resume

Do not resume just because the command stopped as expected. A tester must
validate traffic and application health on the updated active member. At a
minimum, record:

- the change approval and the person performing the test;
- the original cluster-state evidence and current ACTIVE/STANDBY roles;
- application and traffic checks through the updated active member;
- gateway, interface, policy, ICAP, and package health required by the plan;
- the explicit decision to continue or stop.

Confirm that the first invocation left the original-state evidence under the
same manual ID:

```bash
test -s "ansible/reports/cluster_initial_state_${RUN_ID}.json"
```

If any test fails or the evidence is missing, do not resume. Follow the
remediation and rollback process for the approved change.

## 5. Resume At The Second Member

After the tester explicitly approves continuation, rerun the same command with
the same package file, targets, versions, and `RUN_ID`, adding only the resume
boundary:

```bash
python3 servicenow_checkpoint_runner.py \
  --chg-number "$RUN_ID" \
  --package-file "$CPUSE_CSV" \
  --target-ips '192.0.2.20,192.0.2.21' \
  --mds-host 192.0.2.10 \
  --activity-type software_patch_activity \
  --environment lab \
  --current-version R82 \
  --target-version R82 \
  --icap-mode disabled \
  --preserve-original-active true \
  --tester-gate true \
  --start-at second-member
```

The runner refreshes discovery, starts at the second-member phase, verifies the
stored original-state evidence, processes the remaining member, restores the
original active member when requested, captures final evidence, and runs the
postcheck. See the
[synthetic resume output](expected/resume.txt) for the expected shape.

Archive both run directories, the shared reports for `RUN_ID`, the tester's
evidence, and the final summary with the external change record. In CLI mode,
the runner cannot create a tester CTASK or prove who approved the resume; that
