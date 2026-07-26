# In-Store Route Optimizer — Master Plan

> **Living document.** This is the single source of truth for the product. It folds the PRD, the architecture, the algorithm design, and everything we learn about provider data into one place.
>
> **How to use this doc as we go:**
> - New discoveries about how a store's data/API/site works → **§14 Provider Intelligence & Findings**, as a dated entry under that provider. Use the finding template at the top of §14.
> - When a finding changes a design decision → update the relevant section (§7–§9) *and* leave the finding entry in §14 as the paper trail.
> - Resolved open questions → move them from "open" to "resolved" in **§13** with the answer and a link to the §14 entry.
> - Every substantive edit → one line in the **§16 Changelog**.
> - Confidence tags on findings: `confirmed` (observed in live data / official docs), `strong` (multiple independent implementations agree), `inferred` (reasoned, not yet verified), `unconfirmed` (needs a test).

**Version:** 1.7 · **Last updated:** 2026-07-23 · **Owner:** RAM
**Prototype status:** #659 routes selected live H-E-B products on the current Atlas map; universal map pipeline live across 5 stores (see §15).

---

## 1. Summary

A mobile-first app that turns a grocery list into the **provably shortest walking path** through a specific store: entrance → every item → checkout. The user picks their store, enters their list, and gets a numbered route drawn on the store map that re-optimizes live as items are checked off.

Three problems; the first two are hard, the third is solved:

1. **Data — where is each item, in *this* store?** (§7, §8, §14) — the real bottleneck.
2. **Map ingestion — turn a map image into a routable graph.** (§9.1) — de-risked by the prototype.
3. **Routing — compute the optimal order.** (§9.5) — **solved**; fixed-endpoint TSP runs in milliseconds at grocery-list scale.

---

## 2. Problem Statement

Shoppers with 15–40 items waste time backtracking because lists are written in recall order, not store order. Store apps (H-E-B, Kroger) surface *one item's* aisle at a time, but nothing sequences the *whole trip* into a shortest path. Measured on our pilot map, an average unordered list walks ~760 m vs ~390 m optimal — roughly double — plus the constant cognitive load of "where is this?". Felt most by parents shopping with kids, weekly bulk shoppers in large-format stores (H-E-B Plus is 100k+ sq ft), and anyone who dislikes the store.

---

## 3. Goals & Non-Goals

### Goals
- **G1 — Route quality:** Median walking distance ≥30% shorter than the user's list-order route, measured on real trips.
- **G2 — Coverage:** ≥85% of list items auto-located (no user action) in the pilot store at beta; ≥95% at maturity.
- **G3 — Speed:** Route computed and rendered in <1 s p95 (<300 ms target) — fast enough to re-route on every check-off.
- **G4 — Habit:** ≥40% of beta users complete a second routed trip within 14 days (weekly-cadence product; one repeat = habit signal).
- **G5 — Scalable onboarding:** New store from map image → routable in <1 hour of human time.

### Non-Goals (v1)
- **Real-time indoor positioning** (blue-dot). Needs beacons/dead-reckoning; numbered-stop routing works without it. Revisit v3.
- **Price comparison / deals / coupons.** Different product; adds provider load. Parking lot.
- **Online ordering / cart hand-off.** We optimize *in-store* trips; pickup/delivery users aren't the segment.
- **Chains beyond Kroger-banner + H-E-B.** Two providers prove the abstraction; more is config, later.
- **Multi-store trip splitting.** Different optimization, different promise.

---

## 4. Users & User Stories

**Persona A — Weekly shopper (primary)**
- Select my store and paste my list → get a route without re-typing my life into a new app.
- Route updates when I check off / skip / add, so the plan always reflects where I am.
- Frozen items sequenced near the end so ice cream isn't soup at checkout.
- See distance/time for the trip so I can decide whether to bring the kids.

**Persona B — Store contributor**
- Upload a screenshot of the store map (e.g. from the H-E-B app) so my store becomes routable.
- Get basic aisle-order routing even before the map is processed, so the app is useful day one.

**Persona C — Returning user**
- My store and staples are remembered so this week's list takes seconds.

**Edge/error**
- Unmatched item → pick from top suggestions, so one weird item doesn't break the trip.
- Unknown location → item flagged with a "likely area" guess, never silently dropped.
- Item in a different aisle than shown → one-tap "wrong spot" report so the data improves.

---

## 5. User Flow (v1)

1. **Pick store** — geolocate → nearby stores from provider → confirm (persisted as default).
2. **Build list** — autocomplete against that store's catalog and select exact products. Free-text paste remains a later fallback.
3. **Route** — one tap → optimal route on the map: numbered stops, entrance → checkout, total distance/time.
4. **Shop** — check items off; route re-solves from current stop; skipped items re-insert optimally.
5. **Done** — trip summary: distance walked, estimate of distance/time saved.

---

## 6. System Architecture & Pipeline

Three planes. **The scraping agent is never in the user's hot path** — the load-bearing decision.

```
==================== OFFLINE (once per store) =========================
 map source (vector asset preferred; raster screenshot/photo fallback)
   [official directory PDF: item->aisle table -> seeds LOCATION DB  §7 Tier 0]
   -> [vector] exact shelf/label geometry               } §8.1
      [raster] adaptive threshold + palette clustering  }
   -> obstacle-mask hygiene (enclosed-fill + closing)   }
   -> connected-component marker detection (centroids)  }
   -> VLM label reading (badge digits, dept names)      }
   -> occupancy grid + corridor graph (medial axis)     }
   -> connectivity + route-legality validation          }
   -> human QA pass (~10 min)                            }
   -> all-pairs anchor distance matrix (precomputed)    }
   => STORE PROFILE {grid, corridor graph, anchors, matrix, calibration}

==================== BACKGROUND (continuous) ==========================
 lookup queue (cache misses, TTL refreshes, corrections)
   -> enrichment worker
        Kroger : official Products API                  §8, §14.2
        H-E-B  : persisted-query / SSR client
                 -> Playwright headless agent (fallback) §8, §14.1
   => LOCATION DB {store_id, product -> aisle/side/bay/dept, source, confidence, TTL}

==================== ONLINE (user request, <300 ms) ===================
 store select -> list input
   -> product resolution (fuzzy match, catalog cache)   §9.2
   -> location lookup (LOCATION DB only; misses queued) §9.3
   -> stop consolidation (items -> aisle-segment stops) §9.5
   -> distance matrix assembly (precomputed lookups)    §9.4
   -> TSP solve (exact <=18 stops, heuristic beyond)    §9.5
   -> path trace + render (route polyline on map)       §9.4, §9.6
   => route + interactive shopping mode
```

**Store selection & the store profile.** Everything is keyed by `(chain, store_id)` because aisle numbers are meaningless across stores. Selecting a store loads its profile: map asset, occupancy grid, anchor set, calibration, catalog/location cache pointers, provider config. Store with **no map yet** → degrade to **serpentine mode** (§9.5, Level 0) + prompt to contribute the map.

**User-supplied maps.** Accepted: H-E-B app screenshot, chain-site map, photo of a printed directory. They enter the §9.1 ingestion pipeline and activate after QA. We **render our own stylized map from parsed geometry** (walls, fixtures, anchors) — cleaner UX and avoids redistributing chain artwork.

