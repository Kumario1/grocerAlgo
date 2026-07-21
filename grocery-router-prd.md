# In-Store Route Optimizer — PRD & Technical Plan

**Version:** 0.9 (draft) · **Date:** July 20, 2026 · **Owner:** RAM
**Status of prototype:** Routing engine proven end-to-end on a real store floor plan (41/41 aisle markers auto-detected; optimal 11-item route computed and rendered; ~48% walking reduction vs average unordered list).

---

## 1. Summary

A mobile-first app that turns a grocery list into the **provably shortest walking path** through a specific store: entrance → every item → checkout. The user picks their store, enters their list, and gets a numbered route drawn on the store map that re-optimizes live as they check items off.

Two hard problems, one easy problem:

1. **Hard:** knowing *where each item is* in *each specific store* (data acquisition).
2. **Hard:** turning a *map image* into a *routable graph* (map ingestion).
3. **Easy (solved):** computing the optimal route (fixed-endpoint TSP — milliseconds at grocery-list scale).

The prototype has already solved #3 and de-risked #2. This document specifies the product and the full pipeline, with the algorithm choices as the core section (§9).

---

## 2. Problem Statement

Shoppers with a list of 15–40 items waste significant time backtracking because lists are written in recall order, not store order. Store apps (H-E-B, Kroger) will show *one item's* aisle at a time, but no mainstream product sequences the *whole trip* into a shortest path. The cost: a typical unordered list at our pilot map walks ~760 m vs ~390 m optimal — roughly double the distance, plus the cognitive load of constant "where is this?" searching. The people who feel it most: parents shopping with kids, weekly bulk shoppers in large-format stores (H-E-B Plus is 100k+ sq ft), and anyone who hates the store.

---

## 3. Goals

- **G1 — Route quality:** Median walking distance ≥30% shorter than the user's list-order route, measured on real trips in the pilot store.
- **G2 — Coverage:** ≥85% of list items auto-located (no user intervention) in the pilot store at beta; ≥95% at maturity.
- **G3 — Speed:** Route computed and rendered in <1 s p95 (<300 ms target) — fast enough to re-route on every check-off.
- **G4 — Habit:** ≥40% of beta users complete a second routed trip within 14 days (this is a weekly-cadence product; one repeat = habit signal).
- **G5 — Scalable onboarding:** A new store goes from map image to routable in <1 hour of human time.

## 4. Non-Goals (v1)

- **Real-time indoor positioning** (blue-dot tracking). Requires beacons/dead-reckoning; the numbered-stop route works without it. Revisit at v3.
- **Price comparison / deals / coupons.** Different product; adds provider load. Parking lot.
- **Online ordering or cart hand-off.** We optimize *in-store* trips; pickup/delivery users aren't the segment.
- **Chains beyond Kroger-banner + H-E-B.** Two providers prove the abstraction; more is config, later.
- **Multi-store trip splitting** ("milk here, produce there"). Interesting, but a different optimization problem and a different user promise.

---

## 5. Users & User Stories

**Persona A — The weekly shopper (primary):**
- As a weekly shopper, I want to select my store and paste my list so that I get a route without re-typing my life into a new app.
- As a weekly shopper, I want the route to update when I check off or skip items so that the plan always reflects where I actually am.
- As a weekly shopper, I want frozen items sequenced near the end so that ice cream isn't soup at checkout.
- As a weekly shopper, I want to see distance/time for the trip so that I can decide whether to bring the kids.

**Persona B — The store contributor:**
- As a user whose store isn't mapped yet, I want to upload a screenshot of the store map (e.g., from the H-E-B app) so that my store becomes routable.
- As a contributor, I want basic aisle-order routing even before the map is processed so that the app is useful on day one.

**Persona C — Returning user:**
- As a returning user, I want my store and staple items remembered so that building this week's list takes seconds.

**Edge/error stories:**
- As a shopper, when an item can't be matched to the catalog, I want to pick from the top suggestions so that one weird item doesn't break the trip.
- As a shopper, when an item's location is unknown, I want it flagged with a "likely area" guess rather than silently dropped.
- As a shopper, when I find an item in a different aisle than shown, I want a one-tap "wrong spot" report so that the data improves.

---

## 6. User Flow (v1)

1. **Pick store** — geolocate → nearby stores from provider → user confirms (persisted as default).
2. **Build list** — autocomplete against that store's catalog, or paste free text.
3. **Route** — one tap → optimal route rendered on the map: numbered stops, entrance → checkout, total distance/time.
4. **Shop** — check items off; route re-solves from the current stop; skipped items re-insert optimally.
5. **Done** — trip summary: distance walked, estimate of distance/time saved.

