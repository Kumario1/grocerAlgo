"""Image-only PDF map extraction support.

The vector extractor deliberately does not import image-processing code.  This
module owns the raster-only path and keeps its normalized OCR records and
geometry helpers testable without opening a PDF.
"""
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw


class RasterExtractionError(RuntimeError):
    """The scan cannot satisfy a hard extraction invariant."""


def is_raster_page(page):
    """True only for image-only pages containing one full-page map scan."""
    if page.get_drawings() or page.get_text("words"):
        return False
    images = page.get_image_info()
    if len(images) != 1:
        return False
    image = images[0]
    return (image["width"] >= 1000 and image["height"] >= 1000
            and _rect_area(image["bbox"]) >= .8 * page.rect.get_area())


def _rect_area(rect):
    x0, y0, x1, y1 = rect
    return max(0, x1 - x0) * max(0, y1 - y0)


def parse_tesseract_tsv(tsv, orientation=0):
    """Convert Tesseract TSV rows to the fallback's backend-neutral words."""
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t",
                              quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf", -1))
        except ValueError:
            continue
        if not text or confidence < 0:
            continue
        x, y = float(row["left"]), float(row["top"])
        w, h = float(row["width"]), float(row["height"])
        words.append({"text": text, "confidence": round(confidence / 100, 4),
                      "bbox": [x, y, x + w, y + h],
                      "orientation": orientation, "backend": "tesseract"})
    return words


def reconstruct_aisle_sequence(badges):
    """Fill gaps in one regular badge run, rejecting ambiguous numbering.

    At least two OCR values must independently establish both ordering and
    offset.  The result must be exactly 1..N, matching the production hard
    gate rather than guessing isolated unreadable digits.
    """
    if len(badges) < 2:
        raise RasterExtractionError("cannot establish a unique aisle run")
    centers = [((b["bbox"][0] + b["bbox"][2]) / 2,
                (b["bbox"][1] + b["bbox"][3]) / 2) for b in badges]
    axis = 0 if np.ptp([p[0] for p in centers]) >= np.ptp([p[1] for p in centers]) else 1
    ordered = sorted(zip(badges, centers), key=lambda item: item[1][axis])
    known = []
    for i, (badge, _) in enumerate(ordered):
        text = badge.get("text", "").strip()
        if text.isdigit() and int(text) > 0:
            known.append((i, int(text)))
    if len(known) < 2:
        raise RasterExtractionError("cannot establish a unique aisle run")
    directions = set()
    for (i, value), (j, other) in zip(known, known[1:]):
        gap = j - i
        delta = other - value
        if not gap or abs(delta) != gap:
            raise RasterExtractionError("cannot establish a unique aisle run")
        directions.add(1 if delta > 0 else -1)
    if len(directions) != 1:
        raise RasterExtractionError("cannot establish a unique aisle run")
    direction = directions.pop()
    start = known[0][1] - known[0][0] * direction
    values = [start + i * direction for i in range(len(ordered))]
    if sorted(values) != list(range(1, len(values) + 1)):
        raise RasterExtractionError("cannot establish a unique aisle run")
    return {f"AISLE {value}": [round(center[0], 2), round(center[1], 2)]
            for value, (_, center) in sorted(zip(values, ordered))}


def discover_boundary(structural_mask):
    """Find a unique store-size outline while closing only discovery gaps."""
    mask = (structural_mask > 0).astype(np.uint8) * 255
    # Boundary walls are long components.  Excluding compact annotations here
    # prevents exterior entrance / drive-through icons from becoming little
    # walkable protrusions when discovery gaps are temporarily closed.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    boundary_source = np.zeros_like(mask)
    for label in range(1, count):
        _, _, width, height, _ = stats[label]
        if width > .12 * mask.shape[1] or height > .12 * mask.shape[0]:
            boundary_source[labels == label] = 255
    eligible = []
    for fraction in (.10, .15, .24):
        gap = max(5, round(min(mask.shape) * fraction))
        horizontal = cv2.morphologyEx(
            boundary_source, cv2.MORPH_CLOSE, cv2.getStructuringElement(
                cv2.MORPH_RECT, (gap, 1)))
        vertical = cv2.morphologyEx(
            boundary_source, cv2.MORPH_CLOSE, cv2.getStructuringElement(
                cv2.MORPH_RECT, (1, gap)))
        closed = cv2.bitwise_or(horizontal, vertical)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        eligible = [c for c in contours if cv2.contourArea(c)
                    >= .2 * mask.shape[0] * mask.shape[1]]
        if eligible:
            break
    if len(eligible) != 1:
        raise RasterExtractionError(
            f"unique outer boundary not found ({len(eligible)} candidates)")
    contour = eligible[0]
    contour = cv2.approxPolyDP(contour, .003 * cv2.arcLength(contour, True),
                               True)
    return [[float(x), float(y)] for [[x, y]] in contour]


