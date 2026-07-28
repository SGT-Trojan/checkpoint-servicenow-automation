# Contributing

1. Do not include customer names, private addresses, credentials, package
   binaries, ServiceNow instance identifiers, or live evidence.
2. Open a focused branch and include tests proportional to the change.
3. Run Python tests, Ansible syntax checks, and the public-content scanner.
4. Document behavioral and security changes.
5. Use adversarial review for targeting, governance, resume, credential, and
   failover logic.
6. Keep public filenames, branch references, commit messages, and review text
   neutral. Do not include non-public attribution or coordination-protocol fields.

Live-system tests require explicit authorization and must not run in public CI.
Submit sanitized summaries rather than raw operational evidence.
