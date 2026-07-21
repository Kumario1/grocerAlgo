#!/usr/bin/env python3
"""One-shot: guide-austin-659.pdf (page 1 = map) -> data/heb659_geometry.json."""
import json, fitz

PDF = "guide-austin-659.pdf"
OUT = "data/heb659_geometry.json"
WHITE = (1.0, 1.0, 1.0)

def extract():
    page = fitz.open(PDF)[1]
    words = page.get_text("words")  # (x0, y0, x1, y1, text, ...)

    anchors = {}
    for x0, y0, x1, y1, t, *_ in words:
        if t.isdigit() and 1 <= int(t) <= 45:
            anchors[f"AISLE {int(t)}"] = [(x0 + x1) / 2, (y0 + y1) / 2]
    assert len([k for k in anchors if k.startswith("AISLE")]) == 45, anchors.keys()

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

    fixtures, obstacle_paths = [], []
    for dr in page.get_drawings():
        if dr["type"] in ("f", "fs") and dr.get("fill") not in (None, WHITE):
            r = dr["rect"]
            fixtures.append([r.x0, r.y0, r.x1, r.y1])
        elif dr["type"] == "s":
            for item in dr["items"]:
                if item[0] == "l":                       # line segment
                    a, b = item[1], item[2]
                    obstacle_paths.append([[a.x, a.y], [b.x, b.y]])
                elif item[0] == "re":                    # stroked rect -> 4 edges
                    r = item[1]
                    c = [[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1], [r.x0, r.y1]]
                    obstacle_paths += [[c[i], c[(i + 1) % 4]] for i in range(4)]

    geom = {"page": {"w": page.rect.width, "h": page.rect.height},
            "anchors": anchors, "fixtures": fixtures,
            "obstacle_paths": obstacle_paths}
    json.dump(geom, open(OUT, "w"))
    print(f"{len(anchors)} anchors, {len(fixtures)} fixtures, "
          f"{len(obstacle_paths)} wall segments -> {OUT}")
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
    for k, (x, y) in geom["anchors"].items():
        dr.ellipse([x * s - 4, y * s - 4, x * s + 4, y * s + 4], fill="blue")
    im.save("data/heb659_extract_overlay.png")
    print("overlay -> data/heb659_extract_overlay.png")

if __name__ == "__main__":
    overlay(extract())
