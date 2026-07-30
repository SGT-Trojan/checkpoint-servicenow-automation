# Check Point JHF Currency and Download

Use `cpuse_jhf_fetch.py` to list or download Check Point JHF packages. The tool
checks package metadata and hashes. It does not copy a package to the MDS and
does not install anything.

New to JHF terms or the maintenance flow? Read [Start Here](../docs/START_HERE.md).

## Which Take Does It Select?

The official catalog can publish two different candidates:

- `recommended` is the default. Check Point currently marks this Take as Recommended.
- `latest` must be selected explicitly. It may be newer, but Check Point has not marked it Recommended.
- `archived-recommended` was Recommended in the past. Select it by Take number or from the menu.

Do not automatically install Latest in production. Finding or downloading a
package is not approval to install it. The normal change, compatibility,
readiness, checksum, rollback, and cluster checks still apply.

An older Take is not a current recommendation. Select one only for a known
compatibility, rollback, reproduction, or lab need. The archive may not contain
every old Take. The tool never uses an older package to start a downgrade.

Some older archive records show the wrong release label. The tool accepts this
only for a previously Recommended Take and only when the archive section, title,
filename, Take, and both hashes identify the requested package. The menu shows
the stale label in a notice. Current Recommended and Latest records must have
an exact release match.

## Requirements

- Python 3.9 or later
- HTTPS access to `sc1.checkpoint.com`, `support.checkpoint.com`, `iapi-services-ucs.checkpoint.com`, and `dl3.checkpoint.com`
- Sufficient local storage for the package and any `.part` file

Public JHF records need no UserCenter password. The tool accepts no credentials.

The tool uses `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` when they are set. If
a proxy inspects TLS, Python must trust the CA that signed the proxy certificate.

## Commands

Discover the current R82 Recommended and Latest Takes:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82
```

List the current and archived Recommended packages:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --list
```

Open a numbered selection menu:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --menu
```

After you choose a package, the menu checks its metadata and shows a summary.
It then asks whether to download:

- `y` downloads to `./jhf_packages`, or to the directory set by `--dest`;
- `n` prints the checked selection without downloading; and
- `Ctrl+C` exits with status 130 and no traceback.

Use `--menu --download` when choosing the menu item is enough confirmation.

For scripts and scheduled jobs, select a Take without the menu. `--take` prints
JSON only. Add `--download` to download the package:

```bash
python3 tools/cpuse_jhf_fetch.py --version R82 --take 91
python3 tools/cpuse_jhf_fetch.py \
  --version R82 \
  --take 91 \
  --download \
  --dest /srv/checkpoint/packages
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

## How It Works

The tool performs these steps:

1. Fetch the official release-specific JHF downloads page.
2. Parse `Take N - Recommended` and `Take N - Latest` as distinct records.
3. For `--list`, `--menu`, or `--take`, parse the structured `sk174185` archive and retain only that release's archived Recommended CPUSE TAR records.
4. De-duplicate current and archived entries by Take, preferring the current Recommended or Latest designation.
5. Resolve the CPUSE TAR download ID from each record.
6. Before an exact selection or download, read structured metadata from the Support Center record.
7. Require exact agreement among requested release, Take, canonical filename, SHA1, and SHA256.
8. Report `update_available=true` only when the selected Take is numerically higher than `--installed-take`.
9. On `--download`, obtain a short-lived URL from Check Point's public endpoint, require the host to be `dl3.checkpoint.com`, and stream to a partial file.
10. Verify both published hashes before atomically promoting the partial file to the canonical filename.

Discovery fails closed when Recommended is absent, a requested policy is unavailable, metadata is malformed or inconsistent, the signed URL is untrusted, or network access fails. Download fails closed on any checksum mismatch.

## Using It in Scheduled Automation

A scheduler can run the read-only check each day and save the JSON result:

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

The tests use offline HTML/JSON fixtures and cover policy separation, archived-release filtering, ambiguous archive rejection, exact/menu selection, menu download confirmation and refusal, clean cancellation, release URL mapping, metadata mismatches, narrowly corroborated stale archive labels, canonical R81.20 filenames, checksum parsing, trusted signed URLs, and verified local-file reuse.
