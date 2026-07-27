# Live Validation and Certification Matrix

Last updated: 2026-07-27

## How to Read This Document

This matrix records scenarios that were executed against a controlled
Management Server, CMA, and two-member Check Point cluster. It is intended to
show the evidence behind the implementation, not to replace Check Point's
compatibility guidance or certify another environment.

- **ServiceNow governed** means the catalog request, automated readiness,
  approvals, native change states, implementation task, real tester gate,
  remediation/resume behavior, final validation, and closure were exercised.
- **Runner-level** means the firewall workflow was executed live from the
  command line. Some runner certifications used a governance override and a
  simulated tester gate; those are identified below.
- **Independently reviewed** means retained evidence and implementation claims
  were checked by a second coding agent without accessing the live systems.
- **Take 0** means no separately installed Jumbo Hotfix remained after Take 76
  was removed; it does not mean an unpatched Gaia image contains no fixes.

## Certified Environment Anchors

| Component | Validated value |
|---|---|
| Cluster shape | Two Security Gateway members with controlled active/standby failover |
| Management | MDS with a CMA, Access Control and Threat Prevention policy gates |
| R81.20 baseline | Gaia R81.20 build 634, no separately installed JHF after Take 76 removal |
| R82 target image | Gaia R82 build 777 with JHF Take 60 embedded in the Blink image |
| Deployment Agent | Build 2771, enabled, connected, current, and licensed during the final cycles |
| Health gates | SIC, ClusterXL, PNOTEs, required and virtual interfaces, policy, CPUSE, capacity, licensing, and ICAP where stated |

## Full ServiceNow-Governed CDT Certification

All rows in this section completed through independent ServiceNow catalog
chains on 2026-07-26. No governance override or simulated tester gate was used.

| Activity | Starting state | Ending state | Live controls exercised | Result |
|---|---|---|---|---|
| JHF install | R81.20 build 634, Take 0 | R81.20 build 634, Take 76 on both members | Automated readiness, approvals, guarded one-member CDT execution, real reboots, tester gate, final validation, closure | Passed; independently reviewed |
| JHF uninstall | R81.20 build 634, Take 76 | R81.20 build 634, Take 0 on both members | `Take 76` alias resolution through CPRID/CPInstLog, guarded rolling removal, real reboots, tester gate, final validation, closure | Passed; independently reviewed |
| Major upgrade | R81.20 build 634, Take 0 | R82 build 777, embedded Take 60 on both members | Mixed-version policy gate, MVC on/off, rolling upgrade, controlled failover, real tester gate, SSH-host-key remediation tasks, phase-specific resume, final policy and closure | Passed; independently reviewed |

The governed major-upgrade evidence proved version, Take, policy, ClusterXL,
PNOTEs, interfaces, remediation/resume, and closure. Its generated final
postcheck recorded ICAP as skipped because of a then-existing propagation
defect. ICAP was observed separately, and the propagation defect was later
fixed; that original evidence set is not retroactively described as proving
final governed ICAP.

## Runner-Level CDT Certification

These live runs on 2026-07-26 restored the same R81.20 baseline and exercised the
firewall workflow before the full ServiceNow certification. They used a lab
governance override and simulated tester gates.

| Activity | Starting state | Ending state | Result |
|---|---|---|---|
| Take 76 install | R81.20 build 634, Take 0 | R81.20 Take 76 on both members | Passed; real rolling CDT execution and reboots; independently reviewed |
| Take 76 uninstall | R81.20 Take 76 | R81.20 Take 0 on both members | Passed; authoritative CPInstLog identity resolution and rolling CDT removal; independently reviewed |
| Major upgrade | R81.20 build 634, Take 0 | R82 build 777, Take 60 | Passed; MVC, policy, failover, support diff, ownership restoration, ICAP and postcheck; independently reviewed |

