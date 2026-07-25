#!/usr/bin/env python3
"""H-E-B product picker and optimal in-store route API, one store at a time."""
import glob
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from functools import lru_cache, partial
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import breadth_first_order
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from router import calibrate as cal
from router import engine
from router.directory import load_directory
from router.resolve import resolve
from router.heb import HEBClient, HEBConnectionError

os.makedirs("logs", exist_ok=True)
_handler = logging.handlers.RotatingFileHandler(
    "logs/app.log", maxBytes=2_000_000, backupCount=3)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger("grocer").addHandler(_handler)
logging.getLogger("grocer").addHandler(logging.StreamHandler())
logging.getLogger("grocer").setLevel(logging.INFO)
log = logging.getLogger("grocer.app")

DEFAULT_STORE = "659"
MAX_SNAP_M = 5.0        # nothing on a shelf is further than this from a corridor


@asynccontextmanager
async def lifespan(api):
    yield
    # Without this the spawned Chrome outlives the server and keeps the
    # .heb-<store> profile locked, so the next connect() dies with
    # "Chrome closed before startup".
    await api.state.heb.close()


app = FastAPI(title="grocerAlgo — H-E-B store routes", lifespan=lifespan)
app.state.heb = HEBClient(int(DEFAULT_STORE))
app.state.onboarding = None

# Both spellings of a department: H-E-B's Atlas and the printed guide disagree
# on a handful of names. Universal, not per-store — a guide that uses neither
# spelling simply misses and falls through to the department anchor.
DEPARTMENT_ALIASES = {
    "FROZEN": "FROZEN FOODS",
    "MARKET": "MEAT",
    "CHECKSTANDS": "CHECKOUT",
}


def store_ids():
    """Every store with a built map profile, newest onboarding included."""
    found = set()
    for path in glob.glob("data/*/profile.npz"):
        name = os.path.basename(os.path.dirname(path))
        if name.isdigit():
            found.add(name)
    return sorted(found, key=int)


def store_name(store_id):
    config = cal.store_config(store_id)
    if config.get("name"):
        return config["name"]
    city = cal.guide_city(store_id)
    return (f"H-E-B #{store_id} · {city.replace('-', ' ').title()}" if city
            else f"H-E-B #{store_id}")


def _walk_graph(free):
    """4-neighbour walkability as a sparse graph.

    SciPy runs the same BFS as engine.bfs, but in compiled code. It makes
    request-time paths between product cells cheap enough to choose which end
    of each aisle to use instead of forcing a return to its numbered mouth.
    """
    width = free.shape[1]
    ids = np.arange(free.size).reshape(free.shape)
    sideways = ids[:, :-1][free[:, :-1] & free[:, 1:]]
    down = ids[:-1][free[:-1] & free[1:]]
    rows = np.concatenate((sideways, sideways + 1, down, down + width))
    cols = np.concatenate((sideways + 1, sideways, down + width, down))
    return csr_matrix((np.ones(len(rows), np.uint8), (rows, cols)),
                      shape=(free.size, free.size))


class Store:
    """One store's accepted map plus whatever live-catalog layer it has earned.

    Everything here is per store and nothing is per request, so it is built
    once and cached. A store without a passing calibration still loads — it
    just cannot place products, and says why.
    """

    def __init__(self, store_id):
        self.id = store_id
        self.name = store_name(store_id)
        self.config = cal.store_config(store_id)
        with open(f"data/{store_id}/geometry.json") as handle:
            self.geometry = json.load(handle)
        profile = np.load(f"data/{store_id}/profile.npz", allow_pickle=True)
        self.names = [str(name) for name in profile["names"]]
        self.idx = {name: i for i, name in enumerate(self.names)}
        self.cells = profile["cells"]
        self.distances = profile["D"]
        self.free = profile["free"]
        self.m_per_cell = float(profile["m_per_cell"])
        self.cell = float(profile["cell"])
        self.width = self.free.shape[1]
        self.start = tuple(int(v) for v in self.cells[self.idx["ENTRANCE"]])
        self.end = tuple(int(v) for v in self.cells[self.idx["CHECKOUT"]])
        self.reach, _ = engine.bfs(self.free, self.start)
        self.walk_graph = _walk_graph(self.free)

        # Free-text routing needs an authored directory; only the pilot store
        # has one, and a store without it routes selected products instead.
        self.directory = {}
        for name in ("directory.csv", "departments.csv"):
            path = f"data/{store_id}/{name}"
            if os.path.exists(path):
                self.directory |= load_directory(path, set(self.names))

        # §8.3 per-item shelf positions + the aisle segments they are ordered
        # along. Absent (a store with no guide labels) => items fall back to
        # the aisle anchor.
        try:
            with open(f"data/{store_id}/shelf_positions.json") as handle:
                self.shelf_items = json.load(handle)["items"]
        except FileNotFoundError:
            self.shelf_items = {}

        self.atlas = cal.load_atlas(store_id)
        self.calibration = cal.load_calibration(store_id)
        self.blocked_reason = cal.blocked_reason(store_id)
        self.runs = cal.shelf_runs(self.atlas["psas"]) if self.atlas else {}
        self.carry = cal.transform(self.calibration) if self.calibration else None


