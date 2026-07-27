# Security Policy

## Reporting

Do not open a public issue containing credentials, tokens, private topology,
firewall configuration, ServiceNow records, package binaries, or live evidence.
Use GitHub's private vulnerability reporting feature when available.

## Operational security

- Retrieve credentials from an approved vault and inject them at runtime.
- Never place passwords, API keys, TOTP seeds, SSH private keys, or session IDs
  in Git, activity CSV files, command arguments, or evidence.
- Use a dedicated least-privilege ServiceNow integration account.
- Separate MDS and gateway credentials where the environment requires it.
- Verify SSH host-key changes through an authoritative channel.
- Keep live-system access outside GitHub-hosted runners.
- Treat all install, remove, upgrade, failover, policy, and snapshot operations
  as governed production changes.

## Supported versions

This project is a reference implementation, not a vendor support commitment.
Validate the exact Check Point release, Deployment Agent, CDT/API behavior,
ServiceNow family release, and package critical information in your environment.
