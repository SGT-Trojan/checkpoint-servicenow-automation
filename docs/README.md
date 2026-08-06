# Documentation

Choose the section that best matches what you need to do. Start with
[Start here](START_HERE.md) if the project or its terminology is new to you.

## New users

- [Start here](START_HERE.md) explains the purpose, terms, safety model, and
  basic operating paths.
- [Workflow walkthrough](WORKFLOW_WALKTHROUGH.md) follows a request from intake
  through completion.
- [Tested scenarios](CERTIFIED_SCENARIOS.md) records the versions, Takes,
  execution paths, and limits exercised in the lab.

## Firewall operators

- [Practical examples](../examples/README.md) provides safe component and
  composed-workflow examples.
- [Runner CLI walkthrough](../examples/runner_cli/README.md) covers the complete
  workflow without a ServiceNow connection.
- [Standalone Python workflow](STANDALONE_PYTHON_WORKFLOW.md) covers operation
  without ServiceNow or Ansible.
- [Architecture and engineering guide](ARCHITECTURE_AND_ENGINEERING_GUIDE.md)
  explains execution order, state, recovery, and system boundaries.
- [CDT and Management API deployment](CDT_AND_MANAGEMENT_API.md) explains how
  the deployment backends differ.

## ServiceNow administrators

- [ServiceNow build guide](SERVICENOW_BUILD_GUIDE.md) contains the complete
  platform implementation.
- [Governed ticket example](SERVICENOW_TICKET_EXAMPLE.md) provides sample
  request data, attachments, record states, and phase mapping.
- [Workflow walkthrough](WORKFLOW_WALKTHROUGH.md) shows how request, change,
  tester, and remediation records move through the workflow.

## Tools and package information

- [Patch inventory guide](../tools/CHECKPOINT_PATCH_INVENTORY.md) covers managed
  gateway patch inventory.
- [JHF currency and download guide](../tools/JHF_CURRENCY_AND_DOWNLOAD.md)
  covers Recommended Take discovery and verified downloads.
- [Deployment Agent currency guide](../tools/DEPLOYMENT_AGENT_CURRENCY.md)
  covers Deployment Agent assessment and package handling.

## Developers and contributors

- [Component and integration reference](COMPONENT_REFERENCE.md) documents
  scripts, playbooks, inputs, outputs, and integration contracts.
- [Architecture and engineering guide](ARCHITECTURE_AND_ENGINEERING_GUIDE.md)
  explains module ownership and cross-component behavior.
- [Contributing](../CONTRIBUTING.md) lists validation commands and contribution
  rules.
- [Security](../SECURITY.md) describes reporting, credential handling, and safe
  deployment expectations.