MAJOR_LABELS = (
    "ENTRANCE", "EXIT", "CHECKSTANDS", "PRODUCE", "BAKERY", "DELI",
    "SEAFOOD", "MARKET", "DAIRY", "TORTILLERIA", "SUSHIYA", "FLORAL",
    "PHARMACY", "RESTROOMS", "BUSINESS CENTER",
)


def _page_image(page):
    """Use the original full-page scan instead of resampling it when safe."""
    images = page.get_image_info(xrefs=True)
    if len(images) == 1 and images[0].get("xref"):
        info = images[0]
        bbox = info["bbox"]
        if (info["width"] >= 1000 and info["height"] >= 1000
                and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                >= .8 * page.rect.width * page.rect.height):
            raw = page.parent.extract_image(info["xref"])["image"]
            return Image.open(io.BytesIO(raw)).convert("RGB")
    pix = page.get_pixmap(dpi=300, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _red_mask(image):
    rgb = np.asarray(image)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return ((((hsv[..., 0] < 12) | (hsv[..., 0] > 170))
             & (hsv[..., 1] > 60) & (hsv[..., 2] > 80))
            .astype(np.uint8) * 255)


def _deskew(image):
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    lines = cv2.HoughLines(cv2.Canny(gray, 60, 180), 1, np.pi / 720, 120)
    angles = []
    for [[_, theta]] in lines[:200] if lines is not None else ():
        angle = np.degrees(theta) - 90
        while angle <= -45:
            angle += 90
        while angle > 45:
            angle -= 90
        if abs(angle) <= 3:
            angles.append(angle)
    skew = float(np.median(angles)) if angles else 0.0
    if abs(skew) < .15:
        return image, 0.0
    return image.rotate(skew, resample=Image.Resampling.BICUBIC,
                        expand=False, fillcolor="white"), round(skew, 3)


def _run_tesseract(image, orientation, psm=11, whitelist=None):
    command = shutil.which("tesseract")
    if not command:
        raise RasterExtractionError(
            "Tesseract OCR is unavailable; install with `brew install "
            "tesseract` (macOS) or `apt install tesseract-ocr` (Linux)")
    with tempfile.NamedTemporaryFile(suffix=".png") as source:
        image.save(source.name)
        args = [command, source.name, "stdout", "--psm", str(psm)]
        if whitelist:
            args += ["-c", f"tessedit_char_whitelist={whitelist}"]
        args.append("tsv")
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RasterExtractionError(
                f"Tesseract timed out after {exc.timeout}s "
                f"(orientation={orientation}, psm={psm})") from exc
    if result.returncode:
        raise RasterExtractionError(
            f"Tesseract failed (orientation={orientation}, psm={psm}): "
            f"{result.stderr.strip()}")
    return parse_tesseract_tsv(result.stdout, orientation)


def normalize_vision_phrase(text, confidence, bbox, orientation):
    """Split a Vision line observation into positioned word records."""
    tokens = text.split()
    if not tokens:
        return []
    x0, y0, x1, y1 = map(float, bbox)
    records = []
    for i, token in enumerate(tokens):
        if x1 - x0 >= y1 - y0:
            box = [x0 + (x1 - x0) * i / len(tokens), y0,
                   x0 + (x1 - x0) * (i + 1) / len(tokens), y1]
        else:
            box = [x0, y0 + (y1 - y0) * i / len(tokens), x1,
                   y0 + (y1 - y0) * (i + 1) / len(tokens)]
        records.append({"text": token, "confidence": round(float(confidence), 4),
                        "bbox": box, "orientation": orientation,
                        "backend": "vision"})
    return records


def _vision_words(image, orientation):
    """Run Apple Vision without requiring a separately generated wrapper."""
    try:
        import objc
        from Foundation import NSURL
        objc.loadBundle("Vision", globals(),
                        bundle_path="/System/Library/Frameworks/Vision.framework")
        request_cls = objc.lookUpClass("VNRecognizeTextRequest")
        handler_cls = objc.lookUpClass("VNImageRequestHandler")
    except Exception as exc:
        raise RasterExtractionError(f"Apple Vision OCR is unavailable: {exc}")
    with tempfile.NamedTemporaryFile(suffix=".png") as source:
        image.save(source.name)
        request = request_cls.alloc().init()
        request.setRecognitionLevel_(0)  # VNRequestTextRecognitionLevelAccurate
        request.setUsesLanguageCorrection_(True)
        request.setMinimumTextHeight_(.003)
        handler = handler_cls.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(source.name), {})
        ok = handler.performRequests_error_([request], None)
    if not ok:
        raise RasterExtractionError("Apple Vision failed")
    words = []
    width, height = image.size
    for result in request.results() or ():
        candidates = result.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        box = result.boundingBox()
        x0 = box.origin.x * width
        x1 = (box.origin.x + box.size.width) * width
        y0 = (1 - box.origin.y - box.size.height) * height
        y1 = (1 - box.origin.y) * height
        words.extend(normalize_vision_phrase(
            str(candidate.string()), float(candidate.confidence()),
            [x0, y0, x1, y1], orientation))
    return words