@lru_cache(maxsize=4)
def load_store(store_id):
    return Store(store_id)


def get_store(store_id):
    """Resolve a requested store, or 404. The only place a store id becomes
    a filesystem path, so it is also where an unknown one is stopped."""
    if store_id not in store_ids():
        raise HTTPException(404, f"unknown store {store_id!r}")
    return load_store(store_id)


def catalog_store(store_id):
    """A store allowed to place products: exact placement or nothing."""
    store = get_store(store_id)
    if store.calibration is None:
        raise HTTPException(409, f"store {store_id} cannot place products yet — "
                                 f"{store.blocked_reason}")
    return store


def atlas_to_guide(store, point):
    return store.carry(point)


def on_corridor(store, group, point):
    """Move an Atlas shelf point onto the corridor of its own aisle."""
    return cal.to_corridor(store.runs, group, point)


@lru_cache(maxsize=64)
def shortest_tree(store_id, start):
    """Predecessors from one product cell; shared by every candidate direction."""
    store = load_store(store_id)
    source = start[1] * store.width + start[0]
    return breadth_first_order(
        store.walk_graph, source, directed=True, return_predecessors=True)[1]


@lru_cache(maxsize=512)
def leg_path(store_id, start, end):
    """Shortest legal path between arbitrary walkable cells."""
    store = load_store(store_id)
    cell = store.cell
    if start == end:
        return ((start[0] * cell + cell / 2, start[1] * cell + cell / 2),)
    source = start[1] * store.width + start[0]
    target = end[1] * store.width + end[0]
    parent, path = shortest_tree(store_id, start), []
    while target != source:
        if target < 0:
            raise ValueError(f"unreachable route cell: {start} -> {end}")
        path.append((target % store.width * cell + cell / 2,
                     target // store.width * cell + cell / 2))
        target = int(parent[target])
    path.append((start[0] * cell + cell / 2, start[1] * cell + cell / 2))
    return tuple(engine.string_pull(store.free, path[::-1], cell))


def anchor_xy(store, name):
    cx, cy = store.cells[store.idx[name]]
    return (float(cx) * store.cell + store.cell / 2,
            float(cy) * store.cell + store.cell / 2)


def item_at(store, entry, fallback):
    """Where the product actually sits, or the aisle itself when unknown."""
    placed = store.shelf_items.get(entry)
    if placed:
        return {"x": placed["x"], "y": placed["y"], "t": placed.get("t", 0.0),
                "approx": placed["approx"], "cell": placed.get("cell")}
    return {"x": fallback[0], "y": fallback[1], "t": 0.0, "approx": True,
            "cell": None}


def path_length(pts):
    return sum(math.hypot(q[0] - p[0], q[1] - p[1])
               for p, q in zip(pts, pts[1:]))


def orient(groups, start, end, leg):
    """Choose forward/reverse item order for each aisle in a fixed TSP order."""
    variants = [(g, list(reversed(g))) for g in groups]
    costs, previous = [], []
    for i, pair in enumerate(variants):
        row, back = [math.inf, math.inf], [-1, -1]
        for direction, group in enumerate(pair):
            inside = sum(path_length(leg(a["route_cell"], b["route_cell"]))
                         for a, b in zip(group, group[1:]))
            if i == 0:
                row[direction] = path_length(
                    leg(start, group[0]["route_cell"])) + inside
            else:
                for prior_direction, prior in enumerate(variants[i - 1]):
                    candidate = costs[-1][prior_direction] + path_length(leg(
                        prior[-1]["route_cell"], group[0]["route_cell"])) + inside
                    if candidate < row[direction]:
                        row[direction], back[direction] = candidate, prior_direction
        costs.append(row)
        previous.append(back)
    direction = min(range(2), key=lambda d: costs[-1][d] + path_length(
        leg(variants[-1][d][-1]["route_cell"], end)))
    directions = [direction]
    for i in range(len(groups) - 1, 0, -1):
        directions.append(previous[i][directions[-1]])
    directions.reverse()
    return [variants[i][d] for i, d in enumerate(directions)]


def join(legs):
    out = []
    for leg in legs:
        out += leg if not out else leg[1:]        # legs share the stop vertex
    return out


def routed(store, groups, start, end):
    leg = partial(leg_path, store.id)
    directed = orient(groups, start, end, leg)
    cells = [start] + [p["route_cell"] for g in directed for p in g] + [end]
    return join(leg(a, b) for a, b in zip(cells, cells[1:])), directed


def walked_m(store, pts):
    """Length of the drawn line — what the shopper actually walks."""
    return path_length(pts) / store.cell * store.m_per_cell


@lru_cache(maxsize=128)
def anchor_order(store_id, anchor_keys):
    store = load_store(store_id)
    names = ("ENTRANCE", "CHECKOUT") + anchor_keys
    ids = [store.idx[name] for name in names]
    matrix = [[int(store.distances[a][b]) for b in ids] for a in ids]
    return tuple(engine.tsp_order(matrix, len(anchor_keys))[1])


class CatalogProduct(BaseModel):
    id: str
    name: str
    brand: str | None = None
    size: str | None = None
    image_url: str | None = None
    inventory_state: str | None = None
    location_label: str | None = None
    selectable: bool = True


class LocateReq(BaseModel):
    products: list[CatalogProduct]


class ProductRouteItem(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1)


class RouteReq(BaseModel):
    items: list[str | ProductRouteItem]


class OnboardReq(BaseModel):
    store: str
    city: str | None = None
    # resume an interrupted run at a pipeline stage rather than paying for
    # stages 3 and 4 (~50 minutes of agent time) a second time
    from_stage: str | None = Field(None, alias="from")

    model_config = {"populate_by_name": True}


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/stores")
def stores():
    """Every onboarded store, and for those that cannot place products, why."""
    out = []
    for store_id in store_ids():
        reason = cal.blocked_reason(store_id)
        out.append({"id": store_id, "name": store_name(store_id),
                    "ready": reason is None, "blocked_reason": reason})
    return {"stores": out, "default": DEFAULT_STORE}


@app.get("/api/geometry")
def geometry(store: str = Query(DEFAULT_STORE)):
    return get_store(store).geometry


@app.get("/api/walkability.png")
def walkability(store: str = Query(DEFAULT_STORE)):
    get_store(store)
    return FileResponse(f"data/{store}/qa/walkable_overlay.png")


@app.get("/api/heb/status")
def heb_status(store: str = Query(DEFAULT_STORE)):
    get_store(store)
    return app.state.heb.status(store)


@app.post("/api/heb/connect")
async def heb_connect(store: str = Query(DEFAULT_STORE)):
    catalog_store(store)
    try:
        return await app.state.heb.connect(store)
    except HEBConnectionError as e:
        raise HTTPException(503, str(e)) from e


@app.post("/api/heb/connect/confirm")
async def heb_confirm(store: str = Query(DEFAULT_STORE)):
    catalog_store(store)
    try:
        return await app.state.heb.confirm(store)
    except (HEBConnectionError, ValueError) as e:
        raise HTTPException(503, str(e)) from e


@app.get("/api/products")
async def products(q: str = Query(min_length=3),
                   store: str = Query(DEFAULT_STORE)):
    catalog_store(store)
    q = q.strip()
    if len(q) < 3:
        raise HTTPException(422, "search requires at least 3 characters")
    try:
        return {"products": await app.state.heb.search(q, store)}
    except (HEBConnectionError, ValueError) as e:
        raise HTTPException(503, str(e)) from e


def snap_distance_m(store, point):
    """How far a shopper would be walked off `point` to stand somewhere legal.

    Large means the point is not on the shopping floor at all, so whatever
    produced it should not be believed.
    """
    try:
        cell = engine.snap(store.free, store.reach, point, store.cell)
    except (TypeError, ValueError):
        return math.inf
    return math.hypot(cell[0] * store.cell + store.cell / 2 - point[0],
                      cell[1] * store.cell + store.cell / 2 - point[1]
                      ) / store.cell * store.m_per_cell


def label_map_point(store, location_label, placement):
    """Where the printed shelf label says the product is."""
    anchors = store.geometry["anchors"]
    label = re.sub(r"\s+", " ", (location_label or "").upper())
    aisle = re.search(r"\bAISLE\s+(\d+)\b", label)
    if aisle:
        return anchors.get(cal.guide_aisle_name(store.config, int(aisle[1])))

    for name in sorted(anchors, key=len, reverse=True):
        if not name.startswith("AISLE ") and name in label:
            return anchors[name]
    group = placement["group"].removeprefix("ANCHOR:")
    return anchors.get(DEPARTMENT_ALIASES.get(group, group))


def place(store, location_label, placement):
    """Where the product is on this store's map, and how well we know it.

    "exact" is earned: it means H-E-B gave shelf-face geometry for this
    product, the store's calibration passed its gates, and the transformed
    point lands on floor a shopper can stand on. Anything else is a
    department-level fact — H-E-B knowing only that it is somewhere in
    Produce — and must not be drawn as if it were a shelf position.
    """
    mapped = None
    if placement["group"].startswith("PSA:") and placement.get("point"):
        mapped = atlas_to_guide(store, on_corridor(
            store, placement["group"], placement["point"]))
        if snap_distance_m(store, mapped) <= MAX_SNAP_M:
            return mapped, "exact"
        # H-E-B answers for some bulk packs with a pallet slot off the shopping
        # floor while its own label names a real aisle — 16|88 sits in the
        # bottom-left vestibule and is labelled "Aisle 13". Snapping such a
        # point to the nearest legal cell silently parks the product at the
        # entrance, so believe the label instead.
        log.warning("placement %s maps %s off the floor; using the label %r",
                    placement["group"], [round(v, 1) for v in mapped],
                    location_label)

    named = label_map_point(store, location_label, placement)
    return (named if named is not None else mapped), "department"


def exact_map_point(store, location_label, placement):
    """Map the live Atlas placement onto the accepted guide profile.

    A PSA is physical shelf geometry and is transformed. A department ANCHOR
    is a text label placed by eye — the two drawings put "Dairy" 50 pt apart —
    so it is matched to the guide's own anchor by NAME instead of warped.
    """
    return place(store, location_label, placement)[0]


LOCATED_PRODUCTS = {}


@app.post("/api/products/locate")
async def locate_products(req: LocateReq, store: str = Query(DEFAULT_STORE)):
    shop = catalog_store(store)
    # One session carries one store. Placing this store's products through a
    # session logged into another would pin the wrong building's shelves onto
    # this map, and every coordinate would look perfectly reasonable.
    session = getattr(app.state.heb, "store_id", None)
    if session is not None and session != int(store):
        raise HTTPException(503, f"Connect store #{store} first")
    located = []
    for model in req.products:
        product = model.model_dump()
        try:
            placement = await app.state.heb.locate(
                model.id, model.location_label, shop.atlas)
        except (HEBConnectionError, ValueError) as e:
            raise HTTPException(503, str(e)) from e
        result = product | {
            "routable": False,
            "approx": None,
            "placement_state": None,
            "placement_group": None,
        }
        if placement:
            point, state = place(
                shop, placement.get("location_label") or model.location_label,
                placement)
            try:
                route_cell = engine.snap(shop.free, shop.reach, point, shop.cell)
            except (TypeError, ValueError):
                log.warning("locate %s %r group=%s atlas=%s mapped=%s NO-SNAP",
                            model.id, model.location_label, placement["group"],
                            placement.get("point"), point)
                placement = None
            else:
                x = route_cell[0] * shop.cell + shop.cell / 2
                y = route_cell[1] * shop.cell + shop.cell / 2
                # Two error sources stack here: the Atlas->guide warp that
                # produced `point`, and the walk to the nearest walkable cell.
                # Logging both separately is what tells them apart.
                log.info(
                    "locate %s %s %r psa=%s group=%s atlas=%s mapped=%.1f,%.1f "
                    "shown=%.1f,%.1f snap=%.1f %s",
                    shop.id, model.id, placement.get("location_label")
                    or model.location_label, placement.get("psa_key"),
                    placement["group"], placement.get("point"),
                    point[0], point[1], x, y,
                    math.hypot(x - point[0], y - point[1]), state)
                result |= {
                    "routable": True,
                    "approx": state != "exact",
                    "placement_state": state,
                    "location_label": placement.get("location_label")
                                      or model.location_label,
                    "placement_group": placement["group"],
                    "x": x,
                    "y": y,
                    "route_cell": route_cell,
                }
        LOCATED_PRODUCTS[(store, model.id)] = result
        located.append({k: v for k, v in result.items()
                        if k != "route_cell"})
    return {"products": located}


def selected_route(store, items):
    """Route resolved H-E-B products on this store's exact walkable profile."""
    quantities, requested = {}, []
    for item in items:
        if item.product_id not in quantities:
            requested.append(item.product_id)
            quantities[item.product_id] = 0
        quantities[item.product_id] += item.quantity

    groups, unrouted = {}, []
    for product_id in requested:
        product = LOCATED_PRODUCTS.get((store.id, product_id))
        if not product or not product.get("routable"):
            unrouted.append({
                "product_id": product_id,
                "item": product.get("name") if product else product_id,
                "quantity": quantities[product_id],
                "location_label": (
                    product.get("location_label") if product else None),
                "reason": ("no store placement" if product
                           else "placement not resolved"),
            })
            continue
        pick = product | {
            "product_id": product_id,
            "item": product["name"],
            "quantity": quantities[product_id],
            "route_cell": tuple(product["route_cell"]),
        }
        groups.setdefault(product["placement_group"], []).append(pick)

    if not groups:
        raise HTTPException(422, "no selected product is routable")

    group_keys = list(groups)
    for key in group_keys:
        groups[key].sort(key=lambda p: (p["x"], p["y"], p["product_id"]))

    # The matrix is rebuilt from this request's actual product cells. Products
    # sharing a PALS aisle/area or fallback anchor stay one solver stop, while
    # routed() still walks through and displays every product in that group.
    representatives = [groups[key][0]["route_cell"] for key in group_keys]
    cells = [store.start, store.end] + representatives
    matrix = [[0.0] * len(cells) for _ in cells]
    for i, start in enumerate(cells):
        for j in range(i + 1, len(cells)):
            distance = path_length(leg_path(store.id, start, cells[j]))
            matrix[i][j] = matrix[j][i] = distance
    order = engine.tsp_order(matrix, len(group_keys))[1]
    ordered_keys = [group_keys[index - 2] for index in order]

    path, directed = routed(
        store, [groups[key] for key in ordered_keys], store.start, store.end)
    baseline_path, _ = routed(
        store, [groups[key] for key in group_keys], store.start, store.end)
    baseline = walked_m(store, baseline_path)
    distance = walked_m(store, path)

    stops, number = [], 1
    for group in directed:
        for product in group:
            stops.append({
                "n": number,
                "product_id": product["product_id"],
                "item": product["item"],
                "quantity": product["quantity"],
                "location_label": product.get("location_label"),
                "placement_group": product["placement_group"],
                "x": product["x"],
                "y": product["y"],
                "approx": product["approx"],
                "approximation_state": product.get("placement_state")
                                       or "department",
            })
            number += 1
    return {
        "stops": stops,
        "path": path,
        "distance_m": round(distance, 1),
        "baseline_m": round(baseline, 1),
        "saved_m": round(baseline - distance, 1),
        "unrouted": unrouted,
    }


@app.post("/api/route")
def route(req: RouteReq, store: str = Query(DEFAULT_STORE)):
    shop = get_store(store)
    if not req.items:
        raise HTTPException(400, "empty list")
    if not all(isinstance(item, str) for item in req.items):
        if any(isinstance(item, str) for item in req.items):
            raise HTTPException(400, "cannot mix product IDs and free text")
        return selected_route(shop, req.items)
    if not shop.directory:
        raise HTTPException(
            422, f"store {store} has no free-text directory; select products")
    matched, unmatched = resolve(req.items, shop.directory)

    # SOLVER-side consolidation only: one TSP stop per anchor, because you walk
    # into an aisle once. The response below still lists every item separately —
    # collapsing two products into one pin at the aisle mouth is a display bug,
    # not an optimisation.
    # ponytail: first-anchor pick; route-aware anchor choice if corrections say otherwise
    stops = {}
    for m in matched:
        stops.setdefault(m["anchors"][0], []).append((m["query"], m["entry"]))
    anchor_keys = list(stops)
    if not anchor_keys:
        raise HTTPException(422, "no item could be located")

    names = ["ENTRANCE", "CHECKOUT"] + anchor_keys
    order = anchor_order(shop.id, tuple(anchor_keys))

    # Where each product sits. Segment t gives the canonical forward order;
    # orient() later chooses forward or reverse from the surrounding route.
    picks = {}
    for si in order:
        key = names[si]
        here = anchor_xy(shop, key)
        fallback_cell = tuple(int(v) for v in shop.cells[shop.idx[key]])
        placed = [item_at(shop, entry, here) | {"query": query, "anchor": key}
                  for query, entry in stops[key]]
        for p in placed:
            p["route_cell"] = tuple(p["cell"]) if p.get("cell") else fallback_cell
        picks[key] = sorted(placed, key=lambda p: p["t"])

    route_keys = [names[si] for si in order]
    path, directed = routed(shop, [picks[k] for k in route_keys],
                            shop.start, shop.end)

    # G1 telemetry: the same list walked in the order it was written, measured
    # the same way, so "you saved N m" is a real comparison and not two metrics.
    # Both choose their best aisle directions, so the difference is ordering.
    list_keys = list(dict.fromkeys(m["anchors"][0] for m in matched))
    baseline_path, _ = routed(shop, [picks[k] for k in list_keys],
                              shop.start, shop.end)
    baseline = walked_m(shop, baseline_path)

    out_stops, n = [], 1
    for group in directed:
        for p in group:
            out_stops.append({"n": n, "item": p["query"], "anchor": p["anchor"],
                              "x": p["x"], "y": p["y"], "approx": p["approx"]})
            n += 1
    return {"stops": out_stops, "path": path,
            "distance_m": round(walked_m(shop, path), 1),
            "baseline_m": round(baseline, 1),
            "saved_m": round(baseline - walked_m(shop, path), 1),
            "unmatched": unmatched}


# --- onboarding a store the app does not have yet -------------------------
#
# pipeline.sh drives a headless agent loop for roughly an hour, with
# --dangerously-skip-permissions. It is spawned as an argument list (never a
# shell string) in its own process group, one run at a time, and can be killed.
#
# One at a time is a property of the machine, not of the person asking: the run
# drives a browser and a headless agent that cannot share a box. So a second
# store does not get refused, it gets a place in line — admission is a queue,
# execution is still the single slot.

STORE_NUMBER = re.compile(r"^\d{1,4}$")
CITY_SLUG = re.compile(r"^[a-z-]{2,40}$")
PIPELINE_STAGE = re.compile(r"^[1-6]$")
ONBOARDING_STATE = "logs/onboarding.json"
ADMIN_TOKEN = "GROCER_ADMIN_TOKEN"
# starlette's TestClient calls itself "testclient" instead of an address — it
# is the same process, which is as loopback as a client gets.
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def admin(request: Request):
    """Who may spawn an hour-long agent with permissions skipped.

    Two trust models, and which one is in force is decided by whether the
    operator set GROCER_ADMIN_TOKEN. Set: the bearer is the only credential,
    which is what makes putting this on a public address safe. Unset: the
    credential is being on the box — loopback only, so local dev and the test
    suite need no configuration, and an exposed deploy refuses rather than
    quietly onboarding for strangers. Reading endpoints stay public; these
    three are the ones that start processes.
    """
    token = os.environ.get(ADMIN_TOKEN)
    if token:
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        # compare_digest: a timing oracle on the token is worth more to an
        # attacker than the microseconds it costs us
        if scheme.lower() != "bearer" or not secrets.compare_digest(
                given.encode(), token.encode()):
            raise HTTPException(401, "admin token required",
                                headers={"WWW-Authenticate": "Bearer"})
        return
    if (request.client.host if request.client else None) not in LOOPBACK:
        raise HTTPException(403, f"set {ADMIN_TOKEN} to onboard from off-box")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, TypeError):
        return False
    return True


def _remembered():
    """Disk is the whole state: {"current": run or None, "queue": [waiting]}.

    The pipeline outlives the server that spawned it — an hour-long agent with
    permissions skipped is exactly the thing that must not become invisible
    just because uvicorn restarted. The pid on disk is how we find the run
    again, and the queue lives beside it for the same reason: a restart mid-
    line must not silently drop the stores nobody has started yet.
    """
    try:
        with open(ONBOARDING_STATE) as handle:
            found = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"current": None, "queue": []}
    if "current" not in found and "queue" not in found:
        found = {"current": found or None}   # a flat record, written before
    return {"current": found.get("current"),  # there was a queue to write
            "queue": found.get("queue") or []}


