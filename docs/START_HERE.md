# Start Here

This page explains the project without assuming that you know ServiceNow,
Ansible, or Check Point deployment tools. Read it before the detailed guides.

## What this project is for

The project runs software maintenance on Check Point gateways. It can:

- install or remove a Jumbo Hotfix Accumulator (JHF);
- run a major version upgrade;
- update the Check Point Deployment Agent; and
- record the work in ServiceNow when ServiceNow is used.

For a two-member cluster, the normal JHF flow changes the standby member first.
It checks that member, moves traffic to it, and waits for a person to test it.
Only then can the workflow change the second member.

## Before you use it

This code can change firewalls. It needs setup for your environment. You must
supply your own inventory, credentials, approved packages, checksums, and
system details. Start in a non-production environment.

Use the [tested scenarios](CERTIFIED_SCENARIOS.md) to see what has been run in a
lab. A tested version does not guarantee that a different version or topology
will work.

## Common terms

| Term | Plain meaning |
|---|---|
| JHF | Jumbo Hotfix Accumulator, a Check Point software update bundle |
| Take | The numbered release of a JHF, such as Take 107 |
| CPUSE | The Check Point installer that manages software packages on Gaia |
| MDS | Multi-Domain Server, the Check Point management system used by this project |
| CMA | The management server for one domain on an MDS |
| CDT | Central Deployment Tool, one way to deploy packages from Check Point management |
| Management API | A separate Check Point interface that can also deploy packages |
| Cluster member | One gateway in a high-availability cluster |
| Active member | The member currently handling traffic |
| Standby member | The member waiting to take traffic; this member is changed first |
| Readiness check | A check that confirms the target, package, health, space, and other prerequisites |
| Tester gate | A required pause where a person checks traffic and services before member 2 |
| Recovery task | A task that records a failure and waits for an engineer to fix it |
| Fail closed | Stop when required information is missing, unclear, or unsafe |
| Activity plan | A JSON file that tells the runner what to change and what checks to use |

## Choose how much to use

You do not have to adopt the whole project.

| Your goal | Where to start |
|---|---|
| List or download a JHF | [JHF currency and download](../tools/JHF_CURRENCY_AND_DOWNLOAD.md) |
| Read installed patch data | [Patch inventory](../tools/CHECKPOINT_PATCH_INVENTORY.md) |
| Call one Python helper | [Component reference](COMPONENT_REFERENCE.md) |
| Use the Ansible playbooks with your own variables | [Component reference](COMPONENT_REFERENCE.md) |
| Follow a complete component example | [Practical examples](../examples/README.md) |
| Run the full command-line workflow | [Runner CLI walkthrough](../examples/runner_cli/README.md) |
| Connect the workflow to ServiceNow | [ServiceNow build guide](SERVICENOW_BUILD_GUIDE.md) |
| Start with a sanitized ServiceNow ticket | [Governed ticket example](SERVICENOW_TICKET_EXAMPLE.md) |
| Follow one request from start to finish | [Workflow walkthrough](WORKFLOW_WALKTHROUGH.md) |

## Safe first steps

1. Clone the repository and create a Python virtual environment.
2. Run the offline tests listed in the README.
3. Read the example inventory and activity plans.
4. Replace every sample address, object name, path, package, and checksum.
5. Put passwords in protected environment variables or a secrets vault.
6. Run only read-only discovery and readiness checks.
7. Confirm that the discovered cluster, members, policy, version, and package are correct.
8. Review the exact command and its execution switch before allowing a change.

Printing a plan does not make a change. Helpers that can change a system require
an execution flag, and some playbooks also require an execution variable. Keep
those checks in your own playbooks.

## What happens during a rolling JHF change

The full workflow follows this order:

1. Find one exact cluster and its members.
2. Check cluster health and package requirements.
3. Save which member is active.
4. Install or remove the package on the standby member.
5. Wait for the member to reboot and become healthy.
6. Move traffic to the changed member.
7. Pause until the tester task is Closed Complete.
8. Change the other member.
9. Run final health and package checks.
10. Restore the original active member when requested.

If a phase fails, the workflow stops. It saves the failed phase and does not
retry the change by itself. An engineer must fix the cause and approve the
restart.

## What to read next

Use the [workflow walkthrough](WORKFLOW_WALKTHROUGH.md) for a step-by-step
example. Use the [architecture guide](ARCHITECTURE_AND_ENGINEERING_GUIDE.md)
when you need details about workers, the runner, playbooks, logs, and recovery.
Use the [component reference](COMPONENT_REFERENCE.md) when you only need a
script, playbook, command option, or input key.
