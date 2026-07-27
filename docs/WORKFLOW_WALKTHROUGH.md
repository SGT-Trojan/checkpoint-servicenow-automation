# CheckPoint FW Automation — End-to-End Walkthrough (Requester: Firewall Engineer)

This sanitized walkthrough describes the implemented control flow. All names, addresses, records, and evidence references are illustrative.

## Phase 0 — Request submission (Firewall Engineer)

1. Firewall Engineer opens the Service Portal → Service Catalog → **Check Point Firewall Automation** → **CheckPoint FW Maintenance Activity** (the single catalog item; the old Patch/Upgrade items are retired).
2. He fills the simplified form:
   - **Activity Type** — must explicitly pick one of three: Version Upgrade Activity / Software Patch Activity / Deployment Agent Install (no default).
   - **Environment**, **ICAP Check Mode**, **Target Firewall IPs**, **MDS Host/IP**, **Current/Target Check Point Version**.
   - **CPUSE Package** (mandatory) — downloads `CPUSE_Package_Template.csv` from the link under the field, fills `sequence_number, action (install/uninstall/upgrade), package_name, sha1, sha256, package_type, notes`, uploads it. JHF aliases (Take 91 / T91 / JHF_T91) are allowed; automation resolves them.
   - **CPUSE Dependency Checklist** (optional) — `expected_state (Present/Not Present), package_name, notes`.
   - **Preserve Original Active Member**, **Tester Validation Gate**, requested maintenance window, **Special Instructions**.
   - He does NOT choose execution engine, staging method, package directory, CMA, or credentials — backend policy owns those (CDT for package execution, CPRID from MDS, `/var/log/tmp`, SSH under the hood for health checks).
3. **Order Now** → REQ is created (e.g., REQ_EXAMPLE).

## Phase 1 — Intake (ServiceNow business rule, automatic, seconds)

4. The RITM is created carrying all variables and the uploaded files.
5. The intake BR (`create read`) fires exactly once per open RITM (idempotent; never retro-fires on closed/legacy RITMs):
   - Auto-approves the RITM (lab model) and writes a structured description tagged `[CHECKPOINT_AUTOMATION_INTAKE]` (activity, IPs, MDS, versions, ICAP/tester flags, attachment inventory, backend policy statements).
   - Sets REQ/RITM short descriptions and summary notes.
   - Creates the **"Automated Check Point readiness validation - <activity>"** SCTASK assigned to Firewall Deploy.
6. Deliberately, **no CHG exists yet** — a change is only created for validated, actionable work.

## Phase 2 — Automated readiness validation (readiness worker, picks up within 60s)

7. `snow-checkpoint-readiness-worker` polls, claims the SCTASK, downloads the RITM's CSV/XLSX attachments, parses package steps and dependency requirements, then runs **read-only** validation playbooks against the environment:
   - Target discovery: MDS → CMA/domain, cluster object, member names, management/access IPs, policy package.
   - Activity-plan validation, gateway precheck (one ACTIVE/one STANDBY, PNOTEs, monitored interfaces, ICAP if required), Deployment Agent readiness, package presence + checksum in `/var/log/tmp` on the MDS, per-step `requires_present`/`requires_absent` prerequisite checks with alias resolution.
8. **Pass** → SCTASK closes Closed Complete with `u_checkpoint_readiness_status=ready`, `source=automated`, summary + evidence path; the same readiness fields are stamped on the RITM.
9. **Fail** → SCTASK closes Closed Incomplete (`status=failed`) and a **"Firewall Deploy manual readiness remediation - <task>"** SCTASK is created (assigned to Firewall Deploy) carrying the failure, the exact check, and the evidence directory. The engineer remediates, sets readiness `ready`, closes Complete → flow continues. If instead they set `rejected`/`not_viable` or close Incomplete → the RITM is auto-closed incomplete and **no CHG is ever created**.

## Phase 3 — Governed CHG creation (readiness-S business rule, automatic)

10. The ready closure triggers CHG creation (field-driven only; close-note text is audit, not logic):
    - CHG carries the `[CHECKPOINT_AUTOMATION]` marker, parent RITM, CI resolved from target IPs to a firewall **member** CI (Network Device category), Firewall Deploy submitter/group, and implementation/test/backout plans.
    - Two governed CTASKs are created: **"Implementation - Check Point firewall automation workflow"** (the automation driver and evidence trail) and **"Tester validation gate - Check Point automation"** (description states: automation pauses after member 1 + failover; Closed Complete authorizes member 2; Closed Incomplete keeps it blocked).

