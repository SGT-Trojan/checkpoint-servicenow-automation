# Major Upgrade Structure

This is a plan-and-validate example. It demonstrates the shape of a two-member major-upgrade request. It does not run an upgrade, and it is not a certified combination.

The full governed sequence is:

1. Validate the plan, run prechecks, check the Deployment Agent, and capture cluster state and a baseline.
2. Validate and stage the upgrade image.
3. Upgrade the standby member.
4. Run the mixed-version policy gate.
5. Enable MVC.
6. Fail over to the upgraded member.
7. Continue only after the tester task is Closed Complete.
8. Upgrade the second member.
9. Install final policy, disable MVC, restore ownership when requested, and run final validation.

Only the validation wrapper is provided:

```bash
ansible-playbook examples/major_upgrade/playbooks/validate.yml \
  -e example_plan_file="$PWD/examples/major_upgrade/activity-plan.json"
```

The image name, SHA-256, versions, policy package, target objects, and compatibility choices are placeholders. Use the complete runner for an actual governed workflow. No Gaia run-script proof of concept is included.