def _remember(current, queue):
    with open(ONBOARDING_STATE, "w") as state:
        json.dump({"current": current, "queue": queue}, state)


def _tail(path):
    try:
        with open(path) as handle:
            return handle.read()[-8000:]
    except (FileNotFoundError, TypeError):
        return ""


def _pipeline(store, city, stage):
    return (["./pipeline.sh", store] + ([city] if city else [])
            + (["--from", stage] if stage else []))


def _spawn(store, command, path, queue):
    """Take the one slot, leaving whoever is behind it in line.

    Shared by both POSTs and by the drain, so a run the queue started is
    indistinguishable from one a person started: same slot, same log tail,
    same Stop button, same record on disk.
    """
    handle = open(path, "w")
    process = subprocess.Popen(command, stdout=handle,
                               stderr=subprocess.STDOUT, start_new_session=True)
    app.state.onboarding = {"store": store, "process": process, "log": path}
    _remember({"store": store, "pid": process.pid, "log": path,
               "returncode": None}, queue)
    return process


def _drain(queue):
    """Hand the freed slot to the head of the line.

    Lazily, from whoever asks for status, because there is no worker to
    supervise and no worker that can die quietly: the poll that notices a
    finished run is already happening every 2 s in the browser, so the wait is
    at most one poll while anyone is watching, and a server that restarts
    mid-queue drains on the first GET instead of needing to be told.
    """
    head, rest = queue[0], queue[1:]
    path = f"logs/onboard-{head['store']}.log"
    try:
        _spawn(head["store"], _pipeline(head["store"], head.get("city"),
                                        head.get("from")), path, rest)
    except OSError as error:
        # A drain happens inside a status poll, so a spawn that cannot happen
        # at all still has to move the line on: otherwise every poll from here
        # raises on the same entry and the dialog goes dark for good.
        log.error("queued store %s could not start: %s", head["store"], error)
        with open(path, "w") as handle:
            handle.write(f"could not start the pipeline: {error}\n")
        _remember({"store": head["store"], "pid": None, "log": path,
                   "returncode": None}, rest)
        app.state.onboarding = None
        return
    log.info("onboarding queued store %s (%d still waiting)",
             head["store"], len(rest))


