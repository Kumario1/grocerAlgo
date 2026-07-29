#!/usr/bin/env python3
"""Push local verified H-E-B states to PROD for stores that need them.

Env:
  GROCER_PROD_URL      e.g. https://your-service.up.railway.app
  GROCER_ADMIN_TOKEN   admin bearer token on PROD (and used if LOCAL needs it)

Skips a store when PROD /api/heb/status already reports connected + map_ready.
Stores with no local verified state are reported and counted as failures.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from router.calibrate import catalog_store_ids, is_catalog_enabled
from router.heb import HEBClient, HEBConnectionError


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def prod_healthy(client: httpx.Client, store: int) -> bool:
    response = client.get("/api/heb/status", params={"store": store})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    body = response.json()
    return bool(body.get("connected") and body.get("map_ready"))


def wait_for_catalog(client: httpx.Client, store: int, timeout: float = 600):
    """Railway rebuild: wait until the store is listed and catalog-enabled."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/stores")
            response.raise_for_status()
            listed = {
                s["id"]: s for s in response.json().get("stores", [])
            }
            row = listed.get(str(store))
            if row and row.get("catalog_enabled"):
                return
            last = row
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(10)
    raise SystemExit(
        f"PROD never listed store {store} as catalog_enabled "
        f"(last={last!r})"
    )


def push_one(
    heb: HEBClient,
    prod: httpx.Client,
    store: int,
    token: str,
) -> str:
    if prod_healthy(prod, store):
        return "skip"
    try:
        state = heb.export_state(store)
    except HEBConnectionError as exc:
        raise HEBConnectionError(
            f"no local verified state for #{store}: {exc}") from exc
    response = prod.put(
        "/api/heb/state",
        params={"store": store},
        headers=_auth_headers(token),
        json=state,
        timeout=120,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise HEBConnectionError(
            f"PROD import #{store} HTTP {response.status_code}: {detail}")
    return "ok"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stores", nargs="*", type=int,
        help="store ids (default: every catalog-enabled store)")
    parser.add_argument(
        "--wait-catalog", action="store_true",
        help="poll PROD /api/stores until each id is catalog_enabled")
    parser.add_argument(
        "--wait-timeout", type=float, default=600,
        help="seconds to wait for catalog (default 600)")
    args = parser.parse_args(argv)

    prod_url = os.environ.get("GROCER_PROD_URL", "").rstrip("/")
    token = os.environ.get("GROCER_ADMIN_TOKEN", "")
    if not prod_url or not token:
        raise SystemExit(
            "GROCER_PROD_URL and GROCER_ADMIN_TOKEN are required")

    stores = args.stores or list(catalog_store_ids())
    if not stores:
        raise SystemExit("no catalog-enabled stores to push")
    bad = [s for s in stores if not is_catalog_enabled(s)]
    if bad:
        raise SystemExit(f"not catalog-enabled: {bad}")

    heb = HEBClient(stores[0])
    failed = []
    with httpx.Client(base_url=prod_url, timeout=60) as prod:
        for store in stores:
            try:
                if args.wait_catalog:
                    wait_for_catalog(prod, store, args.wait_timeout)
                result = push_one(heb, prod, store, token)
                print(f"push {store}: {result}")
            except (HEBConnectionError, httpx.HTTPError, SystemExit) as exc:
                print(f"push {store}: FAIL — {exc}", file=sys.stderr)
                failed.append(store)
    if failed:
        print(f"push_heb_states: {len(failed)} failed: {failed}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
