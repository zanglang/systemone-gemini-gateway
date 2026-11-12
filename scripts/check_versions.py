#!/usr/bin/env python3
"""Release consistency check: tag == pyproject version == Chart.yaml appVersion.

Usage: check_versions.py <tag> [--root DIR]

The tag may carry a leading "v". Stdlib only. Exits 0 when all three agree,
1 with a message per mismatch otherwise (2 on unreadable or malformed inputs).
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

CHART_REL = Path("charts/systemone-gemini-gateway/Chart.yaml")
PYPROJECT_REL = Path("pyproject.toml")

_APP_VERSION_RE = re.compile(r"""^appVersion:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


def read_pyproject_version(path: Path) -> str:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        return str(data["project"]["version"])
    except KeyError as exc:
        raise ValueError(f"{path}: no [project].version") from exc


def read_chart_app_version(path: Path) -> str:
    match = _APP_VERSION_RE.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{path}: no appVersion line")
    return match.group(1)


def check(tag: str, root: Path) -> list[str]:
    """Return a list of mismatch messages (empty when consistent)."""
    version = tag[1:] if tag.startswith("v") else tag
    py_version = read_pyproject_version(root / PYPROJECT_REL)
    app_version = read_chart_app_version(root / CHART_REL)

    errors: list[str] = []
    if py_version != version:
        errors.append(f"tag version {version!r} != pyproject.toml [project].version {py_version!r}")
    if app_version != version:
        errors.append(f"tag version {version!r} != Chart.yaml appVersion {app_version!r}")
    if py_version != app_version:
        errors.append(
            f"pyproject.toml version {py_version!r} != Chart.yaml appVersion {app_version!r}"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="release tag, e.g. v0.1.0 (leading v optional)")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repository root (default: cwd)"
    )
    args = parser.parse_args(argv)

    try:
        errors = check(args.tag, args.root)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if errors:
        print("Version mismatch:", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        return 1
    print(f"OK: {args.tag} matches pyproject.toml and Chart.yaml appVersion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
