# grocerAlgo domain context

This local pilot routes a shopper through Lakeline H‑E‑B Plus! corporate
store #659. These terms are canonical in code, tests, UI copy, and future
planning.

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
key, and whether it is exact or approximate.

Placement resolution descends from exact PALS-to-PSA, to approximate PALS,
to aisle/department anchors. A product with no defensible position has no
Placement and is visibly unrouted.

The displayed route uses the exact 2018 #659 guide profile. Current Atlas PSA
coordinates are carried through a calibrated current-to-guide transform, then
snapped to the nearest entrance-reachable customer cell. Fallback placements
use aisle/department anchors. Because the two maps are different vintages,
all cross-version placements remain visibly approximate.

## Route Stop

One customer visit used by the route solver. Catalog Products sharing a PALS
area/aisle or fallback anchor are consolidated into one solver Route Stop,
while every List Entry remains a separately numbered visible pick.

The route runs from the entrance through every routable Route Stop to
checkout. Unrouted List Entries remain outside the path in an explicit
collection; they are never silently dropped.
