#!/usr/bin/env python3
"""Phase 1 web app: list in -> optimal #659 route out. All data preloaded."""
import json
import math
import os
from functools import lru_cache
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import breadth_first_order
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from router import engine
from router.directory import load_directory
from router.resolve import resolve

app = FastAPI(title="grocerAlgo — HEB #659")

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
DIRECTORY = {**load_directory("data/659/directory.csv", set(NAMES)),
             **load_directory("data/659/departments.csv", set(NAMES))}

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

def orient(groups, start, end):
    """Choose forward/reverse item order for each aisle in a fixed TSP order."""
    variants = [(g, list(reversed(g))) for g in groups]
    costs, previous = [], []
    for i, pair in enumerate(variants):
        row, back = [math.inf, math.inf], [-1, -1]
        for direction, group in enumerate(pair):
            inside = sum(path_length(leg_path(a["route_cell"], b["route_cell"]))
                         for a, b in zip(group, group[1:]))
            if i == 0:
                row[direction] = path_length(leg_path(start, group[0]["route_cell"])) + inside
            else:
                for prior_direction, prior in enumerate(variants[i - 1]):
                    candidate = costs[-1][prior_direction] + path_length(leg_path(
                        prior[-1]["route_cell"], group[0]["route_cell"])) + inside
                    if candidate < row[direction]:
                        row[direction], back[direction] = candidate, prior_direction
        costs.append(row)
        previous.append(back)
    direction = min(range(2), key=lambda d: costs[-1][d] + path_length(
        leg_path(variants[-1][d][-1]["route_cell"], end)))
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

def routed(groups, start, end):
    directed = orient(groups, start, end)
    cells = [start] + [p["route_cell"] for g in directed for p in g] + [end]
    return join(leg_path(a, b) for a, b in zip(cells, cells[1:])), directed

def walked_m(pts):
    """Length of the drawn line — what the shopper actually walks."""
    return path_length(pts) / CELL * M_PER_CELL

@lru_cache(maxsize=128)
def anchor_order(anchor_keys):
    names = ("ENTRANCE", "CHECKOUT") + anchor_keys
    ids = [IDX[n] for n in names]
    Dsub = [[int(D[a][b]) for b in ids] for a in ids]
    return tuple(engine.tsp_order(Dsub, len(anchor_keys))[1])

class RouteReq(BaseModel):
    items: list[str]

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/geometry")
def geometry():
    return GEOM

@app.get("/api/map.png")
def map_png():
    """The real guide page as the map background, so a route is checked against
    the store as drawn rather than against our own boxes. Rendered once, cached
    into the (gitignored) qa dir. 404s when the guide PDF isn't around — the
    fixture outlines below it still render."""
    path = "data/659/qa/map.png"
    if not os.path.exists(path):
        try:
            import fitz
            from router import derive, raster
            os.makedirs(os.path.dirname(path), exist_ok=True)
            page = fitz.open(derive.pdf_path("659"))[1]
            raster.render_source(page, GEOM, dpi=144).save(path)
        except Exception as e:
            raise HTTPException(404, f"no map render: {e}")
    return FileResponse(path)

@app.post("/api/route")
def route(req: RouteReq):
    if not req.items:
        raise HTTPException(400, "empty list")
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

    start = tuple(int(v) for v in CELLS[IDX["ENTRANCE"]])
    end = tuple(int(v) for v in CELLS[IDX["CHECKOUT"]])
    route_keys = [names[si] for si in order]
    path, directed = routed([picks[k] for k in route_keys], start, end)

    # G1 telemetry: the same list walked in the order it was written, measured
    # the same way, so "you saved N m" is a real comparison and not two metrics.
    # Both choose their best aisle directions, so the difference is ordering.
    list_keys = list(dict.fromkeys(m["anchors"][0] for m in matched))
    baseline_path, _ = routed([picks[k] for k in list_keys], start, end)
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