def unrotate_bbox(bbox, angle, original_size):
    """Map a PIL-rotated OCR box back into the upright image coordinates."""
    x0, y0, x1, y1 = map(float, bbox)
    width, height = original_size
    angle %= 360
    if angle == 90:
        return [float(width - y1), x0, float(width - y0), x1]
    if angle == 180:
        return [float(width - x1), float(height - y1),
                float(width - x0), float(height - y0)]
    if angle == 270:
        return [y0, float(height - x1), y1, float(height - x0)]
    return [x0, y0, x1, y1]


def _dedupe_words(words):
    """Collapse repeated observations from right-angle OCR passes."""
    kept = []
    for word in sorted(words, key=lambda item: item["confidence"], reverse=True):
        text = re.sub(r"[^A-Z0-9]", "", word["text"].upper())
        if not text:
            continue
        box = word["bbox"]
        area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
        center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        duplicate = False
        for other in kept:
            if other[0] != text or other[1] != (word.get("region") == "red-label"):
                continue
            candidate, other_area, other_center = other[2:5]
            overlap = (max(0.0, min(box[2], candidate[2])
                           - max(box[0], candidate[0]))
                       * max(0.0, min(box[3], candidate[3])
                             - max(box[1], candidate[1])))
            if (overlap / min(area, other_area) >= .45
                    or np.hypot(center[0] - other_center[0],
                                center[1] - other_center[1]) <= 4):
                duplicate = True
                break
        if not duplicate:
            kept.append((text, word.get("region") == "red-label", box,
                         area, center, word))
    return [item[-1] for item in kept]


def _ocr(image, backend, orientation):
    if backend == "vision":
        words = _vision_words(image, orientation)
        red_words = _vision_words(Image.fromarray(255 - _red_mask(image)),
                                  orientation)
        runner = _vision_words
    elif backend == "tesseract":
        words = _run_tesseract(image, orientation)
        red_words = _run_tesseract(
            Image.fromarray(255 - _red_mask(image)), orientation)
        runner = _run_tesseract
    else:
        raise ValueError(f"unknown raster OCR backend: {backend}")
    # Product text is printed both horizontally and vertically along shelves.
    # OCR the two right-angle views, then return every box to upright space.
    for angle in (90, 270):
        rotated = image.rotate(angle, expand=True)
        for word in runner(rotated, (orientation + angle) % 360):
            words.append({**word,
                          "bbox": unrotate_bbox(word["bbox"], angle,
                                                image.size),
                          "region": "shelf-band"})
    # The color-isolated pass is authoritative for department labels.  Keep
    # full-page words as the positioned product-label coverage net.
    return _dedupe_words(
        words + [{**word, "region": "red-label"} for word in red_words])


