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
import re
import sys

from router import calibrate as cal
from router.calibrate import (aisle_agreement, corridor_segment,
                              label_fits_segment, nearest_aisle)
from router.heb import HEBClient, HEBConnectionError

# Spread across the store so a systematic aisle-offset error cannot hide in
# one department. Each is a plain shopper search.
PROBES = ["milk", "bread", "tortillas", "cereal", "pasta sauce", "coffee",
          "shampoo", "paper towels", "frozen pizza", "peanut butter",
          "black beans", "dish soap"]

def labels_pass(checked, agreed):
    """Enough independent labels agree to tolerate one stale catalog item."""
    return checked >= 6 and agreed / checked >= .9


async def verify(store, record):
    """Do labelled products land in the aisle their own label names?"""
    atlas = cal.load_atlas(store)
    config = cal.store_config(store)
    with open(f"data/{store}/geometry.json") as handle:
        guide = json.load(handle)
    carry, runs = cal.transform(record), cal.shelf_runs(atlas["psas"])

    client = HEBClient(int(store), allow_unsupported=True)
    try:
        await client.connect()
        await client.select_store(store)
        await client.confirm()
        checked, agreed, misses = 0, 0, []
        seen = set()
        for probe in PROBES:
            for product in await client.search(probe):
                label = re.search(r"\baisle\s+(\d+)\b",
                                  product.get("location_label") or "", re.I)
                if not label or product["id"] in seen:
                    continue
                seen.add(product["id"])
                placement = await client.locate(
                    product["id"], product["location_label"], atlas)
                if (not placement or placement.get("approx")
                        or not placement["group"].startswith("PSA:")):
                    continue
                segment = corridor_segment(atlas["psas"], runs,
                                           placement["group"], placement["point"])
                want = cal.guide_aisle_name(config, int(label[1]))
                carried = [carry(end) for end in segment]
                got, agreed_here = aisle_agreement(
                    guide["anchors"], want, carried)
                checked += 1
                if agreed_here:
                    agreed += 1
                else:
                    misses.append({"product": product["name"], "label":
                                   product["location_label"], "want": want,
                                   "got": got})
                break                       # one product per probe is enough
    finally:
        await client.close()
    return {"checked": checked, "agreed": agreed, "misses": misses,
            "pass": labels_pass(checked, agreed)}


def resolve_live_gates(record, seen):
    """Live labels settle an offline tie or a small non-retail PSA tail.

    The margin gate means "two aisle correspondences fit the drawing equally
    well", and its own message sends you here. So a passing live check has to
    actually clear it, or the gate is unresolvable and the store is blocked
    forever: labels are the stronger evidence anyway, testing the
    customer-visible claim against the store as it is today. A correspondence
    one aisle off cannot pin six labelled products to the aisle their own
    labels name.
    """
    if not seen["pass"]:
        return record
    if record["gates"].get("margin") is False:
        record["gates"]["margin"] = True
        record["notes"].append(
            f"margin: the offline tie was resolved by live labels "
            f"({seen['agreed']}/{seen['checked']} products pinned to the aisle "
            f"their label names)")
    floor = record["gates"].get("floor")
    if (isinstance(floor, dict) and not floor.get("pass")
            and floor.get("on_floor_pct", 0) >=
            100 * cal.LIVE_MIN_ON_FLOOR):
        floor["pass"] = True
        record["notes"].append(
            f"floor: {floor['on_floor_pct']}% plus live label agreement "
            f"({seen['agreed']}/{seen['checked']}) excludes the small "
            "non-retail Atlas tail")
    return record


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    store = sys.argv[1]
    record = cal.calibrate(store)
    offline_ok = record["verdict"] == "pass"
    # Offline success is only a candidate. Persist this before browser work:
    # failure or interruption must never leave a fresh fit publicly enabled.
    if offline_ok:
        record["verdict"] = "pending"
    cal.write(store, record)

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
        resolve_live_gates(record, seen)
        record["verdict"] = "pass" if all(
            (gate.get("pass") if isinstance(gate, dict) else gate)
            for gate in record["gates"].values()) else "fail"

    path = cal.write(store, record)
    gates = " ".join(
        f"{name}={'ok' if (g.get('pass') if isinstance(g, dict) else g) else 'FAIL'}"
        for name, g in record["gates"].items())
    print(f"    gates: {gates}")
    print(f"    {record['verdict'].upper()} -> {path}")
    if "--verify" not in sys.argv and offline_ok:
        print(f"    offline gates passed; run calibrate.py {store} --verify "
              "before enabling this store")
        return 0
    if record["verdict"] != "pass":
        print("    store stays unroutable until this passes; pin the aisle "
              f"correspondence in data/{store}/store.json if the search "
              "chose wrong")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