def onboarding_status():
    state = _remembered()
    queue = state["queue"]
    run = app.state.onboarding
    if run:
        code = run["process"].poll()
        if code is not None:
            if queue:
                _drain(queue)
                return onboarding_status()  # the drained run is the answer now
            # remember how it ended: a browser that reconnects after this
            # server restarts is otherwise told a finished run is simply not
            # running, which is how a failed pipeline reads as nothing at all
            _remember({"store": run["store"], "pid": run["process"].pid,
                       "log": run["log"], "returncode": code}, queue)
        return {"running": code is None, "store": run["store"],
                "returncode": code, "log": _tail(run["log"]),
                "queue": [waiting["store"] for waiting in queue]}
    found = state["current"]
    alive = _alive(found.get("pid")) if found else False
    if not alive and queue:
        # the run finished while this server was down; the line is still owed
        _drain(queue)
        return onboarding_status()
    if not found:
        return {"running": False, "store": None, "returncode": None,
                "log": "", "queue": []}
    return {"running": alive, "store": found.get("store"),
            "returncode": None if alive else found.get("returncode"),
            "log": _tail(found.get("log")),
            "queue": [waiting["store"] for waiting in queue]}


def onboarding_pid():
    run = app.state.onboarding
    if run and run["process"].poll() is None:
        return run["process"].pid
    found = _remembered()["current"]
    return found.get("pid") if found and _alive(found.get("pid")) else None


