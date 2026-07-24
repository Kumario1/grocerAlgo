#!/usr/bin/env python3
"""Lakeline #659 product picker and optimal in-store route API."""
import collections
import json
import logging
import logging.handlers
import math
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import breadth_first_order
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
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

@asynccontextmanager
async def lifespan(api):
    yield
    # Without this the spawned Chrome outlives the server and keeps the
    # .heb-659 profile locked, so the next connect() dies with
    # "Chrome closed before startup".
    await api.state.heb.close()


app = FastAPI(title="grocerAlgo — HEB #659", lifespan=lifespan)
app.state.heb = HEBClient()

GEOM = json.load(open("data/659/geometry.json"))
_p = np.load("data/659/profile.npz", allow_pickle=True)
NAMES = [str(n) for n in _p["names"]]
IDX = {n: i for i, n in enumerate(NAMES)}
CELLS = _p["cells"]
D = _p["D"]
FREE = _p["free"]
M_PER_CELL = float(_p["m_per_cell"])
CELL = float(_p["cell"])
W = FREE.shape[1]
START = tuple(int(v) for v in CELLS[IDX["ENTRANCE"]])
END = tuple(int(v) for v in CELLS[IDX["CHECKOUT"]])
REACH, _ = engine.bfs(FREE, START)
DIRECTORY = {**load_directory("data/659/directory.csv", set(NAMES)),
             **load_directory("data/659/departments.csv", set(NAMES))}

ATLAS = {
    "geometry": json.load(open("data/659-atlas/geometry.json")),
    "psas": json.load(open("data/659-atlas/psas.json")),
}
LOCATED_PRODUCTS = {}


def _axis_fit(atlas_names, guide_names, axis):
    """Scale + offset carrying one Atlas axis onto the guide's."""
    return np.polyfit(
        [ATLAS["geometry"]["anchors"][n][axis] for n in atlas_names],
        [GEOM["anchors"][n][axis] for n in guide_names], 1)


# Both drawings are axis-aligned plans of the same floor, so one scale and
# offset per axis is the entire relationship. Each axis is fitted where that
# axis carries physical spacing: x from the top row of aisles, y from the
# left-hand column. Residual is under 2.2 pt across all 32 of those anchors.
#
# The rubber-sheet interpolator this replaces was fitted through every anchor
# including the department *labels*, which are text placed by eye and sit
# metres from their counterpart on the other map. Its control points also lay
# on three near-collinear lines with nothing in the store interior, so the
# sliver triangles between them smeared a single straight aisle's shelf points
# across up to 140 pt — two aisles' worth — which is where products were
# landing beside the aisle they were actually in.
X_FIT = _axis_fit([f"AISLE {n}" for n in range(1, 14)],
                  [f"AISLE {n}" for n in range(1, 14)], 0)
Y_FIT = _axis_fit([f"AISLE {n}" for n in range(23, 42)],
                  [f"AISLE {n + 4}" for n in range(23, 42)], 1)


def atlas_to_guide(point):
    return [float(X_FIT[0] * point[0] + X_FIT[1]),
            float(Y_FIT[0] * point[1] + Y_FIT[1])]


def _corridors():
    """Atlas centre-line of every (area, aisle) shelf run.

    A PSA is a spot on a shelf FACE, and an aisle's two faces straddle the
    corridor the shopper actually walks — so a product's pin belongs on the
    midline between them. Each run is long and thin, so the axis with the
    smaller extent is the one across it; anything squarer than 2:1 is a
    department blob rather than a run and is left alone.

    Taken from the Atlas's own PSA geometry, so it needs no correspondence
    between Atlas and guide aisle NUMBERS — which is just as well, because 40
    of #659's aisle numbers are reused by several departments at opposite ends
    of the store.
    """
    runs = collections.defaultdict(list)
    for key, point in ATLAS["psas"].items():
        area, aisle, _, _ = key.split("|")
        runs[(area, aisle)].append(point)
    lines = {}
    for run, points in runs.items():
        if len(points) < 4:
            continue
        low = [min(p[a] for p in points) for a in (0, 1)]
        high = [max(p[a] for p in points) for a in (0, 1)]
        extent = [high[a] - low[a] for a in (0, 1)]
        axis = 0 if extent[0] < extent[1] else 1
        if extent[axis] * 2 > extent[1 - axis]:
            continue
        lines[run] = (axis, (low[axis] + high[axis]) / 2)
    return lines