## Phase 4 — Change governance (Firewall Engineer / CAB)

11. Firewall Engineer drives the CHG through the normal change model: **Assess** (approvals requested) → **Authorize** (CAB approvals) → **Scheduled** → **Implement**. State-jumping is blocked by the platform.
12. On entering Implement, ServiceNow's change model spawns its own default tasks; for automation CHGs these are relabeled **"Change-model default: … (auto-managed, no action needed)"** and left open (the automation closes them at the end — closing them early makes the model auto-advance the change, which is exactly the CHG_EXAMPLE incident).

## Phase 5 — Execution (implementation worker, picks up within 60s of Implement)

13. `snow-checkpoint-worker` finds the CHG (marker + Implement + approved), re-validates the full governance gate (marker, state, approval, closed readiness SCTASK on the parent RITM, open Implementation CTASK), and launches the runner. It will never double-launch (singleton lock + per-CHG state machine) and never auto-retries a failure.
14. Runner phase sequence (each phase posts a note to the CHG; the mirror BR copies it to the Implementation CTASK; logs attach to the CTASK):
    discovery → validate plan → precheck → DA readiness → cluster-state capture → baseline support capture → MDS package/checksum + air-gap gate → **member 1**: prerequisites → controlled CDT candidates (only the target member enabled) → guarded CDT execute → reboot/readiness monitor → **failover to member 1** → **tester gate pause**.

    Deployment Agent Install is intentionally different: validate plan → precheck → DA readiness → MDS package/checksum + air-gap acknowledgement → prerequisites → direct `installer agent install` on all target members in one install-deployment-agent phase → DA readiness. It does not run baseline/final support capture, support diff, failover, tester gate, restore-original-active, or final JHF/package postcheck.

    Version Upgrade Activity (major upgrade) adds the mixed-version phases to the rolling sequence: after member 1 completes it runs the mixed-version policy gate (`31_major_policy_gate.yml`) and turns MVC on (`32_major_mvc.yml`) before failover and the tester gate; after member 2 it runs the final policy install and turns MVC off, then continues with restore/final capture/diff/postcheck. Major upgrades require a two-member cluster — standalone targets are rejected at plan time.

## Phase 6 — Tester gate (human)

15. The worker parks in `waiting_tester` and notes it on the CHG. The tester validates traffic, services, policy behavior, and ICAP on the upgraded member, then closes **"Tester validation gate - Check Point automation"** as **Closed Complete**. Only that exact task, deliberately closed Complete, opens the gate (suppressed/relabeled/auto-generated tasks cannot).
16. The worker resumes from `second-member`: prerequisites → CDT → monitor → restore original active (if requested) → final support capture → support diff → final postcheck (JHF installs validated by take + health; removals by inventory absence; `.tar` vs `.tgz` identity tolerated).

## Phase 7 — Failure branch (any phase)

17. On any failure the worker creates **"Engineer remediation required - Check Point automation at <phase>"** with failed phase/playbook/step, log path, run directory, and resume instructions, with `u_checkpoint_resume_status=pending`. The CHG stays in Implement.
18. The engineer remediates, sets **Checkpoint Resume Status = approved** on the CTASK (field on the form), closes it Complete → the worker resumes from the failed phase. Closing it Incomplete, or with rejected/not_viable, permanently blocks the change until deliberate manual intervention.

## Phase 8 — Success bookkeeping (automatic)

19. On success the worker:
    - Creates and closes **"Final validation - Check Point post-implementation checks"** (Closed Complete) with the postcheck outcome and evidence paths.
    - Closes the Implementation CTASK Closed Complete with a completion summary.
    - Closes the relabeled change-model default tasks so the model can advance its phase naturally.
    - Moves the CHG to **Review**.

## Phase 9 — Closure (Firewall Engineer / change management)

20. Firewall Engineer (or change management) reviews and closes the CHG (close code successful); the RITM/REQ complete through the normal catalog flow. Durable evidence lives in `checkpoint-servicenow-automation/runs/<CHG>_*/` (activity plan, per-phase logs, support captures, diffs) plus the ServiceNow record trail: REQ → RITM (readiness fields) → readiness SCTASKs → CHG (approvals + phase notes) → Implementation/Tester/Final-validation CTASKs.

## What Firewall Engineer never has to provide

Credentials (worker environment now; CyberArk in production), execution engine choice, staging method, package source directory, CMA/domain/policy names, cluster/member objects, CDT candidate control — all discovered, policy-driven, or vault-owned.
