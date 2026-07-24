#!/usr/bin/env python3
"""Demo routes drawn on the real store map — the walkability + visibility check.

Seeded random cross-store pairs (plus ENTRANCE->CHECKOUT and a couple of anchor
pairs) are traced across the profile's walkable grid, smoothed per leg, and
validated: every drawn segment must stay on walkable floor. The result is
rendered over the real guide map so it can be eyeballed, and the command exits
nonzero if any path is illegal.

Usage: python3 route_demo.py [store] [--n N] [--seed S] [--out PATH] [--raw]
       (default store 659; writes data/<store>/qa/route_demo.png)
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

from router import engine

COLORS = ["#1a73e8", "#d93025", "#188038", "#f29900", "#9334e6",
          "#00897b", "#c2185b", "#5d4037", "#455a64", "#e65100"]

# departments worth a cross-store demo leg, in the order we prefer them
DEMO_DEPTS = ("PRODUCE", "DAIRY", "FROZEN FOODS", "BAKERY", "MEAT", "DELI")

# the acceptance list (G2/G3 in the plan) — one canonical copy, also used by
# tests/test_api.py and, once frozen, by the route golden
ACCEPTANCE_LIST = ["milk", "bananas", "shrimp", "sourdough bread", "sliced turkey",
                   "tortilla chips", "pasta", "ice cream", "paper towels", "toothpaste",
                   "dog food", "shampoo", "cereal", "coffee", "olive oil", "rice",
                   "frozen pizza", "yogurt", "eggs", "butter", "salsa", "peanut butter",
                   "dish soap", "trash bags", "batteries"]


def load(store):
    """Everything the demo needs from the store profile."""
    p = np.load(f"data/{store}/profile.npz", allow_pickle=True)
    names = [str(n) for n in p["names"]]
    return {"free": p["free"], "cell": float(p["cell"]),
            "m_per_cell": float(p["m_per_cell"]), "names": names, "D": p["D"],
            "cells": p["cells"], "idx": {n: i for i, n in enumerate(names)}}


def demo_pairs(prof, n=6, seed=1):
    """Cell pairs to route: named anchors first, then seeded random crossings.

    Random pairs must be at least 40% of the store diagonal apart so they
    genuinely cross the floor instead of hopping between neighbouring cells.
    """
    free, cells, idx, names = prof["free"], prof["cells"], prof["idx"], prof["names"]
    h, w = free.shape

    def at(name):
        return (int(cells[idx[name]][0]), int(cells[idx[name]][1]))

    pairs = []
    if "ENTRANCE" in idx and "CHECKOUT" in idx:
        pairs.append(("ENTRANCE -> CHECKOUT", at("ENTRANCE"), at("CHECKOUT")))
    aisles = sorted((s for s in names if s.startswith("AISLE ")),
                    key=lambda s: int(s.split()[1]))
    if len(aisles) >= 2:
        pairs.append((f"{aisles[0]} -> {aisles[-1]}", at(aisles[0]), at(aisles[-1])))
    depts = [d for d in DEMO_DEPTS if d in idx]
    if len(depts) >= 2:
        pairs.append((f"{depts[0]} -> {depts[-1]}", at(depts[0]), at(depts[-1])))

    ys, xs = np.where(free)
    rng = np.random.default_rng(seed)
    min_sep = 0.40 * float(np.hypot(w, h))
    want, tries = len(pairs) + n, 0
    while len(pairs) < want and tries < 20000:
        tries += 1
        i, j = rng.integers(0, len(xs), 2)
        a, b = (int(xs[i]), int(ys[i])), (int(xs[j]), int(ys[j]))
        if np.hypot(a[0] - b[0], a[1] - b[1]) >= min_sep:
            pairs.append((f"random #{len(pairs) - 2}", a, b))
    return pairs


def length_m(pts, cell, m_per_cell):
    return sum(np.hypot(q[0] - p[0], q[1] - p[1])
               for p, q in zip(pts, pts[1:])) / cell * m_per_cell


def demo_paths(prof, pairs):
    """Trace + smooth + legality-check each pair. One dict per path."""
    free, cell, w = prof["free"], prof["cell"], prof["free"].shape[1]
    out = []
    for label, a, b in pairs:
        dist, par = engine.bfs(free, a)
        if dist[b[1] * w + b[0]] < 0:
            out.append({"label": label, "raw": [], "smooth": [],
                        "bad_raw": [], "bad_smooth": [], "unreachable": True})
            continue
        raw = engine.trace(par, w, b, cell)
        smooth = engine.string_pull(free, raw, cell)
        out.append({"label": label, "raw": raw, "smooth": smooth,
                    "bad_raw": engine.path_is_legal(free, raw, cell),
                    "bad_smooth": engine.path_is_legal(free, smooth, cell),
                    "unreachable": False})
    return out


def render(prof, paths, store, out_path, draw_raw=False, mask=False):
    """Draw the paths over the real guide map (same overlay recipe as map_qa)."""
    import fitz
    from router import derive, raster

    geom = json.load(open(f"data/{store}/geometry.json"))
    page = fitz.open(derive.pdf_path(store))[1]
    im = raster.render_source(page, geom, dpi=144).convert("RGB")
    S = im.width / geom["page"]["w"]                     # pdf-pt -> render px
    if mask:      # tint the walkable grid so "does the line stay on floor" is visible
        mm = Image.fromarray(prof["free"].astype(np.uint8) * 255).resize(
            im.size, Image.NEAREST)
        im = Image.composite(
            Image.blend(im, Image.new("RGB", im.size, (30, 160, 60)), 0.30), im, mm)
    dr = ImageDraw.Draw(im)
    for k, p in enumerate(paths):
        if not p["smooth"]:
            continue
        color = COLORS[k % len(COLORS)]
        if draw_raw:                                     # the BFS staircase, faint
            dr.line([(x * S, y * S) for x, y in p["raw"]], fill="#b0b0b0", width=2)
        dr.line([(x * S, y * S) for x, y in p["smooth"]],
                fill=color, width=5, joint="curve")
        (sx, sy), (ex, ey) = p["smooth"][0], p["smooth"][-1]
        r = 9
        dr.ellipse([sx * S - r, sy * S - r, sx * S + r, sy * S + r],
                   fill=color, outline="#ffffff", width=3)
        dr.rectangle([ex * S - r, ey * S - r, ex * S + r, ey * S + r],
                     fill=color, outline="#ffffff", width=3)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path)
    return im


def live_route(items=None):
    """The app's real route for a shopping list, as (path, stops).

    Imported lazily: loading app.py pulls in the whole profile + directory.
    """
    from fastapi.testclient import TestClient
    import app as webapp

    body = TestClient(webapp.app).post(
        "/api/route", json={"items": items or ACCEPTANCE_LIST}).json()
    return body["path"], body["stops"], body["distance_m"]


def render_live(prof, store, out_path, items=None):
    """Draw the real route + numbered stops on the real map."""
    path, stops, dist_m = live_route(items)
    im = render(prof, [], store, out_path)               # map background only
    geom = json.load(open(f"data/{store}/geometry.json"))
    S = im.width / geom["page"]["w"]
    dr = ImageDraw.Draw(im)
    dr.line([(x * S, y * S) for x, y in path], fill="#1a73e8", width=6, joint="curve")
    for s in stops:
        x, y, r = s["x"] * S, s["y"] * S, 15
        dr.ellipse([x - r, y - r, x + r, y + r], fill="#d93025", outline="#fff", width=3)
        dr.text((x - (4 if s["n"] < 10 else 9), y - 7), str(s["n"]), fill="#fff")
    for pt in (path[0], path[-1]):
        x, y, r = pt[0] * S, pt[1] * S, 17
        dr.ellipse([x - r, y - r, x + r, y + r], fill="#188038", outline="#fff", width=3)
    im.save(out_path)
    return path, stops, dist_m


def render_corridor(prof, store, out_path):
    """Draw the §8.1 corridor graph and where each anchor lands on it."""
    from router import corridor, derive

    g = corridor.build(prof["free"], prof["cell"])
    cfg = derive.load_store(f"data/{store}")
    fixed = corridor.assign(g, cfg["anchors"])
    im = render(prof, [], store, out_path)
    geom = json.load(open(f"data/{store}/geometry.json"))
    S = im.width / geom["page"]["w"]
    dr = ImageDraw.Draw(im)
    for e in g["edges"]:
        dr.line([(x * S, y * S) for x, y in e["pts"]], fill="#1a73e8", width=3)
    for x, y in g["nodes"]:
        dr.ellipse([x * S - 3, y * S - 3, x * S + 3, y * S + 3], fill="#f29900")
    for name, a in fixed.items():                    # anchor -> its segment
        ax, ay = cfg["anchors"][name]
        px, py = a["xy"]
        dr.line([(ax * S, ay * S), (px * S, py * S)], fill="#d93025", width=3)
        dr.ellipse([px * S - 6, py * S - 6, px * S + 6, py * S + 6],
                   fill="#d93025", outline="#fff", width=2)
    im.save(out_path)
    return g, fixed


# department anchors worth a directional tour, in rough preference order — any
# not present in a given store are simply skipped (route_directional.png)
TOUR_DEPTS = ("PRODUCE", "BAKERY", "SEAFOOD", "MEAT", "FISH/MEAT/CHILI", "DELI",
              "MARKET/DELI", "DAIRY", "FROZEN FOODS", "KITCHEN")


def render_directional(prof, store, out_path):
    """ENTRANCE -> TSP over the store's departments -> CHECKOUT, drawn with
    travel-direction arrows (same look as the web app's marker-mid route)."""
    idx, cells, free, cell, w = (prof["idx"], prof["cells"], prof["free"],
                                 prof["cell"], prof["free"].shape[1])
    depts = [d for d in TOUR_DEPTS if d in idx]
    names = ("ENTRANCE", "CHECKOUT") + tuple(depts)
    ids = [idx[n] for n in names]
    Dsub = [[int(prof["D"][a][b]) for b in ids] for a in ids]
    _, order = engine.tsp_order(Dsub, len(depts))
    tour = ["ENTRANCE"] + [names[i] for i in order] + ["CHECKOUT"]

    def acell(n):
        return (int(cells[idx[n]][0]), int(cells[idx[n]][1]))

    path, stops = [], []
    for a, b in zip(tour, tour[1:]):
        dist, par = engine.bfs(free, acell(a))
        bc = acell(b)
        sm = engine.string_pull(free, engine.trace(par, w, bc, cell), cell)
        path += sm if not path else sm[1:]
        if b not in ("ENTRANCE", "CHECKOUT"):
            stops.append((sm[-1], b))

    im = render(prof, [], store, out_path)               # map background only
    geom = json.load(open(f"data/{store}/geometry.json"))
    S = im.width / geom["page"]["w"]
    dr = ImageDraw.Draw(im)
    dr.line([(x * S, y * S) for x, y in path], fill="#1a73e8",
            width=max(2, round(2 * S)), joint="curve")
    size = 6 * S
    for i in range(1, len(path)):                        # arrowhead per ~28pt step
        (ax, ay), (bx, by) = path[i - 1], path[i]
        seg = np.hypot(bx - ax, by - ay)
        if seg < 1e-6:
            continue
        n = max(1, int(np.ceil(seg / 28)))
        ux, uy = (bx - ax) / seg, (by - ay) / seg
        for k in range(1, n + 1):
            px, py = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
            cx, cy = px * S, py * S
            dr.polygon([(cx + ux * size, cy + uy * size),
                        (cx - ux * size + -uy * size * 0.7,
                         cy - uy * size + ux * size * 0.7),
                        (cx - ux * size - -uy * size * 0.7,
                         cy - uy * size - ux * size * 0.7)], fill="#1a73e8")
    for k, ((sx, sy), _name) in enumerate(stops, 1):     # numbered red stops
        x, y, r = sx * S, sy * S, 15
        dr.ellipse([x - r, y - r, x + r, y + r], fill="#d93025", outline="#fff", width=3)
        dr.text((x - (4 if k < 10 else 9), y - 7), str(k), fill="#fff")
    for pt in (path[0], path[-1]):                       # green start / end
        x, y, r = pt[0] * S, pt[1] * S, 17
        dr.ellipse([x - r, y - r, x + r, y + r], fill="#188038", outline="#fff", width=3)
    im.save(out_path)
    return tour, engine.path_is_legal(free, path, cell)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("store", nargs="?", default="659")
    ap.add_argument("--n", type=int, default=6, help="random cross-store paths")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="default data/<store>/qa/route_demo.png")
    ap.add_argument("--raw", action="store_true",
                    help="also draw the unsmoothed BFS staircase")
    ap.add_argument("--mask", action="store_true",
                    help="tint the walkable grid under the paths")
    ap.add_argument("--route", action="store_true",
                    help="render the app's real route for the acceptance list instead")
    ap.add_argument("--corridor", action="store_true",
                    help="render the corridor graph and anchor->segment assignment")
    ap.add_argument("--directional", action="store_true",
                    help="render a department tour with travel-direction arrows")
    a = ap.parse_args()

    prof = load(a.store)
    if a.directional:
        out = a.out or f"data/{a.store}/qa/route_directional.png"
        tour, bad = render_directional(prof, a.store, out)
        print(f"  {len(tour)} anchors  {'FAIL ' + str(bad[:3]) if bad else 'PASS'}")
        print(f"-> {out}")
        return 1 if bad else 0
    if a.corridor:
        out = a.out or f"data/{a.store}/qa/corridor.png"
        g, fixed = render_corridor(prof, a.store, out)
        print(f"  {len(g['nodes'])} nodes, {len(g['edges'])} edges, "
              f"{len(fixed)} anchors projected")
        print(f"-> {out}")
        return 0
    if a.route:
        out = a.out or f"data/{a.store}/qa/route_live.png"
        path, stops, dist_m = render_live(prof, a.store, out)
        bad = engine.path_is_legal(prof["free"], path, prof["cell"])
        drawn = length_m(path, prof["cell"], prof["m_per_cell"])
        print(f"  {len(stops)} stops, {len(path)} pts, reported {dist_m} m, "
              f"drawn {drawn:.1f} m  {'FAIL ' + str(bad[:3]) if bad else 'PASS'}")
        print(f"-> {out}")
        return 1 if bad else 0
    paths = demo_paths(prof, demo_pairs(prof, a.n, a.seed))
    cell, mpc = prof["cell"], prof["m_per_cell"]

    failed = 0
    for p in paths:
        if p["unreachable"]:
            print(f"  {p['label']:<24} UNREACHABLE")
            failed += 1
            continue
        bad = p["bad_raw"] or p["bad_smooth"]
        failed += bool(bad)
        print(f"  {p['label']:<24} raw {len(p['raw']):5d} pts "
              f"{length_m(p['raw'], cell, mpc):7.1f} m  ->  smooth "
              f"{len(p['smooth']):4d} pts {length_m(p['smooth'], cell, mpc):7.1f} m  "
              f"{'FAIL ' + str(bad[:3]) if bad else 'PASS'}")

    out = a.out or f"data/{a.store}/qa/route_demo.png"
    render(prof, paths, a.store, out, draw_raw=a.raw, mask=a.mask)
    print(f"\n{len(paths)} paths, {failed} illegal -> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
