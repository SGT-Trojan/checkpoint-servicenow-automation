# Use One Shipped Component in Your Playbook

This example imports the read-only MDS package-validation playbook. It loads the JSON plan once and keeps the shipped credential and failure assertions.

```bash
PLAN="$PWD/examples/jhf_install_cdt/activity-plan.json"
ansible-playbook -i ansible/inventory/hosts.yml \
  examples/custom_playbook/playbooks/validate_mds_package.yml \
  -e @examples/common/vars.yml -e "example_plan_file=$PLAN"
```

The command contacts the MDS but does not install a package. The placeholder plan fails locally until its identity and checksum are replaced.

For a mutating helper, copy the shipped playbook's assertions and its prerequisite chain. Keep both controls: the playbook must assert an explicit execution boolean, and only then may it pass the helper's execution switch. Never replace them with one unchecked variable.
