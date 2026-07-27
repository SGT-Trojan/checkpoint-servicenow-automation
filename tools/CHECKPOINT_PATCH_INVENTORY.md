# Check Point MDS Patch Inventory

`checkpoint_patch_inventory.py` is a read-only utility intended to run locally in Expert/root mode on a Check Point Multi-Domain Server.

It discovers all domains and selects only servers whose domain server type is `management server`, excluding CLM/logging-only servers. For every domain it pages through `show gateways-and-servers`, selects physical gateway targets and cluster members, and uses CMA-scoped CPRID to collect:

- `show version all`
- `show installer status all`
- `show installer packages installed`
- `show installer packages`

The full package inventory enriches the installed-package output, distinguishing `Installed` from `Installed as part of`. Imported or merely available packages are not reported as installed.

## Install and run on MDS

```bash
chmod 0755 checkpoint_patch_inventory.py
./checkpoint_patch_inventory.py
```

The default output is a timestamped directory under `/var/log/tmp`. To control it:

```bash
./checkpoint_patch_inventory.py \
  --output-dir /var/log/tmp/patch_inventory_20260715 \
  --workers 4 \
  --timeout 240
```

Filters and diagnostics:

```bash
./checkpoint_patch_inventory.py --domain CMA-A
./checkpoint_patch_inventory.py --gateway CP-FW-A --gateway CP-FW-B
./checkpoint_patch_inventory.py --discovery-only
./checkpoint_patch_inventory.py --verbose
```

Outputs:

- `checkpoint_patch_inventory.csv`: one row per gateway and installed package.
- `checkpoint_gateway_summary.csv`: one row per gateway with inferred current JHF take and DA build.
- `discovery.json`: discovered CMAs and gateway targets.
- `errors.csv`: partial discovery/collection failures.
- `raw/*.txt`: exact Clish evidence returned through CPRID.
- `run.log`: execution log.

Exit codes:

- `0`: all discovered gateways collected successfully.
- `1`: report created with one or more partial failures.
- `2`: no usable CMA/domain discovery.

The script does not modify gateway configuration, import or install packages, change policy, or invoke direct SSH.
