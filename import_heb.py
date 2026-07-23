#!/usr/bin/env python3
"""Import a rendered H-E-B page's Atlas map into router data."""
import argparse
import json
from pathlib import Path

from router.heb import extract_atlas_svg, parse_atlas


def main():
    p = argparse.ArgumentParser()
    p.add_argument("page")
    p.add_argument("--out", default="data/659-atlas")
    args = p.parse_args()
    out = Path(args.out)
    boundary = json.loads((out / "boundary_atlas.json").read_text())
    atlas = parse_atlas(extract_atlas_svg(Path(args.page).read_text()),
                        boundary=boundary)
    out.mkdir(parents=True, exist_ok=True)
    (out / "geometry.json").write_text(
        json.dumps(atlas["geometry"], indent=1) + "\n")
    (out / "psas.json").write_text(json.dumps(atlas["psas"], indent=1) + "\n")
    (out / "source.json").write_text(json.dumps({
        "kind": "heb-atlas",
        "store": "659",
        "scale": 0.18,
        "sha256": atlas["digest"],
        "page": args.page,
    }, indent=1) + "\n")
    print(f"{len(atlas['psas'])} PSA points -> {out}")


if __name__ == "__main__":
    main()