---

## 7. System Architecture & Pipeline

Three planes. **The scraping agent is never in the user's hot path** — that is the load-bearing architectural decision.

```
==================== OFFLINE (once per store) =========================
 map image (user screenshot / chain asset)
   -> threshold & layer separation
   -> connected-component marker detection      } §9.1
   -> digit OCR + department label OCR          }
   -> occupancy grid + connectivity validation  }
   -> human QA pass (~10 min)
   -> all-pairs anchor distance matrix (precomputed)
   => STORE PROFILE {grid, anchors, matrix, calibration}

==================== BACKGROUND (continuous) ==========================
 lookup queue (cache misses, TTL refreshes, user corrections)
   -> enrichment worker
        Kroger  : official Products API
        H-E-B   : unofficial GraphQL client -> Playwright headless agent (fallback)
   => LOCATION DB {store_id, product -> aisle/side/bay/pin, source, confidence, TTL}

==================== ONLINE (user request, <300 ms) ===================
 store select -> list input
   -> product resolution (fuzzy match, catalog cache)   §9.2
   -> location lookup (LOCATION DB only; misses queued) §9.3
   -> stop consolidation (items -> aisle-segment stops) §9.5
   -> distance matrix assembly (precomputed lookups)    §9.4
   -> TSP solve (exact <=18 stops, heuristic beyond)    §9.5
   -> path trace + render (route polyline on map)
   => route + interactive shopping mode                 §9.6
```

**Store selection & the store profile.** Everything is keyed by `(chain, store_id)` because aisle numbers are meaningless across stores. Selecting a store loads its profile: map asset, occupancy grid, anchor set, calibration, catalog/location cache pointers, provider config. If a store has **no map yet**, the app degrades to **serpentine mode** (§9.5, Level 0): items sorted by aisle number with alternating traversal — needs only aisle labels, no geometry — plus a prompt to contribute the map (Persona B).

**User-supplied maps.** Accepted inputs: H-E-B app map screenshot, chain-site map, photo of a printed directory. They enter the offline ingestion pipeline and activate after QA. Important: we never redistribute the chain's map artwork — we **render our own stylized map from the parsed geometry** (walls, fixtures, anchors), which is both cleaner UX and avoids shipping their IP.

---

## 8. Data Acquisition Strategy (the Kroger-vs-agent decision)

**Decision: both, behind one interface — and cache-first for everything.**

```
ProviderInterface
  find_stores(geo | zip)                  -> [Store]
  search_catalog(store_id, text)          -> [Product]
  locate(store_id, product)               -> LocationRecord
        {aisle?, side?, bay?, dept?, pin_xy?, source, confidence, verified_at}
```

**KrogerAdapter (official, launch-safe).** Free public developer program; OAuth2 client-credentials. Locations API powers store selection; Products API with `filter.locationId` returns price, availability, and **aisle location** per store — exactly the record we need, sanctioned. Rate limits are managed with a token bucket plus a nightly warm of the top-N SKUs per active store, so live traffic rarely touches the API at all.

**HEBAdapter (tiered, cache-first).** H-E-B has no public API, so:

- **Tier 1 — Location DB (our cache).** Hit → done, ~10 ms. This serves ~all requests after warm-up.
- **Tier 2 — background lookup job** on a miss: worker tries the reverse-engineered **GraphQL client** first (fast, brittle), falls back to a **Playwright headless agent** that sets the store on heb.com, searches the item, and extracts the aisle string — and, if present in the payload, the **map-pin coordinates**. Result written to the DB with TTL. The user sees "locating… likely Aisle 6–8" and the item resolves on the next route refresh.
- **Tier 3 — model prior + crowdsourcing.** If still unknown: category→aisle prediction (§9.8) with visible uncertainty, plus a one-tap in-store confirmation. **User confirmations write user-sourced records — this crowdsourced layer is the long-term moat**, because it makes us progressively independent of scraping.

**Why cache-first wins on every axis:**

- *Volume:* a store carries 30–50k SKUs, but the top ~3k cover ~90% of list lines (classic Pareto). Warm-up ≈ a few thousand agent lookups once per store, then a trickle of misses and TTL refreshes. Tiny traffic → low block risk, low ToS exposure.
- *Latency:* the hot path is a DB read, never a browser session.
- *Resilience:* if H-E-B changes their site and scraping breaks, the product keeps working on cached + crowdsourced data while we fix the adapter.
- *Freshness:* planograms are stable for weeks–months. TTL 60–90 days per record; a "wrong spot" report invalidates and re-queues; a spike of corrections in one store triggers a bulk refresh (planogram-drift detector).

