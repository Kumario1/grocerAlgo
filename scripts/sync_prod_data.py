#!/usr/bin/env python3
"""Regenerate .dockerignore store whitelist from passing calibrations.

Catalog-enabled stores (profile.npz + calibration verdict==pass) are the only
data dirs baked into the Railway image. Everything else under data/ stays
excluded so a half-onboarded map cannot silently ship.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"

PREAMBLE = """\
.git
.venv
.heb-*
runtime
logs
tests
guides
*.pdf
.DS_Store

data/*
"""


def catalog_ids(root: Path = ROOT) -> list[int]:
    """Store ids with a built map and a passing calibration."""
    found = []
    data = root / "data"
    if not data.is_dir():
        return found
    for profile in sorted(data.glob("*/profile.npz")):
        name = profile.parent.name
        if not name.isdigit():
            continue
        cal_path = data / f"{name}-atlas" / "calibration.json"
        try:
            record = json.loads(cal_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if record.get("verdict") == "pass":
            found.append(int(name))
    return sorted(found)


def whitelist_lines(ids: list[int]) -> str:
    parts = []
    for store in ids:
        parts.append(f"!data/{store}/")
        parts.append(f"!data/{store}/**")
        parts.append(f"!data/{store}-atlas/")
        parts.append(f"!data/{store}-atlas/**")
    return "\n".join(parts) + ("\n" if parts else "")


def render_dockerignore(ids: list[int]) -> str:
    return PREAMBLE + whitelist_lines(ids)


def parse_whitelisted(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"^!data/(\d+)/$", text, re.M)}


def sync_dockerignore(
    root: Path = ROOT,
    force: bool = False,
    dry_run: bool = False,
) -> list[int]:
    ids = catalog_ids(root)
    path = root / ".dockerignore"
    previous = path.read_text() if path.exists() else ""
    old_ids = parse_whitelisted(previous)
    new_text = render_dockerignore(ids)
    lost = old_ids - set(ids)
    if lost and not force:
        raise SystemExit(
            f"sync_prod_data: refusing to drop whitelisted stores {sorted(lost)} "
            f"(pass --force to shrink)"
        )
    if dry_run:
        sys.stdout.write(new_text)
        return ids
    if previous != new_text:
        path.write_text(new_text)
    return ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="allow removing stores that left the catalog")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the new .dockerignore to stdout")
    parser.add_argument(
        "--root", type=Path, default=ROOT,
        help="repo root (tests pass a temp tree)")
    args = parser.parse_args(argv)
    os.chdir(args.root)
    ids = sync_dockerignore(args.root, force=args.force, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"sync_prod_data: {len(ids)} catalog stores → .dockerignore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