A later runner-level CDT cycle on 2026-07-27 discovered R82 Take 107 as the
current Recommended Take, downloaded and verified the 2.49 GB package, staged it
to the MDS, and installed it one standby member at a time:

| Activity | Starting state | Ending state | Result |
|---|---|---|---|
| Recommended JHF install | R82 build 777, Take 60 | R82 build 777, Take 107 on both members | Passed with real reboots, controlled failover, policy and strict health checks; independently reviewed |

## Runner-Level Management API Certification

The isolated Management API backend was live-tested on 2026-07-27. It was not
selected by the ServiceNow catalog, and no successful API run invoked CDT.
These runs used a lab governance override and simulated tester gate.

| Activity | Starting state | Ending state | Execution path | Result |
|---|---|---|---|---|
| Take 76 install | R81.20 build 634, Take 0 | R81.20 Take 76 | API repository import, verification, and cluster execution | Passed; independently reviewed |
| Take 76 uninstall | R81.20 Take 76 | R81.20 Take 0 | API inventory and CPRID/CPInstLog identity resolution, then guarded direct CPUSE member removal | Passed; independently reviewed; explicitly not an API-only uninstall claim |
| Major upgrade | R81.20 build 634, Take 0 | R82 build 777, Take 60 | API package execution plus mixed-version policy, MVC, explicit failover, final policy, restoration, and postcheck gates | Passed; independently reviewed |

The tested API rejected the per-member semantics required for safe rolling
cluster uninstall. The direct CPUSE fallback is deliberate, visible in the
workflow, and does not invoke CDT.

## Earlier Governed Functional Coverage

Before the principal independently reviewed July 26 certification cycle, a
ServiceNow-governed functional E2E cycle completed on 2026-07-13. It exercised
an R82 Take 91 software-patch install, the Deployment Agent activity, and an
R81.20-to-R82 CDT major upgrade through catalog, readiness, change, task, and
closure handling. The July 26 rows are the principal certification matrix
because their exact baseline states and retained evidence received the later
independent certification review; the earlier cycle remains relevant functional
coverage rather than an omitted claim.

## Deployment Agent Functional Coverage

The 2026-07-13 ServiceNow-driven Deployment Agent activity used the dedicated
short workflow. Both gateways already resolved to build 2771, so this proves
idempotent package/readiness handling and dual-member execution behavior; it is
not evidence of an upgrade from every older Agent build.

## Explicitly Not Certified

The following must not be inferred from the successful rows:

- R81.20 Take 76 directly to R82 Take 60. The certified major upgrade began
  after Take 76 removal, from R81.20 Take 0.
- R82 to R82.10, R82.10 to a later release, or any release not listed above.
- Standalone gateways. The major and API workflows certified here require a
  two-member cluster and reject unsupported shapes.
- Clusters with more than two members.
- Management API selection from the ServiceNow catalog. ServiceNow continues to
  use CDT until backend selection receives its own governed certification.
- API-only rolling uninstall with the tested API version.
- Fresh Deployment Agent installation from every historical build.
- Every historical or Latest JHF. Package discovery does not imply installation
  approval or compatibility.

## Certification Required for a New Combination

Before adding another release, Take, topology, or backend to this matrix:

1. Verify vendor critical information, supported upgrade path, package identity,
   checksums, Deployment Agent policy, licensing, and rollback capacity.
2. Run complete target resolution and readiness against the intended topology.
3. Exercise real member changes, reboots, failover, tester and remediation gates,
   policy handling, resume behavior, final health checks, and ownership restore.
4. Retain a sanitized evidence index and integrity manifest outside the public
   repository; never publish credentials, session identifiers, private topology,
   package binaries, or raw operational evidence.
5. Obtain independent review of the implementation and evidence claims.
6. Add the exact starting state, ending state, backend, governance mode, limits,
   date, and review status to this document.

Raw certification evidence is intentionally excluded from this public
repository. The matrix records bounded outcomes without distributing private
infrastructure data or vendor package binaries.
