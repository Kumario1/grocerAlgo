# grocerAlgo

**Turn a grocery list into the shortest walk through your actual store.**
Entrance → every item → checkout, drawn on the real floor plan, ordered by a
solver instead of by whatever order you happened to write things down.

![The app routing five real H-E-B products through Lakeline #659](docs/img/app-route.png)

---

## The problem

A list is written in recall order. A store is laid out in aisle order. Those
two things have nothing to do with each other, so you backtrack — a lot.

Measured on our pilot store with a real 25-item list:

| | |
|---|---|
| Walking the list as written | **818 m** |
| Walking the optimal route | **426 m** |
| Saved | **392 m — 48%** |
| Time to compute | **216 ms** |

Store apps will tell you the aisle for *one* item at a time. None of them
sequence the whole trip. That gap is the product.

---

## Where it is today

A working local app against **Lakeline H-E-B Plus! #659** in Austin, plus a
map pipeline that has onboarded **6 stores**.

- Search H-E-B's **live catalog** — real products, real stock state, real
  shelf labels, scoped to one store.
- Pick exact products, set quantities, get a numbered route on the floor plan.
- Route re-solves on every list change in **under a second**.
- Items H-E-B can't place are shown as explicitly unrouted, never dropped.

Against the goals in [`plan.md`](plan.md):

| Goal | Target | Now |
|---|---|---|
| G1 · route quality | ≥30% shorter than list order | **48%** |
| G2 · coverage | ≥85% of items auto-located | **92%** (23/25) |
| G3 · speed | <1 s p95 | **216 ms** |
| G5 · onboarding | new store routable in <1 h human time | one command + agent loop |

---

## How it works

Three problems. The third one is the easy one.

### 1 · Where is each item, in *this* store?

The hard one, and the real bottleneck. H-E-B exposes per-store product
locations through PALS — a product resolves to a PSA (a spot on a specific
shelf face) and a printed label like "Aisle 17" or "In Dairy on the Back Wall".
grocerAlgo drives a local browser session to read the catalog the way a
customer's browser does, then maps each PSA onto the store's floor plan.

### 2 · Turn a map image into a routable graph

H-E-B publishes a store guide as a vector PDF. The pipeline turns that into a
walkable grid — and then *proves* it walkable, because a map that merely looks
right will happily route you through a freezer.

![Map ingestion pipeline for store 659](docs/img/pipeline.png)

```
guide-<city>-<store>.pdf        discover.py finds and downloads this
  → extract.py                  geometry.json — fixtures, walls, boundary, labels
  → router/derive.py            per-store zones + seal zones, auto-derived
  → build_profile.py            profile.npz — walkable grid, anchors, all-pairs D
  → map_qa.py                   qa/*.png + report.json (machine-readable)
```

**The algorithm is frozen and store-agnostic.** Every per-store difference
lives in small JSON files under `data/<store>/` — zones, seal zones,
exclusions, inclusions, walk truth. Adding a store means adding data, never
code. That constraint is what makes the sixth store as cheap as the second.

![Six H-E-B stores onboarded through the same pipeline](docs/img/stores.png)

All six report zero VERIFY flags, zero sealed floor patches, and zero
unreachable shelf labels.

### 3 · Compute the optimal order

Solved. Fixed-endpoint TSP (exact Held-Karp, vectorised over popcount layers)
runs in milliseconds at grocery-list scale. Products sharing an aisle collapse
to one solver stop — you walk into an aisle once — while every item stays a
separately numbered pick. Each aisle is then entered from whichever end the
surrounding route prefers, rather than always from its numbered mouth.

---

## The road here

Some of it was the plan. The interesting parts were not.

**The map has to be proven, not eyeballed.** Early walkable grids looked
perfect and routed through a bakery counter. The pipeline now flood-fills from
the entrance and fails if any shelf label is unreachable or any patch of floor
is sealed off. Store 659's grid is frozen pixel-by-pixel — 63,445 walkable
cells in `data/659/golden_free.npy`. Any universal-rule change that moves one
blessed pixel fails the suite; re-blessing is a deliberate, documented act.

**Map annotations are not objects.** Badge hexagons, decorative trinkets and
aisle markers are printed *on* the floor. Treating them as fixtures walled off
corridors that are perfectly walkable in real life.

**Two drawings of the same store do not line up.** H-E-B's live Atlas map and
the printed guide are different vintages. The first calibration rubber-sheeted
Atlas coordinates through ~50 control points — all of them *label* positions,
lying on three near-collinear lines with nothing in the store interior where
products actually sit. The sliver triangles between them stretched aisle 17's
35 pt shelf run across 93 pt of map, so ice cream pinned two aisles from where
it was. Both are axis-aligned plans of one floor, so the honest model is one
scale and offset per axis: residual under 2.2 pt across 32 anchors, and a
shelf face now maps to a straight 4 pt line. Department *labels* are matched by
name instead of transformed, because the two drawings put "Dairy" 50 pt apart.

**Trust the label over the coordinate.** H-E-B answers for some bulk packs with
a pallet bay off the shopping floor while its own label names a real aisle — a
40-pack of water resolved to the vestibule and got snapped 32 m to the
entrance. Any placement landing more than 5 m from walkable floor is no longer
believed; the printed label wins. All 2,677 PSAs in the store are swept in CI
to keep it that way.

**One browser page, one navigation.** Typeahead fires overlapping searches, and
a second navigation aborts the first — which the client read as "H-E-B dropped
us" and answered by tearing down the whole browser. Every keystroke could kill
the session. Navigation is now serialised and a single failure retries.

---

## Tests

**479 tests**, ~40 s. The suite is weighted toward the things that are
expensive to get wrong.

| Suite | Tests | What it defends |
|---|---:|---|
| `test_walkability.py` | 328 | every store's floor: shelves solid, corridors open, no orphans |
| `test_api.py` | 23 | routing endpoints, placement, off-floor guards |
| `test_raster.py` | 21 | image-only guide fallback, gate by gate |
| `test_heb.py` | 19 | catalog parsing, session handling, concurrency |
| `test_legality.py` | 17 | no path ever crosses a fixture |
| `test_derive.py` | 14 | per-store config derives from labels alone |
| `test_coverage.py` | 12 | no section of a store goes unmapped |
| `test_corridor.py` | 10 | corridor width matches real cart capacity |
| `test_engine.py` | 8 | TSP, BFS, string pulling |
| `test_golden.py` + `test_route_golden.py` | 8 | frozen grid and frozen route, pixel-exact |
| others | 19 | directory resolution, extraction, PDF discovery |

```bash
python3 -m pytest -q
```

The two golden suites are the tripwires. Everything else can be argued about;
those either match the blessed artifact or the build is red.

---

## Run it locally

```bash
pip install -r requirements.txt
brew install tesseract             # macOS — raster-PDF OCR fallback
# apt install tesseract-ocr        # Debian/Ubuntu

python3 -m uvicorn app:app --port 8000
# open http://localhost:8000
```

Choose **Connect H-E-B**. A persistent local Chrome profile opens — select
Lakeline H-E-B Plus! #659 there, return to the app, and confirm. The profile
lives in `.heb-659/` and is gitignored. **grocerAlgo never stores credentials**;
the session belongs to your own browser profile.

Logs land in `logs/app.log` (rotating, gitignored): every catalog fetch with
timing, every disconnect with its exception, and every placement as
`atlas → mapped → shown` with the snap distance.

The Atlas snapshot that the connection check validates against was imported
once from a saved product page, and is re-importable:

```bash
python3 import_heb.py "Fresh Sweet Cob Corn - Texas-Size Pack - Shop Corn at H-E-B.html"
python3 build_profile.py 659-atlas
```

### Onboarding another store

```bash
./pipeline.sh <store>                 # store number in, audited map out
./pipeline.sh <store> Cedar Park      # city names are slugged automatically
./pipeline.sh <store> --no-agents     # mechanical stages only (smoke test)

./rebuild.sh <store>                  # rebuild + full test suite, one command
```

discover → rebuild → onboarding agent ([`docs/onboarding.md`](docs/onboarding.md))
→ adversarial audit agent ([`docs/audit.md`](docs/audit.md)) → human visual
verdict. The automated run only reaches the final gate on `AUDIT CLEAN`.
Convergence means `rebuild.sh` exits 0, `report.json` has zero VERIFY flags and
empty coverage lists, and the audit role agrees. Final acceptance is still a
human looking at the walkable and reachable graphs.

Vector guides use the standard extraction path. The image-only fallback is
still experimental — it passes the structural precision/recall, exact-aisle,
aisle-position, anchor and runtime gates on both OCR backends, but not the
boundary IoU gate, so normal runs reject raster guides.
Opt in with `GROCER_RASTER_EXPERIMENTAL=1`.

```bash
python3 raster_benchmark.py --backend tesseract   # scores every gate,
python3 raster_benchmark.py --backend vision      # exits nonzero on a miss
```

---

## Where it's going

Today this runs on one machine against one store account. Making it something
other people can use means solving one thing first:

**The catalog connection is a local browser session.** That is what makes the
data honest — it is the same page a customer sees — and it is exactly what
does not survive being moved to a server. A hosted version needs a real answer
here: a server-side session pool, an official data agreement, or a thin local
companion that keeps the browser on the user's own machine. This is the gate,
not the UI.

Behind that:

- **Mobile-first UI.** Check items off as you shop; the route re-solves from
  where you are. The solver is already fast enough to do this on every tap.
- **Frozen last.** Sequence the freezer aisle near checkout so ice cream
  survives the trip. A soft constraint on the TSP, not a new algorithm.
- **More stores, then more chains.** The pipeline is store-agnostic by
  construction; a second chain proves the abstraction is real.
- **Contributed maps.** A shopper whose store isn't mapped uploads a screenshot
  and the raster fallback takes it from there — once it clears the boundary
  gate.

Explicitly **not** in v1: blue-dot indoor positioning, price comparison,
online ordering, multi-store trip splitting. Full reasoning in
[`plan.md`](plan.md) §3.

---

## Repo map

| Path | What |
|---|---|
| `app.py` | FastAPI app — catalog, placement, routing endpoints |
| `router/` | engine (TSP, BFS), map derivation, H-E-B client, QA checks |
| `static/index.html` | the whole front end |
| `data/<store>/` | per-store truth: geometry, profile, zones, QA artifacts |
| `plan.md` | living master plan — PRD, architecture, provider findings |
| `CONTEXT.md` | canonical domain vocabulary used in code, tests and UI |
| `docs/` | onboarding and audit runbooks for the agent loop |
