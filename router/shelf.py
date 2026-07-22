"""Per-item shelf positions, from the category labels printed on the guide (§8.3).

An aisle NUMBER is printed at the aisle mouth; the CATEGORY names are printed
down the aisle, beside the products they label. Matching directory items to
those labels is what lets two items in one aisle show up as two stops where the
products actually are, instead of collapsing to one pin at the aisle entrance.

Items with no printed label keep the aisle position and are flagged `approx`,
so the UI can say "somewhere in aisle 31" rather than inventing a shelf.

Build-time only.
"""
import collections

import numpy as np
from rapidfuzz import fuzz, process

from router import corridor

GAP_PT = 4.0          # x-gap that separates two labels sharing a text line
SCORE = 88            # rapidfuzz token_set_ratio floor for "this is that label"
MAX_PT = 220.0        # ~26 m: a label further than this from its aisle is a
                      # different department that happens to share a word


def label_phrases(page, gap=GAP_PT):
    """Printed labels as (text, x, y) centres, in PDF points.

    PyMuPDF returns words, and a single text line often carries several
    unrelated labels side by side ("Diabetic Aids" and "Vitamins"), so lines
    are split wherever the horizontal gap opens up.
    """
    lines = collections.defaultdict(list)
    for x0, y0, x1, y1, text, block, line, _ in page.get_text("words"):
        lines[(block, line)].append((x0, y0, x1, y1, text))
    out = []
    for words in lines.values():
        words.sort()
        groups, cur = [], [words[0]]
        for prev, nxt in zip(words, words[1:]):
            if nxt[0] - prev[2] > gap:
                groups.append(cur)
                cur = []
            cur.append(nxt)
        groups.append(cur)
        for grp in groups:
            xs = [c for g in grp for c in (g[0], g[2])]
            ys = [c for g in grp for c in (g[1], g[3])]
            out.append((" ".join(g[4] for g in grp),
                        (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return out


def positions(directory, anchors, phrases, max_pt=MAX_PT):
    """item -> {x, y, anchor, approx}.

    A label's text is not unique on the page — #659 prints "Rice" in two
    different aisles — so every candidate above the score floor is considered
    and the one nearest the item's own aisle wins. Without that tie-break an
    item lands in the wrong half of the store.
    """
    names = [t for t, _, _ in phrases]
    out = {}
    for item, keys in directory.items():
        anchor = keys[0]
        ax, ay = anchors.get(anchor, (None, None))
        hits = process.extract(item, names, scorer=fuzz.token_set_ratio,
                               score_cutoff=SCORE, limit=8)
        best = None
        for _, _, idx in hits:
            _, x, y = phrases[idx]
            d = float(np.hypot(x - ax, y - ay)) if ax is not None else 0.0
            if d <= max_pt and (best is None or d < best[0]):
                best = (d, x, y)
        if best:
            out[item] = {"x": best[1], "y": best[2], "anchor": anchor,
                         "approx": False}
        elif ax is not None:
            out[item] = {"x": float(ax), "y": float(ay), "anchor": anchor,
                         "approx": True}
    return out


def build(page, directory, anchors, g, free=None, cell=None):
    """Everything the request path needs to place, order and reach items.

    Item `t` is precomputed against the aisle's corridor segment, so ordering
    at request time is a sort on a number already in the profile. `cell` is the
    walkable cell a shopper stands in to reach the product — the label itself
    is printed on the shelf, which is not somewhere you can stand.
    """
    from router import engine

    cell = cell or engine.CELL
    phrases = label_phrases(page)
    placed = corridor.assign(g, anchors)
    items = positions(directory, anchors, phrases)
    for item, p in items.items():
        seg = placed.get(p["anchor"])
        p["t"] = 0.0 if seg is None else corridor._project_edge(
            g["edges"][seg["edge"]], np.array([p["x"], p["y"]]))[1]
        if free is not None:
            try:
                cx, cy = engine.nearest_free(free, (p["x"], p["y"]), cell)
                p["cell"] = [int(cx), int(cy)]
            except ValueError:
                pass                       # nowhere to stand; route to the aisle
    segments = {}
    for name, seg in placed.items():
        e = g["edges"][seg["edge"]]
        segments[name] = {"a": [float(v) for v in e["pts"][0]],
                          "b": [float(v) for v in e["pts"][-1]],
                          "length": e["length"], "t": seg["t"]}
    return {"items": items, "segments": segments}


def walk_order(items, segment, entry_xy):
    """Indices of `items` in the order you pass them walking from `entry_xy`.

    §8.5's entry-direction-aware intra-segment order: which end of the aisle you
    come in from decides whether t runs forwards or backwards.
    """
    if len(items) < 2:
        return list(range(len(items)))
    a, b = np.asarray(segment["a"]), np.asarray(segment["b"])
    e = np.asarray(entry_xy, float)
    forward = np.hypot(*(e - a)) <= np.hypot(*(e - b))
    ts = [it.get("t", 0.0) for it in items]
    return sorted(range(len(items)), key=lambda i: ts[i] if forward else -ts[i])