CORRIDORS = _corridors()


def on_corridor(group, point):
    """Move an Atlas shelf point onto the corridor of its own aisle."""
    line = CORRIDORS.get(tuple(group.split(":")[1:]))
    if not line:
        return point
    axis, value = line
    point = list(point)
    point[axis] = value
    return point


# SciPy runs the same 4-neighbour BFS as engine.bfs, but in compiled code. It
# makes request-time paths between product cells cheap enough to choose which
# end of each aisle to use instead of forcing a return to its numbered mouth.
_ids = np.arange(FREE.size).reshape(FREE.shape)
_lr = _ids[:, :-1][FREE[:, :-1] & FREE[:, 1:]]
_ud = _ids[:-1][FREE[:-1] & FREE[1:]]
_rows = np.concatenate((_lr, _lr + 1, _ud, _ud + W))
_cols = np.concatenate((_lr + 1, _lr, _ud + W, _ud))
WALK_GRAPH = csr_matrix((np.ones(len(_rows), np.uint8), (_rows, _cols)),
                        shape=(FREE.size, FREE.size))

# §8.3 per-item shelf positions + the aisle segments they are ordered along.
# Absent (a store with no guide labels) => items fall back to the aisle anchor.
try:
    _sp = json.load(open("data/659/shelf_positions.json"))
    SHELF_ITEMS = _sp["items"]
except FileNotFoundError:
    SHELF_ITEMS = {}

def anchor_xy(name):
    cx, cy = CELLS[IDX[name]]
    return (float(cx) * CELL + CELL / 2, float(cy) * CELL + CELL / 2)

def item_at(entry, fallback):
    """Where the product actually sits, or the aisle itself when unknown."""
    p = SHELF_ITEMS.get(entry)
    if p:
        return {"x": p["x"], "y": p["y"], "t": p.get("t", 0.0),
                "approx": p["approx"], "cell": p.get("cell")}
    return {"x": fallback[0], "y": fallback[1], "t": 0.0, "approx": True,
            "cell": None}

@lru_cache(maxsize=64)
def shortest_tree(start):
    """Predecessors from one product cell; shared by every candidate direction."""
    s = start[1] * W + start[0]
    return breadth_first_order(
        WALK_GRAPH, s, directed=True, return_predecessors=True)[1]