def _orientation_score(words):
    red = [word for word in words if word.get("region") == "red-label"]
    labels = {_canonical_label(word["text"]) for word in red}
    labels.discard(None)
    legible = sum(word["confidence"] for word in words
                  if len(word["text"].strip()) >= 2)
    return len(labels) * 100 + legible


def _canonical_label(text):
    cleaned = re.sub(r"[^A-Z ]", "", text.upper()).strip()
    compact = cleaned.replace(" ", "")
    best, score = None, 0.0
    for label in MAJOR_LABELS:
        candidate = label.replace(" ", "")
        value = SequenceMatcher(None, compact, candidate).ratio()
        if value > score:
            best, score = label, value
    return best if score >= .72 else None


def _major_anchors(words):
    anchors = {}
    pending_business = None
    for word in sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0])):
        label = _canonical_label(word["text"])
        if word["text"].strip().upper() == "BUSINESS":
            pending_business = word
            continue
        if word["text"].strip().upper() == "CENTER" and pending_business:
            a, b = pending_business["bbox"], word["bbox"]
            word = {**word, "bbox": [min(a[0], b[0]), min(a[1], b[1]),
                                      max(a[2], b[2]), max(a[3], b[3])]}
            label = "BUSINESS CENTER"
        if not label:
            continue
        box = word["bbox"]
        point = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
        name, suffix = label, 2
        while name in anchors:
            if np.hypot(*(np.asarray(anchors[name]) - point)) < 10:
                break
            name, suffix = f"{label} {suffix}", suffix + 1
        anchors.setdefault(name, point)
    return anchors


def _doorway_entrances(anchors):
    """Use the inside EXIT label of each paired doorway as the route seed."""
    entrances = [name for name in anchors
                 if name == "ENTRANCE" or name.startswith("ENTRANCE ")]
    exits = [name for name in anchors
             if name == "EXIT" or name.startswith("EXIT ")]
    available = set(exits)
    for entrance in entrances:
        if not available:
            break
        ex = min(available, key=lambda name: np.hypot(
            anchors[name][0] - anchors[entrance][0],
            anchors[name][1] - anchors[entrance][1]))
        anchors[entrance] = list(anchors[ex])
        available.remove(ex)
    return anchors


def _badge_candidates(image, backend, orientation):
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    dark = (gray < 120).astype(np.uint8) * 255
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        fill = area / max(1, w * h)
        scale = min(image.size) / 1700
        if not (12 * scale <= h <= 25 * scale
                and 20 * scale <= w <= 42 * scale
                and 1.2 <= w / h <= 2.5 and fill > .3):
            continue
        pad = max(3, round(4 * scale))
        crop = image.crop((max(0, x - pad), max(0, y - pad),
                           min(image.width, x + w + pad),
                           min(image.height, y + h + pad)))
        crop = crop.resize((200, 100), Image.Resampling.BICUBIC)
        try:
            if backend == "vision":
                found = _vision_words(crop, orientation)
            else:
                found = _run_tesseract(crop, orientation, psm=7,
                                       whitelist="0123456789")
        except RasterExtractionError:
            found = []
        values = [q["text"] for q in found
                  if q["text"].isdigit() and 1 <= int(q["text"]) <= 60]
        candidates.append({"bbox": [x, y, x + w, y + h],
                           "text": values[0] if values else "",
                           "confidence": max((q["confidence"] for q in found),
                                             default=0.0)})
    return candidates


def _complete_run(run):
    centers = [((b["bbox"][0] + b["bbox"][2]) / 2,
                (b["bbox"][1] + b["bbox"][3]) / 2) for b in run]
    axis = 0 if np.ptp([p[0] for p in centers]) >= np.ptp([p[1] for p in centers]) else 1
    ordered = sorted(zip(run, centers), key=lambda item: item[1][axis])
    known = [(i, int(b["text"])) for i, (b, _) in enumerate(ordered)
             if b.get("text", "").isdigit()]
    if len(known) < 2:
        raise RasterExtractionError("cannot establish a unique aisle run")
    steps = {(b - a) // (j - i) for (i, a), (j, b) in zip(known, known[1:])
             if j > i and abs(b - a) == j - i}
    if len(steps) != 1 or any(abs(b - a) != j - i
                              for (i, a), (j, b) in zip(known, known[1:])):
        raise RasterExtractionError("cannot establish a unique aisle run")
    step = steps.pop()
    start = known[0][1] - known[0][0] * step
    return [(start + i * step, center) for i, (_, center) in enumerate(ordered)]


