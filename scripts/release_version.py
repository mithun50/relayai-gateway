#!/usr/bin/env python3
"""
Set the release version for BOTH packages from the date, and date the changelog.

Versions are date based: YYYY.M.D (no zero padding), for example 2026.11.11.
That one string is valid for npm (semver) and PyPI (PEP 440) and sorts correctly
in both. At most one release per day.

    python scripts/release_version.py            # today's date
    python scripts/release_version.py 2026.12.1  # a specific version
    python scripts/release_version.py --check    # verify both packages agree

It updates python/src/relayai/_version.py and node/package.json, and renames the
"## [Unreleased]" heading in CHANGELOG.md to "## [VERSION] - YYYY-MM-DD", leaving a
fresh empty Unreleased section on top.
"""
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_VERSION = ROOT / "python" / "src" / "relayai" / "_version.py"
NODE_PACKAGE = ROOT / "node" / "package.json"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_RE = re.compile(r"^(\d{4})\.(1[0-2]|[1-9])\.(3[01]|[12]\d|[1-9])$")


def current():
    py = re.search(r'__version__ = "([^"]+)"', PY_VERSION.read_text(encoding="utf-8")).group(1)
    node = json.loads(NODE_PACKAGE.read_text(encoding="utf-8"))["version"]
    return py, node


def main(argv):
    if argv[:1] == ["--check"]:
        py, node = current()
        if py != node:
            print(f"version mismatch: python {py}, node {node}")
            return 1
        print(f"both packages are at {py}")
        return 0

    today = datetime.date.today()
    version = argv[0] if argv else f"{today.year}.{today.month}.{today.day}"
    match = VERSION_RE.match(version)
    if not match:
        print(f"'{version}' is not a date version (YYYY.M.D, no zero padding)")
        return 2
    try:
        date = datetime.date(*(int(g) for g in match.groups()))
    except ValueError:
        print(f"'{version}' is not a real date")
        return 2

    py, node = current()
    if version in (py, node):
        print(f"already at {version}. Date versions allow one release per day.")
        return 1

    PY_VERSION.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    pkg = NODE_PACKAGE.read_text(encoding="utf-8")
    NODE_PACKAGE.write_text(re.sub(r'"version": "[^"]+"', f'"version": "{version}"', pkg, count=1), encoding="utf-8")

    log = CHANGELOG.read_text(encoding="utf-8")
    if "## [Unreleased]" not in log:
        print("CHANGELOG.md has no '## [Unreleased]' section; add your notes there first.")
        return 1
    log = log.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{version}] - {date.isoformat()}", 1)
    CHANGELOG.write_text(log, encoding="utf-8")
    print(f"version set to {version} in both packages; CHANGELOG.md dated {date.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
