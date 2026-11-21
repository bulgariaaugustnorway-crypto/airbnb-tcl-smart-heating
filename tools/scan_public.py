"""Offline release scan; reports locations and categories, never matched values."""
import ast
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "aws-account-number": re.compile(r"(?<![\w])\d{12}(?![\w])"),
    "telegram-token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"),
    "private-calendar-url": re.compile(r"https?://(?:www\.)?airbnb\.[a-z.]+/[^\s\"'<>]*(?:ical|\.ics)[^\s\"'<>]*"),
    "aws-arn": re.compile(r"arn:aws(?:-[a-z-]+)?:[a-z0-9-]+:[^\s\"']+"),
    "absolute-user-path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]|/[U]sers/|/[h]ome/)[^\s\"']+"),
}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "security-results", "review-results"}
PRIVATE_DIRS = {"data/cache", "data/history", "logs", ".aws-sam", "dist", "build", ".aws"}
SAFE_SUFFIXES = {".py", ".md", ".txt", ".toml", ".lock", ".ics", ".yml", ".yaml"}
SECRET_FIELDS = {"TCL_PASSWORD", "TCL_USERNAME", "AIRBNB_ICAL_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "SESSION_SECRET_KEY", "DASHBOARD_PASSWORD_HASH", "NOTIFICATION_WEBHOOK_URL", "S3_BUCKET"}


def scan_text(name, text):
    findings = []
    for category, pattern in RULES.items():
        for match in pattern.finditer(text):
            # A zero account is an explicitly non-routable synthetic test fixture.
            if category == "aws-account-number" and set(match.group()) == {"0"}:
                continue
            findings.append({"file": name, "line": text.count("\n", 0, match.start()) + 1, "category": category})
    return findings


def scan_tree(root=ROOT):
    findings = []
    count = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for directory in list(dirs):
            path = Path(folder) / directory
            if path.is_symlink():
                findings.append({"file": path.relative_to(root).as_posix(), "category": "symlink"})
                dirs.remove(directory)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            path = Path(folder) / filename
            name = path.relative_to(root).as_posix()
            if path.is_symlink():
                findings.append({"file": name, "category": "symlink"})
                continue
            count += 1
            if any(name.startswith(d + "/") for d in PRIVATE_DIRS):
                findings.append({"file": name, "category": "runtime-or-private-artifact"})
            if (filename.startswith(".env") and filename != ".env.example") or filename == "samconfig.toml":
                findings.append({"file": name, "category": "private-configuration"})
            allowed_special = {".gitignore", ".gitattributes", ".samignore", ".env.example", "LICENSE"}
            if path.suffix not in SAFE_SUFFIXES and filename not in allowed_special:
                findings.append({"file": name, "category": "unexpected-file-type"})
                continue
            text = path.read_text(encoding="utf-8-sig")
            findings.extend(scan_text(name, text))
            if filename == ".env.example":
                for line_no, line in enumerate(text.splitlines(), 1):
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        if key in SECRET_FIELDS and value.strip():
                            findings.append({"file": name, "line": line_no, "category": "nonempty-example-secret"})
    return {"files_scanned": count, "findings": findings}


def main():
    result = scan_tree()
    print(json.dumps(result, indent=2))
    return int(bool(result["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