def _aisle_anchors(candidates):
    if len(candidates) < 2:
        raise RasterExtractionError("no aisle badge candidates found")
    median_w = float(np.median([b["bbox"][2] - b["bbox"][0]
                                for b in candidates]))
    median_h = float(np.median([b["bbox"][3] - b["bbox"][1]
                                for b in candidates]))
    centers = [((b["bbox"][0] + b["bbox"][2]) / 2,
                (b["bbox"][1] + b["bbox"][3]) / 2) for b in candidates]
    groups, unused = [], set(range(len(candidates)))
    while unused:
        seed = unused.pop()
        group, changed = {seed}, True
        while changed:
            changed = False
            for i in list(unused):
                if any(abs(centers[i][0] - centers[j][0]) < .75 * median_w
                       or abs(centers[i][1] - centers[j][1]) < .75 * median_h
                       for j in group):
                    group.add(i)
                    unused.remove(i)
                    changed = True
        if len(group) >= 2:
            groups.append([candidates[i] for i in group])
    numbered = [item for group in groups for item in _complete_run(group)]
    values = [value for value, _ in numbered]
    if sorted(values) != list(range(1, len(values) + 1)) or len(values) < 20:
        raise RasterExtractionError(
            f"aisle badges do not form exact 1..N set: {sorted(values)}")
    return {f"AISLE {value}": [center[0], center[1]]
            for value, center in sorted(numbered)}


