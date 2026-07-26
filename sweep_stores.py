#!/usr/bin/env python3
"""Build the fleet work list: every published store guide -> stores.txt.

Usage: python3 sweep_stores.py [first] [last]      default 1 999

Sources, best first:
  1. storelist/heb_texas_store_numbers_and_cities.csv — the real store
     directory (store number + city). One targeted request per store.
  2. Blind probe of discover.CITIES across the store-number range — the
     pre-directory fallback, paced so it cannot repeat the greedy sweep
     that got this IP blocked by Akamai for a few hours on 2026-07-25.

Each store then goes through discover.py (download + validate + preflight;
a local guide wins without a request). Already-onboarded stores are
skipped. stores.txt orders fresh guides first, stale-risk (the 388 tells)
last, so early fleet capacity goes to stores users can actually route in.
stores.txt is never written while the control URL fails to probe — a blind
sweep must not masquerade as an empty fleet.
"""
import concurrent.futures
import csv
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

import discover

CONTROL = f"{discover.BASE}/guide-austin-659.pdf"   # known live since day one
DIRECTORY = "storelist/heb_texas_store_numbers_and_cities.csv"


def probe(url, tries=3):
    """A 404 is a miss; anything else is retried with backoff and finally
    returns None — unknown is not the same answer as absent."""
    req = urllib.request.Request(url, method="HEAD")
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return (r.status == 200
                        and "pdf" in r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError:
            return False
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


def find(store):
    """Blind fallback: (store, city) on a hit, (store, None) on a clean
    miss, (store, "?") when throttling left the answer unknown."""
    local = glob.glob(f"guide-*-{store}.pdf")
    if local:
        name = os.path.basename(local[0])
        return store, name[len("guide-"):-len(f"-{store}.pdf")]
    unknown = False
    for slug in discover.CITIES:
        time.sleep(0.05)  # ponytail: crude rate cap, Akamai holds grudges
        hit = probe(f"{discover.BASE}/guide-{slug}-{store}.pdf")
        if hit:
            return store, slug
        if hit is None:
            unknown = True
    return store, ("?" if unknown else None)


def targets(first, last):
    """(store, city-slug) pairs to fetch, and the unresolved leftovers."""
    if os.path.exists(DIRECTORY):
        with open(DIRECTORY) as f:
            rows = [(int(r["store_number"]), discover.city_slug(r["city"]))
                    for r in csv.DictReader(f)]
        rows = [(s, c) for s, c in rows if first <= s <= last]
        print(f"{len(rows)} stores in {DIRECTORY}")
        return rows, []
    print(f"no {DIRECTORY} — blind-probing stores {first}-{last} across "
          f"{len(discover.CITIES)} city slugs (paced)...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        results = list(pool.map(find, range(first, last + 1)))
    hits = [(s, c) for s, c in results if c and c != "?"]
    unresolved = [s for s, c in results if c == "?"]
    print(f"{len(hits)} guides found, {len(unresolved)} unresolved")
    return hits, unresolved


def main(first, last):
    hits, unresolved = targets(first, last)

    if probe(CONTROL) is not True:
        raise SystemExit(
            "the CDN is not answering this IP right now (the control guide "
            "failed to probe) — refusing to write stores.txt off a blind "
            "sweep. Wait out the block and rerun.")

    rows, failed = [], []
    for store, slug in hits:
        # walk_truth.json is the one truth file every onboarded store carries
        if os.path.exists(f"data/{store}/walk_truth.json"):
            continue                        # already onboarded
        time.sleep(0.3)                     # pace the downloads too
        try:
            discover.discover(store, slug)  # download + validate + preflight
        except SystemExit as err:
            if slug and "no guide PDF found" in str(err):
                try:                        # directory city ≠ CDN slug: scan
                    discover.discover(store, None)
                except SystemExit as err2:
                    failed.append((store, slug, str(err2)))
                    continue
            else:                           # validation complaint: needs eyes
                failed.append((store, slug, str(err)))
                continue
        with open(f"data/{store}/source.json") as f:
            src = json.load(f)
        # the slug that actually answered, straight from the guide's name
        slug = os.path.basename(src["pdf"])[len("guide-"):-len(f"-{store}.pdf")]
        rows.append((src.get("stale_risk", False), store, slug))

    rows.sort()                             # fresh first, stale-risk last
    with open("stores.txt", "w") as out:
        for _, store, slug in rows:
            out.write(f"{store} {slug}\n")
    print(f"stores.txt: {len(rows)} stores to onboard, "
          f"{sum(1 for stale, *_ in rows if stale)} stale-risk at the end")
    for store, slug, err in failed:
        print(f"SKIPPED {store} ({slug}): {err.splitlines()[0]}")
    if unresolved:
        print(f"UNRESOLVED (rerun once unblocked): "
              f"{' '.join(map(str, unresolved))}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1,
         int(sys.argv[2]) if len(sys.argv) > 2 else 999)
