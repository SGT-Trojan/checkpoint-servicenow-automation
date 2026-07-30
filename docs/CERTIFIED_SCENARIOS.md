# What We Tested

Last updated: 2026-07-27

## How to Read This Page

This page lists the maintenance runs completed on one lab Management Server,
CMA, and two-member cluster. Use it to understand the tested starting point,
result, and safety checks. It does not replace Check Point compatibility
guidance, and it does not certify your environment.

- **ServiceNow and CDT** means the test started with a catalog request and used
  real readiness, approval, change, tester, recovery, validation, and closure
  steps.
- **Command line** means the runner changed the lab cluster without starting
  from a ServiceNow request. The table says when a lab override or simulated
  tester gate was used.
- **Independently reviewed** means a second reviewer checked the saved results
  and the related code without connecting to the lab.
- **No separately installed JHF** means Take 76 had been removed. It does not
  identify a product Take numbered 0 or mean that the base Gaia image has no
  fixes.

## Test Environment

| Component | Validated value |
|---|---|
| Cluster shape | Two Security Gateway members with controlled active/standby failover |
| Management | MDS with a CMA, Access Control and Threat Prevention policy gates |
| R81.20 baseline | Gaia R81.20 build 634, no separately installed JHF after Take 76 removal |
| R82 target image | Gaia R82 build 777 with JHF Take 60 embedded in the Blink image |
| Deployment Agent | Build 2771, enabled, connected, current, and licensed during the final cycles |
| Health gates | SIC, ClusterXL, PNOTEs, required and virtual interfaces, policy, CPUSE, capacity, licensing, and ICAP where stated |

## ServiceNow and CDT Tests

These tests ran through ServiceNow on 2026-07-26. They used the real readiness,
approval, change, tester, recovery, and closure steps. No lab override or simulated
tester gate was used.

| Activity | Starting state | Ending state | Live controls exercised | Result |
|---|---|---|---|---|
| JHF install | R81.20 build 634, no separately installed JHF | R81.20 build 634, Take 76 on both members | Automated readiness, approvals, guarded one-member CDT execution, real reboots, tester gate, final validation, closure | Passed; independently reviewed |
| JHF uninstall | R81.20 build 634, Take 76 | R81.20 build 634, no separately installed JHF on both members | `Take 76` alias resolution through CPRID/CPInstLog, guarded rolling removal, real reboots, tester gate, final validation, closure | Passed; independently reviewed |
| Major upgrade | R81.20 build 634, no separately installed JHF | R82 build 777, embedded Take 60 on both members | Mixed-version policy gate, MVC on/off, rolling upgrade, controlled failover, real tester gate, SSH-host-key remediation tasks, phase-specific resume, final policy and closure | Passed; independently reviewed |

The ServiceNow major-upgrade test checked the version, Take, policy, ClusterXL,
PNOTEs, interfaces, recovery path, and closure. Its final report marked ICAP as
skipped because of a bug that was fixed later. ICAP passed a separate check, but
we do not claim that the original final report tested it.

## Command-Line CDT Tests

These tests ran from the command line on 2026-07-26. They restored the same
R81.20 baseline and used a lab override and simulated tester gates.

| Activity | Starting state | Ending state | Result |
|---|---|---|---|
| Take 76 install | R81.20 build 634, no separately installed JHF | R81.20 Take 76 on both members | Passed; real rolling CDT execution and reboots; independently reviewed |
| Take 76 uninstall | R81.20 Take 76 | R81.20 build 634, no separately installed JHF on both members | Passed; authoritative CPInstLog identity resolution and rolling CDT removal; independently reviewed |
| Major upgrade | R81.20 build 634, no separately installed JHF | R82 build 777, Take 60 | Passed; MVC, policy, failover, support diff, ownership restoration, ICAP and postcheck; independently reviewed |

A later command-line test on 2026-07-27 found R82 Take 107 as Recommended. It
downloaded and checked the 2.49 GB package, copied it to the MDS, and installed
it on one standby member at a time:

| Activity | Starting state | Ending state | Result |
|---|---|---|---|
| Recommended JHF install | R82 build 777, Take 60 | R82 build 777, Take 107 on both members | Passed with real reboots, controlled failover, policy and strict health checks; independently reviewed |

## Command-Line Management API Tests

The Management API backend was tested from the command line on 2026-07-27.
ServiceNow did not select this backend, and the API runs did not call CDT. These
tests used a lab override and simulated tester gate. See [CDT and Management API
deployment](CDT_AND_MANAGEMENT_API.md) for the commands and the differences
between the two backends.

| Activity | Starting state | Ending state | Execution path | Result |
|---|---|---|---|---|
| Take 76 install | R81.20 build 634, no separately installed JHF | R81.20 Take 76 | API repository import, verification, and cluster execution | Passed; independently reviewed |
| Take 76 uninstall | R81.20 Take 76 | R81.20 build 634, no separately installed JHF | API inventory and CPRID/CPInstLog identity resolution, then guarded direct CPUSE member removal | Passed; independently reviewed; explicitly not an API-only uninstall claim |
| Major upgrade | R81.20 build 634, no separately installed JHF | R82 build 777, Take 60 | API package execution plus mixed-version policy, MVC, explicit failover, final policy, restoration, and postcheck gates | Passed; independently reviewed |

The tested API could not safely remove the package one member at a time. The
workflow therefore uses its clearly marked direct CPUSE fallback. That fallback
does not call CDT.

## Earlier ServiceNow Tests

An earlier ServiceNow test ran on 2026-07-13. It covered an R82 Take 91 patch
install, the Deployment Agent activity, and an R81.20-to-R82 CDT upgrade. The
July 26 tests are the main results because their starting states and saved
evidence received the later independent review.

## Deployment Agent Test

The 2026-07-13 ServiceNow test used the short Deployment Agent workflow. Both
gateways already had build 2771, so the workflow correctly made no change. This
does not prove upgrades from older Agent builds.

## What We Have Not Tested

Do not assume that the successful rows also cover these cases:

- R81.20 Take 76 directly to R82 Take 60. The certified major upgrade began
  after Take 76 removal, from R81.20 build 634 with no separately installed JHF.
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

## Before Adding a New Combination

Before adding another release, Take, cluster type, or backend to this page:

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

Raw test evidence is not public because it contains environment details. This
page records the result and its limits without publishing private data or vendor
packages.
