#!/usr/bin/env python3
"""Sweep the CDN for every published store guide and write the fleet work list.

Usage: python3 sweep_stores.py [first] [last]      default 1 999

For each store number in range: probe discover.CITIES for a live guide URL,
then download + validate + preflight the hit (discover.py owns all three).
Stores this checkout already onboarded are skipped. Output is stores.txt,
one "store city" line each, fresh guides first and stale-risk guides (see
discover.preflight — the 388 tells) last, so early fleet capacity goes to
stores users can actually route in.

Rerunnable: discover treats a local guide as final, so a rerun only probes
and downloads the gaps.
"""
import concurrent.futures
import json
import os
import sys

import discover


def find(store):
    for slug in discover.CITIES:
        if discover.probe(f"{discover.BASE}/guide-{slug}-{store}.pdf"):
            return store, slug
    return store, None


def main(first, last):
    print(f"probing stores {first}-{last} across {len(discover.CITIES)} "
          f"city slugs (HEAD requests only)...")
    with concurrent.futures.ThreadPoolExecutor(32) as pool:
        hits = [(s, c) for s, c in pool.map(find, range(first, last + 1)) if c]
    print(f"{len(hits)} guides published on the CDN")

    rows, failed = [], []
    for store, slug in hits:
        # walk_truth.json is the one truth file every onboarded store carries
        if os.path.exists(f"data/{store}/walk_truth.json"):
            continue                        # already onboarded
        try:
            discover.discover(store, slug)  # download + validate + preflight
        except SystemExit as err:           # validation complaint: needs eyes
            failed.append((store, slug, str(err)))
            continue
        with open(f"data/{store}/source.json") as f:
            stale = json.load(f).get("stale_risk", False)
        rows.append((stale, store, slug))

    rows.sort()                             # fresh first, stale-risk last
    with open("stores.txt", "w") as out:
        for _, store, slug in rows:
            out.write(f"{store} {slug}\n")
    print(f"stores.txt: {len(rows)} stores to onboard, "
          f"{sum(1 for stale, *_ in rows if stale)} stale-risk at the end")
    for store, slug, err in failed:
        print(f"SKIPPED {store} ({slug}): {err}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1,
         int(sys.argv[2]) if len(sys.argv) > 2 else 999)
