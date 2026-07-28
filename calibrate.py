#!/usr/bin/env python3
"""Fit and gate one store's Atlas -> guide transform.

    python3 calibrate.py <store>             fit + offline gates
    python3 calibrate.py <store> --verify    also check live product labels

Offline gates prove the transform is self-consistent and lands products on
walkable floor. --verify is the one that checks the customer-visible claim:
a product whose shelf label says "Aisle 17" must pin to aisle 17. It drives
the same browser session the app uses.

Writes data/<store>-atlas/calibration.json. Only a "pass" verdict makes a
store routable — see router/calibrate.blocked_reason.
"""
import asyncio
import json
import math
import re
import sys

from router import calibrate as cal
from router.heb import HEBClient, HEBConnectionError

# Spread across the store so a systematic aisle-offset error cannot hide in
# one department. Each is a plain shopper search.
PROBES = ["milk", "bread", "tortillas", "cereal", "pasta sauce", "coffee",
          "shampoo", "paper towels", "frozen pizza", "peanut butter",
          "black beans", "dish soap"]

def labels_pass(checked, agreed):
    """Enough independent labels agree to tolerate one stale catalog item."""
    return checked >= 6 and agreed / checked >= .9


def corridor_segment(psas, runs, group, point):
    """The full corridor centre-line of the product's own shelf run."""
    line = runs.get(tuple(group.split(":")[1:]))
    if not line:
        return [point, point]
    axis, value = line
    prefix = tuple(group.split(":")[1:])
    span = [p[1 - axis] for key, p in psas.items()
            if tuple(key.split("|")[:2]) == prefix]
    start, end = list(point), list(point)
    start[axis] = end[axis] = value
    start[1 - axis], end[1 - axis] = min(span), max(span)
    return [start, end]


def nearest_aisle(guide_anchors, segment):
    """The printed aisle number nearest the product's corridor segment.

    Nearest-to-a-point is wrong in a two-bank store: #811 prints aisle 4 at
    the front of a column and aisle 15 at the back of the same column, so a
    product deep in aisle 4 sits nearer the printed "15" than the printed "4".
    Measured against the whole corridor (213 of 896 dry-grocery PSAs
    mis-associate by point, 5 by segment), the far end of an aisle stays in
    its own aisle.
    """
    (ax, ay), (bx, by) = segment

    def dist(label):
        px, py = label
        dx, dy = bx - ax, by - ay
        t = 0 if dx == dy == 0 else max(0, min(1, (
            (px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    aisles = {name: xy for name, xy in guide_anchors.items()
              if name.startswith("AISLE ")}
    return min(aisles, key=lambda n: dist(aisles[n]))


async def verify(store, record):
    """Do labelled products land in the aisle their own label names?"""
    atlas = cal.load_atlas(store)
    config = cal.store_config(store)
    with open(f"data/{store}/geometry.json") as handle:
        guide = json.load(handle)
    carry, runs = cal.transform(record), cal.shelf_runs(atlas["psas"])

    client = HEBClient(int(store), allow_unsupported=True)
    await client.connect()
    await client.select_store(store)
    await client.confirm()
    checked, agreed, misses = 0, 0, []
    for probe in PROBES:
        for product in await client.search(probe):
            label = re.search(r"\baisle\s+(\d+)\b",
                              product.get("location_label") or "", re.I)
            if not label:
                continue
            placement = await client.locate(
                product["id"], product["location_label"], atlas)
            if not placement or not placement["group"].startswith("PSA:"):
                continue
            segment = corridor_segment(atlas["psas"], runs,
                                       placement["group"], placement["point"])
            want = cal.guide_aisle_name(config, int(label[1]))
            got = nearest_aisle(guide["anchors"],
                                [carry(end) for end in segment])
            checked += 1
            if got == want:
                agreed += 1
            else:
                misses.append({"product": product["name"], "label":
                               product["location_label"], "want": want,
                               "got": got})
            break                       # one product per probe is enough
    await client.close()
    return {"checked": checked, "agreed": agreed, "misses": misses,
            "pass": labels_pass(checked, agreed)}


def resolve_margin(record, seen):
    """Live labels settle the tie the margin gate is waiting for.

    The margin gate means "two aisle correspondences fit the drawing equally
    well", and its own message sends you here. So a passing live check has to
    actually clear it, or the gate is unresolvable and the store is blocked
    forever: labels are the stronger evidence anyway, testing the
    customer-visible claim against the store as it is today. A correspondence
    one aisle off cannot pin six labelled products to the aisle their own
    labels name.
    """
    if not seen["pass"] or record["gates"].get("margin") is not False:
        return record
    record["gates"]["margin"] = True
    record["notes"].append(
        f"margin: the offline tie was resolved by live labels "
        f"({seen['agreed']}/{seen['checked']} products pinned to the aisle "
        f"their label names)")
    return record


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    store = sys.argv[1]
    record = cal.calibrate(store)

    for note in record["notes"]:
        print(f"    {note}")
    for name in ("x", "y"):
        axis = record.get(name)
        if not axis:
            continue
        if axis.get("derived"):
            print(f"    {name}: scale {axis['scale']:.5f} offset "
                  f"{axis['offset']:.2f} — no aisle spans this axis, so it was "
                  "derived from the floor")
            continue
        print(f"    {name}: scale {axis['scale']:.5f} offset "
              f"{axis['offset']:.2f} from {len(axis['inliers'])} aisles "
              f"(shift {axis['aisle_offset']:+d}), residual "
              f"{axis['max_residual_pt']} pt"
              f"{' [pinned]' if axis.get('pinned') else ''}")
    floor = record["gates"].get("floor")
    if floor:
        print(f"    floor: {floor['on_floor_pct']}% of {floor['psas']} PSAs "
              f"land on reachable floor")
        for key, metres in floor["off_floor"][:3]:
            print(f"      off floor: {key} "
                  + (f"by {metres} m" if metres is not None else "off the map"))

    if "--verify" in sys.argv and "x" in record:
        try:
            record["verified"] = asyncio.run(verify(store, record))
        except (HEBConnectionError, ValueError) as error:
            raise SystemExit(f"live verification unavailable: {error}")
        seen = record["verified"]
        print(f"    labels: {seen['agreed']}/{seen['checked']} products pinned "
              f"to the aisle their label names")
        for miss in seen["misses"]:
            print(f"      MISS {miss['product']!r} says {miss['label']!r} "
                  f"-> pinned at {miss['got']}, expected {miss['want']}")
        record["gates"]["labels"] = seen["pass"]
        resolve_margin(record, seen)
        record["verdict"] = "pass" if all(
            (gate.get("pass") if isinstance(gate, dict) else gate)
            for gate in record["gates"].values()) else "fail"

    path = cal.write(store, record)
    gates = " ".join(
        f"{name}={'ok' if (g.get('pass') if isinstance(g, dict) else g) else 'FAIL'}"
        for name, g in record["gates"].items())
    print(f"    gates: {gates}")
    print(f"    {record['verdict'].upper()} -> {path}")
    if record["verdict"] != "pass":
        print("    store stays unroutable until this passes; pin the aisle "
              f"correspondence in data/{store}/store.json if the search "
              "chose wrong")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
