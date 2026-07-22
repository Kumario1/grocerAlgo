#!/usr/bin/env python3
"""Differential raster fallback benchmark (artifacts stay under /tmp).

Generates the plan's image-only JPEG PDF corpus from the committed vector
guides, runs the raster pipeline against it, and scores every acceptance
gate: structural precision/recall, long components, boundary IoU, raw-grid
IoU, aisle set and positions, major anchors, per-wing positioned-label
recall, and runtime.  Clean and legacy cases gate eligibility; hard cases
are reported for information.

Usage: python3 raster_benchmark.py [--backend tesseract|vision]
                                   [--stores 24,659] [--cases clean,legacy]
"""
import argparse
import json
import math
import re
import time
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from router import engine, raster
from router.derive import pdf_path

STORES = ("24", "659", "388", "790")
CASES = ("clean", "legacy", "hard")
OUT = Path("/tmp/grocerAlgo-raster-benchmark")

GATES = {  # {case: gate values}; hard is informative only
    "clean": {"precision": .93, "recall": .93, "long": 1.0, "boundary": .98,
              "grid": .95, "aisle_err": 4.0, "labels": .90, "wing": .80},
    "legacy": {"precision": .88, "recall": .88, "long": 1.0, "boundary": .96,
               "grid": .92, "aisle_err": 8.0, "labels": .80, "wing": .70},
}
RUNTIME = {"vision": 60.0, "tesseract": 120.0}


