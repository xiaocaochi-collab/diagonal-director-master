#!/usr/bin/env python3
"""Create portable, non-overwriting filenames for director project artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_component(value: str, max_length: int = 80) -> str:
    """Preserve readable Unicode while removing path and platform hazards."""
    if max_length < 16:
        raise ValueError("max_length must be at least 16")
    original = unicodedata.normalize("NFC", str(value))
    cleaned = INVALID_CHARS.sub("-", original)
    cleaned = re.sub(r"\.{2,}", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip(" .-")
    if not cleaned:
        cleaned = "untitled"
    stem, dot, suffix = cleaned.partition(".")
    if stem.casefold() in RESERVED_NAMES:
        cleaned = f"{stem}-file{dot}{suffix}"
    if len(cleaned) > max_length:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        cleaned = cleaned[: max_length - 9].rstrip(" .-") + "-" + digest
    return cleaned


def sanitize_filename(filename: str, max_component_length: int = 80) -> str:
    """Sanitize one filename. Directory components are intentionally rejected."""
    raw = str(filename)
    path = Path(raw)
    if path.name != raw or raw in {"", ".", ".."}:
        raw = raw.replace("/", "-").replace("\\", "-")
        path = Path(raw)
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    safe_stem = sanitize_component(stem, max_component_length)
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:16]
    return safe_stem + safe_suffix


def unique_path(directory: Path, filename: str) -> Path:
    """Return a safe path, appending -02, -03... instead of overwriting."""
    directory = Path(directory)
    safe = sanitize_filename(filename)
    candidate = directory / safe
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    number = 2
    while True:
        candidate = directory / f"{stem}-{number:02d}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitize one project artifact filename.")
    parser.add_argument("name", help="Untrusted scene, location, version, or artifact name")
    parser.add_argument("--suffix", default="", help="Optional extension such as .md")
    parser.add_argument("--directory", type=Path, help="Also choose a non-overwriting path")
    args = parser.parse_args()

    filename = sanitize_filename(args.name + args.suffix)
    path = unique_path(args.directory, filename) if args.directory else Path(filename)
    print(json.dumps({"filename": path.name, "path": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
