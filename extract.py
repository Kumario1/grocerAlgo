#!/usr/bin/env python3
"""One-shot: H-E-B directory PDF (page 1 = map) -> geometry JSON.

Usage: python3 extract.py [store]   (default 659)
Reads guides/guide-<city>-<store>.pdf (resolved via router.derive.pdf_path),
writes data/<store>/geometry.json.
"""
import json, os, sys, fitz
from router.derive import pdf_path
from router import raster

STORE = "659"
PDF = None            # resolved in __main__ (script-only entry point)
OUT = f"data/{STORE}/geometry.json"
WHITE = (1.0, 1.0, 1.0)


def raster_experiment_enabled():
    """The fallback stays opt-in until every differential gate passes."""
    return os.environ.get("GROCER_RASTER_EXPERIMENTAL") == "1"


def stitch_open_boundary(chains, width, height, join_tol=3.0, min_span=.4):
    """Close a fragmented perimeter whose chain endpoint meets another wall.

    Some guide PDFs split the sales-floor outline into two open drawings. One
    chain terminates on the middle of the other chain's last wall, so endpoint
    equality alone cannot join them. Candidate loops must still span most of
    the page and enclose substantial area; the largest such loop wins.
    """
    def projection(point, start, end):
        dx, dy = end[0] - start[0], end[1] - start[1]
        denom = dx * dx + dy * dy
        if not denom:
            return list(start), float("inf")
        t = max(0.0, min(1.0, ((point[0] - start[0]) * dx
                              + (point[1] - start[1]) * dy) / denom))
        q = [start[0] + t * dx, start[1] + t * dy]
        distance = ((point[0] - q[0]) ** 2 + (point[1] - q[1]) ** 2) ** .5
        return q, distance

    def polygon_area(poly):
        return abs(sum(a[0] * b[1] - b[0] * a[1]
                       for a, b in zip(poly, poly[1:]))) / 2

    candidates = []
    for i, first in enumerate(chains):
        if len(first) < 2 or first[0] == first[-1]:
            continue
        for j, second in enumerate(chains):
            if i == j or len(second) < 2 or second[0] == second[-1]:
                continue
            for endpoint in (0, -1):
                oriented = list(reversed(first)) if endpoint == 0 else list(first)
                join = oriented[-1]
                for k, (start, end) in enumerate(zip(second, second[1:])):
                    q, distance = projection(join, start, end)
                    if distance > join_tol:
                        continue
                    continuations = [
                        [q] + list(reversed(second[:k + 1])),
                        [q] + list(second[k + 1:]),
                    ]
                    for continuation in continuations:
                        poly = [list(p) for p in oriented]
                        if distance > 1e-6:
                            poly.append(q)
                        poly.extend([list(p) for p in continuation[1:]])
                        poly.append(list(poly[0]))
                        xs, ys = zip(*poly)
                        if (max(xs) - min(xs) <= min_span * width
                                or max(ys) - min(ys) <= min_span * height):
                            continue
                        area = polygon_area(poly)
                        if area > .25 * width * height:
                            candidates.append((area, poly))
    return max(candidates, default=(0, []), key=lambda item: item[0])[1]