**Onboarding automation (future expansion — noted 2026-07-20).** The OFFLINE plane is deliberately a **human process for now**: RAM runs it by hand for the first pilot store(s). Planned expansion: when a user selects a store we have no data for, a **headless onboarding agent** runs the entire offline pipeline autonomously — acquire a source (published directory PDF → §7 Tier 0; else vector asset; else raster map), parse it (§8.1), **self-verify in a loop** using the same checks the human QA pass applies (marker count, VLM read-back of badge digits, connectivity + route-legality checks, corridor-graph inspection, calibration sanity — §8.1), retry/adjust until checks pass or it escalates — then writes the store profile and kicks off the Location DB warm-up. Human QA becomes the escalation path, not a pipeline stage. Tracked as P2 #17.

**Update 2026-07-22: shipped for the map half.** `docs/onboarding.md` (headless-agent runbook), `docs/audit.md` (independent adversarial audit role — the onboarding agent never grades its own work), `discover.py`/`pipeline.sh` (guide-PDF acquisition), `rebuild.sh` + machine-readable `data/<N>/qa/report.json` (the self-verify loop). Human QA is now the escalation path, exactly as planned. See §13 Phase 1.5.

---

## 7. Data Acquisition Strategy

**Decision: Kroger (official) and H-E-B (unofficial) behind one interface — cache-first for everything.**

```
ProviderInterface
  find_stores(geo | zip)           -> [Store]
  search_catalog(store_id, text)   -> [Product]
  locate(store_id, product)        -> LocationRecord
        { aisle?, side?, bay?, dept?, pin_xy?, source, confidence, verified_at }
```

`LocationRecord` is a **superset** of what any one provider gives; adapters fill what they can. (What each actually fills is tracked in §14.)

### KrogerAdapter — official, launch-safe
Free public developer program, OAuth2 client-credentials. Locations API → store selection; Products API with `filter.locationId` → price, availability, and **aisle location** per store — sanctioned. Rate limits managed with a token bucket + nightly warm of top-N SKUs per active store, so live traffic rarely touches the API. **Detail: fills `aisle` + `side` + `bay` → activates bay-level snapping (§9.3).**

### HEBAdapter — directory-first, scraping-fallback, cache-first
No public API (§14.1) — but H-E-B **publishes official per-store directory PDFs** (HEB-F7/F8), which flipped the strategy on 2026-07-21: published directories are the primary item→aisle source; scraping is a fallback tier, not the plan. Tiers:
- **Tier 0 — official store directory (seed, once per store).** Parse the published directory PDF → item/category→aisle for the whole store in one pass (pilot store #659: 165 entries incl. ranges, "Left Wall", "Checkstands" — F7). Zero scraping, customer-facing artifact → minimal ToS exposure; keeps working no matter what heb.com does. Seeds the Location DB at onboarding; the map half of the same PDF feeds §8.1 vector ingestion.
- **Tier 1 — Location DB (our cache).** Hit → ~10 ms. Serves ~all traffic after seeding/warm-up.
- **Tier 2 — background lookup on miss (fallback-only since F7).** For SKU-level detail and items the directory doesn't list: persisted-query / SSR client first (fast, brittle); Playwright browser session when the WAF challenges or a session refresh is needed. The product object itself fills only the displayed location string, but the separate PALS endpoint plus Atlas PSA index yields precise store-map placement when available (HEB-F12). Result written with TTL.
- **Tier 3 — model prior + crowdsourcing.** Still unknown → category→aisle prediction (§9.8) with visible uncertainty, plus one-tap in-store confirmation. **User confirmations are the long-term moat** — they make us progressively independent of scraping *and* of directory availability.

**Why cache-first wins on every axis:** top ~3k SKUs cover ~90% of list lines (Pareto), so warm-up is a few thousand agent lookups once per store, then a trickle — and a directory-seeded store (Tier 0) already covers category→aisle wall-to-wall, shrinking agent warm-up to SKU-specific gaps. Tiny volume → low block risk + low ToS exposure. Hot path is a DB read. If scraping breaks, the product keeps running on cached + crowdsourced data. Planograms are stable for weeks–months → TTL 60–90 days; a "wrong spot" report invalidates + re-queues; a spike of corrections in one store triggers a bulk refresh (drift detector).

**Legal posture.** Kroger: fully sanctioned. H-E-B: unofficial/ToS-gray — mitigated by minimal volume, personal-use framing, our own rendered maps, and an explicit track to pursue an H-E-B partnership/data license once we have traction. If risk hardens, fall back to Kroger-official + H-E-B-crowdsourced-only.

---

## 8. Algorithms (core section)

The whole product's defensibility lives here. Listed by pipeline stage.

### 8.1 Map parsing & anchor extraction (CV) — §9.1 detail

> **Revised 2026-07-21 from design-review notes.** Two structural changes: (a) the **corridor, not the disc centroid, is the first-class geometric object** — the disc is just a label that names it; (b) obstacle-mask hygiene added to kill a **silent leak bug** that reachability validation cannot catch. Plus robustness moves for the agent-ingests-any-store future (§6).

**The model shift — aisle as segment, not point.** v0 treats a detected disc centroid as "the location of aisle 14." But the disc is a badge dropped *near* the aisle; aisle 14 is really a ~20 m corridor with two enterable ends. Collapsing it to the badge's center measures walking distance to wherever the label happens to sit, and loses the entry/exit endpoint that matters for the *next* leg (§8.3's corridor-walking was bolting the segment on after the fact). Target model: **skeletonize the free space** (medial axis / morphological thinning of the walkable region) → the centerlines of every aisle and cross-aisle as a **sparse corridor graph** — nodes at aisle ends and intersections, edges with real lengths; a few dozen nodes instead of thousands of grid cells. Each disc is assigned to its nearest skeleton edge, and that edge *becomes* "AISLE 14" with two real endpoints. What this buys: **realistic distances** (walk the centerline, not obstacle-hugging grid staircases), **instant routing + tiny profile** (Dijkstra/all-pairs over ~40 nodes vs BFS on a 15 cm grid), **inspectability** (a human or the §6 self-verifying agent can *see* a wrong corridor graph at a glance — far easier QA than a pixel mask), and **§8.3's side/bay interpolation gets its segment for free**. The geometry layer finally speaks the same language as §8.5, which already thinks in aisle segments. **Empirically confirmed on real coordinates (2026-07-21):** on the store-#659 vector map, nearest-glyph matching of aisle numbers ↔ category labels hit only 10/20, because numbers print at aisle *ends* while categories run down aisle *middles* — labels must attach to corridor edges, not nearest points (HEB-F9). Grid BFS stays as shipped v0; the corridor graph is the target abstraction.

