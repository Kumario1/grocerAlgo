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
    # shape is the shelf. Geometry is captured EXACTLY, never as bounding
    # boxes: axis-aligned rects stay rects, everything else (diagonal
    # counters, stepped/curved kiosks) keeps its vertex chain. Thin or
    # open chains are drawn wall lines, not furniture. White fill-only
    # drawings (bodies/background) are skipped since their stroke twins
    # carry the geometry. Page-scale shapes are frames, not furniture.
    W, H = page.rect.width, page.rect.height
    max_fixture_area = 0.10 * W * H
    fixtures, fixture_polys, obstacle_paths = [], [], []

    def chains(dr, bez_n=8):
        """Drawing items -> point chains. 're'/'qu' close themselves;
        beziers are sampled; a new chain starts when an item doesn't
        continue from the previous endpoint."""
        out, cur = [], []

        def flush():
            nonlocal cur
            if len(cur) >= 2:
                out.append(cur)
            cur = []

        def moveto(p):
            if not cur or abs(cur[-1][0] - p.x) > .05 or abs(cur[-1][1] - p.y) > .05:
                flush()
                cur.append([p.x, p.y])

        for it in dr["items"]:
            if it[0] == "re":
                flush()
                r = it[1]
                out.append([[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1],
                            [r.x0, r.y1], [r.x0, r.y0]])
            elif it[0] == "qu":
                flush()
                q = it[1]
                out.append([[q.ul.x, q.ul.y], [q.ur.x, q.ur.y], [q.lr.x, q.lr.y],
                            [q.ll.x, q.ll.y], [q.ul.x, q.ul.y]])
            elif it[0] == "l":
                moveto(it[1])
                cur.append([it[2].x, it[2].y])
            elif it[0] == "c":
                p1, p2, p3, p4 = it[1:5]
                moveto(p1)
                for k in range(1, bez_n + 1):
                    t, m = k / bez_n, 1 - k / bez_n
                    cur.append([m*m*m*p1.x + 3*m*m*t*p2.x + 3*m*t*t*p3.x + t*t*t*p4.x,
                                m*m*m*p1.y + 3*m*m*t*p2.y + 3*m*t*t*p3.y + t*t*t*p4.y])
        flush()
        return out

    def walls(ch):
        obstacle_paths.extend([[round(a, 2) for a in ch[i]],
                               [round(a, 2) for a in ch[i + 1]]]
                              for i in range(len(ch) - 1))

    def fixture(ch):
        """Closed, furniture-sized chain -> exact fixture shape."""
        xs, ys = [p[0] for p in ch], [p[1] for p in ch]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        if bw < 1.5 or bh < 1.5 or bw * bh >= max_fixture_area:
            return False                        # sliver / page frame
        corners = {(round(min(xs), 1), round(min(ys), 1)),
                   (round(max(xs), 1), round(min(ys), 1)),
                   (round(max(xs), 1), round(max(ys), 1)),
                   (round(min(xs), 1), round(max(ys), 1))}
        if {(round(x, 1), round(y, 1)) for x, y in ch} == corners:
            fixtures.append([min(xs), min(ys), max(xs), max(ys)])
        else:
            fixture_polys.append([[round(x, 2), round(y, 2)] for x, y in ch[:-1]])
        return True

    badge_pts = [v for k, v in anchors.items() if k.startswith("AISLE ")]
    for dr in page.get_drawings():
        if dr["type"] == "f" and dr.get("fill") in (None, WHITE):
            continue                            # body/background: stroke twin has it
        sw = dr.get("width") or 0
        for ch in chains(dr):
            xs, ys = [p[0] for p in ch], [p[1] for p in ch]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            if x1 - x0 < 2 and y1 - y0 < 2:
                continue                        # icon confetti, smaller than a cell
            if x1 - x0 < 6 and y1 - y0 < 6 and sw <= 0.61:
                continue    # thin-stroke trinket: dashes, stars, legend marks —
                            # printed decoration, not furniture (real furniture
                            # linework is >=0.69pt or larger than 6pt)
            if (x1 - x0 <= 14 and y1 - y0 <= 14
                    and any(x0 - 1 <= px <= x1 + 1 and y0 - 1 <= py <= y1 + 1
                            for px, py in badge_pts)):
                continue    # the aisle-number badge glyph (hexagon around the
                            # digit): map annotation at the corridor mouth, not
                            # an object — never let it block walkability
            closed = (len(ch) >= 4 and abs(ch[0][0] - ch[-1][0]) < .5
                      and abs(ch[0][1] - ch[-1][1]) < .5)
            if dr["type"] == "s":
                walls(ch)                       # all drawn strokes block their line
                if closed:
                    fixture(ch)                 # ...and closed ones their interior
            elif closed and fixture(ch):
                pass
            else:
                # degenerate fill: zero-width wall line (e.g. the seafood /
                # kitchen counter walls drawn as 2-line white-ish fills)
                walls(ch)

    # self-check: a real supermarket has >100 store-sized fixtures; catching
    # the "kept only decorative confetti" failure mode (2026-07-21 bug).
    big = sum((x1 - x0) * (y1 - y0) > 200 for x0, y0, x1, y1 in fixtures)
    big += sum(1 for ch in fixture_polys
               if (max(p[0] for p in ch) - min(p[0] for p in ch))
               * (max(p[1] for p in ch) - min(p[1] for p in ch)) > 200)
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
            "fixture_polys": fixture_polys,
            "obstacle_paths": obstacle_paths, "boundary": boundary}
    json.dump(geom, open(OUT, "w"))
    print(f"{len(anchors)} anchors, {len(fixtures)} rect fixtures + "
          f"{len(fixture_polys)} poly fixtures, "
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
    for a, b in geom["obstacle_paths"]:
        dr.line([a[0] * s, a[1] * s, b[0] * s, b[1] * s], fill="orange")
    for x0, y0, x1, y1 in geom["fixtures"]:
        dr.rectangle([x0 * s, y0 * s, x1 * s, y1 * s], outline="red")
    for poly in geom["fixture_polys"]:
        dr.polygon([(x * s, y * s) for x, y in poly], outline="magenta")
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
