#!/usr/bin/env python3
"""Discover and securely download Check Point Jumbo Hotfix packages.

The tool reads Check Point's official per-release JHF downloads page, keeps
Recommended and Latest as distinct policies, resolves exact package metadata
from the public Support Center record, and verifies the downloaded artifact.
It never installs or stages a package.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

UA = "checkpoint-jhf-fetch/1.0"
DOCS_BASE = "https://sc1.checkpoint.com/documents/Jumbo_HFA"
DETAIL_BASE = "https://support.checkpoint.com/results/download"
DETAIL_API = "https://iapi-services-ucs.checkpoint.com/public/api/support-center-mms/api/getDownload"
PATH_API = "https://iapi-services-ucs.checkpoint.com/public/api/support-center-mms/api/getDownloadPath"


class FetchError(RuntimeError):
    pass


class CatalogParser(HTMLParser):
    """Collect headings and links in document order without scraping page text blobs."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str, str]] = []
        self.heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "h3":
            self.heading = []
        href = values.get("href")
        if tag.lower() == "a" and href and "/results/download/" in href:
            self.events.append(("link", href, ""))

    def handle_data(self, data: str) -> None:
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h3" and self.heading is not None:
            text = " ".join("".join(self.heading).split())
            self.events.append(("heading", "", text))
            self.heading = None


def version_path(version: str) -> str:
    if not re.fullmatch(r"R\d+(?:\.\d+)?", version):
        raise FetchError(f"invalid Check Point release {version!r}")
    leaf = f"{version}.00" if re.fullmatch(r"R\d+", version) else version
    return f"{DOCS_BASE}/{version}/{leaf}/{version}_Downloads.htm"