Pipeline (vector-first; raster CV is the fallback):
- **Vector source first.** Chain store maps are very often SVG/PDF under the hood (H-E-B's almost certainly is). With a vector source, shelves and text are *exact objects* — no CV, no thresholds, no OCR. Check for/acquire the vector asset before falling back to raster CV; this can skip the entire parsing problem for the common case, and may yield both layout *and* item coordinates from one source. **Validated 2026-07-21 on the store-#659 directory PDF (HEB-F8):** 45/45 aisle numbers with exact coordinates (vs 41 CV-detected + eyeballed on the first map), every department/entrance/checkstand, 563 fixture rectangles as closed geometry — the shelf-interior leak is structurally impossible on this input, and the reconstruction-from-primitives *is* the render-our-own-map step.
- **Layer separation.** v0 shipped fixed grayscale bands per map style (pilot: fixtures ≈113–184, markers <60, departments red). Target: **adaptive/Otsu-per-tile thresholding** (survives lighting gradients in photos) + **palette clustering** (k-means in Lab space) to auto-discover which color is markers vs departments — removes most per-style config, the real scaling wall for arbitrary-map ingestion.
- **Marker detection: connected-component labeling** (`scipy.ndimage.label`, union-find) + shape filters (bbox 12–45 px, aspect 0.6–1.6, fill >0.5). **Pilot result: 41/41 markers, zero false positives.** CC provides the *precise centroid* (geometry); the *reading* comes from the VLM (below). Discs erased from the obstacle mask (they sit in corridors); each centroid → label, assigned to its nearest corridor-graph edge.
- **Label reading: VLM for semantics, classic CV for geometry.** Don't ask a VLM for pixel coordinates (they're bad at that); don't ask a threshold→blob→OCR chain for *meaning* — 20 px digits behind a 0.99 confidence gate dump everything marginal into manual QA, and a single "8 vs 3" misread silently corrupts a store. The VLM reads badge digits and department words (fuzzy-matched to the canonical vocabulary) and doubles as the **verifier in the §6 onboarding-agent loop** ("does the rendered route cross a shelf? does every badge read a plausible, unique number?"). Combine: CC centroid + VLM reading. Tesseract / synthetic-digit CNN remain fallbacks (§8.8).
- **Obstacle-mask hygiene — fixes a silent correctness bug.** Outline-drawn shelves have "walkable" white interiors; any hairline gap in an outline (anti-aliasing, JPEG artifacts, imperfect drawing) lets free space **leak** through a fixture → routes shorter than physically possible that cut through shelves. Reachability validation passes these silently — it checks that you *can* get somewhere, not that the path is *legal*. (The pilot route only looked clean because going around happened to be shortest.) Two cheap fixes, both prerequisites for trusting an automated pipeline: **(1) fill enclosed regions** — flood-fill from outside the store; any free region not connected to the aisle network becomes obstacle (do this first — a few lines, removes the whole silent-wrong-answer class); **(2) morphological closing** on the obstacle mask to seal hairline gaps so shelves are solid barriers.
- **Occupancy grid:** obstacle mask max-pool downsampled (factor 2; ~15 cm/cell), optional 1-cell dilation for clearance. Kept for v0 BFS and as the substrate the skeleton is extracted from.
- **Connectivity validation:** flood fill from the entrance; every anchor reachable or the store is flagged. Catches stranded anchors — **not leaks** (see mask hygiene above); both are needed before "the router can never produce an impossible route" is actually true.
- **Calibration:** px→m from median adjacent-marker spacing (aisle pitch ≈ 2.7–3.2 m), reviewer-overridable. **De-prioritized:** the TSP is scale-invariant — px→m error changes the "23 min" label, never the route order. Don't over-invest here.
- **Human-in-the-loop QA:** web canvas overlay; reviewer confirms/drags anchors *and the corridor graph*, marks entrance(s) + checkout. Target <10 min/store (G5). The corridor graph makes this a glance, not a pixel-mask audit.

**Revision priorities (2026-07-21):**
1. **Enclosed-region fill + morphological closing** — silent-bug fix, few lines; do regardless of everything else.
2. **Vector-source-first check** — potentially skips the whole CV problem for real chain maps. **Done for the directory-PDF class (HEB-F8, Q7 resolved).**
3. **Medial-axis corridor graph** as the geometry model — better distances, faster routing, inspectable, aligns with §8.5.
4. **Adaptive thresholding + palette clustering** — kills per-style config; prerequisite for agent-ingested arbitrary stores.
5. **VLM as reader + self-verifier** (classic CV keeps geometry) — robust OCR replacement and the engine of the agent QA loop.

### 8.2 Product resolution (list text → catalog items)
- **Autocomplete:** prefix search + fuzzy ranking (trigram index; RapidFuzz `token_set_ratio`) over the store's catalog cache, blended with a popularity prior. O(1)/keystroke against an in-memory index.
- **Free-text paste:** rule-based normalization (strip quantities/units: "2x milk 2%" → "milk 2%"), then fuzzy match; below threshold, surface top-3 for one-tap confirm instead of guessing.
- **v2:** embedding-based semantic match ("something for tacos"); LLM pass to expand messy NL lists into itemized entries before matching.

### 8.3 Location snapping (location record → map coordinate) — §9.3 detail
Records arrive in shapes; each has a deterministic snap rule. **Which shapes a provider actually returns is in §14.** Vector-ingested stores (HEB-F8) supply *exact* anchor coordinates straight from the source geometry — nothing detected, nothing to eyeball.
- **H-E-B PALS placement** → exact or approximate PSA point in the Atlas vector map; missing PALS data falls back to the displayed aisle/department anchor (HEB-F12).
- **Aisle label** ("Aisle 14") → aisle anchor.
- **Aisle + side/bay** → the aisle *segment* comes directly from the corridor graph (§8.1) — the skeleton edge the disc labels, with its two real endpoints (v0 fallback: walk the corridor from the anchor to both walls); bay index → linear interpolation along the segment; side → small offset toward that shelf face. *(Kroger — §14.2.)*
- **Department** ("SEAFOOD") → department anchor.
- **Map-pin (x, y)** in the chain's map frame → affine transform into our frame (least-squares from ≥3 correspondences, once per store). *(Not available for HEB — §14.1; keep the code path for future providers.)*

### 8.4 Distance computation (graph search) — §9.4 detail
- **Search substrate.** v0 (shipped): **BFS on the occupancy grid, 4-connected, unit cost** — exact shortest paths in a Manhattan metric. v1 option: **8-connected Dijkstra** (√2 diagonals) or **A\*** with octile heuristic — ~5–8% more realistic, still single-digit ms. Target: **Dijkstra on the medial-axis corridor graph (§8.1)** — ~40 nodes, true centerline walking lines instead of obstacle-hugging grid paths; the grid stays for anchor snapping and path rendering.
- **Precomputation is the trick:** ~60 anchors/store → **all-pairs anchor distance matrix computed once at ingestion** (60 searches, seconds), cached in the profile. Request-time matrix assembly is **O(k²) dict lookups — zero graph search in the hot path**; bay offsets added analytically. On the corridor graph this collapses further: all-pairs over a few dozen nodes is instant and the stored profile shrinks accordingly.
- **Path geometry for rendering:** retrace BFS parents, then **line-of-sight string-pulling** (greedily skip waypoints while the straight segment stays in free space, Bresenham-checked) to smooth grid staircase artifacts.

### 8.5 Route optimization (THE core) — §9.5 detail
- **Formalization:** open-path TSP with **fixed terminals** — minimize Σ shortest-path distance over an ordering of stops, start = entrance, end = checkout, both pinned. All-pairs shortest paths give a metric satisfying the triangle inequality → local search (2-opt) behaves well.
- **Stop consolidation (biggest practical win):** group items by snapped aisle segment → **one TSP stop per segment**. Intra-segment pick order decided *after* the TSP by the direction the route enters the segment. A 30-item list → **8–14 stops**, keeping almost every real list inside the exact-solver regime.
- **Exact solver — Held-Karp bitmask DP.** `dp[mask][j]` = cheapest path from start covering stop-set `mask`, ending at j; answer = min over j of `dp[full][j] + d(j, end)`. **O(n²·2ⁿ)** time, O(n·2ⁿ) space: n=15 → ~7M transitions, <50 ms; usable to **n ≤ 18**. Below that, "shortest possible path" is literally true.
- **Heuristic solver (n > 18):** nearest-neighbor + **2-opt / Or-opt** to convergence (typically within 2–5% of optimal on metric instances), or **Google OR-Tools** routing (cheapest-arc + guided local search, 100–300 ms). OR-Tools also gives constraints for free.
- **Constraints:**
  - *Cold-chain last (P1):* frozen/refrigerated stops restricted to the final third — hard precedence arcs in OR-Tools; in exact DP, a state-feasibility prune (reject visiting a cold stop while >⌈n/3⌉ warm stops remain).
  - *Crush-safe (P2):* heavy/canned before bread/eggs/chips — soft penalty, not hard.
