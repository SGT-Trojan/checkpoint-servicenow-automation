#!/usr/bin/env python3
"""Fail closed when retained evidence contains credential or session material."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


TOKEN_FIELDS = ("authToken", "clientSessionId", "fwmSessionId")
ALLOWED_VALUES = {"", "null", "none", "<redacted>", "redacted", "***"}
GENERAL_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S+"),
    re.compile(r"(?i)\bsshpass\s+-p\s+\S+"),
    re.compile(
        r"""(?ix)
        \b(?:password|passwd|secret|api[_ -]?key)\b
        \s*[:=]\s*
        ["']?
        (?!\s*(?:null|none|<redacted>|redacted|\*{3,})\b)
        [^"'\s]{6,}
        """
    ),
)


def token_values(text: str, field: str) -> list[str]:
    prefix = rf"""(?ix)(?:["']?{re.escape(field)}["']?)\s*[:=]\s*"""
    quoted = re.compile(prefix + r"""(?P<quote>["'])(?P<value>.*?)(?P=quote)""")
    unquoted = re.compile(prefix + r"""(?!["'])(?P<value>[^\s,}}]+)""")
    values = [match.group("value") for match in quoted.finditer(text)]
    values.extend(match.group("value") for match in unquoted.finditer(text))
    return values


def findings(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return [f"{path}: unreadable: {exc}"]
    result: list[str] = []
    for field in TOKEN_FIELDS:
        values = token_values(text, field)
        if any(value.strip().lower() not in ALLOWED_VALUES for value in values):
            result.append(f"{path}: retained value for {field}")
    for pattern in GENERAL_PATTERNS:
        if pattern.search(text):
            result.append(f"{path}: credential pattern {pattern.pattern.splitlines()[0]}")
    return result


def files(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        if path.is_file():
            result.append(path)
        elif path.is_dir():
            result.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
        else:
            raise FileNotFoundError(path)
    return sorted(set(result))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    matches: list[str] = []
    try:
        scanned = files(args.paths)
    except FileNotFoundError as exc:
        parser.error(f"path does not exist: {exc}")
    for path in scanned:
        matches.extend(findings(path))
    if matches:
        print("\n".join(matches))
        return 1
    print(f"Evidence secret scan passed: {len(scanned)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