def _structural_mask(image, words, threshold, badges=()):
    rgb = np.asarray(image)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    neutral = rgb.max(axis=2) - rgb.min(axis=2) < 18
    light = ((gray >= 100) & (gray < threshold) & neutral).astype(np.uint8) * 255
    raw_light = light.copy()
    dark = ((gray < 100) & neutral).astype(np.uint8) * 255
    raw_dark = dark.copy()
    # Delete OCR glyph boxes before line extraction.  Red labels have already
    # been excluded by the neutral-color test and are kept in OCR records.
    for word in words:
        if word.get("region") == "red-label":
            continue
        x0, y0, x1, y1 = [round(v) for v in word["bbox"]]
        cv2.rectangle(dark, (max(0, x0 - 1), max(0, y0 - 1)),
                      (min(dark.shape[1] - 1, x1 + 1),
                       min(dark.shape[0] - 1, y1 + 1)), 0, -1)
        if word.get("region") != "shelf-band":
            cv2.rectangle(light, (max(0, x0 - 1), max(0, y0 - 1)),
                          (min(light.shape[1] - 1, x1 + 1),
                           min(light.shape[0] - 1, y1 + 1)), 0, -1)
    for badge in badges:
        x0, y0, x1, y1 = [round(v) for v in badge["bbox"]]
        cv2.rectangle(dark, (max(0, x0 - 3), max(0, y0 - 3)),
                      (min(dark.shape[1] - 1, x1 + 3),
                       min(dark.shape[0] - 1, y1 + 3)), 0, -1)
        cv2.rectangle(light, (max(0, x0 - 3), max(0, y0 - 3)),
                      (min(light.shape[1] - 1, x1 + 3),
                       min(light.shape[0] - 1, y1 + 3)), 0, -1)
    ink = cv2.bitwise_or(light, dark)
    length = max(12, round(min(image.size) * .009))
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    # Restore visible shelf/wall runs that OCR boxes may have cut apart. At
    # 200 DPI, .012 of the short side is roughly the 12-PDF-point component
    # floor used by the differential benchmark.
    long = max(length, round(min(image.size) * .012))
    light_horizontal = cv2.morphologyEx(
        raw_light, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (long, 1)))
    light_vertical = cv2.morphologyEx(
        raw_light, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, long)))
    dark_horizontal = cv2.morphologyEx(
        raw_dark, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (long, 1)))
    dark_vertical = cv2.morphologyEx(
        raw_dark, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, long)))
    diagonal = np.zeros_like(ink)
    lines = cv2.HoughLinesP(cv2.Canny(ink, 60, 180), 1, np.pi / 180,
                            threshold=max(8, length),
                            minLineLength=length, maxLineGap=4)
    for [[x0, y0, x1, y1]] in lines if lines is not None else ():
        angle = abs(np.degrees(np.arctan2(y1 - y0, x1 - x0))) % 90
        if 8 < angle < 82:
            cv2.line(diagonal, (x0, y0), (x1, y1), 255, 2)
    return cv2.morphologyEx(
        cv2.bitwise_or(cv2.bitwise_or(horizontal, vertical),
                       cv2.bitwise_or(diagonal,
                                      cv2.bitwise_or(
                                          cv2.bitwise_or(light_horizontal,
                                                         light_vertical),
                                          cv2.bitwise_or(dark_horizontal,
                                                         dark_vertical)))),
                            cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def _geometry(mask):
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE,
                                           cv2.CHAIN_APPROX_SIMPLE)
    fixtures, polys = [], []
    image_area = mask.shape[0] * mask.shape[1]
    for i, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        parent = hierarchy[0][i][3] if hierarchy is not None else -1
        if not (area >= image_area * .000025 and area < image_area * .08
                and w >= 6 and h >= 6):
            continue
        # Holes are fixture interiors. Top-level compact contours retain
        # service counters and irregular structures that are not double-lined.
        if parent < 0 and area / max(1, w * h) < .12:
            continue
        approx = cv2.approxPolyDP(contour,
                                  .01 * cv2.arcLength(contour, True), True)
        points = [[float(px), float(py)] for [[px, py]] in approx]
        if len(points) == 4 and all(
                abs(a[0] - b[0]) < 2 or abs(a[1] - b[1]) < 2
                for a, b in zip(points, points[1:] + points[:1])):
            fixtures.append([float(x), float(y), float(x + w), float(y + h)])
        elif 3 <= len(points) <= 40:
            polys.append(points)
    lines = cv2.HoughLinesP(mask, 1, np.pi / 180, threshold=20,
                            minLineLength=max(10, round(min(mask.shape) * .008)),
                            maxLineGap=5)
    paths, seen = [], set()
    for [[x0, y0, x1, y1]] in lines if lines is not None else ():
        if (x1, y1) < (x0, y0):
            x0, y0, x1, y1 = x1, y1, x0, y0
        key = tuple(round(v / 3) for v in (x0, y0, x1, y1))
        if key in seen:
            continue
        seen.add(key)
        paths.append([[float(x0), float(y0)], [float(x1), float(y1)]])
    return fixtures, polys, paths


def _write_artifacts(directory, original, image, red, mask, badges, geom):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "failure.json").unlink(missing_ok=True)
    original.save(directory / "original.png")
    image.save(directory / "normalized.png")
    Image.fromarray(red).save(directory / "red-label-mask.png")
    Image.fromarray(mask).save(directory / "structural-mask.png")
    badge_image = image.copy()
    draw = ImageDraw.Draw(badge_image)
    for badge in badges:
        draw.rectangle(badge["bbox"], outline="blue", width=3)
    badge_image.save(directory / "badge-candidates.png")
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    for a, b in geom["obstacle_paths"]:
        draw.line([tuple(a), tuple(b)], fill="orange", width=1)
    for fixture in geom["fixtures"]:
        draw.rectangle(fixture, outline="red", width=2)
    for poly in geom["fixture_polys"]:
        draw.polygon([tuple(p) for p in poly], outline="magenta")
    boundary = [tuple(p) for p in geom["boundary"]]
    draw.line(boundary + [boundary[0]], fill="green", width=5)
    overlay.save(directory / "geometry-overlay.png")


