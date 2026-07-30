# Update the Deployment Agent

This example shows the Deployment Agent branch of the workflow. It validates the plan, checks the current agent, validates the package on the MDS, checks prerequisites, and reaches the guarded direct CPUSE step. A final readiness check follows a real update.

The sample package and checksum are placeholders, and direct execution remains disabled.

```bash
ansible-playbook examples/deployment_agent/playbooks/validate.yml \
  -e example_plan_file="$PWD/examples/deployment_agent/activity-plan.json"

ansible-playbook examples/deployment_agent/playbooks/workflow.yml \
  -e @examples/common/vars.yml \
  -e example_plan_file="$PWD/examples/deployment_agent/activity-plan.json"
```

Review the package source, published SHA-256, target members, Deployment Agent version, and change approval before enabling any mutating step.
