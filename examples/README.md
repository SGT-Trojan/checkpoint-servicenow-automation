# Examples

These examples show how to use the scripts and playbooks without copying the full ServiceNow workflow. Start with `read_only/discover_and_check` or `expected_failures`.

## Before You Run Anything

- Replace every TEST-NET address, object name, package name, path, and checksum.
- Use a non-production environment first.
- Read [Start Here](../docs/START_HERE.md) for the flow and common terms.
- Use the [component reference](../docs/COMPONENT_REFERENCE.md) for accepted options, variables, keys, outputs, and return codes.
- Use the [tested scenarios](../docs/CERTIFIED_SCENARIOS.md) to see what was tested. An example does not prove support for your environment.

Connected examples read `CP_PASSWORD`, `CP_EXPERT_PASSWORD`, and, when needed, `CP_SSH_PROXY_PASSWORD`. Load them from a vault or protected shell environment. Do not create a credential file under `examples/`. See [`servicenow_checkpoint_worker.env.example`](../servicenow_checkpoint_worker.env.example).

## Safety Contract

Read-only examples may write local reports but do not install packages, reboot gateways, or fail over the cluster. Install and upgrade examples are disabled twice:

1. Their package identity or checksum is a placeholder, so validation fails.
2. Their execution variable is `false`, so the playbook refuses to authorize the helper's execution switch.

The removal example has no package file to hash. It still keeps execution disabled and requires exact installed-package resolution before CDT candidate generation can pass.

Before a change, complete discovery, readiness, package/hash validation, cluster-state capture, and candidate identity checks. After the first member and failover, continue only when the tester task is Closed Complete. Finish with second-member checks, postcheck, and ownership restoration when requested.

## Scenario Index

| Goal | Example | Default behavior |
|---|---|---|
| Follow completed ServiceNow-governed scenarios | [`governed`](governed/README.md) | Sanitized ticket walkthroughs; package fixtures fail closed |
| Run the complete workflow without ServiceNow | [`runner_cli`](runner_cli/README.md) | Placeholder CSV; execution blocked until copied and replaced |
| Run a journaled workflow without ServiceNow or Ansible | [`standalone_python`](standalone_python/README.md) | Placeholder plans; validation fails until protected copies are completed |
| Discover and check a cluster | [`read_only/discover_and_check`](read_only/discover_and_check/README.md) | Connected, read-only |
| Observe fail-closed behavior | [`expected_failures`](expected_failures/README.md) | Offline, CI-safe |
| Understand a CDT JHF install | [`jhf_install_cdt`](jhf_install_cdt/README.md) | Placeholder plan; execution disabled |
| Understand a CDT JHF removal | [`jhf_remove_cdt`](jhf_remove_cdt/README.md) | Alias resolution; execution disabled |
| Understand an API JHF install | [`jhf_install_api`](jhf_install_api/README.md) | Placeholder plan; execution disabled |
| Update the Deployment Agent | [`deployment_agent`](deployment_agent/README.md) | Placeholder plan; execution disabled |
| Inspect major-upgrade structure | [`major_upgrade`](major_upgrade/README.md) | Plan and validation only |
| Compose helpers in your own workflow | [`direct_helpers`](direct_helpers/README.md) | Guidance; no execution |
| Import one component | [`custom_playbook`](custom_playbook/README.md) | Connected, read-only |

The runner walkthrough is ServiceNow-free but still uses Ansible. The standalone
Python walkthrough composes the helpers without `ansible-playbook`. The governed
record lifecycle remains in the [build guide](../docs/SERVICENOW_BUILD_GUIDE.md)
and [ticket example](../docs/SERVICENOW_TICKET_EXAMPLE.md).