- **Baseline telemetry:** every solve also scores the user's original list order → "you saved 340 m" is measured per real trip (feeds G1).

### 8.6 Dynamic re-routing — §9.6 detail
On every check-off / skip / add: re-solve the **remaining sub-TSP** with current position (last interacted stop) as the new fixed start. n shrinks monotonically → re-solves get faster over the trip. Debounce 300 ms. Skipped items re-enter and land wherever now optimal.

### 8.7 Latency budget (online path, p95)
| Stage | Target |
|---|---|
| Product resolution (cached catalog, per item) | 50 ms |
| Location lookup (DB hit) | 10 ms |
| Stop consolidation + matrix assembly | 5 ms |
| TSP solve (exact, n ≤ 18) | 60 ms |
| Path trace + render payload | 30 ms |
| **Total route request** | **< 300 ms** |

Cache-miss lookups excluded by design — they run async (§7, Tier 2).

### 8.8 Learned components (v2 roadmap) — §9.8 detail
- **Marker-digit CNN** (synthetic training data) — demoted to *fallback*: VLM-as-reader is now the primary label reader (§8.1); the CNN only matters if VLM cost/latency in the ingestion loop ever becomes a problem.
- **Category→aisle prior:** per-chain classifier (gradient boosting / multinomial logistic over taxonomy features) predicting a probable aisle *range* for items with no record — trained on the growing Location DB, served with calibrated confidence. Solves per-store cold start; **needed at launch for HEB** since some items return null (§14.1). **Training data is now free:** every published store directory (HEB-F7) is a labeled item/category→aisle dataset — collect a few dozen and cold-start for un-directoried stores mostly dissolves.
- **Trip-time model:** regression on per-category pick/dwell times + walking speed → accurate trip estimates.
- **Floor-plan segmentation model (long-term):** vectorize truly wild map styles (hand sketches, mall directories). Scope shrank: adaptive thresholding + palette clustering (§8.1) now removes most per-style config without a learned model; this remains only for inputs classic CV can't normalize.

---

## 9. (Reserved) Algorithm deep-dives

Detailed derivations, pseudocode, and benchmarks for each §8 subsection live here as they're written. *(Currently the operative detail is inline in §8; expand here when an algorithm needs more than a paragraph.)*

---

## 10. Data Model (sketch)

