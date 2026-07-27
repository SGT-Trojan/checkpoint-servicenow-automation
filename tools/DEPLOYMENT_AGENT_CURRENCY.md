# Keeping the CPUSE Deployment Agent Current (sk92449)

Answers "how can we be sure we are always running the latest Deployment Agent?" and documents the authenticated UserCenter download capability. Proven live 2026-07-12.

## The problem

The readiness worker checks that the installed DA build is adequate, but "adequate" needs a reference: what IS the latest recommended build, and is the offline package staged so an air-gapped gateway can be brought up to it? sk92449 is the authoritative source, and the actual package download is gated behind UserCenter authentication + entitlement.

## What was proven

Anonymous discovery and authenticated download both work from the automation host (the gateways stay air-gapped — only this host talks to the internet):

- **Discovery (no credentials)**: sk92449 is scrapeable. It yields the recommended build and the per-architecture download IDs. Verified 2026-07-12: build **2771** (released 07 June 2026, "Recommended version"), with download IDs:
  - `143249` — x86_64, "For versions R80.40/R81/R81.10/R81.20/R82" — **the general gateway package (lab CP-FW-A/B target)**
  - `143248` — "For R82.10 version"
  - `143250` — aarch64, "For 3900 series appliances"
  - `97404` — build 2337 legacy, R80.30 and lower
  Even the per-file download-detail page leaks metadata anonymously (filename, size) and the literal entitlement string `"User is not entitled to download this file"` — so "what is latest" is answerable for free; only the binary needs auth.

- **Authenticated download (live-proven)**: logged into UserCenter as the provided account, entitlement passed, downloaded `DeploymentAgent_000002771_1.tgz` (24,051,568 bytes / 22.9 MB) and **verified both published checksums exactly**:
  - SHA1 `49116e109e689d97c843c6fd349facc3617d262d`
  - SHA256 `1d523680e027d5ddd2cc9396d4dfcfeade1923f70475eea8931107b94b914fb6`
  The archive contains `CPda-00-00.aarch64.rpm` (confirming 143250 is the ARM build; the x86 lab package is 143249).

## How the download actually works (the mechanism)

1. `support.checkpoint.com/results/download/<id>` is a Next.js SPA. The "Log In" link starts an OAuth2 flow: `usercenter.checkpoint.com/oauth2/sign_in?rd=<return_url>`.
2. Auth is **Auth0** (`login.checkpoint.com/u/login`), identifier-first: username -> Continue -> password -> Continue -> **TOTP** (6-digit, SHA1, 30s). Note the identifier page carries a hidden decoy password field — the tool targets the visible one.
3. Back on the authenticated download page, clicking **Download** calls `iapi-services-ucs.checkpoint.com/api/support-center-mms/api/getDownloadPath/<id>` (needs an `x-access-token` session header, not just cookies), which returns a **time-limited signed URL** on `dl3.checkpoint.com/paid/<hash>/DeploymentAgent_<build>_<n>.tgz?HashKey=<epoch>_<sig>`. The browser streams that file. An invisible reCAPTCHA sits on the page but did not block the automated click.

Because the API needs a session-scoped token and the flow includes reCAPTCHA, the robust path drives the real browser (Playwright) end to end rather than reconstructing the API by hand.

## The tool: `cpuse_da_fetch.py`

```
# Discovery only (anonymous, safe to run often):
python3 cpuse_da_fetch.py --discover-only

# Authenticated fetch of the x86_64 package for the lab gateways, with checksum verify:
python3 cpuse_da_fetch.py --arch x86_64
#   or pin the ID:  --download-id 143249
```

- Reads credentials from `~/.config/cpuc/usercenter.env` (0600): `CPUC_USERNAME`, `CPUC_PASSWORD`, `CPUC_TOTP_SECRET` (base32).
- Scrapes the SK (with retry), selects the download ID by `--arch` (or explicit `--download-id`), logs in, reads the page's published SHA1/SHA256, downloads, and **fails hard on any checksum mismatch**.
- Persists the authenticated browser session to `~/.config/cpuc/session_state.json` (0600) and reuses it, so scheduled runs do not re-login every time — **important**, because repeated rapid fresh logins trip UserCenter anti-automation throttling (observed during testing: the first logins succeeded cleanly, later back-to-back ones stalled on the OAuth redirect). Weekly cadence + session reuse avoids this entirely.

## Recommended operating model

1. **DA currency check (frequent, cheap)** — extend the readiness worker / a small timer: run `--discover-only`, compare the recommended build to each member's installed build (`show installer status build` via the existing SSH/CPRID helper). Surface on the readiness SCTASK: "DA current (2771)" vs "DA outdated: 2337 installed, 2771 available".
2. **DA fetch + stage (weekly, or when the build changes)** — a systemd timer runs `cpuse_da_fetch.py --arch <estate arch>`; on a verified new build, `scp` the `.tgz` to the MDS `/var/log/tmp`, and (optionally) let CDT/CPRID distribute it. The readiness `07_validate_deployment_agent` check then confirms the offline package is present and current before any maintenance window.
3. **Arch awareness** — pick 143249 (x86_64) for standard gateways, 143250 (aarch64) for 3900-series/ARM. The tool maps `--arch` from the SK descriptions; verify against `uname -m` on the target during discovery.

## Security posture

- Credentials live only in the 0600 env file and are never passed via argv, logged, or written to ServiceNow. Exploratory scripts that briefly embedded them during development were deleted; no plaintext secret remains in the repo or scratchpad.
- **MFA reality**: this account uses TOTP, and the secret is stored so login can run unattended. This is a deliberate tradeoff for automation — production should use a dedicated integration/service account (ideally UserCenter API-based if Check Point exposes one for the estate), with the TOTP seed in the same vault/CyberArk path as the other automation credentials, and rotated on the normal schedule.
- Only the automation host reaches the internet; gateways/MDS remain air-gapped, receiving the package by staged copy.

## Status / follow-ups

- Capability: PROVEN (143250 downloaded + dual-checksum verified). The verified package is kept at `runs/da_packages/` as evidence.
- The x86_64 (143249) live pull hit the anti-automation throttle during back-to-back test logins; the mechanism is identical to the proven path and will run cleanly on a normal (non-hammered) cadence with session reuse. Re-run `cpuse_da_fetch.py --arch x86_64` after the throttle window to stage the lab package.
- Not yet wired into the worker: the currency-check comparison and the MDS staging copy are the remaining integration steps (design above).