**Legal posture.** Kroger path: fully sanctioned. H-E-B path: unofficial and ToS-gray — mitigated by minimal volume, personal-use framing, our own rendered maps (no artwork redistribution), and an explicit track to pursue an H-E-B partnership/data license once we have traction data. If risk hardens, launch posture falls back to Kroger-official + H-E-B-crowdsourced-only.

---

## 9. Algorithms (core section)

### 9.1 Map parsing & anchor extraction (CV)

- **Layer separation by ink intensity.** Grayscale thresholding exploits that map layers occupy distinct value bands (pilot map: fixtures ≈ gray 113–184, aisle markers pure black <60, department labels mid-band red). Thresholds are per-map-style config.
- **Marker detection: connected-component labeling** (`scipy.ndimage.label`, union-find) over the near-black mask, then shape filters — bbox 12–45 px, aspect 0.6–1.6, fill ratio >0.5. Result on pilot map: **41/41 markers, zero false positives.** Detected discs are erased from the obstacle mask (they sit in walkable corridors) and their centroids become aisle anchors.
- **Digit OCR** on marker crops replaces the one manual step in the prototype: Tesseract in single-word mode, or a tiny 2-conv-layer CNN trained on *synthetically rendered* digits in the map's style plus augmentation (rotation/noise/scale) — no hand labeling needed. Confidence <0.99 → QA queue.
- **Department labels:** red-channel color mask → components → OCR → fuzzy match against a canonical department vocabulary (PRODUCE, DAIRY, DELI…).
- **Occupancy grid:** obstacle mask max-pool downsampled (factor 2; 1 cell ≈ 15 cm), optional 1-cell binary dilation of obstacles as a clearance buffer.
- **Connectivity validation:** flood fill from the entrance; every anchor must be reachable, else the store is flagged for QA. Guarantees the router can never produce an impossible route.
- **Calibration:** px→meters from the median spacing of adjacent aisle markers (standard aisle pitch ≈ 2.7–3.2 m), overridable by a reviewer-entered known dimension.
- **Human-in-the-loop QA:** web canvas overlay; reviewer confirms/drag-adjusts anchors and marks entrance(s) + checkout zone. Target <10 min/store (G5).

### 9.2 Product resolution (list text → catalog items)

- **Autocomplete path:** prefix search + fuzzy ranking (trigram index; RapidFuzz `token_set_ratio`) over the store's catalog cache, blended with a popularity prior. O(1) per keystroke against an in-memory index.
- **Free-text paste path:** rule-based normalization (strip quantities/units: "2x milk 2%" → "milk 2%"), then fuzzy match; below a confidence threshold, surface top-3 candidates for one-tap confirmation instead of guessing.
- **v2:** embedding-based semantic matching (handles "something for tacos"), and an LLM pass that expands messy natural-language lists into itemized entries before matching.

### 9.3 Location snapping (location record → map coordinate)

Location records arrive in four shapes; each has a deterministic snap rule:

- **Aisle label** ("Aisle 14") → aisle anchor.
- **Aisle + side/bay** (Kroger provides side L/R and bay number) → the aisle *segment* is inferred by walking the corridor from the anchor in both directions to the walls; bay index → linear interpolation along the segment; side → small offset toward that shelf face. Buys intra-aisle ordering precision.
- **Department** ("SEAFOOD") → department anchor.
- **Map-pin (x, y)** in the chain's own map frame (if the H-E-B agent captures it) → affine transform into our map frame, estimated once per store from ≥3 point correspondences via least squares.

### 9.4 Distance computation (graph search)

- **Search on the occupancy grid.** v0 (shipped in prototype): **BFS, 4-connected, unit cost** — exact shortest paths in a Manhattan metric. v1: **8-connected Dijkstra** with √2 diagonal cost, or **A\*** with the octile-distance heuristic — ~5–8% more realistic distances, still single-digit ms per query.
- **Precomputation is the trick:** each store has only ~60 anchors, so the **all-pairs anchor distance matrix** is computed once at ingestion (60 searches, seconds) and cached in the store profile. At request time, matrix assembly for a list is **O(k²) dictionary lookups — zero graph search in the hot path** (bay-offset corrections are added analytically along the segment).
- **Path geometry for rendering:** retrace BFS parent pointers, then **line-of-sight string-pulling** (greedily skip waypoints while the straight segment stays in free space, checked with Bresenham traversal) to smooth the staircase artifacts of grid paths.