@lru_cache(maxsize=512)
def leg_path(start, end):
    """Shortest legal path between arbitrary walkable cells."""
    if start == end:
        return ((start[0] * CELL + CELL / 2,
                 start[1] * CELL + CELL / 2),)
    s, u = start[1] * W + start[0], end[1] * W + end[0]
    parent, path = shortest_tree(start), []
    while u != s:
        if u < 0:
            raise ValueError(f"unreachable route cell: {start} -> {end}")
        path.append((u % W * CELL + CELL / 2, u // W * CELL + CELL / 2))
        u = int(parent[u])
    path.append((start[0] * CELL + CELL / 2,
                 start[1] * CELL + CELL / 2))
    return tuple(engine.string_pull(FREE, path[::-1], CELL))


def path_length(pts):
    return sum(math.hypot(q[0] - p[0], q[1] - p[1])
               for p, q in zip(pts, pts[1:]))

def orient(groups, start, end, leg=leg_path):
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

def routed(groups, start, end, leg=leg_path):
    directed = orient(groups, start, end, leg)
    cells = [start] + [p["route_cell"] for g in directed for p in g] + [end]
    return join(leg(a, b) for a, b in zip(cells, cells[1:])), directed

def walked_m(pts):
    """Length of the drawn line — what the shopper actually walks."""
    return path_length(pts) / CELL * M_PER_CELL


@lru_cache(maxsize=128)
def anchor_order(anchor_keys):
    names = ("ENTRANCE", "CHECKOUT") + anchor_keys
    ids = [IDX[n] for n in names]
    Dsub = [[int(D[a][b]) for b in ids] for a in ids]
    return tuple(engine.tsp_order(Dsub, len(anchor_keys))[1])

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

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/geometry")
def geometry():
    return GEOM


@app.get("/api/walkability.png")
def walkability():
    return FileResponse("data/659/qa/walkable_overlay.png")


@app.get("/api/heb/status")
def heb_status():
    return app.state.heb.status()


@app.post("/api/heb/connect")
async def heb_connect():
    try:
        return await app.state.heb.connect()
    except HEBConnectionError as e:
        raise HTTPException(503, str(e)) from e


@app.post("/api/heb/connect/confirm")
async def heb_confirm():
    try:
        return await app.state.heb.confirm()
    except (HEBConnectionError, ValueError) as e:
        raise HTTPException(503, str(e)) from e


@app.get("/api/products")
async def products(q: str = Query(min_length=3)):
    q = q.strip()
    if len(q) < 3:
        raise HTTPException(422, "search requires at least 3 characters")
    try:
        return {"products": await app.state.heb.search(q)}
    except (HEBConnectionError, ValueError) as e:
        raise HTTPException(503, str(e)) from e


MAX_SNAP_M = 5.0        # nothing on a shelf is further than this from a corridor


def snap_distance_m(point):
    """How far a shopper would be walked off `point` to stand somewhere legal.

    Large means the point is not on the shopping floor at all, so whatever
    produced it should not be believed.
    """
    try:
        cell = engine.snap(FREE, REACH, point, CELL)
    except (TypeError, ValueError):
        return math.inf
    return math.hypot(cell[0] * CELL + CELL / 2 - point[0],
                      cell[1] * CELL + CELL / 2 - point[1]) / CELL * M_PER_CELL


def label_map_point(location_label, placement):
    """Where the printed shelf label says the product is."""
    label = re.sub(r"\s+", " ", (location_label or "").upper())
    aisle = re.search(r"\bAISLE\s+(\d+)\b", label)
    if aisle:
        number = int(aisle[1])
        anchor = f"AISLE {number + 4 if number >= 23 else number}"
        return GEOM["anchors"].get(anchor)

    aliases = {
        "FROZEN": "FROZEN FOODS",
        "MARKET": "MEAT",
        "CHECKSTANDS": "CHECKOUT",
    }
    for name in sorted(GEOM["anchors"], key=len, reverse=True):
        if not name.startswith("AISLE ") and name in label:
            return GEOM["anchors"][name]
    group = placement["group"].removeprefix("ANCHOR:")
    return GEOM["anchors"].get(aliases.get(group, group))


def exact_map_point(location_label, placement):
    """Map the live Atlas placement onto the accepted #659 guide profile.

    A PSA is physical shelf geometry and is transformed. A department ANCHOR
    is a text label placed by eye — the two drawings put "Dairy" 50 pt apart —
    so it is matched to the guide's own anchor by NAME instead of warped.
    """
    exact = None
    if placement["group"].startswith("PSA:") and placement.get("point"):
        exact = atlas_to_guide(
            on_corridor(placement["group"], placement["point"]))
        if snap_distance_m(exact) <= MAX_SNAP_M:
            return exact
        # H-E-B answers for some bulk packs with a pallet slot off the shopping
        # floor while its own label names a real aisle — 16|88 sits in the
        # bottom-left vestibule and is labelled "Aisle 13". Snapping such a
        # point to the nearest legal cell silently parks the product at the
        # entrance, so believe the label instead.
        log.warning("placement %s maps %s off the floor; using the label %r",
                    placement["group"], [round(v, 1) for v in exact],
                    location_label)

    named = label_map_point(location_label, placement)
    return named if named is not None else exact


@app.post("/api/products/locate")
async def locate_products(req: LocateReq):
    located = []
    for model in req.products:
        product = model.model_dump()
        try:
            placement = await app.state.heb.locate(
                model.id, model.location_label, ATLAS)
        except (HEBConnectionError, ValueError) as e:
            raise HTTPException(503, str(e)) from e
        result = product | {
            "routable": False,
            "approx": None,
            "placement_group": None,
        }
        if placement:
            point = exact_map_point(
                placement.get("location_label") or model.location_label,
                placement)
            try:
                route_cell = engine.snap(FREE, REACH, point, CELL)
            except (TypeError, ValueError):
                log.warning("locate %s %r group=%s atlas=%s mapped=%s NO-SNAP",
                            model.id, model.location_label, placement["group"],
                            placement.get("point"), point)
                placement = None
            else:
                x = route_cell[0] * CELL + CELL / 2
                y = route_cell[1] * CELL + CELL / 2
                # Two error sources stack here: the Atlas->guide warp that
                # produced `point`, and the walk to the nearest walkable cell.
                # Logging both separately is what tells them apart.
                log.info(
                    "locate %s %r psa=%s group=%s atlas=%s mapped=%.1f,%.1f "
                    "shown=%.1f,%.1f snap=%.1f",
                    model.id, placement.get("location_label")
                    or model.location_label, placement.get("psa_key"),
                    placement["group"], placement.get("point"),
                    point[0], point[1], x, y, math.hypot(x - point[0], y - point[1]))
                result |= {
                    "routable": True,
                    "approx": True,
                    "location_label": placement.get("location_label")
                                      or model.location_label,
                    "placement_group": placement["group"],
                    "x": x,
                    "y": y,
                    "route_cell": route_cell,
                }
        LOCATED_PRODUCTS[model.id] = result
        located.append({k: v for k, v in result.items()
                        if k != "route_cell"})
    return {"products": located}

def selected_route(items):
    """Route resolved H-E-B products on the exact #659 walkable profile."""
    quantities, requested = {}, []
    for item in items:
        if item.product_id not in quantities:
            requested.append(item.product_id)
            quantities[item.product_id] = 0
        quantities[item.product_id] += item.quantity

    groups, unrouted = {}, []
    for product_id in requested:
        product = LOCATED_PRODUCTS.get(product_id)
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
    cells = [START, END] + representatives
    matrix = [[0.0] * len(cells) for _ in cells]
    for i, start in enumerate(cells):
        for j in range(i + 1, len(cells)):
            distance = path_length(leg_path(start, cells[j]))
            matrix[i][j] = matrix[j][i] = distance
    order = engine.tsp_order(matrix, len(group_keys))[1]
    ordered_keys = [group_keys[index - 2] for index in order]

    path, directed = routed(
        [groups[key] for key in ordered_keys],
        START, END, leg_path)
    baseline_path, _ = routed(
        [groups[key] for key in group_keys],
        START, END, leg_path)
    baseline = walked_m(baseline_path)
    distance = walked_m(path)

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
                "approximation_state": (
                    "approximate" if product["approx"] else "exact"),
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
def route(req: RouteReq):
    if not req.items:
        raise HTTPException(400, "empty list")
    if not all(isinstance(item, str) for item in req.items):
        if any(isinstance(item, str) for item in req.items):
            raise HTTPException(400, "cannot mix product IDs and free text")
        return selected_route(req.items)
    matched, unmatched = resolve(req.items, DIRECTORY)

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
    order = anchor_order(tuple(anchor_keys))

    # Where each product sits. Segment t gives the canonical forward order;
    # orient() later chooses forward or reverse from the surrounding route.
    picks = {}
    for si in order:
        key = names[si]
        here = anchor_xy(key)
        fallback_cell = tuple(int(v) for v in CELLS[IDX[key]])
        placed = [item_at(entry, here) | {"query": query, "anchor": key}
                  for query, entry in stops[key]]
        for p in placed:
            p["route_cell"] = tuple(p["cell"]) if p.get("cell") else fallback_cell
        picks[key] = sorted(placed, key=lambda p: p["t"])

    route_keys = [names[si] for si in order]
    path, directed = routed([picks[k] for k in route_keys], START, END)

    # G1 telemetry: the same list walked in the order it was written, measured
    # the same way, so "you saved N m" is a real comparison and not two metrics.
    # Both choose their best aisle directions, so the difference is ordering.
    list_keys = list(dict.fromkeys(m["anchors"][0] for m in matched))
    baseline_path, _ = routed([picks[k] for k in list_keys], START, END)
    baseline = walked_m(baseline_path)

    out_stops, n = [], 1
    for group in directed:
        for p in group:
            out_stops.append({"n": n, "item": p["query"], "anchor": p["anchor"],
                              "x": p["x"], "y": p["y"], "approx": p["approx"]})
            n += 1
    return {"stops": out_stops, "path": path,
            "distance_m": round(walked_m(path), 1),
            "baseline_m": round(baseline, 1),
            "saved_m": round(baseline - walked_m(path), 1),
            "unmatched": unmatched}