def extract():
    doc = fitz.open(PDF)
    page = doc[1]
    if raster.is_raster_page(page):
        if not raster_experiment_enabled():
            raise SystemExit(
                "raster fallback remains experimental because the universal "
                "differential benchmark has not passed; inspect "
                "docs/superpowers/specs/2026-07-22-raster-fallback-"
                "benchmark-results.md or set GROCER_RASTER_EXPERIMENTAL=1 "
                "for benchmark/holdout QA only")
        config_path = f"data/{STORE}/raster.json"
        try:
            config = json.load(open(config_path))
        except FileNotFoundError:
            config = {}
        artifact_dir = f"data/{STORE}/qa/raster"
        geom = raster.extract_page(page, config, artifact_dir=artifact_dir)
        json.dump(geom, open(OUT, "w"))
        print(f"raster/{geom['ocr_backend']}: {len(geom['anchors'])} anchors, "
              f"{len(geom['fixtures'])} rect fixtures + "
              f"{len(geom['fixture_polys'])} poly fixtures, "
              f"{len(geom['obstacle_paths'])} wall segments, boundary "
              f"{len(geom['boundary'])} vertices -> {OUT}; QA {artifact_dir}")
        return geom
    words = page.get_text("words")  # (x0, y0, x1, y1, text, ...)

    anchors, seen = {}, []
    # Stacked digit spans share one bbox and only the last-painted shows:
    # store #658 leaves a stale "21" hidden under aisle 10's badge. Keep the
    # visible winner — within a block, word order is content (paint) order.
    badges = {}
    for x0, y0, x1, y1, t, *_ in words:
        if t.isdigit() and 1 <= int(t) <= 60:
            badges[round(x0, 1), round(y0, 1)] = (int(t), x0, y0, x1, y1)
    for n, x0, y0, x1, y1 in badges.values():
        seen.append(n)
        anchors[f"AISLE {n}"] = [(x0 + x1) / 2, (y0 + y1) / 2]
    # aisle badges must be a clean 1..N run, each number exactly once
    # (store #659: 45, store #24: 43); duplicates or holes mean the map
    # page carries stray digits and needs a smarter filter. Floor is 15,
    # matching discover.py's validation gate: store #6 (Stephenville) is a
    # real 15-aisle store, and a stray-digit page fails the clean-run
    # check long before it fails the count.
    aisles = sorted(seen)
    assert len(aisles) >= 15 and aisles == list(range(1, len(aisles) + 1)), \
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
                      "SEAFOOD", "MEAT", "DAIRY", "FLORAL", "BLOOMS",
                      "PHARMACY", "FROZEN", "KITCHEN", "RESTROOM"):
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
        dashed = (dr.get("dashes") or "[] 0") != "[] 0"
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
                if dashed and closed:
                    continue    # a dashed closed ring is a zone marking the
                                # shopper walks across (store #658's Blooms
                                # brand ellipse sealed 89 m² of browse floor);
                                # open dashed ticks still wall their line
                walls(ch)                       # all drawn strokes block their line
                if closed:
                    fixture(ch)                 # ...and closed ones their interior
            elif closed and fixture(ch):
                pass
            else:
                # degenerate fill: zero-width wall line (e.g. the seafood /
                # kitchen counter walls drawn as 2-line white-ish fills)
                walls(ch)

    # self-check against the "kept only decorative confetti" failure mode
    # (2026-07-21 bug). Floor scales with store size: a 45-aisle superstore
    # has hundreds of store-sized fixtures, a real 15-aisle store (#6
    # Stephenville) has 97 — confetti yields near zero either way.
    big = sum((x1 - x0) * (y1 - y0) > 200 for x0, y0, x1, y1 in fixtures)
    big += sum(1 for ch in fixture_polys
               if (max(p[0] for p in ch) - min(p[0] for p in ch))
               * (max(p[1] for p in ch) - min(p[1] for p in ch)) > 200)
    assert big >= max(50, 2 * len(aisles)), \
        f"only {big} store-sized fixtures — wrong fill/stroke filter?"

    # Sales-floor boundary: the map draws the interior outline as one CLOSED
    # thick-stroke polyline (store #659: 18 segments, stroke width ~1.85).
    # Everything outside it (parking, drive-thru, curbside) is not walkable.
    def find_boundary(min_width, min_span=.45):
        best = []
        for dr in page.get_drawings():
            if dr["type"] != "s" or not dr.get("width") or dr["width"] < min_width:
                continue
            for pts in chains(dr):
                if len(pts) <= 4 or pts[0] != pts[-1]:
                    continue
                xs, ys = zip(*pts)
                # span floor is 0.45, not 0.6: store #6's real floor fills
                # only 58% of its sheet. That lets the page frame compete,
                # so a ~full-page bbox is rejected outright — the frame is
                # never the floor.
                spans = ((max(xs) - min(xs)) > min_span * page.rect.width
                         and (max(ys) - min(ys)) > min_span * page.rect.height)
                frame = ((max(xs) - min(xs)) > 0.95 * page.rect.width
                         and (max(ys) - min(ys)) > 0.95 * page.rect.height)
                if spans and not frame and len(pts) > len(best):
                    best = pts
        if not best:
            thick_chains = []
            for dr in page.get_drawings():
                if dr["type"] == "s" and (dr.get("width") or 0) >= min_width:
                    thick_chains.extend(chains(dr))
            best = stitch_open_boundary(thick_chains, W, H, min_span=min_span)
        return best

    # A ladder, not a lower constant: 1.5 first (the classic template, and
    # exactly the old behavior — every shipped store resolves here), then
    # 0.9 for newer guides that outline the floor at 0.96 pt (store #14),
    # then a 0.40 span for wide guides with a deep parking apron (#372).
    boundary = (find_boundary(1.5) or find_boundary(0.9)
                or find_boundary(1.5, .40) or find_boundary(.9, .40))
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
    im = raster.render_source(page, geom, dpi=144)
    s = im.width / geom["page"]["w"]
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
        OUT = f"data/{STORE}/geometry.json"
    PDF = pdf_path(STORE)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    overlay(extract())
