"""Static secret scanner for source and configuration files in AI SDLC SaaS."""

from __future__ import annotations

import re
from pathlib import Path

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".local_dev",
    "dist",
    "artifacts",
}

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("openai_sk_key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{24,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("stripe_live_key", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
]

FORBIDDEN_FILENAMES = {".env", ".env.production", ".env.staging", ".netrc", ".git-credentials"}


def scan_directory(root: Path) -> list[str]:
    """Return list of human-readable secret scan findings under `root`."""
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.name in FORBIDDEN_FILENAMES:
            findings.append(f"{rel}: forbidden secret file committed/present")
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".db", ".sqlite", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if rel == "scripts/check_secrets.py":
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule_name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    # Allow explicit synthetic test fixtures clearly marked with FAKE/DUMMY/TEST_ONLY
                    upper = line.upper()
                    if any(marker in upper for marker in ("TEST_ONLY", "DUMMY", "EXAMPLE", "PLACEHOLDER", "SYNTHETIC")):
                        continue
                    findings.append(f"{rel}:{lineno}: potential secret ({rule_name})")
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan_directory(root)
    if findings:
        for item in findings:
            print(f"[SECRET-SCAN FAIL] {item}")
        return 1
    print("Secret scan PASS: 0 findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
