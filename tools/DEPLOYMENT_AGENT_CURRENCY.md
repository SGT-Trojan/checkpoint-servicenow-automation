# Check Point Deployment Agent Currency (sk92449)

Use `cpuse_da_fetch.py` to find the Recommended Deployment Agent build and
download an approved offline package. Discovery is public. Package download
requires an entitled Check Point UserCenter account.

New to the project? Read [Start Here](../docs/START_HERE.md) first.

## Why This Tool Exists

The readiness worker can compare the installed build with a required build. It
still needs a trusted source for the current recommendation. Check Point article
`sk92449` provides that recommendation. UserCenter controls access to the package.

## What the Test Showed

Public discovery returned Recommended build **2771**, released 07 June 2026.
It also returned these package IDs:

| Download ID | Platform |
|---|---|
| `143249` | x86_64 for R80.40 through R82 |
| `143248` | R82.10 |
| `143250` | aarch64 for 3900-series appliances |
| `97404` | Older build 2337 for R80.30 and earlier |

An authenticated test downloaded package `143250` and verified both published
hashes:

- SHA1 `49116e109e689d97c843c6fd349facc3617d262d`
- SHA256 `1d523680e027d5ddd2cc9396d4dfcfeade1923f70475eea8931107b94b914fb6`

The downloaded archive contained an aarch64 RPM, which matches the package
description. The x86_64 package uses ID `143249`.

## How the Download Works

1. The tool opens the Check Point download page.
2. Playwright signs in through UserCenter with username, password, and TOTP.
3. The page returns a short-lived `dl3.checkpoint.com` URL, and the tool downloads
   the package and verifies both hashes.

The download needs a session token and may show reCAPTCHA. The tool therefore
uses Playwright to complete the normal browser flow. It does not try to rebuild
the private download API.

## The tool: `cpuse_da_fetch.py`

```
# Discovery only (anonymous, safe to run often):
python3 cpuse_da_fetch.py --discover-only

# Authenticated fetch of the x86_64 package for the lab gateways, with checksum verify:
python3 cpuse_da_fetch.py --arch x86_64
#   or pin the ID:  --download-id 143249
```

- Reads `CPUC_USERNAME`, `CPUC_PASSWORD`, and `CPUC_TOTP_SECRET` from
  `~/.config/cpuc/usercenter.env`. The file must use mode `0600`.
- Selects a package by `--arch` or `--download-id`.
- Stops on any checksum mismatch.
- Saves the browser session in `~/.config/cpuc/session_state.json` with mode
  `0600`. Reusing the session avoids repeated logins and UserCenter throttling.

## Suggested Schedule

1. Run `--discover-only` regularly and compare the Recommended build with
   `show installer status build` on each member.
2. Download again when the Recommended build changes. Copy the verified package
   to the MDS only after approval.
3. Check the target architecture with `uname -m`. Use package `143249` for
   x86_64 gateways and `143250` for supported aarch64 appliances.

The readiness task should show whether the installed build is current. The
`07_validate_deployment_agent` playbook can then confirm that the correct
offline package is staged before maintenance starts.

## Security

- Keep UserCenter credentials in the mode `0600` environment file. Do not pass
  them on the command line, write them to logs, or store them in ServiceNow.
- Treat the TOTP seed like a password. Use a dedicated account, keep the seed in
  the same secrets manager as the other automation credentials, and rotate it.
- Only the automation host needs internet access. Copy the verified package to
  the MDS through the approved staging process.

## Limits and Follow-up Work

- The authenticated test covered package `143250`. It did not complete a live
  download of the x86_64 package `143249`.
- Back-to-back test logins triggered UserCenter throttling. Reuse the saved
  session and allow normal time between downloads.
- The readiness worker does not yet run the currency comparison or copy the
  package to the MDS.