def _write_failure_artifacts(directory, original, image, error, backend,
                             rotation, threshold, words=(), badges=(),
                             mask=None, boundary_count=None,
                             segment_count=None):
    """Persist every diagnostic available when a candidate aborts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    original.save(directory / "original.png")
    if image is not None:
        image.save(directory / "normalized.png")
        Image.fromarray(_red_mask(image)).save(directory / "red-label-mask.png")
        badge_image = image.copy()
        draw = ImageDraw.Draw(badge_image)
        for badge in badges:
            draw.rectangle(badge["bbox"], outline="blue", width=3)
        badge_image.save(directory / "badge-candidates.png")
    if mask is not None:
        Image.fromarray(mask).save(directory / "structural-mask.png")
    with open(directory / "failure.json", "w") as output:
        json.dump({"backend": backend, "rotation": rotation,
                   "threshold": threshold, "reason": str(error),
                   "ocr_words": len(words), "badges": len(badges),
                   "boundary_vertices": boundary_count,
                   "segments": segment_count}, output, indent=2)


def extract_page(page, config=None, backend="auto", artifact_dir=None):
    """Extract normal routing geometry from one full-page image map."""
    if not is_raster_page(page):
        raise RasterExtractionError("raster fallback called for a vector page")
    config = config or {}
    original = _page_image(page)
    rotation, skew = int(config.get("rotation", 0)) % 360, 0.0
    attempted = []
    choices = (["vision", "tesseract"] if backend == "auto" else [backend])
    last_error = None
    boundary_count = segment_count = None
    for choice in choices:
        attempted.append(choice)
        image, words, badges, mask = original, [], [], None
        try:
            if "rotation" in config:
                candidates = [int(config["rotation"]) % 360]
            else:
                candidates = [0, 90, 180, 270]
            oriented = []
            for candidate in candidates:
                candidate_image = original.rotate(candidate, expand=True)
                candidate_image, candidate_skew = _deskew(candidate_image)
                candidate_words = _ocr(candidate_image, choice, candidate)
                oriented.append((_orientation_score(candidate_words), candidate,
                                 candidate_skew, candidate_image,
                                 candidate_words))
            _, rotation, skew, image, words = max(oriented, key=lambda item: item[0])
            badges = _badge_candidates(image, choice, rotation)
            anchors = _doorway_entrances({**_major_anchors(
                [word for word in words if word.get("region") == "red-label"]),
                                          **_aisle_anchors(badges)})
            missing = [label for label in ("ENTRANCE", "EXIT", "CHECKSTANDS",
                                            "RESTROOMS")
                       if label not in anchors]
            if missing:
                raise RasterExtractionError(
                    f"{choice}: missing required anchors {missing}")
            departments = {name.rstrip(" 23456789") for name in anchors
                           if name.rstrip(" 23456789") not in
                           {"ENTRANCE", "EXIT", "CHECKSTANDS", "RESTROOMS",
                            "BUSINESS CENTER"}
                           and not name.startswith("AISLE ")}
            if len(departments) < 5:
                raise RasterExtractionError(
                    f"{choice}: only {len(departments)} department anchors")
            entrance_n = sum(name == "ENTRANCE" or name.startswith("ENTRANCE ")
                             for name in anchors)
            exit_n = sum(name == "EXIT" or name.startswith("EXIT ")
                         for name in anchors)
            if entrance_n != exit_n:
                raise RasterExtractionError(
                    f"{choice}: entrance/exit count mismatch "
                    f"({entrance_n}/{exit_n})")
            positioned = [word for word in words
                          if word.get("region") != "red-label"]
            if len(positioned) < 150:
                raise RasterExtractionError(
                    f"{choice}: only {len(positioned)} positioned product "
                    "labels (need 150 for coverage QA)")
            legible = [word for word in positioned
                       if word["confidence"] >= .5 and len(word["text"]) >= 2
                       and sum(char.isalpha() for char in word["text"])
                       / len(word["text"]) >= .7]
            if len(legible) < 120:
                raise RasterExtractionError(
                    f"{choice}: only {len(legible)} legible positioned labels "
                    "(need 120 for coverage QA)")
            mask = _structural_mask(image, words,
                                    int(config.get("threshold", 245)), badges)
            boundary = discover_boundary(mask)
            boundary_count = len(boundary)
            fixtures, polys, paths = _geometry(mask)
            segment_count = len(paths)
            if len(fixtures) + len(polys) < 75 or len(paths) < 100:
                raise RasterExtractionError(
                    f"{choice}: insufficient structure: fixtures="
                    f"{len(fixtures) + len(polys)}, segments={len(paths)}")
            from router import engine
            route_cell = max(2.0, 2.0 * image.width /
                             (page.rect.height if rotation in (90, 270)
                              else page.rect.width))
            pixel_geom = {"page": {"w": image.width, "h": image.height},
                          "boundary": boundary, "fixtures": fixtures,
                          "fixture_polys": polys, "obstacle_paths": paths}
            raw = engine.build_grid(pixel_geom, cell=route_cell)
            seed = engine.nearest_free(raw, anchors["ENTRANCE"], route_cell)
            reach, _ = engine.bfs(raw, seed)
            height, width = raw.shape
            unreachable = []
            for name, (x, y) in anchors.items():
                if not name.startswith("AISLE "):
                    continue
                cx, cy = int(x // route_cell), int(y // route_cell)
                if (not raw[cy, cx]
                        or reach[cy * width + cx] < 0):
                    unreachable.append(name)
            if unreachable:
                raise RasterExtractionError(
                    f"{choice}: aisle mouths unreachable from entrance: "
                    f"{unreachable}")
            break
        except RasterExtractionError as exc:
            last_error = exc
            if artifact_dir:
                _write_failure_artifacts(
                    artifact_dir, original, image, exc, choice, rotation,
                    int(config.get("threshold", 245)), words, badges, mask,
                    boundary_count, segment_count)
    else:
        location = str(artifact_dir) if artifact_dir else "(not written)"
        raise RasterExtractionError(
            f"raster extraction failed; backends={attempted}; rotation={rotation}; "
            f"threshold={config.get('threshold', 245)}; "
            f"boundary={boundary_count}; segments={segment_count}; "
            f"artifacts={location}; "
            f"reason={last_error}")

    if rotation in (90, 270):
        page_width, page_height = page.rect.height, page.rect.width
    else:
        page_width, page_height = page.rect.width, page.rect.height
    sx, sy = page_width / image.width, page_height / image.height

    def point(p):
        return [round(p[0] * sx, 2), round(p[1] * sy, 2)]

    normalized_words = []
    for word in words:
        x0, y0, x1, y1 = word["bbox"]
        normalized_words.append({**word, "bbox": [round(x0 * sx, 2),
                                                   round(y0 * sy, 2),
                                                   round(x1 * sx, 2),
                                                   round(y1 * sy, 2)]})
    geom = {"source_kind": "raster", "rotation": rotation, "deskew": skew,
            "ocr_backend": choice,
            "page": {"w": page_width, "h": page_height},
            "anchors": {name: point(value) for name, value in anchors.items()},
            "fixtures": [[round(x0 * sx, 2), round(y0 * sy, 2),
                          round(x1 * sx, 2), round(y1 * sy, 2)]
                         for x0, y0, x1, y1 in fixtures],
            "fixture_polys": [[point(value) for value in poly]
                              for poly in polys],
            "obstacle_paths": [[point(a), point(b)] for a, b in paths],
            "boundary": [point(value) for value in boundary],
            "ocr_words": normalized_words}
    if artifact_dir:
        # Artifact overlays use image pixels, so retain the pre-scaled geometry.
        pixel_geom = {"fixtures": fixtures, "fixture_polys": polys,
                      "obstacle_paths": paths, "boundary": boundary}
        _write_artifacts(artifact_dir, original, image, _red_mask(image), mask,
                         badges, pixel_geom)
    return geom


def render_source(page, geom, dpi=144):
    """Render the same oriented source used to generate raster geometry."""
    if geom.get("source_kind") != "raster":
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    image = _page_image(page).rotate(int(geom.get("rotation", 0)), expand=True)
    skew = float(geom.get("deskew", 0))
    if skew:
        image = image.rotate(skew, resample=Image.Resampling.BICUBIC,
                             expand=False, fillcolor="white")
    width = round(geom["page"]["w"] * dpi / 72)
    height = round(geom["page"]["h"] * dpi / 72)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def coverage_words(page, geom):
    """Fitz-compatible word tuples for vector and stored raster OCR."""
    if geom.get("source_kind") != "raster":
        return page.get_text("words")
    words = [word for word in geom.get("ocr_words", ())
             if word.get("region") != "red-label"]
    return [(*word["bbox"], word["text"], i, 0, i)
            for i, word in enumerate(words)]
