#!/usr/bin/env python3
"""Fetch the latest CPUSE Deployment Agent from Check Point UserCenter (sk92449).

Two-part design:
  1. Discovery (anonymous): scrape sk92449 to learn the current recommended DA
     build and the per-architecture download IDs. No credentials needed.
  2. Download (authenticated): drive the UserCenter Auth0 login (identifier ->
     password -> TOTP) with Playwright, open the download-detail page, read the
     published SHA1/SHA256, click Download (which calls the getDownloadPath API
     and streams a signed dl3.checkpoint.com URL), then verify the checksum.

Credentials come from an env file (default ~/.config/cpuc/usercenter.env,
0600, root/owner only):
    CPUC_USERNAME=...
    CPUC_PASSWORD=...
    CPUC_TOTP_SECRET=<base32 TOTP secret>

The Check Point gateways stay air-gapped: only this automation host talks to
UserCenter. The verified package is then staged to the MDS /var/log/tmp so the
readiness worker can confirm an offline DA package is present and current.

Download ID catalog (build 2771, verified 2026-07-12 from sk92449):
    143249  x86_64, "For versions R80.40/R81/R81.10/R81.20/R82"   <- general gateways
    143248  "For R82.10 version"
    143250  aarch64, "For 3900 series appliances"
    97404   build 2337 legacy, R80.30 and lower
Always re-scrape; IDs and the recommended build change when Check Point publishes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from pathlib import Path

SK_URL = "https://support.checkpoint.com/results/sk/sk92449"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
DEFAULT_ENV = "~/.config/cpuc/usercenter.env"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def load_env(path: str) -> dict:
    env = {}
    for line in Path(os.path.expanduser(path)).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for req in ("CPUC_USERNAME", "CPUC_PASSWORD", "CPUC_TOTP_SECRET"):
        if not env.get(req):
            raise SystemExit(f"ERROR: {req} missing from {path}")
    return env


def scrape_catalog(retries: int = 3) -> dict:
    """Anonymous: return {'build': '2771', 'packages': [{id, desc, arch}]}."""
    import urllib.request, time
    raw = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(SK_URL, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            if "results/download/" in raw:
                break
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    txt = html.unescape(raw.encode().decode("unicode_escape", "replace"))
    # Top table row reads: "<build> <DD Month YYYY> Recommended version".
    # Match the build number that precedes a date preceding "Recommended version".
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt))
    build = None
    hdr = re.search(r"(\d{4})\s+\d{1,2}\s+\w+\s+\d{4}\s+Recommended version", plain)
    if hdr:
        build = hdr.group(1)
    else:
        m = re.search(r"latest CPUSE Deployment Agent build[^\d]*(\d{4})", plain)
        build = m.group(1) if m else None
    packages = []
    for mm in re.finditer(r"results/download/(\d+)\"[^>]*>\s*(?:<[^>]+>\s*)*\(?TGZ\)?", txt):
        did = mm.group(1)
        pre = re.sub(r"<[^>]+>", " ", txt[max(0, mm.start() - 260):mm.start()])
        pre = re.sub(r"\s+", " ", pre).strip()[-160:]
        arch = ("aarch64" if "3900" in pre or "appliance" in pre.lower()
                else "x86_64" if "R80.40" in pre or "R81" in pre else "unknown")
        packages.append({"id": did, "arch": arch, "desc": pre})
    return {"build": build, "packages": packages}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def require_published_sha256(value: object) -> str:
    published = str(value or "").strip()
    if not SHA256_RE.fullmatch(published):
        raise SystemExit("ERROR: download page did not provide a valid published SHA256")
    return published.lower()


def verify_download(path: Path, published_sha256: str) -> str:
    expected = require_published_sha256(published_sha256)
    actual = sha256_file(path).lower()
    if actual != expected:
        path.unlink(missing_ok=True)
        raise SystemExit("ERROR: checksum mismatch against the SK-published SHA256")
    return actual


def persist_session_state(context: object, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(state_file))
    try:
        os.chmod(state_file, 0o600)
    except OSError as exc:
        state_file.unlink(missing_ok=True)
        raise SystemExit(f"ERROR: could not secure UserCenter session state: {exc}") from exc


def download(env: dict, download_id: str, dest_dir: Path) -> dict:
    import pyotp
    from playwright.sync_api import sync_playwright
    dest_dir.mkdir(parents=True, exist_ok=True)
    start = ("https://usercenter.checkpoint.com/oauth2/sign_in?rd="
             f"https%3A%2F%2Fsupport.checkpoint.com%2Fresults%2Fdownload%2F{download_id}")
    # Reuse a saved browser session when available: repeated fresh Auth0 logins
    # trip UserCenter anti-automation throttling, so log in once and reuse the
    # session for subsequent (e.g. weekly) checks. Delete the state file to force
    # a fresh login.
    state_file = Path(os.path.expanduser("~/.config/cpuc/session_state.json"))
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx_kwargs = {"accept_downloads": True, "user_agent": UA}
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        ctx = b.new_context(**ctx_kwargs)
        pg = ctx.new_page()
        pg.goto(start, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2000)
        if pg.query_selector("#username"):
            pg.fill("#username", env["CPUC_USERNAME"])
            pg.click("button:has-text('Continue')")
            # Auth0 identifier-first: a hidden decoy password field exists on the
            # username page; wait for the real VISIBLE one on the password step.
            pw = pg.wait_for_selector("input[type=password]:visible", timeout=30000)
            pw.fill(env["CPUC_PASSWORD"])
            pg.click("button:has-text('Continue'), button[type=submit]")
            pg.wait_for_timeout(4000)
            if re.search(r"code|one-time|authenticat|verif", pg.inner_text("body"), re.I):
                for cand in ("input[name=code]", "input[autocomplete=one-time-code]",
                             "input[type=text]", "input[type=tel]"):
                    if pg.query_selector(cand):
                        pg.fill(cand, pyotp.TOTP(env["CPUC_TOTP_SECRET"]).now())
                        for bt in ("button:has-text('Continue')", "button:has-text('Verify')",
                                   "button[type=submit]"):
                            if pg.query_selector(bt):
                                pg.click(bt); break
                        break
        for _ in range(25):
            if "support.checkpoint.com/results/download" in pg.url:
                break
            pg.wait_for_timeout(1500)
        if "support.checkpoint.com/results/download" not in pg.url:
            b.close(); raise SystemExit("ERROR: login did not return to the download page")
        try:
            pg.wait_for_selector("text=/File Name/i", timeout=30000)
        except Exception:
            pass
        pg.wait_for_timeout(2500)
        body = pg.inner_text("body")
        published = {
            "file_name": (re.search(r"File Name\s*[:\n]*\s*(\S+\.tgz)", body) or [None, None])[1],
            "sha1": (re.search(r"SHA1\s*[:\n]*\s*([0-9a-f]{40})", body) or [None, None])[1],
            "sha256": (re.search(r"SHA256\s*[:\n]*\s*([0-9a-f]{64})", body) or [None, None])[1],
        }
        if re.search(r"not entitled", body, re.I):
            b.close(); raise SystemExit("ERROR: account is not entitled to this download")
        try:
            expected_sha256 = require_published_sha256(published["sha256"])
        except SystemExit:
            b.close()
            raise
        try:
            with pg.expect_download(timeout=90000) as di:
                pg.get_by_role("button", name=re.compile("download", re.I)).first.click()
            d = di.value
            dest = dest_dir / d.suggested_filename
            d.save_as(str(dest))
            got = verify_download(dest, expected_sha256)
            persist_session_state(ctx, state_file)
        finally:
            b.close()
    return {"path": str(dest), "size": dest.stat().st_size, "sha256": got,
            "published": published, "verified": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="x86_64", help="x86_64 | aarch64 (picks the download ID from the SK)")
    ap.add_argument("--download-id", default="", help="override the SK-derived download ID")
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    ap.add_argument("--dest", default="/opt/checkpoint-automation/runs/da_packages")
    ap.add_argument("--discover-only", action="store_true", help="scrape the SK and print the catalog; no login")
    args = ap.parse_args()

    # Only scrape when we actually need the SK to choose an ID. An explicit
    # --download-id must work even if the SK is unreachable/throttled.
    did = args.download_id
    if args.discover_only or not did:
        catalog = scrape_catalog()
        print(json.dumps({"discovered": catalog}, indent=2))
        if args.discover_only:
            return 0
        match = [p for p in catalog["packages"] if p["arch"] == args.arch]
        if not match:
            raise SystemExit(f"ERROR: no {args.arch} package found in the SK; pass --download-id explicitly")
        did = match[0]["id"]
        print(f"Selected download ID {did} for arch {args.arch} (build {catalog['build']})")
    else:
        print(f"Using explicit download ID {did}")

    env = load_env(args.env_file)
    result = download(env, did, Path(args.dest))
    print(json.dumps({"download": result}, indent=2))
    if not result["verified"]:
        raise SystemExit("ERROR: checksum mismatch against the SK-published SHA256")
    print(f"OK: {result['path']} verified ({result['sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