### 9.5 Route optimization (the core)

- **Formalization:** open-path TSP with **fixed terminals** — minimize Σ shortest-path distance over an ordering of stops S, with start s (entrance) and end t (checkout) pinned. Because distances are all-pairs shortest paths, the induced metric satisfies the triangle inequality, which is what makes local-search heuristics (2-opt) behave well.
- **Stop consolidation (biggest practical win):** group items by snapped aisle segment → **one TSP stop per segment**. Intra-segment pick order is decided *after* the TSP by the direction the route enters the segment (sort items by position along the walking direction). A 30-item list typically collapses to **8–14 stops**, which keeps almost every real list inside the exact-solver regime.
- **Exact solver — Held-Karp bitmask DP.** `dp[mask][j]` = cheapest path from s covering stop-set `mask` and ending at j; answer = min over j of `dp[full][j] + d(j, t)`. Complexity **O(n²·2ⁿ)** time, O(n·2ⁿ) space: n=15 → ~7M transitions, <50 ms; usable to **n ≤ 18**. Below that threshold the "shortest possible path" claim is mathematically true, not marketing.
- **Heuristic solver (n > 18):** nearest-neighbor construction + **2-opt and Or-opt** local search to convergence (typically within 2–5% of optimal on metric instances), or **Google OR-Tools** routing (cheapest-arc init + guided local search, 100–300 ms budget). We use OR-Tools here because it also gives us constraints for free.
- **Constraints:**
  - *Cold-chain last (P1):* frozen/refrigerated stops restricted to the final third of the route — hard precedence arcs in OR-Tools; in the exact DP, a state-feasibility prune (reject transitions that visit a cold stop while more than ⌈n/3⌉ warm stops remain).
  - *Crush-safe (P2):* heavy/canned before bread/eggs/chips — soft penalty term, not a hard constraint.
- **Baseline telemetry:** every solve also scores the user's original list order, so "you saved 340 m" is computed per real trip, not estimated (feeds G1).

### 9.6 Dynamic re-routing

On every check-off / skip / add: re-solve the **remaining sub-TSP** with the current position (last interacted stop) as the new fixed start. n is monotonically shrinking, so re-solves get *faster* over the trip; debounce at 300 ms. Skipped items simply re-enter the stop set and land wherever now optimal.

### 9.7 Latency budget (online path, p95 targets)

| Stage | Target |
|---|---|
| Product resolution (cached catalog, per item) | 50 ms |
| Location lookup (DB hit) | 10 ms |
| Stop consolidation + matrix assembly | 5 ms |
| TSP solve (exact, n ≤ 18) | 60 ms |
| Path trace + render payload | 30 ms |
| **Total route request** | **< 300 ms** |

Cache-miss location lookups are excluded by design — they run async (§8, Tier 2).

### 9.8 Learned components (v2 — the ML roadmap)

- **Marker-digit CNN** (synthetic training data; replaces Tesseract where map fonts get weird).
- **Category→aisle prior:** per-chain classifier (gradient boosting or multinomial logistic over product taxonomy features) predicting a probable aisle *range* for items with no location record — trained on the accumulating Location DB, served with calibrated confidence ("likely aisle 6–8"). Solves per-store cold start.
- **Trip-time model:** regression on per-category pick/dwell times + walking speed → accurate "23 min trip" estimates.
- **Floor-plan segmentation model (long-term):** vectorize arbitrary map styles (mall directories, hand sketches) to remove per-style threshold config from §9.1.

---

## 10. Data Model (sketch)

```
stores            (id, chain, name, address, geo, status, default_map_id)
map_assets        (id, store_id, source_image_ref, grid_blob, calibration_m_per_px,
                   anchors jsonb[{key:"AISLE 14"|"PRODUCE"|"ENTRANCE"|..., x, y, type}],
                   dist_matrix blob, version, qa_state)
catalog_products  (chain, product_key, name, brand, category, upc, popularity)
product_locations (store_id, product_key, aisle_label, side, bay, anchor_key, pin_xy,
                   source enum[api|graphql|agent|user|model], confidence,
                   verified_at, expires_at)
lists             (user_id, items jsonb, store_id, created_at)
trips             (list_id, route jsonb, events jsonb[checkoff|skip|correction], stats)
```

---

## 11. Requirements

### Must-Have (P0)

1. **Store selection** — geolocate → nearby stores → select & persist default.
   - *Given* location permission, *when* the app opens, *then* nearby supported stores appear within 2 s and the chosen store persists across sessions.
