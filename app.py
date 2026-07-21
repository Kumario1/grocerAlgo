#!/usr/bin/env python3
"""Phase 1 web app: list in -> optimal #659 route out. All data preloaded."""
import json
import numpy as np
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
PARENTS = _p["parents"]
FREE = _p["free"]
M_PER_CELL = float(_p["m_per_cell"])
CELL = float(_p["cell"])
W = FREE.shape[1]
DIRECTORY = {**load_directory("data/659/directory.csv", set(NAMES)),
             **load_directory("data/659/departments.csv", set(NAMES))}

class RouteReq(BaseModel):
    items: list[str]

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/geometry")
def geometry():
    return GEOM

@app.post("/api/route")
def route(req: RouteReq):
    if not req.items:
        raise HTTPException(400, "empty list")
    matched, unmatched = resolve(req.items, DIRECTORY)

    # consolidate: one stop per anchor (first anchor for multi-anchor entries)
    # ponytail: first-anchor pick; route-aware anchor choice if corrections say otherwise
    stops = {}
    for m in matched:
        stops.setdefault(m["anchors"][0], []).append(m["query"])
    anchor_keys = list(stops)
    if not anchor_keys:
        raise HTTPException(422, "no item could be located")

    names = ["ENTRANCE", "CHECKOUT"] + anchor_keys
    ids = [IDX[n] for n in names]
    Dsub = [[int(D[a][b]) for b in ids] for a in ids]
    cost, order = engine.tsp_order(Dsub, len(anchor_keys))

    seq = [0] + order + [1]                       # Dsub indices, ENTRANCE..CHECKOUT
    path = []
    for a, b in zip(seq, seq[1:]):
        par = PARENTS[ids[a]]
        bx, by = CELLS[ids[b]]
        path += engine.trace(par, W, (int(bx), int(by)))

    out_stops = []
    for n, si in enumerate(order, 1):
        key = names[si]
        cx, cy = CELLS[IDX[key]]
        out_stops.append({"n": n, "anchor": key, "items": stops[key],
                          "x": float(cx) * CELL + CELL / 2,
                          "y": float(cy) * CELL + CELL / 2})
    return {"stops": out_stops, "path": path,
            "distance_m": round(cost * M_PER_CELL, 1),
            "unmatched": unmatched}
