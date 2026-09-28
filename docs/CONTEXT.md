# grocerAlgo domain context

This local pilot routes a shopper through one H‑E‑B store at a time, chosen
from the stores the pipeline has onboarded. These terms are canonical in code,
tests, UI copy, and future planning.

## Store

An onboarded H‑E‑B store: a converged walkable map under `data/<store>/`, plus
whatever per-store truth it needs in `data/<store>/store.json`. A Store is
*routable* only when it also has a captured Atlas and a Calibration that
passed. A Store that is mapped but not calibrated is offered nowhere and says
what it is missing.

Each request targets one Store. H‑E‑B browser contexts are isolated per store;
uncached navigation is serialized through one browser queue.

## Calibration

The scale-and-offset per axis that carries a Store's Atlas coordinates onto its
guide, together with the evidence that it is right: which aisles corresponded,
the residual, and the gates it passed. A Calibration is not believed because it
was computed — it is believed because it survived being tested against the
store's floor and against live shelf labels.

The calibration CLI saves an offline fit as pending. A new fit becomes passing
only after live verification; a browser failure leaves it unavailable. Historical
passing records without live evidence still need re-verification.

## Catalog Product

A real, store-specific product returned by H‑E‑B search. It has an H‑E‑B
product ID, display name, brand, size, image, inventory state, and displayed
store location. A Catalog Product is not free text, a generic grocery
category, or a quantity.

Identity is `(store_id, product_id)`. Out-of-stock Catalog Products may be
shown but cannot be selected.

## List Entry

One selected Catalog Product plus the quantity the shopper wants. Selecting
the same Catalog Product again increments its existing List Entry rather than
creating a duplicate.

A quantity change does not alter route geometry. Adding or removing a List
Entry does.

## Placement

The store-specific map position resolved for a Catalog Product. A Placement
records its displayed H‑E‑B location, customer-reachable route cell, grouping
key, and its state: **exact** or **department**.

A Placement is exact when H‑E‑B returned a non-approximate PSA — a spot on a
specific shelf face — for this product, the Store's Calibration passed its
gates, the transformed point lands within 5 m of reachable floor, and any
numbered aisle label agrees with the transformed shelf run. It is
carried through the Calibration, moved onto the corridor midline of its own
shelf run, and snapped to the nearest entrance-reachable customer cell.

A Placement is department-level when H‑E‑B gave approximate geometry or only
an area — "In Produce" — or when a PSA conflicts with the aisle label or lands
off the shopping floor. Its named aisle/department is drawn as an approximate
area. Without a usable named fallback, the product stays unrouted. No
Calibration can turn approximate source information into an exact Placement.

A product with no defensible position has no Placement and is visibly
unrouted.

## Route Stop

One customer visit used by the route solver. Catalog Products sharing a PALS
area/aisle or fallback anchor are consolidated into one solver Route Stop,
while every List Entry remains a separately numbered visible pick.

The route runs from the entrance through every routable Route Stop to
checkout. Unrouted List Entries remain outside the path in an explicit
collection; they are never silently dropped.