2. **List building with autocomplete** against the selected store's catalog.
   - *Given* a selected store, *when* the user types 3+ characters, *then* ranked catalog suggestions appear in <150 ms.
3. **Auto-location of items** — ≥85% of items in the pilot store resolve to a map position with no user action; unresolved items are flagged, never silently dropped.
4. **Optimal route** — entrance → all located items → checkout; exact solver up to 18 consolidated stops; <1 s p95 end-to-end.
   - *Given* a 25-item list in the pilot store, *when* the user taps Route, *then* a numbered route renders in <1 s and its total distance ≤ the list-order distance.
5. **Interactive shopping mode** — check-off, skip, add; route re-solves from current position in <500 ms.
6. **One working provider end-to-end** — Kroger adapter (official) fully wired: store search, catalog, aisle locations.
7. **Map render** — our own stylized map from parsed geometry with route polyline and numbered stops (no chain artwork shipped).

### Nice-to-Have (P1)

8. Free-text list paste with normalization + confirm-on-low-confidence.
9. Cold-chain-last constraint (visible as "frozen saved for the end").
10. Serpentine fallback mode for stores with aisle data but no map; map-contribution upload flow.
11. Trip summary with measured distance/time saved; "wrong spot" one-tap correction.
12. H-E-B pilot store live via cache-first agent pipeline (§8).

### Future Considerations (P2)

13. Crowdsourced location layer with contributor reputation.
14. Category→aisle prediction model for cold-start stores.
15. Multi-entrance stores; route from a chosen entrance.
16. Shared/household lists; recurring staples.

---

## 12. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| H-E-B blocks scraping / ToS action | Medium | High | Cache-first (tiny volume), crowdsourced fallback layer, Kroger-only launch posture available, partnership track once traction |
| Planogram drift → wrong aisles | Certain (slow) | Medium | TTLs, wrong-spot reports auto-invalidate, per-store drift detector triggers bulk refresh |
| Map imagery IP concerns | Low–Med | Medium | Render our own maps from parsed geometry; originals used only as ingestion input |
| Product matching errors | Medium | Medium | Confidence thresholds + top-3 confirm UI; popularity priors |
| OCR/anchor errors corrupt a store | Low | High | Connectivity validation + mandatory QA pass before activation |
| Per-store map unavailability | Medium | Medium | Serpentine mode works with aisle labels only; contributor upload flow |
| Solo-builder bandwidth | High | Medium | Phasing below; Kroger-first cuts the riskiest work out of the critical path |

---

## 13. Phased Plan

- **Phase 0 — done.** Routing engine proven on a real floor plan: marker auto-detection 41/41, occupancy grid, BFS distances, exact TSP, rendered route, 48% measured savings vs average order.
- **Phase 1 (weeks 1–3): Kroger end-to-end.** Store profiles + ingestion QA tool; digit OCR (kill the manual mapping step); Kroger adapter (stores, catalog, aisle locations); web app: pick store → list → rendered route. **Exit:** any mapped Kroger store routable by a stranger.
- **Phase 2 (weeks 4–7): H-E-B pilot + shopping mode.** One pilot store (the H-E-B near campus — same beachhead playbook as GGC, and the beta audience is already there); Location DB warm-up of top ~3k SKUs via agent; interactive check-off + re-route; 20-user beta. **Exit:** ≥85% auto-locate, ≥30% median distance reduction on real trips (G1/G2).
- **Phase 3 (weeks 8+):** corrections/crowdsourcing loop, 5 stores, cold-chain constraint, category→aisle prior, trip-time model.

**Success metrics recap** — *leading:* auto-locate rate, route latency p95, % trips with ≥80% check-offs, measured distance saved per trip; *lagging:* repeat routed trips per user per month (≥2), D30 retention, corrections-per-trip trending down.

---

## 14. Open Questions

- **[Eng spike — blocking for snap precision, not for launch]** Does H-E-B's product payload expose map-pin coordinates the agent can capture, or aisle strings only? Determines whether §9.3's affine-transform path activates.
- **[Founder/legal — before Phase 2 launch]** Acceptable posture on unofficial H-E-B access at beta scale, and when to open the partnership conversation.
- **[Founder — Phase 2]** Standalone app vs distribution through GGC's student audience (shared login, cross-promo at A&M)?
- **[Eng — non-blocking]** Custom Held-Karp + 2-opt vs OR-Tools as the single solver: OR-Tools simplifies constraints, custom keeps the dependency footprint tiny for on-device solving.
- **[Design — Phase 2]** How loud should "unknown location" items be in the route UI — separate bucket at the end vs inline with uncertainty badge?
