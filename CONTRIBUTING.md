# Contributing

1. Do not include customer names, private addresses, credentials, package
   binaries, ServiceNow instance identifiers, or live evidence.
2. Open a focused branch and include tests proportional to the change.
3. Run Python tests, Ansible syntax checks, and the public-content scanner.
4. Document behavioral and security changes.
5. Test targeting, governance, resume, credential, and failover logic with
   failure cases as well as successful cases.
6. Keep filenames, branch names, and commit messages scoped to the change, and
   do not commit local absolute paths.

## Run the checks

Install the project and lint dependencies in a virtual environment, then run:

```bash
python3 -m unittest discover -s ansible/scripts/tests -v
python3 -m unittest discover -s tools/tests -v
python3 -m unittest discover -s tests -p 'test_examples.py' -v
ruff check . --select E9,F --no-cache
ansible-lint ansible/playbooks examples/*/playbooks
python3 -m py_compile checkpoint_cluster_upgrade.py servicenow_checkpoint_*.py ansible/scripts/*.py tools/*.py
for playbook in ansible/playbooks/*.yml $(find examples -path '*/playbooks/*.yml' -print); do
  ansible-playbook --syntax-check "$playbook" -i ansible/inventory/hosts.yml
done
python3 tools/scan_public_repository.py .
```

GitHub Actions runs the `test` and `secrets` checks for every pull request.

Live-system tests require explicit authorization and must not run in public CI.
Submit sanitized summaries rather than raw operational evidence.
