# Install a JHF with CDT

This is a structure demonstration, not a certified combination. It shows the phase order for a two-member cluster. It cannot execute as shipped: the checksum is a placeholder and `allow_cdt_changes` is `false`.

Set the plan path once, then validate it:

```bash
PLAN="$PWD/examples/jhf_install_cdt/activity-plan.json"
ansible-playbook -i ansible/inventory/hosts.yml \
  examples/jhf_install_cdt/playbooks/validate.yml \
  -e @examples/common/vars.yml -e "example_plan_file=$PLAN"
```

Validation fails until you replace the package name, Take, source path, and published SHA-256. After those values pass, the first-member wrapper follows the real prechecks and stops at its execution gate:

```bash
ansible-playbook -i ansible/inventory/hosts.yml \
  examples/jhf_install_cdt/playbooks/first_member.yml \
  -e @examples/common/vars.yml -e "example_plan_file=$PLAN"
```

After an approved first-member change and failover, validate traffic. Continue only when the tester task is Closed Complete:

```bash
ansible-playbook -i ansible/inventory/hosts.yml \
  examples/jhf_install_cdt/playbooks/second_member.yml \
  -e @examples/common/vars.yml -e "example_plan_file=$PLAN"
```

The second wrapper keeps execution disabled too. These wrappers show composition; they do not replace approval, tester identity, or the runner's saved resume state. See the [component reference](../../docs/COMPONENT_REFERENCE.md) for variables and return codes.