@app.post("/api/stores/onboard", dependencies=[Depends(admin)])
def onboard(req: OnboardReq):
    if not STORE_NUMBER.match(req.store):
        raise HTTPException(400, "store must be a 1-4 digit H-E-B store number")
    city = (req.city or "").strip().lower().replace(" ", "-")
    if city and not CITY_SLUG.match(city):
        raise HTTPException(400, "city must be a lowercase slug, e.g. cedar-park")
    stage = (req.from_stage or "").strip()
    if stage and not PIPELINE_STAGE.match(stage):
        raise HTTPException(400, "from must be a pipeline stage number, 1-6")
    # A store that is onboarded but cannot place products has unfinished
    # business — running the pipeline again is how it gets finished, and the
    # pipeline resumes from whatever truth is already on disk.
    if req.store in store_ids() and cal.blocked_reason(req.store) is None:
        raise HTTPException(409, f"store {req.store} is already onboarded")
    live = onboarding_status()          # which also drains a finished run
    state = _remembered()
    if live["running"]:
        # The three ways a store is already spoken for: it holds the slot, it
        # is in line, or it is onboarded — checked above. Anything else waits
        # its turn, so the answer is "yes, in a while" and not "come back in
        # an hour and ask again", which is a person babysitting a queue.
        if req.store == live["store"] or req.store in live["queue"]:
            raise HTTPException(409, f"store {req.store} is already in line")
        _remember(state["current"], state["queue"] +
                  [{"store": req.store, "city": city, "from": stage}])
        log.info("queued store %s (city %r, from stage %s) behind store %s",
                 req.store, city, stage or 1, live["store"])
        return onboarding_status()

    process = _spawn(req.store, _pipeline(req.store, city, stage),
                     f"logs/onboard-{req.store}.log", state["queue"])
    log.info("onboarding store %s (city %r, from stage %s) pid %s", req.store,
             city, stage or 1, process.pid)
    return onboarding_status()


