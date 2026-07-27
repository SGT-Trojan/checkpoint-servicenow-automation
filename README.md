# Check Point and ServiceNow Firewall Automation

Production-oriented reference implementation for automating Check Point firewall
software maintenance with Ansible. It supports direct command-line operation and
optional ServiceNow governance.

## Capabilities

- MDS/CMA, cluster, member, and policy discovery with fail-closed ambiguity handling.
- Readiness validation for SIC, ClusterXL, interfaces, PNOTEs, ICAP, package
  checksums, disk/rollback capacity, CPUSE state, and Deployment Agent health.
- Rolling JHF and wrapper installation/removal with tester and remediation gates.
- Major-version upgrades with mixed-version controls, policy gates, and MVC handling.
- Deployment Agent currency checks and installation.
- Separate CDT and Management Web API deployment backends.
- ServiceNow REQ/RITM/SCTASK/CHG/CTASK orchestration and resumable workers.
- Public JHF discovery, Recommended-versus-Latest policy, secure download, and
  checksum verification.

## Documentation

- [Architecture and engineering guide](docs/ARCHITECTURE_AND_ENGINEERING_GUIDE.md)
- [ServiceNow build guide](docs/SERVICENOW_BUILD_GUIDE.md)
- [Workflow walkthrough](docs/WORKFLOW_WALKTHROUGH.md)
- [Security policy](SECURITY.md)
- [Contribution guide](CONTRIBUTING.md)

## Safety

This repository performs potentially disruptive firewall operations. Start with
offline tests and read-only discovery in a non-production environment. Review
every activity plan, use proper change control, maintain tested rollback
protection, and never bypass cluster, policy, tester, or remediation gates.

No Check Point packages, licenses, credentials, snapshots, ServiceNow exports,
or live-system evidence are distributed here. Product names and trademarks
belong to their respective owners.

## Quick start

The command-line workflow can be used without ServiceNow. ServiceNow integration
is optional and requires the additional platform configuration in the build guide.

```bash
git clone https://github.com/SGT-Trojan/checkpoint-servicenow-automation.git
cd checkpoint-servicenow-automation
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
cp servicenow_checkpoint_worker.env.example .env.local
```

Edit the example inventory and runtime environment for your estate. Keep the
resulting credentials file outside Git and mode `0600`. Start with CLI help and
read-only validation:

```bash
python3 servicenow_checkpoint_runner.py --help
python3 checkpoint_cluster_upgrade.py --help
```

## Quick verification

```bash
python3 -m unittest discover -s ansible/scripts/tests -v
python3 -m unittest discover -s tools/tests -v
for playbook in ansible/playbooks/*.yml; do
  ansible-playbook --syntax-check "$playbook" -i ansible/inventory/hosts.yml
done
python3 tools/scan_public_repository.py .
```

See the build guide before connecting this software to ServiceNow, an MDS, CMA,
or gateway.

## License

Original source code is licensed under the Apache License 2.0. See [LICENSE](LICENSE)
and [NOTICE](NOTICE). Third-party components and vendor products retain their own
licenses.
