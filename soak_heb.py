#!/usr/bin/env python3
"""Exercise the deployed six-store H-E-B catalog and its restart persistence."""
import argparse
import json
import os
import sys
import time

import httpx

from router.heb import SUPPORTED_STORES

QUERIES = (
    "milk", "eggs", "bread", "bananas", "chicken", "rice", "coffee",
    "cereal", "cheese", "yogurt", "apples", "tortillas", "water", "pasta",
    "butter", "bacon", "oranges", "potatoes", "tomatoes", "onions",
)


def accepted(stats, instances, expected_restarts=0, recovered=()):
    return (
        len(instances) >= expected_restarts + 1
        and len(recovered) >= expected_restarts
        and all(
            values["search_ok"] / max(1, values["searches"]) >= .95
            and values["located"] >= 5
            for values in stats.values()
        )
    )


def call(client, method, path, **kwargs):
    for attempt in range(3):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.RequestError:
            if attempt == 2:
                raise
            time.sleep(1)
            continue
        if response.status_code != 429:
            return response
        time.sleep(float(response.headers.get("Retry-After", 1)))
    return response


def probe_store(client, store, stats, products):
    values = stats[str(store)]
    for query in QUERIES:
        values["searches"] += 1
        response = call(
            client, "GET", "/api/products",
            params={"store": store, "q": query})
        if response.is_success and response.json().get("products"):
            values["search_ok"] += 1
            for product in response.json()["products"]:
                products.setdefault(product["id"], product)
        else:
            values["failures"].append(
                f"search {query!r}: HTTP {response.status_code}")
    picked = list(products.values())[:5]
    if len(picked) < 5:
        values["failures"].append("fewer than five products returned")
        return
    response = call(
        client, "POST", "/api/products/locate",
        params={"store": store}, json={"products": picked})
    if response.is_success:
        values["located"] = sum(
            bool(product.get("routable"))
            for product in response.json().get("products", []))
        if values["located"] < 5:
            values["failures"].append("fewer than five routable placements")
    else:
        values["failures"].append(
            f"locate: HTTP {response.status_code}")


def recovered_after_restart(client, stores, headers):
    for store in stores:
        try:
            response = call(
                client, "GET", "/api/heb/recovery",
                params={"store": store}, headers=headers)
        except httpx.RequestError:
            return False
        if not response.is_success:
            return False
        status = response.json()
        cache = status.get("cache", {})
        if (not status.get("connected") or not status.get("map_ready")
                or cache.get("search", 0) < 1
                or cache.get("placement", 0) < 5
                or cache.get("located", 0) < 5):
            return False
    return True


def prime_recovery_cache(client, stores):
    query = f"deployment-restart-{time.time_ns()}"
    return all(
        call(
            client, "GET", "/api/products",
            params={"store": store, "q": query}).is_success
        for store in stores
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--interval", type=float, default=900)
    parser.add_argument("--expected-restarts", type=int, default=0)
    parser.add_argument(
        "--admin-token", default=os.environ.get("GROCER_ADMIN_TOKEN"))
    args = parser.parse_args()
    if args.expected_restarts and not args.admin_token:
        parser.error("--admin-token is required when checking restarts")
    stats = {
        str(store): {
            "searches": 0, "search_ok": 0, "located": 0, "failures": [],
        }
        for store in SUPPORTED_STORES
    }
    instances = set()
    recovered = set()
    deadline = time.monotonic() + args.hours * 3600
    headers = (
        {"Authorization": f"Bearer {args.admin_token}"}
        if args.admin_token else {})

    with httpx.Client(base_url=args.base_url, timeout=45) as client:
        health = call(client, "GET", "/api/health")
        health.raise_for_status()
        instances.add(health.json()["instance"])
        initial_instance = health.json()["instance"]
        for store in SUPPORTED_STORES:
            probe_store(client, store, stats, {})
        if args.expected_restarts and not prime_recovery_cache(
                client, SUPPORTED_STORES):
            raise RuntimeError("could not prime restart recovery cache")
        if args.expected_restarts:
            print(
                "Initial pass complete; restart the container twice within "
                "five minutes.",
                file=sys.stderr, flush=True)
        next_probe = time.monotonic() + args.interval
        while time.monotonic() < deadline:
            time.sleep(min(15, max(0, deadline - time.monotonic())))
            try:
                health = call(client, "GET", "/api/health")
            except httpx.RequestError:
                continue
            if health.is_success:
                instance = health.json()["instance"]
                if (instance != initial_instance and instance not in recovered
                        and recovered_after_restart(
                            client, SUPPORTED_STORES, headers)):
                    recovered.add(instance)
                instances.add(instance)
            if time.monotonic() < next_probe:
                continue
            for index, store in enumerate(SUPPORTED_STORES):
                query = QUERIES[
                    (stats[str(store)]["searches"] + index) % len(QUERIES)]
                values = stats[str(store)]
                values["searches"] += 1
                response = call(
                    client, "GET", "/api/products",
                    params={"store": store, "q": query})
                if response.is_success and response.json().get("products"):
                    values["search_ok"] += 1
                else:
                    values["failures"].append(
                        f"soak search {query!r}: HTTP {response.status_code}")
            next_probe = time.monotonic() + args.interval

    report = {
        "instances": sorted(instances),
        "recovered_instances": sorted(recovered),
        "expected_restarts": args.expected_restarts,
        "stores": stats,
    }
    print(json.dumps(report, indent=2))
    raise SystemExit(
        0 if accepted(
            stats, instances, args.expected_restarts, recovered) else 1)


if __name__ == "__main__":
    main()
