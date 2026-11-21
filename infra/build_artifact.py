#!/usr/bin/env python3
"""
Minimal AWS Lambda Build Artifact Creator and Security Inspector.

Creates a clean, hermetic zip artifact containing only production runtime code
(`lambda_handler.py` and `src/`), explicitly excluding:
- Sensitive files (.env*, credentials, tokens)
- Development/virtual environments (.venv*, env/)
- Tests, fixtures, mock data, and sample calendars
- Python bytecode caches (__pycache__, *.pyc, *.pyo)
- Git metadata (.git*)
- Documentation and temporary logs
"""

import os
import sys
import zipfile
import re
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
OUTPUT_ZIP = DIST_DIR / "lambda_artifact.zip"

PROHIBITED_PATTERNS = [
    re.compile(r"(^|/)\.env.*", re.IGNORECASE),
    re.compile(r"(^|/)\.venv.*", re.IGNORECASE),
    re.compile(r"(^|/)\.git.*", re.IGNORECASE),
    re.compile(r".*\.(pyc|pyo|pyd)$", re.IGNORECASE),
    re.compile(r"(^|/)__pycache__(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)tests(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)data(/|$)", re.IGNORECASE),
    re.compile(r".*\.(key|pem|secret|token)$", re.IGNORECASE),
    re.compile(r".*\.log$", re.IGNORECASE),
    re.compile(r".*\.ics$", re.IGNORECASE),
    re.compile(r"(^|/)sample_calendars(/|$)", re.IGNORECASE),
]

ALLOWED_TOP_LEVEL = ["lambda_handler.py", "src"]


def build_lambda_artifact(output_path: Path = OUTPUT_ZIP) -> Path:
    """Builds minimal zip artifact containing strictly production code."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    files_to_add: List[Tuple[Path, str]] = []

    # 1. Handler
    handler_path = PROJECT_ROOT / "lambda_handler.py"
    if not handler_path.exists():
        raise FileNotFoundError(f"Missing required handler: {handler_path}")
    files_to_add.append((handler_path, "lambda_handler.py"))

    # 2. Production source tree (src/)
    src_dir = PROJECT_ROOT / "src"
    if not src_dir.exists():
        raise FileNotFoundError(f"Missing required source dir: {src_dir}")

    for root, dirs, files in os.walk(src_dir):
        # Exclude pycache directories from walk
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for file in sorted(files):
            if not file.endswith(".py"):
                continue
            full_path = Path(root) / file
            if full_path.is_symlink():
                raise ValueError("Refusing to package a symlink")
            rel_path = full_path.relative_to(PROJECT_ROOT).as_posix()
            files_to_add.append((full_path, rel_path))

    # Write to ZIP
    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for full_path, arcname in files_to_add:
            # Check prohibited patterns
            for pattern in PROHIBITED_PATTERNS:
                if pattern.search(arcname):
                    raise ValueError(f"CRITICAL SECURITY VIOLATION: Refusing to package prohibited file: {arcname}")
            zf.write(full_path, arcname=arcname)

    return output_path


def inspect_archive(zip_path: Path = OUTPUT_ZIP) -> List[str]:
    """Inspects a zip archive and verifies zero sensitive files are present."""
    if not zip_path.exists():
        raise FileNotFoundError(f"Zip archive not found: {zip_path}")

    violations: List[str] = []
    packaged_files: List[str] = []

    with zipfile.ZipFile(zip_path, mode="r") as zf:
        namelist = zf.namelist()
        for name in namelist:
            packaged_files.append(name)
            # 1. Check prohibited patterns
            for pattern in PROHIBITED_PATTERNS:
                if pattern.search(name):
                    violations.append(f"Prohibited pattern '{pattern.pattern}' matched: {name}")

            # 2. Ensure only allowed top-level entries
            top_level = name.split("/")[0]
            if top_level not in ALLOWED_TOP_LEVEL:
                violations.append(f"Unexpected top-level entry: {name}")

    if violations:
        raise ValueError("Archive security verification FAILED:\n" + "\n".join(violations))

    return packaged_files


if __name__ == "__main__":
    print(f"Building minimal Lambda artifact at: {OUTPUT_ZIP}")
    artifact = build_lambda_artifact()
    size_kb = artifact.stat().st_size / 1024
    print(f"Artifact created successfully! Size: {size_kb:.2f} KB")

    print("\nAuditing packaged contents against security policies...")
    entries = inspect_archive(artifact)
    print(f"Total packaged files: {len(entries)}")
    for entry in entries:
        print(f"  + {entry}")
    print("\nPASS: All security checks passed. Zero credentials, caches, tests, or dev environments present.")