@app.post("/api/stores/{store}/verify", dependencies=[Depends(admin)])
def verify_store(store: str):
    """Settle a calibration tie with live shelf labels.

    The last step that used to need a terminal. It drives the same browser the
    app uses, so the session must be on this store — and it is the only thing
    that can clear the margin gate, where two aisle correspondences fit the
    drawing equally well and only the real store knows which is right.
    """
    if store not in store_ids():          # the trust boundary, as everywhere
        raise HTTPException(404, f"unknown store {store}")
    live = onboarding_status()
    # Verification does not queue: it needs the browser pointed at THIS store
    # by a person who is standing there, so a turn that comes up in 40 minutes
    # is worth nothing. It waits for a quiet machine instead.
    if live["running"] or live["queue"]:
        raise HTTPException(409, f"already running for store {live['store']}")

    # tracked in the same slot as a pipeline run: one long job at a time, and
    # the log tail, the stop button and the re-attach all work unchanged
    process = _spawn(store, [sys.executable, "calibrate.py", store, "--verify"],
                     f"logs/verify-{store}.log", _remembered()["queue"])
    log.info("verifying store %s pid %s", store, process.pid)
    return onboarding_status()


@app.get("/api/stores/onboard")
def onboard_status():
    return onboarding_status()


@app.delete("/api/stores/onboard", dependencies=[Depends(admin)])
def onboard_stop(store: str | None = Query(None)):
    # Naming a store takes it out of the line, and only out of the line: the
    # live run is killed by the bare call, so a stray ?store= can never end an
    # hour of agent time by looking like a cancel.
    if store is not None:
        state = _remembered()
        rest = [waiting for waiting in state["queue"]
                if waiting["store"] != store]
        if len(rest) == len(state["queue"]):
            raise HTTPException(404, f"store {store} is not queued")
        _remember(state["current"], rest)
        log.info("store %s taken out of the onboarding queue", store)
        return onboarding_status()
    pid = onboarding_pid()
    if pid is None:
        raise HTTPException(409, "nothing is being onboarded")
    # The headless agent is a child of the shell, so the whole group has to go.
    # Works on a run this server did not start, which is the point.
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    log.info("onboarding stopped (pid %s)", pid)
    return onboarding_status()
