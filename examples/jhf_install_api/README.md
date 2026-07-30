# Install a JHF with the Management API

This example follows the repository's Management API backend. It imports the package into the management repository, verifies it against the full cluster object, and uses the guarded API execution playbook. It does not call CDT.

The placeholder checksum makes plan validation fail. The execution variable is also `false`. Replace both only after reading [CDT and Management API deployment](../../docs/CDT_AND_MANAGEMENT_API.md).

```bash
ansible-playbook examples/jhf_install_api/playbooks/validate.yml \
  -e example_plan_file="$PWD/examples/jhf_install_api/activity-plan.json"

ansible-playbook examples/jhf_install_api/playbooks/first_member.yml \
  -e @examples/common/vars.yml \
  -e example_plan_file="$PWD/examples/jhf_install_api/activity-plan.json"
```

The first-member API operation uses the cluster strategy selected by the helper. Stop for tester approval before the second-member wrapper. This backend does not provide a safe rolling API uninstall path in the tested workflow. API removal falls back to the separately guarded direct helper, so this repository does not ship a `jhf_remove_api` example.

After an approved first-member change and a Closed Complete tester task, the second wrapper is:

```bash
ansible-playbook examples/jhf_install_api/playbooks/second_member.yml \
  -e @examples/common/vars.yml \
  -e example_plan_file="$PWD/examples/jhf_install_api/activity-plan.json"
```
