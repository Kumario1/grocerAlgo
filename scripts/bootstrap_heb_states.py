#!/usr/bin/env python3
"""Bootstrap anonymous H-E-B browser states for catalog-enabled stores.

For each store: skip if local runtime already has a verified state; otherwise
connect → select_store (cookies) → confirm. Failures leave the store for retry.

    python3 -m scripts.bootstrap_heb_states           # all catalog stores
    python3 -m scripts.bootstrap_heb_states 24 659    # selected ids
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from router.calibrate import catalog_store_ids, is_catalog_enabled
from router.heb import HEBClient, HEBConnectionError


async def bootstrap_one(client: HEBClient, store: int) -> str:
    status = client.status(store)
    if status["connected"] and status["map_ready"]:
        return "skip"
    await client.connect(store, fresh=True)
    await client.select_store(store)
    await client.confirm(store)
    return "ok"


async def run(stores: list[int]) -> int:
    client = HEBClient(stores[0] if stores else 659)
    failed = []
    try:
        for store in stores:
            try:
                result = await bootstrap_one(client, store)
                print(f"bootstrap {store}: {result}")
            except (HEBConnectionError, ValueError) as exc:
                print(f"bootstrap {store}: FAIL — {exc}", file=sys.stderr)
                failed.append(store)
    finally:
        await client.close()
    if failed:
        print(
            f"bootstrap_heb_states: {len(failed)} failed: {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stores", nargs="*", type=int,
        help="store ids (default: every catalog-enabled store)")
    args = parser.parse_args(argv)
    stores = args.stores or list(catalog_store_ids())
    if not stores:
        raise SystemExit("no catalog-enabled stores to bootstrap")
    bad = [s for s in stores if not is_catalog_enabled(s)]
    if bad:
        raise SystemExit(f"not catalog-enabled: {bad}")
    return asyncio.run(run(stores))


if __name__ == "__main__":
    raise SystemExit(main())
