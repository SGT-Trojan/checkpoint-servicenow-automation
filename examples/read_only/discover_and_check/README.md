# Discover and Check a Cluster Safely

This example finds one managed cluster, checks both members, and records three health samples. It writes reports under `ansible/reports/` but does not install, reboot, or fail over anything.

1. Copy `examples/common/vars.yml` outside the repository and replace its values.
2. Load `CP_PASSWORD` and `CP_EXPERT_PASSWORD` from the approved secrets source.
3. Run:

```bash
ansible-playbook -i ansible/inventory/hosts.yml \
  examples/read_only/discover_and_check/playbooks/run.yml \
  -e @/secure/path/checkpoint-example-vars.yml
```

The wrapper imports discovery, precheck, and monitoring in that order. Compare the result with the synthetic files under `expected/`. Discovery returns 2 for no target, 3 for ambiguity, 4 for an incomplete scan, 5 for invalid input, and 64 for malformed usage. Any nonzero result stops the example.