def render(store):
    page = fitz.open(pdf_path(store))[1]
    pix = page.get_pixmap(dpi=200, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def degrade(image, case):
    if case == "clean":
        return image
    if case == "legacy":
        return ImageEnhance.Contrast(image.rotate(90, expand=True)) \
            .enhance(.78).filter(ImageFilter.GaussianBlur(.6))
    return ImageEnhance.Contrast(image.resize(
        (round(image.width * .75), round(image.height * .75)))) \
        .enhance(.65).rotate(.75, expand=True, fillcolor="white") \
        .filter(ImageFilter.GaussianBlur(1.0))


def degrade_truth(mask, case):
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    if case == "clean":
        return mask
    if case == "legacy":
        return np.asarray(image.rotate(90, expand=True), bool)
    image = image.resize((round(image.width * .75), round(image.height * .75)),
                         Image.Resampling.NEAREST)
    return np.asarray(image.rotate(.75, expand=True, fillcolor=0,
                                   resample=Image.Resampling.NEAREST), bool)


def corpus_pdf(store, case):
    """Deterministic image-only JPEG PDF for one corpus case."""
    directory = OUT / "corpus"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{store}-{case}.pdf"
    if path.exists():
        return path
    quality = {"clean": 92, "legacy": 70, "hard": 55}[case]
    image = degrade(render(store), case)
    jpeg = directory / f"{store}-{case}.jpg"
    image.save(jpeg, quality=quality)
    page_rect = fitz.open(pdf_path(store))[1].rect
    if case == "legacy":
        width, height = page_rect.height, page_rect.width
    else:
        width, height = page_rect.width, page_rect.height
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_image(page.rect, filename=str(jpeg))
    doc.save(path, deflate=False)
    doc.close()
    return path


def truth_mask(store, size):
    geom = json.load(open(f"data/{store}/geometry.json"))
    sx, sy = size[0] / geom["page"]["w"], size[1] / geom["page"]["h"]
    image = Image.new("1", size)
    draw = ImageDraw.Draw(image)
    width = max(1, round((sx + sy) / 2))
    for x0, y0, x1, y1 in geom["fixtures"]:
        draw.rectangle((x0 * sx, y0 * sy, x1 * sx, y1 * sy),
                       outline=1, width=width)
    for poly in geom.get("fixture_polys", ()):
        points = [(x * sx, y * sy) for x, y in poly]
        draw.line(points + [points[0]], fill=1, width=width)
    for a, b in geom["obstacle_paths"]:
        draw.line((a[0] * sx, a[1] * sy, b[0] * sx, b[1] * sy),
                  fill=1, width=width)
    draw.line([(x * sx, y * sy) for x, y in geom["boundary"]],
              fill=1, width=width * 2)
    return np.asarray(image, bool)


def pixel_metrics(truth, found):
    # 200-DPI corpus: the plan's two-PDF-point tolerance is 5.56 pixels in
    # each direction, hence an 11x11 comparison kernel.
    tolerance = np.ones((11, 11), np.uint8)
    near_truth = cv2.dilate(truth.astype(np.uint8), tolerance) > 0
    near_found = cv2.dilate(found.astype(np.uint8), tolerance) > 0
    precision = float((found & near_truth).sum() / max(1, found.sum()))
    recall = float((truth & near_found).sum() / max(1, truth.sum()))
    return precision, recall


def long_component_recall(store, found, source_size, output_size, case):
    geom = json.load(open(f"data/{store}/geometry.json"))
    sx = source_size[0] / geom["page"]["w"]
    sy = source_size[1] / geom["page"]["h"]
    near = cv2.dilate(found.astype(np.uint8), np.ones((11, 11), np.uint8)) > 0
    hits = []
    for a, b in geom["obstacle_paths"]:
        if math.dist(a, b) <= 12:
            continue
        line = Image.new("1", source_size)
        ImageDraw.Draw(line).line((a[0] * sx, a[1] * sy,
                                  b[0] * sx, b[1] * sy), fill=1, width=2)
        transformed = degrade_truth(np.asarray(line, bool), case)
        if transformed.shape != near.shape:
            transformed = cv2.resize(transformed.astype(np.uint8),
                                     (output_size[0], output_size[1]),
                                     interpolation=cv2.INTER_NEAREST) > 0
        hits.append(bool((transformed & near).any()))
    return sum(hits) / max(1, len(hits))


def _polygon_mask(boundary, page, scale=2.0):
    image = Image.new("1", (round(page["w"] * scale), round(page["h"] * scale)))
    ImageDraw.Draw(image).polygon([(x * scale, y * scale) for x, y in boundary],
                                  fill=1)
    return np.asarray(image, bool)


def boundary_iou(committed, geom):
    a = _polygon_mask(committed["boundary"], committed["page"])
    b = _polygon_mask(geom["boundary"], committed["page"])
    return float((a & b).sum() / max(1, (a | b).sum()))


def grid_iou(committed, geom):
    a = engine.build_grid(committed)
    b = engine.build_grid(geom)
    height = min(a.shape[0], b.shape[0])
    width = min(a.shape[1], b.shape[1])
    a, b = a[:height, :width], b[:height, :width]
    return float((a & b).sum() / max(1, (a | b).sum()))


def aisle_score(committed, geom):
    truth = {name: point for name, point in committed["anchors"].items()
             if name.startswith("AISLE ")}
    found = {name: point for name, point in geom["anchors"].items()
             if name.startswith("AISLE ")}
    if set(truth) != set(found):
        return False, float("inf")
    errors = [math.dist(truth[name], found[name]) for name in truth]
    return True, float(np.median(errors))


def _canon_major(text):
    compact = re.sub(r"[^A-Z ]", "", text.upper())
    return {label for label in raster.MAJOR_LABELS
            if label in compact.split() or label == compact
            or (label == "CHECKSTANDS" and "CHECK" in compact)}


def major_score(committed, geom):
    required = set()
    for name in committed["anchors"]:
        if not name.startswith("AISLE "):
            required |= _canon_major(name)
    found = {re.sub(r" \d+$", "", name) for name in geom["anchors"]
             if not name.startswith("AISLE ")}
    missing = sorted(required - found)
    # The vector reference never anchors these labels, so their presence in
    # the raster result cannot be attested either way.
    unattested = {"MARKET", "TORTILLERIA", "SUSHIYA", "BUSINESS CENTER"}
    false = sorted(found - required - unattested)
    return missing, false


def reference_labels(store):
    """Vector-page product/shelf words near committed structure, by wing."""
    geom = json.load(open(f"data/{store}/geometry.json"))
    boxes = list(geom["fixtures"])
    boxes += [[min(x for x, _ in poly), min(y for _, y in poly),
               max(x for x, _ in poly), max(y for _, y in poly)]
              for poly in geom.get("fixture_polys", ())]
    boxes += [[min(a[0], b[0]), min(a[1], b[1]),
               max(a[0], b[0]), max(a[1], b[1])]
              for a, b in geom["obstacle_paths"]]
    words = []
    for x0, y0, x1, y1, text, *_ in fitz.open(pdf_path(store))[1] \
            .get_text("words"):
        clean = re.sub(r"[^A-Z0-9]", "", text.upper())
        if len(clean) < 3 or not text[:1].isalpha():
            continue
        if sum(c.isalpha() for c in clean) / len(clean) < .7:
            continue
        if _canon_major(text):
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        distance = min(math.hypot(max(left - cx, 0, cx - right),
                                  max(top - cy, 0, cy - bottom))
                       for left, top, right, bottom in boxes)
        if distance <= 16:
            words.append((clean, cx, cy))
    return words, geom["page"]


def label_score(store, geom):
    references, page = reference_labels(store)
    found = {}
    for word in geom.get("ocr_words", ()):
        if word.get("region") == "red-label":
            continue
        clean = re.sub(r"[^A-Z0-9]", "", word["text"].upper())
        if clean:
            box = word["bbox"]
            found.setdefault(clean, []).append(
                ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    wings = {}
    for clean, cx, cy in references:
        wing = (cx > page["w"] / 2, cy > page["h"] / 2)
        hit = any(math.hypot(cx - fx, cy - fy) <= 12
                  for fx, fy in found.get(clean, ()))
        total, hits = wings.get(wing, (0, 0))
        wings[wing] = (total + 1, hits + hit)
    total = sum(t for t, _ in wings.values())
    hits = sum(h for _, h in wings.values())
    overall = hits / max(1, total)
    wing_recalls = [h / t for t, h in wings.values() if t >= 10]
    return overall, min(wing_recalls, default=1.0)


def run_case(store, case, backend):
    path = corpus_pdf(store, case)
    doc = fitz.open(path)
    page = doc[0]
    image = raster._page_image(page)
    committed = json.load(open(f"data/{store}/geometry.json"))
    prefix = OUT / f"{store}-{case}-{backend}"
    row = {"store": store, "case": case, "backend": backend,
           "set": "tuning" if store in ("24", "659") else "validation"}

    # structural mask metrics in the degraded frame
    words = raster._ocr(image, backend, 0)
    found = raster._structural_mask(image, words, 245) > 0
    source = render(store)
    truth = degrade_truth(truth_mask(store, source.size), case)
    if truth.shape != found.shape:
        truth = cv2.resize(truth.astype(np.uint8),
                           (found.shape[1], found.shape[0]),
                           interpolation=cv2.INTER_NEAREST) > 0
    precision, recall = pixel_metrics(truth, found)
    row["precision"] = round(precision, 4)
    row["pixel_recall"] = round(recall, 4)
    row["long_component_recall"] = round(long_component_recall(
        store, found, source.size, image.size, case), 4)
    Image.fromarray(found.astype(np.uint8) * 255).save(
        f"{prefix}-structural.png")
    diff = np.asarray(image).copy()
    grow = np.ones((11, 11), np.uint8)
    diff[found & ~(cv2.dilate(truth.astype(np.uint8), grow) > 0)] = (255, 0, 0)
    diff[truth & ~(cv2.dilate(found.astype(np.uint8), grow) > 0)] = (0, 0, 255)
    Image.fromarray(diff).save(f"{prefix}-diff.jpg", quality=88)

    # full-pipeline gates in upright page points
    started = time.monotonic()
    try:
        geom = raster.extract_page(page, backend=backend,
                                   artifact_dir=prefix.with_suffix(".extract"))
        row["seconds"] = round(time.monotonic() - started, 1)
        row["boundary_iou"] = round(boundary_iou(committed, geom), 4)
        row["grid_iou"] = round(grid_iou(committed, geom), 4)
        exact, err = aisle_score(committed, geom)
        row["aisle_exact"] = exact
        row["aisle_median_err"] = round(err, 2)
        missing, false = major_score(committed, geom)
        row["missing_majors"] = missing
        row["false_majors"] = false
        overall, wing = label_score(store, geom)
        row["label_recall"] = round(overall, 4)
        row["wing_recall"] = round(wing, 4)
    except raster.RasterExtractionError as exc:
        row["seconds"] = round(time.monotonic() - started, 1)
        row["extract_error"] = str(exc)
    doc.close()
    return row


def gate_failures(row):
    gates = GATES.get(row["case"])
    if gates is None:
        return []
    failures = []
    if "extract_error" in row:
        failures.append("extract")
    check = (("precision", "precision", True), ("pixel_recall", "recall", True),
             ("long_component_recall", "long", True),
             ("boundary_iou", "boundary", True), ("grid_iou", "grid", True),
             ("aisle_median_err", "aisle_err", False),
             ("label_recall", "labels", True), ("wing_recall", "wing", True))
    for key, gate, at_least in check:
        if key not in row:
            continue
        value, floor = row[key], gates[gate]
        if (value < floor) if at_least else (value > floor):
            failures.append(key)
    if row.get("aisle_exact") is False:
        failures.append("aisle_set")
    if row.get("missing_majors"):
        failures.append("missing_majors")
    if row.get("false_majors"):
        failures.append("false_majors")
    if row.get("seconds", 0) > RUNTIME[row["backend"]]:
        failures.append("runtime")
    return failures


def run(backend, stores, cases):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = False
    for store in stores:
        for case in cases:
            row = run_case(store, case, backend)
            row["gate_failures"] = gate_failures(row)
            failed |= bool(row["gate_failures"])
            rows.append(row)
            print(json.dumps(row), flush=True)
    with open(OUT / f"results-{backend}.json", "w") as output:
        json.dump(rows, output, indent=2)
    print(f"artifacts: {OUT}")
    verdict = "FAIL" if failed else "PASS"
    print(f"{backend} clean+legacy gates: {verdict}")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("tesseract", "vision"),
                        default="tesseract")
    parser.add_argument("--stores", default=",".join(STORES))
    parser.add_argument("--cases", default=",".join(CASES))
    args = parser.parse_args()
    raise SystemExit(run(args.backend, args.stores.split(","),
                         args.cases.split(",")))
