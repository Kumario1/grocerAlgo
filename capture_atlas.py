#!/usr/bin/env python3
"""Capture one store's live H-E-B Atlas map.

    python3 capture_atlas.py <store>

Writes data/<store>-atlas/{store-map.svg,geometry.json,psas.json,source.json}
— the same four files #659 was built from, which is everything the exact
placement layer needs except the transform. Run calibrate.py next.

The SVG is kept so the capture is reproducible offline (import_heb.py replays
it) and so a later drawing can be diffed against the one a store was
calibrated against.
"""
import asyncio
import json
import sys
from pathlib import Path

from router.heb import (HEBClient, HEBConnectionError, parse_atlas,
                        write_atlas)


async def capture(store):
    client = HEBClient(int(store))
    out = Path(f"data/{store}-atlas")
    try:
        await client.connect()
        # locationNumber names the store explicitly, but the session still has
        # to be a real shopper session, so check the catalog answers first.
        if not await client.sees_store():
            print(f"    note: the browser is not on store #{store} — select it "
                  "there if the map comes back empty")
        svg = await client.atlas_svg()
        boundary = None
        boundary_file = out / "boundary_atlas.json"
        if boundary_file.exists():
            boundary = json.loads(boundary_file.read_text())
        atlas = parse_atlas(svg, boundary=boundary)
        out.mkdir(parents=True, exist_ok=True)
        (out / "store-map.svg").write_text(svg)
        write_atlas(out, store, atlas, str(out / "store-map.svg"))
    finally:
        await client.close()
    return atlas


def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit(__doc__)
    store = sys.argv[1]
    try:
        atlas = asyncio.run(capture(store))
    except (HEBConnectionError, ValueError) as error:
        raise SystemExit(f"capture failed: {error}")
    aisles = [name for name in atlas["geometry"]["anchors"]
              if name.startswith("AISLE ")]
    print(f"    {len(atlas['psas'])} PSA points, {len(aisles)} aisle labels "
          f"-> data/{store}-atlas/")
    print(f"    next: python3 calibrate.py {store}")


if __name__ == "__main__":
    main()
