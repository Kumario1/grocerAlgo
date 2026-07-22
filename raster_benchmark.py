#!/usr/bin/env python3
"""Differential raster fallback benchmark (artifacts stay under /tmp).

Usage: python3 raster_benchmark.py [--backend tesseract|vision]
"""
import argparse
import json
import math
import time
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from router import raster
from router.derive import pdf_path

STORES = ("24", "659", "388", "790")
CASES = ("clean", "legacy", "hard")
OUT = Path("/tmp/grocerAlgo-raster-benchmark")


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
        .enhance(.65).rotate(90.75, expand=True, fillcolor="white") \
        .filter(ImageFilter.GaussianBlur(1.0))


def degrade_truth(mask, case):
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    if case == "clean":
        return mask
    if case == "legacy":
        return np.asarray(image.rotate(90, expand=True), bool)
    image = image.resize((round(image.width * .75), round(image.height * .75)),
                         Image.Resampling.NEAREST)
    return np.asarray(image.rotate(90.75, expand=True, fillcolor=0,
                                   resample=Image.Resampling.NEAREST), bool)


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


def run(backend):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for store in STORES:
        source = render(store)
        truth = truth_mask(store, source.size)
        for case in CASES:
            image = degrade(source, case)
            started = time.monotonic()
            words = raster._ocr(image, backend, 0)
            found = raster._structural_mask(image, words, 245) > 0
            precision, recall = pixel_metrics(degrade_truth(truth, case), found)
            component_recall = long_component_recall(
                store, found, source.size, image.size, case)
            row = {"store": store, "set": ("tuning" if store in ("24", "659")
                                             else "validation"),
                   "case": case, "backend": backend,
                   "precision": round(precision, 4),
                   "pixel_recall": round(recall, 4),
                   "long_component_recall": round(component_recall, 4),
                   "seconds": round(time.monotonic() - started, 3)}
            rows.append(row)
            prefix = OUT / f"{store}-{case}-{backend}"
            image.save(f"{prefix}-original.jpg", quality=90)
            Image.fromarray(raster._red_mask(image)).save(f"{prefix}-red.png")
            Image.fromarray(found.astype(np.uint8) * 255).save(
                f"{prefix}-structural.png")
            diff = np.asarray(image).copy()
            expected = degrade_truth(truth, case)
            diff[found & ~cv2.dilate(expected.astype(np.uint8),
                                     np.ones((11, 11), np.uint8)).astype(bool)] = (255, 0, 0)
            diff[expected & ~cv2.dilate(found.astype(np.uint8),
                                        np.ones((11, 11), np.uint8)).astype(bool)] = (0, 0, 255)
            Image.fromarray(diff).save(f"{prefix}-diff.jpg", quality=88)
            print(store, case, f"P={precision:.3f}", f"R={recall:.3f}",
                  f"long={component_recall:.3f}", f"{row['seconds']:.1f}s",
                  flush=True)
    with open(OUT / f"results-{backend}.json", "w") as output:
        json.dump(rows, output, indent=2)
    print(f"artifacts: {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("tesseract", "vision"),
                        default="tesseract")
    run(parser.parse_args().backend)
