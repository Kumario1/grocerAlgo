#!/usr/bin/env python3
"""Import a saved H-E-B page's Atlas map into router data.

The offline sibling of capture_atlas.py: same parse, same three files, from a
page already on disk rather than a live session.
"""
import argparse
import json
from pathlib import Path

from router.heb import extract_atlas_svg, parse_atlas, write_atlas


def main():
    p = argparse.ArgumentParser()
    p.add_argument("page")
    p.add_argument("--store", default="659")
    p.add_argument("--out")
    args = p.parse_args()
    out = Path(args.out or f"data/{args.store}-atlas")
    boundary_file = out / "boundary_atlas.json"
    boundary = (json.loads(boundary_file.read_text())
                if boundary_file.exists() else None)
    text = Path(args.page).read_text()
    # A saved product page carries the map inside it; a captured one IS the map.
    try:
        svg = extract_atlas_svg(text)
    except ValueError:
        svg = text
    atlas = parse_atlas(svg, boundary=boundary)
    write_atlas(out, args.store, atlas, args.page)
    print(f"{len(atlas['psas'])} PSA points -> {out}")


if __name__ == "__main__":
    main()
