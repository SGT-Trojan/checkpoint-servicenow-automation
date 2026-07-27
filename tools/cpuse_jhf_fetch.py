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
ARCHIVE_URL = "https://support.checkpoint.com/results/sk/sk174185"


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


class ArchiveParser(HTMLParser):
    """Parse archived Recommended CPUSE TAR records from sk174185 tables."""

    def __init__(self) -> None:
        super().__init__()
        self.release: str | None = None
        self.in_heading = False
        self.heading: list[str] = []
        self.in_row = False
        self.cells: list[dict[str, object]] = []
        self.cell: dict[str, object] | None = None
        self.rows: list[tuple[str, list[dict[str, object]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        tag = tag.lower()
        if tag == "h3":
            self.in_heading = True
            self.heading = []
        elif tag == "tr":
            self.in_row = True
            self.cells = []
        elif tag in ("td", "th") and self.in_row:
            self.cell = {"text": [], "links": []}
        elif tag == "a" and self.cell is not None:
            links = self.cell["links"]
            assert isinstance(links, list)
            links.append(values.get("href") or "")

    def handle_data(self, data: str) -> None:
        if self.in_heading:
            self.heading.append(data)
        if self.cell is not None:
            text = self.cell["text"]
            assert isinstance(text, list)
            text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h3" and self.in_heading:
            text = " ".join("".join(self.heading).split())
            match = re.search(r"(R\d+(?:\.\d+)?)\s+Archived Recommended Takes", text, re.I)
            self.release = match.group(1) if match else None
            self.in_heading = False
        elif tag in ("td", "th") and self.cell is not None:
            text = self.cell["text"]
            assert isinstance(text, list)
            self.cell["text"] = " ".join("".join(text).split())
            self.cells.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.in_row:
            if self.release:
                self.rows.append((self.release, self.cells))
            self.in_row = False


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


def parse_archive(raw: str, version: str) -> list[dict[str, object]]:
    """Return official archived Recommended CPUSE TAR records for one release."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw, re.S | re.I
    )
    if not match:
        raise FetchError("download archive response has no structured metadata")
    try:
        data = json.loads(html.unescape(match.group(1)))
        article = data["props"]["pageProps"]["data"]
        if article.get("id") != "sk174185":
            raise FetchError("download archive returned an unexpected article")
        solution = article["solution"]
        if not isinstance(solution, str):
            raise FetchError("download archive solution is not HTML")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FetchError(f"invalid download archive metadata: {exc}") from exc

    parser = ArchiveParser()
    parser.feed(solution)
    records: list[dict[str, object]] = []
    seen: set[int] = set()
    for release, cells in parser.rows:
        if release.lower() != version.lower() or not cells:
            continue
        first = str(cells[0].get("text") or "")
        if not re.fullmatch(r"\d+", first):
            continue
        take = int(first)
        tar_ids: list[str] = []
        for cell in cells:
            if "TAR" not in str(cell.get("text") or "").upper():
                continue
            for link in cell.get("links") or []:
                found = re.search(r"/results/download/(\d+)", str(link))
                if found:
                    tar_ids.append(found.group(1))
        tar_ids = list(dict.fromkeys(tar_ids))
        if len(tar_ids) != 1:
            raise FetchError(
                f"archive Take {take} has {len(tar_ids)} CPUSE TAR download links; expected one"
            )
        if take in seen:
            raise FetchError(f"archive contains duplicate {version} Take {take}")
        seen.add(take)
        records.append(
            {
                "version": version,
                "take": take,
                "policy": "archived-recommended",
                "download_id": tar_ids[0],
                "detail_url": f"{DETAIL_BASE}/{tar_ids[0]}",
                "available_since": str(cells[1].get("text") or "") if len(cells) > 1 else "",
                "recommended_since": str(cells[2].get("text") or "") if len(cells) > 2 else "",
                "archive_url": ARCHIVE_URL,
            }
        )
    return sorted(records, key=lambda item: int(item["take"]), reverse=True)


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
    version_token = str(expected["version"]).replace(".", "_")
    expected_pattern = re.compile(
        rf"Check_Point_{re.escape(version_token)}_JUMBO_HF_MAIN_Bundle_"
        rf"T{int(expected['take'])}_FULL\.tar",
        re.I,
    )
    if version != expected["version"]:
        raise FetchError(f"metadata release mismatch: expected {expected['version']}, got {version}")
    if not expected_pattern.fullmatch(filename):
        raise FetchError(
            f"unexpected package filename for {expected['version']} Take {expected['take']}: {filename}"
        )
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


def fetch_with_retries(url: str, attempts: int, label: str) -> bytes:
    last_error: FetchError | None = None
    for attempt in range(attempts):
        try:
            return fetch_bytes(url)
        except FetchError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error or FetchError(f"{label} discovery failed")


def enrich_record(record: dict[str, object], attempts: int = 3) -> dict[str, object]:
    last_error: FetchError | None = None
    metadata_url = f"{DETAIL_API}/{record['download_id']}"
    for attempt in range(attempts):
        try:
            detail = fetch_bytes(metadata_url).decode("utf-8", "replace")
            enriched = dict(record)
            enriched.update(parse_detail(detail, record))
            enriched["metadata_url"] = metadata_url
            return enriched
        except FetchError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error or FetchError("download metadata discovery failed")


def discover(version: str, attempts: int = 3) -> dict[str, dict[str, object]]:
    """Discover and fully validate the current Recommended and Latest records."""
    catalog_url = version_path(version)
    raw = fetch_with_retries(catalog_url, attempts, "catalog").decode("utf-8", "replace")
    catalog = parse_catalog(raw, version)
    for policy, record in list(catalog.items()):
        enriched = enrich_record(record, attempts)
        enriched["catalog_url"] = catalog_url
        catalog[policy] = enriched
    return catalog


def discover_available(version: str, attempts: int = 3) -> list[dict[str, object]]:
    """Discover current packages plus officially archived Recommended packages."""
    catalog_url = version_path(version)
    current_raw = fetch_with_retries(catalog_url, attempts, "catalog").decode("utf-8", "replace")
    current = parse_catalog(current_raw, version)
    archive_raw = fetch_with_retries(ARCHIVE_URL, attempts, "archive").decode("utf-8", "replace")
    archived = parse_archive(archive_raw, version)
    records = [dict(record, catalog_url=catalog_url) for record in current.values()]
    records.extend(archived)
    by_take: dict[int, dict[str, object]] = {}
    priority = {"recommended": 3, "latest": 2, "archived-recommended": 1}
    for record in records:
        take = int(record["take"])
        previous = by_take.get(take)
        if previous is None or priority[str(record["policy"])] > priority[str(previous["policy"])]:
            by_take[take] = record
    return sorted(by_take.values(), key=lambda item: int(item["take"]), reverse=True)


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


def format_available(records: list[dict[str, object]]) -> str:
    lines = ["#  Take  Status                  Available     Recommended"]
    for index, record in enumerate(records, 1):
        lines.append(
            f"{index:>2} {int(record['take']):>5}  {str(record['policy']):<22} "
            f"{str(record.get('available_since') or '-'):>12}  "
            f"{str(record.get('recommended_since') or '-')}"
        )
    return "\n".join(lines)


def select_take(records: list[dict[str, object]], take: int) -> dict[str, object]:
    matches = [record for record in records if int(record["take"]) == take]
    if len(matches) != 1:
        raise FetchError(f"official current/archive catalog has no unique Take {take}")
    return dict(matches[0])


def select_interactive(records: list[dict[str, object]]) -> dict[str, object]:
    if not records:
        raise FetchError("official current/archive catalog is empty")
    print(format_available(records))
    try:
        answer = input(f"Select package [1-{len(records)}]: ").strip()
        choice = int(answer)
    except (EOFError, ValueError) as exc:
        raise FetchError("menu selection must be a number") from exc
    if choice < 1 or choice > len(records):
        raise FetchError(f"menu selection must be between 1 and {len(records)}")
    return dict(records[choice - 1])


def format_selected(record: dict[str, object]) -> str:
    return "\n".join(
        (
            f"Selected: {record['version']} Take {record['take']} ({record['policy']})",
            f"Title:    {record.get('title') or '-'}",
            f"File:     {record.get('filename') or '-'}",
            f"Size:     {record.get('display_size') or '-'}",
            f"SHA256:   {record.get('sha256') or '-'}",
        )
    )


def confirm_download(record: dict[str, object], destination: Path) -> bool:
    filename = str(record.get("filename") or "selected package")
    while True:
        try:
            answer = input(f"Download {filename} to {destination}? [y/N]: ").strip().lower()
        except EOFError as exc:
            raise FetchError("download confirmation requires y or n") from exc
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("Please enter y or n.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="R82", help="release, for example R82 or R81.20")
    parser.add_argument("--policy", choices=("recommended", "latest"), default="recommended")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--take", type=int, help="select an exact current or archived Recommended Take"
    )
    selection.add_argument("--list", action="store_true", help="list current and archived Recommended packages")
    selection.add_argument(
        "--interactive",
        "--menu",
        action="store_true",
        help="select, review, and optionally download from a numbered package menu"
    )
    parser.add_argument("--installed-take", type=int, help="report whether the selected Take is newer")
    parser.add_argument("--download", action="store_true", help="download and verify the selected package")
    parser.add_argument("--dest", type=Path, default=Path.cwd() / "jhf_packages")
    parser.add_argument("--output", type=Path, help="also write result JSON to this file")
    args = parser.parse_args()
    try:
        if args.list and args.download:
            raise FetchError("--list cannot be combined with --download; select a Take first")

        available: list[dict[str, object]] | None = None
        if args.list or args.take is not None or args.interactive:
            available = discover_available(args.version)
            if args.list:
                rendered = format_available(available)
                print(rendered)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(
                        json.dumps({"available": available}, indent=2, sort_keys=True) + "\n"
                    )
                    os.chmod(args.output, 0o600)
                return 0
            selected = (
                select_interactive(available)
                if args.interactive
                else select_take(available, int(args.take))
            )
            selected = enrich_record(selected)
        else:
            catalog = discover(args.version)
            if args.policy not in catalog:
                raise FetchError(f"official catalog has no {args.policy.title()} Take")
            selected = dict(catalog[args.policy])

        selected["installed_take"] = args.installed_take
        selected["update_available"] = (
            args.installed_take is not None and int(selected["take"]) > args.installed_take
        )
        should_download = args.download
        if args.interactive:
            print(f"\n{format_selected(selected)}")
            should_download = args.download or confirm_download(selected, args.dest)
        result = download(selected, args.dest) if should_download else selected
        payload: dict[str, object] = {"selected": result}
        if available is not None:
            payload["available"] = available
        else:
            payload["catalog"] = catalog
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        if args.interactive:
            if should_download:
                print(f"Downloaded and verified: {result['path']}")
            else:
                print("Selection validated; no package downloaded.")
        else:
            print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n")
            os.chmod(args.output, 0o600)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
