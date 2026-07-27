# Check Point JHF Currency and Download

`cpuse_jhf_fetch.py` discovers Check Point Jumbo Hotfix Accumulator releases and securely downloads CPUSE offline packages. It does not stage or install packages.

## Selection policy

The official catalog can publish two different candidates:

- `recommended` is the default and is suitable for unattended currency checks. A package becoming Recommended indicates that Check Point has promoted that Take for broad use.
- `latest` must be selected explicitly. A Latest Take can be newer than Recommended but has not yet received the Recommended designation.

Do not automatically install Latest in production. Discovery and download can be scheduled; installation must still pass change governance, critical-information review, compatibility/readiness checks, checksum validation, rollback preparation, and cluster gates.

## Requirements

- Python 3.9 or later
- HTTPS access to `sc1.checkpoint.com`, `support.checkpoint.com`, `iapi-services-ucs.checkpoint.com`, and `dl3.checkpoint.com`
- Sufficient local storage for the package and any `.part` file

Public JHF records need no UserCenter password. The tool accepts no credentials.

## Commands

Discover the current R82 Recommended and Latest Takes:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82
```

Compare the Recommended Take with a gateway currently on Take 107:

```bash
python3 tools/cpuse_jhf_fetch.py \
  --version R82 \
  --policy recommended \
  --installed-take 107 \
  --output /secure/evidence/r82-jhf-status.json
```

Download and verify the Recommended package:

```bash
python3 tools/cpuse_jhf_fetch.py \
  --version R82 \
  --policy recommended \
  --download \
  --dest /srv/checkpoint/packages
```

Explicitly inspect or download Latest:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --policy latest --installed-take 107
python3 tools/cpuse_jhf_fetch.py --version R82 --policy latest --download --dest /srv/checkpoint/packages
```

A completed file is reused only when both its SHA1 and SHA256 match official metadata. An interrupted transfer remains as `<filename>.part`; the next run requests the remaining range. A server that does not honor the range causes a clean full rewrite of the partial file. The final artifact is mode `0600` and the destination directory is mode `0700`.

## Data and decisions

The tool performs these steps:

1. Fetch the official release-specific JHF downloads page.
2. Parse `Take N - Recommended` and `Take N - Latest` as distinct records.
3. Resolve the CPUSE TAR download ID from each record.
4. Read structured `__NEXT_DATA__` JSON from the Support Center detail page.
5. Require exact agreement among requested release, Take, canonical filename, SHA1, and SHA256.
6. Report `update_available=true` only when the selected Take is numerically higher than `--installed-take`.
7. On `--download`, obtain a short-lived URL from Check Point's public endpoint, require the host to be `dl3.checkpoint.com`, and stream to a partial file.
8. Verify both published hashes before atomically promoting the partial file to the canonical filename.

Discovery fails closed when Recommended is absent, a requested policy is unavailable, metadata is malformed or inconsistent, the signed URL is untrusted, or network access fails. Download fails closed on any checksum mismatch.

## Automation pattern

A production scheduler can run discovery daily and retain its JSON result. Recommended operation is:

1. Inventory each managed gateway's Gaia release and installed JHF Take.
2. Run this tool once per distinct release with `--policy recommended`.
3. Compare the selected Take to each gateway's current Take.
4. Download once to a controlled repository when at least one gateway is behind.
5. Scan/review the release's Critical Information and release notes.
6. Promote the verified package into the MDS staging repository only after approval.
7. Run the existing readiness workflow: identity/topology, package checksum, Deployment Agent, disk/rollback capacity, SIC, policy, ClusterXL, interfaces, PNOTEs, ICAP, and package prerequisites.
8. Execute the normal rolling workflow with independent member validation, tester gate, controlled failover, final postcheck, and evidence capture.

The scheduler must not infer that a higher Take is approved merely because it is Latest. It must also avoid downgrades: `update_available=false` when the installed Take equals or exceeds the selected Take.

## Tests

```bash
python3 -m unittest discover -s tools/tests -v
python3 -m py_compile tools/cpuse_jhf_fetch.py
```

The tests use offline HTML/JSON fixtures and cover policy separation, release URL mapping, metadata mismatches, checksum parsing, trusted signed URLs, and verified local-file reuse.
