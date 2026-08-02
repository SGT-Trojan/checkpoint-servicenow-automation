# Remove a JHF with CDT

This example shows the two-member CDT removal sequence. It does not run by default. The guarded CDT playbook receives `checkpoint_execute_upgrade: false`.

A removal request often names a Take, while CDT needs the exact installed package
filename. During candidate generation, the helper reads CPInstLog history from
both gateways through CPRID. It matches `source_path`, `package_name`,
`display_name`, `name`, and the step name. The `requires_present` and
`requires_absent` fields are prerequisite checks; they are never removal
identities. The helper stops if the result is missing or ambiguous. A full installed filename is still only a search input; CPInstLog must confirm it uniquely.

1. Replace the TEST-NET values and the `REPLACE_...` Take alias in `activity-plan.json`.
2. Load `CP_PASSWORD` and `CP_EXPERT_PASSWORD` from a protected environment.
3. Validate the plan and run the first-member wrapper.
4. Check traffic on member 1 after failover.
5. Continue only after the tester task is Closed Complete.
6. Run the second-member wrapper and review postcheck evidence.

```bash
ansible-playbook examples/jhf_remove_cdt/playbooks/validate.yml \
  -e example_plan_file="$PWD/examples/jhf_remove_cdt/activity-plan.json"

ansible-playbook examples/jhf_remove_cdt/playbooks/first_member.yml \
  -e @examples/common/vars.yml \
  -e example_plan_file="$PWD/examples/jhf_remove_cdt/activity-plan.json"
```

The second command stops at the explicit execution gate. Do not change that gate until the plan, captured cluster state, CPInstLog resolution, candidate CSV, and change approval all agree.

After an approved first-member change and a Closed Complete tester task, the second wrapper is:

```bash
ansible-playbook examples/jhf_remove_cdt/playbooks/second_member.yml \
  -e @examples/common/vars.yml \
  -e example_plan_file="$PWD/examples/jhf_remove_cdt/activity-plan.json"
```
