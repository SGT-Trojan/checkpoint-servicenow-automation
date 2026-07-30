# Use Helpers Directly

The scripts in `ansible/scripts/` can be called without the full runner. Use this when you are building your own playbook and need one narrow operation.

Start by reading each command's help:

```bash
python3 ansible/scripts/discover_checkpoint_targets.py --help
python3 ansible/scripts/validate_activity_plan.py --help
python3 ansible/scripts/validate_package_prerequisites.py --help
python3 ansible/scripts/generate_cdt_candidates_from_activity.py --help
python3 ansible/scripts/management_api_package_from_activity.py --help
```

A safe composition order is:

1. Validate the activity plan.
2. Resolve the MDS, CMA, cluster, and both members to one authoritative object.
3. Capture the original active and standby members.
4. Validate package identity, SHA-256, and prerequisites.
5. Generate and inspect candidates or verify the API repository identity.
6. Call a guarded execution helper only after your own approval gate.
7. Monitor readiness, validate traffic, process the second member, run postchecks, and restore ownership.

The execution helpers require both a valid plan and an explicit execution switch. Without it, they return a planned-but-not-executed result. The offline checks in [`expected_failures`](../expected_failures/README.md) show this behavior without connecting to a gateway.

For accepted flags and return codes, use the [component reference](../../docs/COMPONENT_REFERENCE.md). For complete lifecycle orchestration, use the runner instead of rebuilding its resume and governance logic one command at a time.
