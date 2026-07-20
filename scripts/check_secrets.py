from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "__pycache__", ".ruff_cache"}
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|API_KEY|JWT|WEBHOOK_TOKEN)[A-Z0-9_]*)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',}]+)"
)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        not normalized
        or normalized in {"...", "<secret>", "<deployed", "none"}
        or normalized.startswith("test-only-")
        or normalized.startswith("replace-")
        or normalized.startswith("settings.")
        or normalized.startswith("str")
    )


def main() -> int:
    findings: list[tuple[Path, int]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if PRIVATE_KEY_MARKER in line or JWT_PATTERN.search(line):
                findings.append((path, line_number))
                continue
            match = SENSITIVE_ASSIGNMENT.search(line)
            if match and not _is_placeholder(match.group(1)):
                findings.append((path, line_number))

    if findings:
        for path, line_number in findings:
            relative = path.relative_to(ROOT)
            print(f"Potential secret at {relative}:{line_number}", file=sys.stderr)
        return 1
    print("No committed secret-like values detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
