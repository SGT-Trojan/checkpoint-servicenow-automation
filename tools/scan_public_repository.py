#!/usr/bin/env python3
"""Fail when a public source tree contains forbidden private or secret material."""
from __future__ import annotations
import os
from pathlib import Path
import re
import sys

PATTERNS = (
    (re.compile(r"dev\d{6}\.service-now\.com", re.I), "ServiceNow PDI"),
    (re.compile(r"\b192\.168\.2\.\d{1,3}\b"), "private lab subnet"),
    (re.compile(r"/home/ubuntu/(?:chatgpt|claude)"), "private absolute path"),
    (re.compile(r"otp" r"auth://", re.I), "TOTP URI"),
    (re.compile(r"-----BEGIN " r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)\b(?:auth" r"Token|clientSessionId|fwmSessionId)\s*[:=]\s*[\"']?(?!<REDACTED>|null|none)[A-Za-z0-9+/=_-]{8,}"), "session token"),
)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__"}

def main(root: Path) -> int:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            findings.append(f"{path}: unreadable: {exc}")
            continue
        for pattern, label in PATTERNS:
            if pattern.search(text):
                findings.append(f"{path}: {label}")
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Public repository content scan passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
