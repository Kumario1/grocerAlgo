#!/usr/bin/env python3
"""One-shot: H-E-B directory PDF (page 1 = map) -> geometry JSON.

Usage: python3 extract_659.py [store]   (default 659)
Reads guide-austin-<store>.pdf, writes data/<store>/geometry.json.
"""
import json, os, sys, fitz

STORE = "659"
PDF = f"guide-austin-{STORE}.pdf"
OUT = f"data/{STORE}/geometry.json"
WHITE = (1.0, 1.0, 1.0)

def extract():
    page = fitz.open(PDF)[1]
    words = page.get_text("words")  # (x0, y0, x1, y1, text, ...)

    anchors, seen = {}, []
    for x0, y0, x1, y1, t, *_ in words:
        if t.isdigit() and 1 <= int(t) <= 60:
            seen.append(int(t))
            anchors[f"AISLE {int(t)}"] = [(x0 + x1) / 2, (y0 + y1) / 2]
    # aisle badges must be a clean 1..N run, each number exactly once
    # (store #659: 45, store #24: 43); duplicates or holes mean the map
    # page carries stray digits and needs a smarter filter.
    aisles = sorted(seen)
    assert len(aisles) >= 20 and aisles == list(range(1, len(aisles) + 1)), \
        f"aisle badges not a clean 1..N run: {aisles}"

    # Multi-word labels (Entrance, Check Stands, department names): join words
    # that share a line, then keep known label phrases.
    lines = {}
    for x0, y0, x1, y1, t, *rest in words:
        key = (round(y0 / 4), rest[0] if rest else 0)   # line bucket
        lines.setdefault(key, []).append((x0, t, (x0 + x1) / 2, (y0 + y1) / 2))
    for parts in lines.values():
        parts.sort()
        phrase = " ".join(p[1] for p in parts).upper()
        cx = sum(p[2] for p in parts) / len(parts)
        cy = sum(p[3] for p in parts) / len(parts)
        for label in ("ENTRANCE", "EXIT", "CHECK", "PRODUCE", "BAKERY", "DELI",
                      "SEAFOOD", "MEAT", "DAIRY", "FLORAL", "PHARMACY",
                      "FROZEN", "KITCHEN", "RESTROOM"):
            if label in phrase:
                anchors.setdefault(phrase if len(phrase) < 30 else label,
                                   [cx, cy])

    # InDesign splits each shelf into TWO drawings: a white fill-only body
    # ("f", fill=white) and a stroke-only outline ("s" with "re" items).
    # Fill color is therefore useless as a furniture signal — the stroked
    # rectangle is the shelf. Fixtures = stroked rects + all "fs" combos +
    # colored fills; white fill-only rects (bodies/background) are skipped
    # since their outlines already carry them. Page-scale rects (store
    # perimeter, background frames) are walls, not furniture.
    max_fixture_area = 0.10 * page.rect.width * page.rect.height
    fixtures, obstacle_paths = [], []

    def add_fixture(r):
        if 0 < (r.x1 - r.x0) * (r.y1 - r.y0) < max_fixture_area:
            fixtures.append([r.x0, r.y0, r.x1, r.y1])

    for dr in page.get_drawings():
        if dr["type"] == "fs" or (dr["type"] == "f"
                                  and dr.get("fill") not in (None, WHITE)):
            add_fixture(dr["rect"])
        elif dr["type"] == "s":
            for item in dr["items"]:
                if item[0] == "l":                       # line segment
                    a, b = item[1], item[2]
                    obstacle_paths.append([[a.x, a.y], [b.x, b.y]])
                elif item[0] == "re":                    # stroked rect: fixture
                    add_fixture(item[1])                 # ...and 4 wall edges
                    r = item[1]
                    c = [[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1], [r.x0, r.y1]]
                    obstacle_paths += [[c[i], c[(i + 1) % 4]] for i in range(4)]

    # self-check: a real supermarket has >100 store-sized fixtures; catching
    # the "kept only decorative confetti" failure mode (2026-07-21 bug).
    big = sum((x1 - x0) * (y1 - y0) > 200 for x0, y0, x1, y1 in fixtures)
    assert big >= 100, f"only {big} store-sized fixtures — wrong fill/stroke filter?"

    # Sales-floor boundary: the map draws the interior outline as one CLOSED
    # thick-stroke polyline (store #659: 18 segments, stroke width ~1.85).
    # Everything outside it (parking, drive-thru, curbside) is not walkable.
    boundary = []
    for dr in page.get_drawings():
        if dr["type"] != "s" or not dr.get("width") or dr["width"] < 1.5:
            continue
        pts = []
        for item in dr["items"]:
            if item[0] == "l":
                if not pts:
                    pts.append([item[1].x, item[1].y])
                pts.append([item[2].x, item[2].y])
        if len(pts) > 4 and pts[0] == pts[-1]:            # closed chain
            r = dr["rect"]
            if ((r.x1 - r.x0) > 0.6 * page.rect.width
                    and (r.y1 - r.y0) > 0.6 * page.rect.height
                    and len(pts) > len(boundary)):
                boundary = pts
    assert boundary, "no closed thick-stroke boundary polygon found"

    geom = {"page": {"w": page.rect.width, "h": page.rect.height},
            "anchors": anchors, "fixtures": fixtures,
            "obstacle_paths": obstacle_paths, "boundary": boundary}
    json.dump(geom, open(OUT, "w"))
    print(f"{len(anchors)} anchors, {len(fixtures)} fixtures, "
          f"{len(obstacle_paths)} wall segments, "
          f"boundary {len(boundary)} vertices -> {OUT}")
    return geom

def overlay(geom):
    """QA: render extracted geometry over the real map page -> PNG."""
    from PIL import Image, ImageDraw
    page = fitz.open(PDF)[1]
    pix = page.get_pixmap(dpi=144)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    s = pix.width / geom["page"]["w"]
    dr = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in geom["fixtures"]:
        dr.rectangle([x0 * s, y0 * s, x1 * s, y1 * s], outline="red")
    dr.line([(x * s, y * s) for x, y in geom["boundary"]], fill="green", width=4)
    for k, (x, y) in geom["anchors"].items():
        dr.ellipse([x * s - 4, y * s - 4, x * s + 4, y * s + 4], fill="blue")
    path = OUT.replace("geometry.json", "extract_overlay.png")
    im.save(path)
    print(f"overlay -> {path}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        STORE = sys.argv[1]
        PDF = f"guide-austin-{STORE}.pdf"
        OUT = f"data/{STORE}/geometry.json"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    overlay(extract())