```
stores            (id, chain, name, address, geo, status, default_map_id)
map_assets        (id, store_id, source_image_ref, source_kind enum[vector|raster],
                   grid_blob, calibration_m_per_px,
                   corridor_graph jsonb{nodes[{x,y}], edges[{a,b,len,label?}]},   -- §8.1
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
1. **Store selection** — geolocate → nearby → select & persist default. *Given location permission, when the app opens, then nearby supported stores appear within 2 s and the choice persists.*
2. **List building with autocomplete** against the selected store's catalog. *3+ chars → ranked suggestions in <150 ms.*
3. **Auto-location** — ≥85% of items resolve to a map position with no user action; unresolved flagged, never dropped.
4. **Optimal route** — entrance → located items → checkout; exact solver up to 18 consolidated stops; <1 s p95. *25-item list → numbered route in <1 s, total ≤ list-order distance.*
5. **Interactive shopping mode** — check-off / skip / add; re-solve from current position in <500 ms.
6. **One working provider end-to-end** — H-E-B #659 directory-powered (Tier 0 seed, §7); Kroger adapter follows in Phase 2.
7. **Map render** — our own stylized map from parsed geometry with route polyline + numbered stops (no chain artwork shipped).

### Nice-to-Have (P1)
8. Free-text paste with normalization + confirm-on-low-confidence.
9. Cold-chain-last constraint (shown as "frozen saved for the end").
10. Serpentine fallback for stores with aisle data but no map; map-upload flow.
11. Trip summary with measured distance/time saved; "wrong spot" one-tap correction.
12. Kroger adapter end-to-end (stores, catalog, aisle/side/bay locations) — Phase 2.

### Future Considerations (P2)
13. Crowdsourced location layer with contributor reputation.
14. Category→aisle prediction model for cold-start stores.
15. Multi-entrance stores; route from a chosen entrance.
16. Shared/household lists; recurring staples.
17. **Agent-automated store onboarding** — headless agent runs the offline pipeline end-to-end (map acquisition → parse → self-verify loop → store profile → warm-up) when an unmapped store is selected. **Shipped for the map half 2026-07-22 (§6 update, §13 Phase 1.5);** remaining: Location-DB warm-up + on-demand triggering from store selection.

---

## 12. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| H-E-B blocks scraping / ToS action | Medium | Medium (was High) | Directory-first seeding (F7) makes scraping fallback-only; cache-first (tiny volume), crowdsourced fallback, Kroger-only posture available, partnership track |
| Planogram drift → wrong aisles | Certain (slow) | Medium | TTLs, wrong-spot auto-invalidate, per-store drift detector → bulk refresh |
| Map imagery IP concerns | Low–Med | Medium | Render our own maps; originals used only as ingestion input |
| Product matching errors | Medium | Medium | Confidence thresholds + top-3 confirm; popularity priors |
| OCR/anchor errors corrupt a store | Low | High | VLM read-back verification (§8.1) + connectivity validation + mandatory QA before activation |
| Mask leaks → illegal through-shelf routes (silent) | Medium | High | Enclosed-region fill + morphological closing (§8.1); route-legality check in QA/agent loop — reachability alone cannot catch this |
| Per-store map unavailability | Medium | Medium | Serpentine mode (aisle labels only); contributor upload |
| HEB buildId / query-hash rotation breaks scraper | Medium | Low (fallback-only since F7) | Auto-refresh buildId from homepage each run; hash-not-found → re-discover; cache + directory keep serving meanwhile (§14.1) |
| Directory PDFs unavailable/stale for some stores | Medium | Medium | Fall back to Tier 2 scraping + Tier 3 prior/crowdsourcing (§7); directory freshness checked against wrong-spot report rate |
| Solo-builder bandwidth | High | Medium | Phasing (§13/roadmap); HEB-first rides already-parsed #659 assets — riskiest work (data acquisition) already done for the pilot |

---

## 13. Roadmap & Open Questions

### Phased plan

> **Reordered 2026-07-21 (v1.3): H-E-B first.** Kroger-first existed to dodge H-E-B scraping risk; HEB-F7/F8 dissolved that risk (official directory + vector map, zero scraping) and the #659 assets are already parsed — the shortest path to a routable real store is now H-E-B. Kroger moves to Phase 2 as the precision tier and second-provider proof. Pilot store **resolved: Austin #659** (assets in hand); the A&M store becomes store #2 in Phase 2 so the beta still lands on the audience.

- **Phase 0 — done.** Routing engine on a real floor plan: markers 41/41, occupancy grid, BFS distances, exact TSP, rendered route, ~48% measured savings vs average order (§15).
- **Phase 1 — done (2026-07-21).** H-E-B #659 end-to-end, directory-powered, zero scraping: store profile from vector assets, directory-seeded location data (165 entries incl. ranges/multi-locations/named zones), fuzzy resolution + autocomplete over the directory, web app (`app.py`) rendering the optimal route on the rebuilt map. Exit met: stranger-usable routed map for #659. Routing runs on the v0 primitives (grid BFS + Held-Karp in `router/engine.py`).
- **Phase 1.5 — done (2026-07-21→22, unplanned pull-forward of P2 #17).** **Universal map pipeline — data, not code.** Extraction/build/QA code frozen and store-agnostic; ALL store-specific truth lives in `data/<N>/` JSON (geometry, zones, seal_zones, exclusions, inclusions, walk_truth); zones/seal-zones auto-derive from extracted labels; #659 pixel-golden regression (`golden_free.npy`). Headless-agent onboarding runbook (`docs/onboarding.md`) + independent adversarial audit role (`docs/audit.md`); coverage nets (unreachable-shelf-labels, sealed-floor-patches) added after store 24 shipped with its pharmacy wing sealed and every other signal green; `discover.py` + `pipeline.sh` as the autonomous acquisition interface (Q8: URL pattern confirmed). **Stores onboarded: #24, #388 (fragmented guide), #790 (Plano); #265 (Cedar Park, raster-scan guide) in flight** on the guarded raster-extraction fallback (`router/raster.py` — OCR, deskew, boundary discovery).
- **Phase 2 (now): the main route algorithm — #659 as base store.** Upgrade routing from the Phase-1 v0 primitives to the §8 target model. Built and tuned on #659 only, but written store-agnostic from day one — no store constant may live outside `data/659/`:
  1. **Corridor graph** (§8.1) — medial-axis skeleton of the walkable grid → sparse node/edge graph; aisle badges and department anchors assigned to *segments* (real entry/exit endpoints), not points; persisted in the store profile.
  2. **Distance layer** (§8.4) — all-pairs anchor matrix precomputed at build time into the profile; request-time assembly is dict lookups only; path trace + line-of-sight string-pulling for rendering.
  3. **Stops & solve** (§8.5) — items → aisle-segment stops (consolidation, entry-direction-aware intra-segment order); Held-Karp ≤18 stops, 2-opt beyond; fixed entrance→checkout endpoints; every solve also scores the user's list order (savings telemetry, G1).
  4. **Legality + goldens** — every rendered route validated against the free grid (no through-shelf legs — the §8.1 leak class must be impossible at the route layer too); frozen route golden for a fixed 25-item list on #659.
  **Exit:** #659 routes on the corridor model, <300 ms p95 (§8.7), route golden frozen, algorithm reads store truth exclusively from `data/659/`.
- **Phase 3: routing as data — every onboarded store routes.** Run the untouched Phase-2 algorithm across #24, #388, #790, #265. Per-store directory extraction from each guide PDF (today only #659 has `directory.csv`); per-store route goldens; discrepancies fixed by editing `data/<N>/`, never the algorithm — the Phase-1.5 onboarding discipline extended to routing. Shopping mode (check-off → re-solve remaining sub-TSP, §8.6) lands here. **Exit:** all 5 stores route end-to-end through the same code path; a new store needs only its `data/<N>/` folder.
- **Phase 4 (was Phase 2): second provider + beta.** Kroger adapter (stores, catalog, aisle/side/bay — the precision tier and second-provider proof); onboard the A&M-area store for the beta audience; Tier-2 HEB scraper only if directory coverage gaps demand it; 20-user beta. **Exit:** ≥85% auto-locate, ≥30% median distance reduction on real trips (G1/G2).
- **Phase 5 (was Phase 3):** corrections/crowdsourcing loop, cold-chain constraint, category→aisle prior, trip-time model.

**Metrics recap** — *leading:* auto-locate rate, route latency p95, % trips ≥80% checked off, measured distance saved/trip. *lagging:* repeat routed trips/user/month (≥2), D30 retention, corrections/trip trending down.

### Open questions
| # | Question | Owner | Blocking? | Status |
|---|---|---|---|---|
| Q1 | Does HEB's `productLocation` object hide any field beyond `.location` (e.g. coordinates)? | Eng | No (snap precision only) | **Resolved** → §14.1-F12: the product object exposes location text only; precise placement comes from the separate PALS endpoint joined to Atlas PSA elements |
| Q2 | Acceptable posture on unofficial HEB access at beta scale; when to open partnership talks | Founder/Legal | Before Phase 2 launch | Open |
| Q3 | Standalone app vs distribution through GGC's A&M audience (shared login, cross-promo) | Founder | Phase 2 | Open |
| Q4 | Custom Held-Karp+2-opt vs OR-Tools as the single solver (constraints vs dependency footprint / on-device) | Eng | No | Open |
| Q5 | How loud should "unknown location" items be in the route UI (end bucket vs inline badge) | Design | Phase 2 | Open |
| Q6 | Kroger `filter.locationId` — is `side`/`bay` populated for all SKUs or only some? Affects bay-snap coverage | Eng | No | Open → verify during Phase 1 |
| Q7 | Is H-E-B's in-app store map backed by a vector/structured asset (aisle geometry, maybe item pins)? If yes, vector-first ingestion (§8.1) skips raster CV entirely and may reopen the pin_xy path (HEB-F2) | Eng | No (big simplifier if yes) | **Resolved** → §14.1-F12: Atlas is a structured SVG with fixtures, landmarks, 41 aisle labels, and PSA points; PALS joins products to those points |
| Q8 | Directory PDF coverage: does H-E-B publish these for all/most stores, and at a guessable URL pattern (`guide-<city>-<store#>.pdf`)? Determines how far Tier 0 scales | Eng | No | **Largely resolved (2026-07-22)** → pattern confirmed; `discover.py` fetched guides for 5 stores (24, 265, 388, 659, 790). Variants found: fragmented guides (#388), raster-scan guides (#265 → raster fallback). Residual: coverage breadth across the full chain |

---

## 14. Provider Intelligence & Findings  *(living log)*

> **Finding template** — copy this for each new entry:
> ```
> #### [PROVIDER]-F[n] — <one-line title>   (YYYY-MM-DD, <confidence>)
> Source: <where we learned it>
> What: <the fact / shape / behavior>
> Implication: <what it changes in §7–§9, or "none">
> ```
> Keep entries append-only; if a later finding supersedes one, add a new entry and note "supersedes [PROVIDER]-Fk".

### 14.1 H-E-B  (unofficial)

**Summary as of 2026-07-23:** No public API. Data is reachable through H-E-B's browser session and is gated by Incapsula. The product object contains a store-scoped display string, while the separate PALS service plus Atlas SVG provides precise placement when available (F12).

**Update 2026-07-21:** H-E-B **publishes official per-store directory PDFs** (alphabetical item→aisle list + fully-vector floor plan). These are now the **primary** item-location source; the scraping stack above demotes to fallback (F7–F9).

#### HEB-F1 — No official developer API (2026-07-20, confirmed)
Source: web search of HEB developer surfaces; only a México-market Azure APIM portal exists (not the US grocery catalog).
What: There is no sanctioned US product/location API. All access is reverse-engineered against heb.com.
Implication: HEBAdapter is unofficial/tiered by necessity (§7). Drives the entire cache-first + crowdsourcing posture.