def fetch_bytes(url: str, *, timeout: int = 60, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": UA, "Accept": "*/*"}
    request_headers.update(headers or {})
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=request_headers), timeout=timeout
        ) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"request failed for {url}: {exc}") from exc


def parse_catalog(raw: str, version: str) -> dict[str, dict[str, object]]:
    parser = CatalogParser()
    parser.feed(raw)
    selected: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    for kind, href, text in parser.events:
        if kind == "heading":
            match = re.fullmatch(r"Take\s+(\d+)\s+-\s+(Recommended|Latest)", text, re.I)
            current = None
            if match:
                policy = match.group(2).lower()
                current = {"version": version, "take": int(match.group(1)), "policy": policy}
                selected[policy] = current
        elif current is not None and "download_id" not in current:
            match = re.search(r"/results/download/(\d+)", href)
            if match:
                current["download_id"] = match.group(1)
                current["detail_url"] = f"{DETAIL_BASE}/{match.group(1)}"
    for policy, record in selected.items():
        if "download_id" not in record:
            raise FetchError(f"{policy} Take found but its CPUSE package link is missing")
    if "recommended" not in selected:
        raise FetchError("official catalog does not identify a Recommended Take")
    return selected


def parse_detail(raw: str, expected: dict[str, object]) -> dict[str, object]:
    try:
        if raw.lstrip().startswith("{"):
            source = json.loads(raw)
            item = {
                "title": source.get("fileDisplayName"),
                "version": source.get("versionDisplayName"),
                "fileName": source.get("fileName"),
                "datePublished": source.get("fileDate"),
                "size": source.get("fileSize"),
                "sha1": source.get("sha1"),
                "sha256": source.get("sha256"),
            }
            public_file = True
        else:
            match = re.search(
                r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw, re.S | re.I
            )
            if not match:
                raise FetchError("download detail response has no structured metadata")
            data = json.loads(html.unescape(match.group(1)))
            item = data["props"]["pageProps"]["data"]
            public_file = bool(data["props"]["pageProps"].get("publicFile"))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FetchError(f"invalid download metadata: {exc}") from exc

    version = str(item.get("version") or "")
    filename = str(item.get("fileName") or "")
    sha1 = str(item.get("sha1") or "").lower()
    sha256 = str(item.get("sha256") or "").lower()
    expected_name = (
        f"Check_Point_{expected['version']}_jumbo_hf_main_Bundle_"
        f"T{expected['take']}_FULL.tar"
    )
    if version != expected["version"]:
        raise FetchError(f"metadata release mismatch: expected {expected['version']}, got {version}")
    if filename != expected_name:
        raise FetchError(f"unexpected package filename: expected {expected_name}, got {filename}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
        raise FetchError("download metadata is missing a valid SHA1")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise FetchError("download metadata is missing a valid SHA256")
    return {
        **expected,
        "title": item.get("title"),
        "filename": filename,
        "published": item.get("datePublished"),
        "display_size": item.get("size"),
        "sha1": sha1,
        "sha256": sha256,
        "public_file": public_file,
    }


def discover(version: str, attempts: int = 3) -> dict[str, dict[str, object]]:
    catalog_url = version_path(version)
    last_error: FetchError | None = None
    for attempt in range(attempts):
        try:
            catalog = parse_catalog(fetch_bytes(catalog_url).decode("utf-8", "replace"), version)
            break
        except FetchError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    else:
        raise last_error or FetchError("catalog discovery failed")

    for record in catalog.values():
        for attempt in range(attempts):
            try:
                metadata_url = f"{DETAIL_API}/{record['download_id']}"
                detail = fetch_bytes(metadata_url).decode("utf-8", "replace")
                record.update(parse_detail(detail, record))
                record["metadata_url"] = metadata_url
                break
            except FetchError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
        else:
            raise last_error or FetchError("download metadata discovery failed")
        record["catalog_url"] = catalog_url
    return catalog


def signed_url(download_id: str) -> str:
    raw = fetch_bytes(f"{PATH_API}/{download_id}").decode("utf-8", "replace")
    try:
        url = json.loads(raw)["filePath"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FetchError(f"invalid signed-download response: {exc}") from exc
    if not isinstance(url, str) or not url.startswith("https://dl3.checkpoint.com/"):
        raise FetchError("download API returned an untrusted URL")
    return url


def file_hashes(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def download(record: dict[str, object], destination: Path) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    final = destination / str(record["filename"])
    partial = final.with_suffix(final.suffix + ".part")
    if final.exists():
        got_sha1, got_sha256 = file_hashes(final)
        if got_sha1 != record["sha1"] or got_sha256 != record["sha256"]:
            raise FetchError(f"existing package checksum mismatch: {final}")
        os.chmod(final, 0o600)
        return {**record, "path": str(final), "bytes": final.stat().st_size, "verified": True, "reused": True}
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    url = signed_url(str(record["download_id"]))
    request = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"package download failed: {exc}") from exc
    status = getattr(response, "status", response.getcode())
    mode = "ab" if offset and status == 206 else "wb"
    if offset and status not in (200, 206):
        response.close()
        raise FetchError(f"server rejected resumed download with HTTP {status}")
    try:
        with response, partial.open(mode) as output:
            os.chmod(partial, 0o600)
            for chunk in iter(lambda: response.read(8 * 1024 * 1024), b""):
                output.write(chunk)
    except OSError as exc:
        raise FetchError(f"cannot save package: {exc}") from exc

    got_sha1, got_sha256 = file_hashes(partial)
    if got_sha1 != record["sha1"] or got_sha256 != record["sha256"]:
        raise FetchError(
            f"checksum mismatch for {partial}: sha1={got_sha1}, sha256={got_sha256}"
        )
    partial.replace(final)
    os.chmod(final, 0o600)
    return {**record, "path": str(final), "bytes": final.stat().st_size, "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="R82", help="release, for example R82 or R81.20")
    parser.add_argument("--policy", choices=("recommended", "latest"), default="recommended")
    parser.add_argument("--installed-take", type=int, help="report whether the selected Take is newer")
    parser.add_argument("--download", action="store_true", help="download and verify the selected package")
    parser.add_argument("--dest", type=Path, default=Path.cwd() / "jhf_packages")
    parser.add_argument("--output", type=Path, help="also write result JSON to this file")
    args = parser.parse_args()
    try:
        catalog = discover(args.version)
        if args.policy not in catalog:
            raise FetchError(f"official catalog has no {args.policy.title()} Take")
        selected = dict(catalog[args.policy])
        selected["installed_take"] = args.installed_take
        selected["update_available"] = (
            args.installed_take is not None and int(selected["take"]) > args.installed_take
        )
        result = download(selected, args.dest) if args.download else selected
        payload = {"selected": result, "catalog": catalog}
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n")
            os.chmod(args.output, 0o600)
        return 0
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