#### HEB-F2 — Product location is an aisle string, not coordinates (2026-07-20, strong)
Source: reading an open-source HEB client (persisted-query + Next.js SSR); two independent extraction points, plus its README and live integration tests.
What: The payload exposes a `productLocation` object whose only meaningful child is `.location`, a display string like `"Aisle 5"` or `"In Produce"`. No x/y, bay, or shelf coordinates on products. (Store lat/long exists, but that's for mapping the *building*, not items.) The field is **optional/nullable** — the client's live tests assert on nutrition/ingredients/warnings but not location, implying it's inconsistent.
Implication: §9.3 uses the **aisle-label → aisle-anchor** path as the HEB norm; the map-pin/affine path stays in code but is dead for HEB. Aisle-level is exactly the resolution the TSP needs (stop consolidation already treats an aisle as one stop), so routing quality is unaffected; we only lose intra-aisle pick ordering. Nullable location → the category→aisle prior (§9.8) and crowdsourcing are **launch-critical for HEB**, not v2 niceties. Residual: this client doesn't dump the full raw `productLocation` node, so a hidden sibling field isn't 100% ruled out (Q1) — one authenticated raw fetch closes it (set store 737, GET `product-detail/127074.json`, print the raw node).

#### HEB-F3 — Access mechanics & endpoints (2026-07-20, strong)
Source: same client's HTTP layer + config.
What:
- **Persisted GraphQL:** `POST https://www.heb.com/graphql` with `extensions.persistedQuery.sha256Hash` (query body not sent). Used for typeahead, store search, cart, coupons, and the `SelectPickupFulfillment` store-switch mutation.
- **Product detail (carries location):** `GET https://www.heb.com/_next/data/{buildId}/en/product-detail/{productId}.json` → `pageProps.product.productLocation.location`.
- **Search (also carries location):** `GET /search?q=...`, scrape `__NEXT_DATA__` JSON → `searchGridV2.items[].productLocation`.
Implication: Adapter needs both a persisted-query caller and an SSR/JSON fetcher. Store must be set (mutation) *before* fetching detail to get store-correct aisles — mechanical proof of the `(chain, store_id)` keying.

#### HEB-F4 — Two rotating secrets: buildId + query hashes (2026-07-20, confirmed)
Source: client comments + buildId extraction code.
What: The `_next/data` URLs require a `buildId` that changes every HEB deploy; it's scraped from the homepage (`/_next/static/{buildId}/_buildManifest.js`). The persisted-query `sha256Hash` values are hardcoded and "may change when HEB deploys new code."
Implication: The Tier-2 worker must (a) refresh buildId from the homepage each run/session, and (b) on `PersistedQueryNotFound`, re-discover hashes. Cache keeps serving during breakage (§12 risk row). This is the concrete brittleness behind "GraphQL-first, Playwright-fallback."

#### HEB-F5 — Incapsula/Imperva WAF + auth requirement (2026-07-20, confirmed)
Source: client's `_detect_security_challenge` + auth flow.
What: HEB fronts with Incapsula (`reese84`, `_Incapsula_Resource`, `challenge-platform`). httpx requests get challenge pages when bot detection fires. Full product search needs real browser **session cookies**; typeahead works anonymous; product-detail JSON works anonymous as a fallback but auth is preferred. Recovery = Playwright navigates heb.com, performs a search, and saves refreshed `storageState`.
Implication: Confirms the Playwright headless-agent fallback in §7 Tier 2. Session refresh is a periodic maintenance job, not per-request.

#### HEB-F6 — Useful constants for testing (2026-07-20, confirmed)
Source: client's known-stores + integration test IDs.
What: Sandbox store IDs — 737 (The Heights, Houston), 579 (Buffalo Speedway), 150 (Montrose). Stable test product IDs — olive oil `127074`, organic bananas `320228`, Clorox wipes `1904127`.
Implication: Use these to build/verify the adapter and to run the Q1 raw-payload check without guessing IDs.

#### HEB-F7 — Official published store-directory PDF: item→aisle for a whole store, zero scraping (2026-07-21, confirmed)
Source: `guide-austin-659.pdf` (in repo; also ~/Downloads) — H-E-B's own store directory for **store #659, H-E-B plus!, 14028 N US-183, Austin**; InDesign-authored, customer-facing.
What: Two halves, both gold: (a) an **alphabetical item→aisle directory** — 165 entries parsed, including the messy cases (Pet Supplies → "33 - 35", Baby Accessories → "Left Wall, 37", Wine → "1 & 2", Ice → "Checkstands", Gift Cards → "Checkstands, 19", Toothpicks → "19, 28"); (b) a floor-plan map that is **fully vector with live text** (→ F8).
Implication: **Flips §7.** Everything the scraping stack existed for (F3–F5) was answering "where is each item in this store" — the directory answers it wholesale from an official published document. Published directories become the primary item→aisle source; scraping demotes to fallback for SKU-level detail and items the directory omits. The pilot store seeds its Location DB with **zero scraping**, and the posture is far less ToS-gray (customer-facing artifact). Aisle-value parsing must handle ranges, multi-locations, and named zones ("Left Wall", "Checkstands", "Kitchen") — see the CSV. Deliverables: the 165-entry Location-DB seed, now living as `data/659/directory.csv`.

#### HEB-F8 — The directory's map is fully vector → exact extraction, no CV/OCR (2026-07-21, confirmed)
Source: same PDF, map half; extraction verified by overlay + reconstruction.
What: Extracted **exactly** rather than detected: **all 45 aisle numbers (1–45) with precise coordinates** (vs the first map's 41 CV-detected discs read by eye); every department, both entrance/exit pairs, checkstands, restrooms, carts — all with coordinates; **563 shelf/fixture rectangles as exact closed geometry**. No thresholding, no OCR gate — and the shelf-interior **leak bug (§8.1) is structurally impossible** here: fixtures are real rectangles, not pixel outlines. Overlay confirms alignment; the reconstruction rebuilds the entire store from extracted primitives alone — which *is* the §6 "render our own stylized map" step, so no H-E-B artwork ships.
Implication: §8.1's vector-source-first path is **validated in practice** for this map class. Raster CV (adaptive thresholds, VLM reading) remains only for scans/photos/other chains without vector assets. §8.3 anchors get exact coordinates. Deliverables: `docs/evidence/heb659_overlay.png` (proof), `docs/evidence/heb659_vector_reconstruction.png` (rebuilt map).

#### HEB-F9 — Directory and map cross-validate; nearest-glyph matching fails → segment-not-point confirmed on real coordinates (2026-07-21, confirmed)
Source: Run-D matcher joining the directory's item→aisle table against the map's printed category labels.
What: The two halves describe the same store, so they check each other. Nearest-glyph matching of aisle numbers ↔ category labels hit only **10/20** — because **aisle numbers print at aisle *ends* while category labels run down aisle *middles***. That's not a data problem; it's the §8.1 corridor-segment model empirically confirmed with real coordinates: labels must be assigned to corridor *edges*, never to the nearest point.
Implication: Reinforces the medial-axis corridor graph as the join substrate between map geometry and directory text. Operationally moot for item location (the directory hands us item→aisle directly), but it hardens the §6 self-verify loop: any future auto-matcher must match against segments, and a low match rate flags geometry problems.

#### HEB-F10 — Service areas are drawn open but staff-only; boundary + gap-width + labels identify them (2026-07-21, confirmed)
Source: store-owner markup of the #659 walkable-overlay QA; verified against the directory-PDF vector geometry and re-confirmed on store #24.
What: Three map facts with routing consequences. (a) The map's **closed thick-stroke polyline (~1.85 pt) is the sales-floor boundary** — everything outside (parking, drive-thru, curbside, Admin/Receiving/Loading Dock) is non-walkable; found automatically on both #659 (19 vertices) and #24 (11 vertices). (b) **Service areas (deli/bakery islands, seafood counters, pharmacy, kitchen, Meal Simple prep) are drawn as open floor** but are staff-only; their counters leave drawn gaps of ~0.5–1 m (staff pass-throughs), while genuine customer openings are ≥~1.5 m. (c) Department labels print **inside** their service areas, so naive snapping puts stops in staff space.
Implication: Walkable = inside boundary − fixtures − walls − exclusions, then (1) **staff-gap sealing**: morphologically bridge gaps ≤2 cells and cull the disconnected pockets (size-capped so an aggressive kernel can't delete a real region) — catches enclosed service interiors automatically; (2) **service-label attention**: any of DELI/BAKERY/SEAFOOD/SUSHI/KITCHEN/PHARMACY/MEAL SIMPLE/COOKING whose label area is still walkable is flagged `VERIFY:` in map_qa for a human exclusion rect (wide-mouth areas like Pharmacy are geometrically undetectable); (3) snapping then lands department anchors on the customer frontage for free. Side effect (accepted): 2-cell checkout lanes seal. Implemented in `router/engine.py::seal_staff_gaps` + `map_qa.py`; per-store ground truth in `data/<store>/exclusions.json` and `tests/test_walkability.py`.

#### HEB-F11 — The map's non-rect linework is load-bearing; capture geometry exactly, never as bounding boxes (2026-07-21, confirmed)
Source: store-owner QA rounds on #659 ("small things such as these cannot be captured by rectangles"; "no space behind these shelves is visitable"); drawing-level probe of both PDFs.
What: (a) Store #659's map holds **909 non-rectangular fill drawings and ~6,800 bezier/quad items** — diagonal counters, stepped kiosks, curved demo loops (Cooking Connection), and **rotated-quad cafe tables** — which bounding-box capture distorts and quad/curve-dropping deletes outright. (b) Some walls are drawn as **degenerate fills** (zero-width 2-line white fills, e.g. the seafood/kitchen counter walls) that any area-based filter discards. (c) **Open space behind a shelf's back face is not visitable** even when geometrically open; with exact walls captured, those spaces become enclosed pockets that reachability culling removes automatically — no hand exclusions needed.
Implication: The extractor converts every drawing to **point chains** (lines chained, beziers sampled ×8, quads/rects as closed 4-pt chains): closed furniture-sized chains → exact fixtures (`fixtures` rects + `fixture_polys` vertex lists in geometry.json), thin/degenerate/open chains → wall segments, `<2 pt` chains → icon confetti, skipped. Validated: #659 gains 403 poly fixtures (cafe tables now block; the old "walkway" test point was standing on a table), #24 gains 158 with zero hand-authored data and its VERIFY flags drop 4→2 (PHARMACY and SEAFOOD now seal themselves via their own drawn counter walls). Ground truth for per-shelf visitability: the **H-E-B app's item locator** highlights the visitable shelf face for any searched product — screenshot + match against the map to verify anchors/walkability where the drawing is ambiguous.

#### HEB-F12 — PALS plus Atlas provides precise product placement (2026-07-23, confirmed)
Source: saved current H-E-B product page for store #659 and its shipped browser JavaScript; locally parsed `__NEXT_DATA__`, PALS responses, and Atlas SVG.
What: Search `__NEXT_DATA__` returns ranked store-specific products with product ID, brand, size, inventory state, image, and display location. The product object still has location text only. A separate `/pals/v2.0/location/store/{store}/products/{product}` response supplies exact PSA records or an approximate PSA. Atlas `/atlas/v1.0/image` is a structured SVG whose `(area, aisle, side, section)` PSA elements resolve those records to coordinates. The current Lakeline asset has **41 aisle labels, 494 combined fixtures, and 2,677 unique PSA keys**. Incapsula challenges fresh HTTP clients, so the local pilot uses one persistent anonymous Playwright Chrome profile and verifies the active store and Atlas structural hash before enabling search.
Implication: Supersedes F2's routing implication, not its observation about the product object. #659 now routes actual selected Catalog Products at PSA precision when possible, falls back visibly to aisle/department anchors, and fails closed if Atlas structure drifts. The prior 2018 `data/659` golden remains untouched; current map data lives in `data/659-atlas`.

#### HEB-F13 — Current Atlas cannot reproduce the exact #659 guide boundary (2026-07-23, confirmed)
Source: differential QA of the current 41-aisle Atlas SVG against the 2018 45-aisle vector guide and its accepted walkable overlay.
What: Atlas publishes fixtures and PSA points, but not the closed store/customer boundary, Pharmacy/lease rooms, exterior, or the exact service-area wall network. The prior rectangular Atlas boundary was authored and cannot reproduce the accepted overlay. The two assets also represent different aisle layouts, so Atlas coordinates cannot be drawn directly over the guide.
Implication: The UI and route solver use the accepted `data/659` guide profile and its exact walkable overlay. Exact and approximate Atlas PSA coordinates pass through a calibrated, piecewise-linear current-to-guide transform so their within-aisle/department position is retained, then snap to the nearest entrance-reachable customer cell; location-text fallbacks still use aisle/department anchors. Every displayed pin is the route vertex the shopper visits. Cross-version placements remain visibly approximate. Atlas remains the product-location and structural-drift source, not the displayed routing geometry.

*(Next HEB findings append here.)*

### 14.2 Kroger  (official)

**Summary as of 2026-07-20:** Sanctioned public API. This is the precision tier and the launch-safe path.

#### KRO-F1 — Official public API with aisle data (2026-07-20, confirmed)
Source: Kroger Developer docs.
What: Free developer program; **OAuth2 client-credentials** (`POST /v1/connect/oauth2/token`, `product.compact` scope) for public data. **Products API** `GET /v1/products/{id}?filter.locationId={store}` returns price, availability, and **aisle location** — and per docs, side + bay detail. **Locations API** powers store search (also doubles as the item-picker catalog).
Implication: KrogerAdapter fills `aisle` + `side` + `bay` → **activates bay-level snapping (§9.3)**, making the app measurably more precise in Kroger stores than HEB. Good demo-market signal.

#### KRO-F2 — Rate limits (2026-07-20, confirmed)
Source: Kroger Locations API docs.
What: Public Locations API ≈ 1,600 calls/day **per endpoint** (enforced per-endpoint, distributable across operations). Default 10 results/page, `filter.limit` up to 200, no pagination on Locations.
Implication: Reinforces cache-first + nightly top-N warm even for the official path; don't hammer live. Confirm Products API limits during Phase 1.

*(Open: KRO-F3 — is `side`/`bay` populated for all SKUs or only some? → Q6, verify in Phase 1.)*

### 14.3 Cross-provider notes
- `LocationRecord` is a superset; adapters fill a subset. Snapping (§9.3) branches on which fields are present, not on which chain — so a new provider is a new adapter + its findings entry, no router changes.
- Planogram stability (weeks–months) is assumed uniform across chains for TTL purposes until data says otherwise.

---

## 15. Prototype Status (what's built)

- **Map ingestion (partial):** occupancy-grid parser + connected-component marker detector on the real pilot floor plan — **41/41 aisle markers auto-detected, zero false positives**; discs erased from obstacles; department + entrance/checkout anchors placed. (Digit OCR is the remaining manual step, specced in §8.1.)
- **Distance layer:** 4-connected BFS shortest paths on the grid; **all-pairs anchor matrix** approach validated.
- **Optimizer:** exact **Held-Karp** fixed-endpoint TSP with same-aisle **stop consolidation**; solves an 11-item list instantly.
- **Rendering:** optimal route drawn on the map (numbered stops, entrance→checkout, distance readout).
- **Measured result:** optimized route ~390 m vs ~460 m list-order and ~760 m average-random-order → **~48% shorter than an unordered list**.
- **Vector ingestion (2026-07-21, store #659):** official directory PDF parsed exactly — 165-entry item→aisle table (`data/659/directory.csv`, the Location-DB seed), 45/45 aisle badges + departments + entrances/checkstands + 563 fixture rectangles with coordinates, alignment proof (`docs/evidence/heb659_overlay.png`), full stylized rebuild from primitives alone (`docs/evidence/heb659_vector_reconstruction.png`). Note: the Phase-0 CV prototype above ran on a *different* 41-aisle floor plan — the two stores/maps are not the same asset.
- **Universal map pipeline (2026-07-21→22):** frozen store-agnostic extract/build/QA code (`extract.py`, `build_profile.py`, `map_qa.py`, `router/` package); per-store truth as data under `data/<N>/`; #659 pixel-golden regression; headless onboarding runbook + adversarial audit role (`docs/`); `rebuild.sh`/`pipeline.sh`/`discover.py` automation. Stores #24, #388, #790 onboarded and audited; #265 (raster guide) in flight on the raster fallback.
- **Web app (2026-07-23):** exact H-E-B product search and selection, locally persisted List Entries, PALS/Atlas coordinates calibrated onto the accepted exact #659 guide and snapped to customer-reachable route cells, and add/remove re-routing. The existing grid BFS, path-legality, and Held-Karp/heuristic TSP primitives remain unchanged.
- Files: `router/` (engine + pipeline), `app.py` (web app), `docs/archive/store_router.py` (Phase-0 prototype), `data/<N>/` (store truth), `guides/` (source directories), `docs/evidence/` (vector-ingestion deliverables).

---

## 16. Changelog

- **2026-07-23 — v1.9.** Preserved exact PALS/PSA positions through a calibrated Atlas-to-guide transform instead of collapsing them to generic aisle/department anchors; primary PALS placements now win over alternates, and displayed pins use the same reachable cells as the route.
- **2026-07-23 — v1.8.** Replaced the incomplete Atlas-derived display/routing grid with the accepted exact #659 guide overlay and profile; current H-E-B locations map to guide anchors as visible approximations (HEB-F13).
- **2026-07-23 — v1.7.** Real-product routing for Lakeline #659: HEB-F12 records the PALS↔Atlas placement join; current 41-aisle Atlas imported into isolated `data/659-atlas`; persistent anonymous browser connection with store/map verification; product search picker, locally saved draft, placement rehydration, dynamic selected-product TSP, visible approximations/unrouted products, derived-geometry map rendering, and parser/API/profile/golden-route tests. Legacy `data/659` route goldens remain intact.
- **2026-07-22 — v1.6.** Roadmap restructured around what actually shipped (§13): **Phase 1.5 recorded** — universal map pipeline (data-not-code, frozen algorithm, per-store JSON truth, #659 pixel golden), headless onboarding + adversarial audit (P2 #17 shipped for the map half — §6 update), coverage nets, stores #24/#388/#790 onboarded, #265 raster fallback in flight. **Phase 2 redefined as the main route algorithm on #659** (corridor graph, precomputed anchor matrix, segment stops, route legality + goldens); **Phase 3 = same algorithm fitted to all stores as data**; Kroger + beta → Phase 4; growth loop → Phase 5. Q8 largely resolved (`discover.py`, 5 guides fetched; fragmented + raster variants found). §15 updated.
- **2026-07-21 — v1.5.** Exact-geometry extraction: HEB-F11 (non-rect linework is load-bearing — chains/polys/quads/beziers captured exactly; degenerate fills are walls; behind-shelf space auto-culls once real walls exist; H-E-B app item locator = per-shelf ground truth). `geometry.json` gains `fixture_polys`; engine rasterizes them; extract overlay now renders walls + polys; web map draws polygon fixtures. #659: 403 poly fixtures (rotated-quad cafe tables now block). #24: VERIFY flags 4→2 with zero hand data.
- **2026-07-21 — v1.4.** Walkable-area correctness pass from store-owner QA markup: HEB-F10 (boundary polygon is the sales floor; service areas drawn open but staff-only; staff gaps ≤~1 m vs customer openings ≥~1.5 m). Engine gains boundary rasterization + `seal_staff_gaps`; map_qa gains service-label VERIFY pass + per-store folders (`data/<store>/`); walkability ground-truth tests added; validated on stores #659 and #24.
- **2026-07-21** — Phase 1 exit reached: #659 routable end-to-end via web app (see README).
- **2026-07-21 — v1.3.** Roadmap reordered to **H-E-B first** (§13): Kroger-first's de-risking rationale obsolete after F7/F8; Phase 1 = #659 end-to-end (store profile from vector assets, directory-seeded Location DB, directory-entry fuzzy resolution, stranger-usable web app, zero scraping); Kroger + shopping mode + A&M store → Phase 2. Pilot-store conflict resolved: Austin #659. P0 #6 and P1 #12 swapped accordingly; ingestion QA tool + digit OCR dropped from Phase 1. Detailed Phase 1 execution plan: `docs/superpowers/plans/2026-07-21-phase1-heb659.md`.
- **2026-07-21 — v1.2.** Official H-E-B per-store directory PDF discovered and ingested (`guide-austin-659.pdf`, store #659 Austin): HEB-F7 (published item→aisle directory, 165 entries — now the primary HEB data source; scraping demoted to fallback), HEB-F8 (map half fully vector — 45/45 aisles + 563 fixtures extracted exactly; leak bug structurally impossible; §8.1 vector-first validated), HEB-F9 (directory↔map cross-check; 10/20 nearest-glyph result empirically confirms segment-not-point). §7 HEBAdapter gains Tier 0 directory seed; §6 diagram + onboarding-agent note updated; §8.3 exact-anchor note; §8.8 prior gains free directory training data; §12 HEB risk impacts reduced + directory-staleness row; Q7 resolved, Q8 (directory coverage) added; Phase 2 seeding rewritten + pilot-store reconcile note (A&M vs Austin #659); §15 deliverables recorded.
- **2026-07-21 — v1.1.** §8.1 overhauled from design-review notes: aisle modeled as a corridor-graph *segment* (medial-axis skeleton), not a disc-centroid point; obstacle-mask hygiene (enclosed-region fill + closing) added to fix the silent leak bug reachability checks can't catch; vector-source-first ingestion; adaptive thresholding + Lab palette clustering; VLM as label reader + agent-loop verifier (CV keeps geometry); calibration de-prioritized (TSP is scale-invariant). Priority order recorded in §8.1. Ripples: §6 diagram + agent-loop checks, §8.3 segment source, §8.4 corridor-graph substrate, §8.8 CNN→fallback / segmentation-model scope shrunk, §10 corridor_graph + source_kind, §12 leak-risk row, Q7 (HEB vector map) added.
- **2026-07-20 —** Noted future expansion (§6, P2 #17): headless-agent automation of the offline per-store onboarding with a self-verify loop; stays a human process for the pilot store(s).
- **2026-07-20 — v1.0.** Initial master plan. Consolidated PRD + architecture + full algorithm listing (§8) + data model. Established the living-findings convention (§14) and seeded it with the H-E-B reverse-engineering results (HEB-F1…F6) and Kroger API facts (KRO-F1…F2). Recorded prototype status (§15). Open questions Q1–Q6 logged; Q1 mostly resolved via HEB-F2.
