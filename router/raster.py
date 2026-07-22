"""Image-only PDF map extraction support.

The vector extractor deliberately does not import image-processing code.  This
module owns the raster-only path and keeps its normalized OCR records and
geometry helpers testable without opening a PDF.
"""
import csv
import io
import json
import math
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


def _sharpness(image):
    """Median edge-gradient magnitude of the scan.

    Crisp renders measure >=~750 and blurred/low-contrast scans <=~510 on
    the benchmark corpus, so 600 splits the two regimes with margin.  Sharp
    scans can afford ink restoration near structure; blurred scans cannot,
    because blur fuses unrecognized text into line-shaped smears.
    """
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 180) > 0
    if not edges.any():
        return 0.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    return float(np.median(np.hypot(gx, gy)[edges]))


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
    """Prefer the rotation where red department labels read horizontally.

    Label COUNT barely discriminates because Vision reads sideways text,
    so the deciding vote is each recognized label's box aspect: wide at
    the true orientation, tall when the page is rotated.
    """
    labels, votes = set(), 0
    for word in words:
        if word.get("region") != "red-label":
            continue
        label = _canonical_label(word["text"])
        if not label:
            continue
        labels.add(label)
        if len(re.sub(r"[^A-Za-z]", "", word["text"])) >= 4:
            x0, y0, x1, y1 = word["bbox"]
            votes += 1 if x1 - x0 >= y1 - y0 else -1
    legible = sum(word["confidence"] for word in words
                  if len(word["text"].strip()) >= 2)
    return len(labels) * 100 + votes * 40 + legible


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


def _badge_digit_crop(image, box, scale, enhance=False):
    """Isolate a badge's digit glyphs as dark-on-white, or None if fused.

    Handles the three badge styles in the corpus: dark digits inside an
    outline hexagon, knockout (light) digits inside a solid hexagon, and
    tiny badges whose digits only separate from the ring after 4x
    supersampling.  Unreadable badges still anchor positions; their
    numbers are filled by the consensus run fit.
    """
    x, y, w, h = box
    pad = max(3, round(4 * scale))
    crop = image.crop((max(0, x - pad), max(0, y - pad),
                       min(image.width, x + w + pad),
                       min(image.height, y + h + pad)))
    base = cv2.cvtColor(np.asarray(crop), cv2.COLOR_RGB2GRAY)
    big = cv2.resize(base, (base.shape[1] * 4, base.shape[0] * 4),
                     interpolation=cv2.INTER_CUBIC)
    if enhance:
        # Unsharp the degraded upscale: blur fuses ring and digit into one
        # gray band, and OCR refuses digits wearing a hexagon ghost.  Crisp
        # crops skip this - overshoot halos hurt them.
        big = cv2.addWeighted(big, 2.2, cv2.GaussianBlur(big, (0, 0), 3),
                              -1.2, 0)

    def components(gray):
        # Otsu adapts to blur and reduced contrast; a fixed cut clips
        # degraded glyph strokes and loses digits entirely.
        _, dark = cv2.threshold(gray, 0, 255,
                                cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        dark = (dark > 0).astype(np.uint8)
        return dark, cv2.connectedComponentsWithStats(dark)

    def ring_of(stats, count, factor):
        for label in range(1, count):
            _, _, bw, bh, _ = stats[label]
            # The ring spans the badge box itself, not the padded crop.
            if bw >= .8 * w * factor or bh >= .8 * h * factor:
                return label
        return None

    # Attempt 1: geometric ring removal at 4x.  Filling the ring's outer
    # contour and eroding to the interior keeps digit strokes that touch
    # the ring (the leading "1" of 17..19), where component deletion would
    # drop them; 4x gives the erosion enough resolution for tiny badges.
    dark, (count, labels, stats, _) = components(big)
    ring = ring_of(stats, count, 4)
    if ring is not None:
        ring_mask = (labels == ring).astype(np.uint8)
        contours, _ = cv2.findContours(ring_mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        filled = np.zeros_like(ring_mask)
        cv2.drawContours(filled, contours, -1, 1, -1)
        interior = cv2.erode(filled, np.ones((11, 11), np.uint8)) > 0
        # Polarity first: a mostly-dark interior is a solid badge with
        # knockout digits; a mostly-light one is an outline with dark
        # digits.  Testing dark components first would swallow knockout
        # interiors whole.
        knockout = interior.any() and (dark > 0)[interior].mean() >= .55
        target = ((dark == 0) & interior if knockout
                  else (dark > 0) & interior)
        count2, labels2, stats2, _ = cv2.connectedComponentsWithStats(
            target.astype(np.uint8))
        keep = np.zeros(count2, bool)
        for label in range(1, count2):
            _, _, hw, hh, _ = stats2[label]
            if hh >= .22 * big.shape[0] and (not knockout
                                             or hw <= .7 * w * 4):
                keep[label] = True
        if keep.any():
            out = (255 - big) if knockout else big.copy()
            out[~(keep[labels2] & target)] = 255
            return Image.fromarray(out)
    # Attempt 2: plain component split (no ring, or geometric found nothing).
    for factor, gray in ((4, big), (1, base)):
        dark, (count, labels, stats, _) = components(gray)
        ring = ring_of(stats, count, factor)
        keep = np.zeros(count, bool)
        for label in range(1, count):
            if label != ring and stats[label][3] >= .25 * gray.shape[0]:
                keep[label] = True
        if keep.any():
            out = gray.copy()
            out[~keep[labels]] = 255
            return Image.fromarray(out)
    return None


def _badge_candidates(image, backend, orientation):
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    scale = min(image.size) / 1700
    # Blur and JPEG ringing shift each badge's ink threshold individually:
    # a faint hexagon's outline only closes at a loose cut, while a loose
    # cut fuses neighbouring linework around a crisp one.  Detect at each
    # cut and keep the union, since a shape only counts when it looks like
    # a badge at the cut that found it.
    boxes = []
    for cut in (120, 165, 200):
        dark = (gray < cut).astype(np.uint8) * 255
        contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            fill = cv2.contourArea(contour) / max(1, w * h)
            # Window measured across the guide corpus: hexagon badges span
            # 15..43 wide and 12..23 tall in 1700px-normalized units with
            # aspect 1.17..1.93 and solid >=.65 fill.  The vertex count is
            # the discriminator against same-sized fixture rectangles (4)
            # and text fragments (many): the house badge style is a hexagon.
            if not (10 * scale <= h <= 26 * scale
                    and 13 * scale <= w <= 48 * scale
                    and 1.1 <= w / h <= 2.5 and fill > .5):
                continue
            vertices = len(cv2.approxPolyDP(
                contour, .03 * cv2.arcLength(contour, True), True))
            if not 5 <= vertices <= 8:
                continue
            center = (x + w / 2, y + h / 2)
            if any(abs(center[0] - (bx + bw / 2)) < .6 * bw
                   and abs(center[1] - (by + bh / 2)) < .6 * bh
                   for bx, by, bw, bh in boxes):
                continue
            boxes.append((x, y, w, h))
    if not boxes:
        return []
    # One OCR pass over a contact sheet of upscaled digit crops beats a
    # per-badge call on both accuracy (tiny hexagon digits become large,
    # clean glyphs) and runtime (one recognition instead of dozens).  The
    # sheet mimics lines of printed numbers - both backends handle that
    # layout far better than a sparse symbol grid.
    enhance = _sharpness(image) < 600
    shapes = [_badge_digit_crop(image, box, scale, enhance) for box in boxes]
    if not any(crop is not None for crop in shapes):
        return [{"bbox": [x, y, x + w, y + h], "text": "", "confidence": 0.0}
                for x, y, w, h in boxes]
    # Badge digits are only ~20px tall on the page, and which upscale reads
    # a given badge best is not predictable per store: 140px wins overall,
    # 64px still recovers badges 140px garbles.  Read both and pool the
    # votes rather than betting the whole store on one scale.
    votes = {}
    for height in (140, 64):
        crops = []
        for crop in shapes:
            if crop is not None:
                crop = crop.resize((max(24, round(crop.width * height
                                                  / crop.height)), height),
                                   Image.Resampling.LANCZOS)
            crops.append(crop)
        readable = [crop for crop in crops if crop is not None]
        cell_w = max(crop.width for crop in readable) + 56
        cell_h = max(crop.height for crop in readable) + 56
        columns = 10
        rows = math.ceil(len(crops) / columns)
        sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
        for i, crop in enumerate(crops):
            if crop is None:
                continue
            sheet.paste(crop.convert("RGB"),
                        ((i % columns) * cell_w + (cell_w - crop.width) // 2,
                         (i // columns) * cell_h + (cell_h - crop.height) // 2))
        try:
            if backend == "vision":
                found = _vision_words(sheet, orientation)
            else:
                # psm 6 (uniform block) reads the digit grid far better
                # than sparse-text mode.
                found = _run_tesseract(sheet, orientation, psm=6,
                                       whitelist="0123456789")
        except RasterExtractionError:
            found = []
        cells = {}
        for word in found:
            if not word["text"].strip().isdigit():
                continue
            cx = (word["bbox"][0] + word["bbox"][2]) / 2
            cy = (word["bbox"][1] + word["bbox"][3]) / 2
            index = int(cy // cell_h) * columns + int(cx // cell_w)
            if 0 <= index < len(boxes):
                cells.setdefault(index, []).append(word)
        for index, words in cells.items():
            # A two-digit badge often comes back as two words; join them in
            # reading order.  Re-observations of the same glyph overlap in
            # x, so the more confident one replaces rather than extends.
            kept = []
            for word in sorted(words, key=lambda item: item["bbox"][0]):
                x0, x1 = word["bbox"][0], word["bbox"][2]
                overlap = next(
                    (other for other in kept
                     if min(x1, other["bbox"][2]) - max(x0, other["bbox"][0])
                     > .5 * min(x1 - x0, other["bbox"][2] - other["bbox"][0])),
                    None)
                if overlap is None:
                    kept.append(word)
                elif word["confidence"] > overlap["confidence"]:
                    kept[kept.index(overlap)] = word
            text = "".join(word["text"].strip() for word in kept)
            if text.isdigit() and 1 <= int(text) <= 60:
                score = min(word["confidence"] for word in kept)
                tally = votes.setdefault(index, {})
                tally[text] = tally.get(text, 0.0) + score
    read = {index: max(tally.items(), key=lambda item: item[1])
            for index, tally in votes.items()}
    return [{"bbox": [x, y, x + w, y + h],
             "text": read.get(i, ("", 0.0))[0],
             "confidence": min(1.0, read.get(i, ("", 0.0))[1]),
             }
            for i, (x, y, w, h) in enumerate(boxes)]


def _complete_run(run):
    """Fit value = start + step*slot by pair consensus over pitch slots.

    Slots come from GEOMETRY, not list order: a badge box lost to touching
    linework leaves a pitch-sized hole, and index-based numbering would
    silently shift every value after it.  Each read pair votes for a
    (start, step) mapping; the majority mapping wins if it is unique and
    explains at least 60% of the read digits, outvoted digits are treated
    as unreadable, and interior holes of up to three slots are filled at
    their interpolated positions.  The exact-1..N union check in
    _aisle_anchors remains the hard safety net.
    """
    centers = [((b["bbox"][0] + b["bbox"][2]) / 2,
                (b["bbox"][1] + b["bbox"][3]) / 2) for b in run]
    axis = 0 if np.ptp([p[0] for p in centers]) >= np.ptp([p[1] for p in centers]) else 1
    ordered = sorted(zip(run, centers), key=lambda item: item[1][axis])
    gaps = [b[1][axis] - a[1][axis] for a, b in zip(ordered, ordered[1:])]
    positive = sorted(gap for gap in gaps if gap > 1)
    if positive:
        median_gap = positive[len(positive) // 2]
        # The badge pitch is the smallest real gap; overlapping duplicate
        # boxes (near-zero gaps) must not shrink it.
        unit = min(gap for gap in positive if gap > .35 * median_gap)
    else:
        unit = 1.0
    # Two position models: plain order (robust to pitch drift within a
    # corridor) and pitch slots (robust to a missing badge box, which
    # otherwise shifts every later value).  Whichever explains more read
    # digits wins; ties go to plain order.
    index_slots = list(range(len(ordered)))
    pitch_slots = [0]
    for gap in gaps:
        pitch_slots.append(pitch_slots[-1] + max(1, round(gap / unit)))

    def fit(slots):
        known = [(slots[i], int(b["text"]))
                 for i, (b, _) in enumerate(ordered)
                 if b.get("text", "").isdigit()]
        if len(known) < 2:
            return None
        votes = {}
        for a_index, (i, a) in enumerate(known):
            for j, b in known[a_index + 1:]:
                gap, delta = j - i, b - a
                if not gap or delta % gap:
                    continue
                step = delta // gap
                # Aisle numbers advance one per slot along a regular run.
                if abs(step) != 1:
                    continue
                votes.setdefault((a - i * step, step), set()).update(
                    {(i, a), (j, b)})
        if not votes:
            return None
        supports = {mapping: len(inliers)
                    for mapping, inliers in votes.items()}
        top = max(supports.values())
        winners = [mapping for mapping, count in supports.items()
                   if count == top]
        if len(winners) != 1 or top < max(2, math.ceil(.6 * len(known))):
            return None
        return top, winners[0]

    def positive(slots, result):
        # Aisle numbers start at 1; a fit that walks a run into zero or
        # negative values has locked onto the wrong offset.  A descending
        # run puts its smallest value at the LAST slot, so both ends must
        # be checked, not just the first.
        if result is None:
            return None
        start, step = result[1]
        return result if min(start + slot * step
                             for slot in slots) >= 1 else None

    plain = positive(index_slots, fit(index_slots))
    pitched = positive(pitch_slots, fit(pitch_slots))
    if plain is None and pitched is None:
        raise RasterExtractionError("cannot establish a unique aisle run")
    if pitched is not None and (plain is None or pitched[0] > plain[0]):
        slots, (start, step) = pitch_slots, pitched[1]
    else:
        slots, (start, step) = index_slots, plain[1]
    numbered = [(start + slots[i] * step, center)
                for i, (_, center) in enumerate(ordered)]
    for i, ((_, before), (_, after)) in enumerate(zip(ordered, ordered[1:])):
        hole = slots[i + 1] - slots[i]
        if 2 <= hole <= 3:
            for k in range(1, hole):
                t = k / hole
                numbered.append((start + (slots[i] + k) * step,
                                 (before[0] + t * (after[0] - before[0]),
                                  before[1] + t * (after[1] - before[1]))))
    return numbered


def _runs_along(candidates, axis, tolerance):
    """Cluster badges into straight runs: aligned across `axis`, ordered
    along it, split where the pitch jumps (separate corridors)."""
    cross = 1 - axis
    rows = []
    for candidate in sorted(candidates, key=lambda b: (
            (b["bbox"][cross] + b["bbox"][cross + 2]) / 2)):
        center = ((candidate["bbox"][0] + candidate["bbox"][2]) / 2,
                  (candidate["bbox"][1] + candidate["bbox"][3]) / 2)
        for row in rows:
            if abs(center[cross] - row[-1][1][cross]) <= tolerance:
                row.append((candidate, center))
                break
        else:
            rows.append([(candidate, center)])
    runs = []
    for row in rows:
        row.sort(key=lambda item: item[1][axis])
        split = [[row[0]]]
        gaps = [b[1][axis] - a[1][axis] for a, b in zip(row, row[1:])]
        pitch = float(np.median(gaps)) if gaps else 0.0
        for previous, item in zip(row, row[1:]):
            gap = item[1][axis] - previous[1][axis]
            if pitch and gap > 1.8 * pitch:
                split.append([])
            split[-1].append(item)
        runs.extend(split)
    return runs


def _aisle_anchors(candidates):
    if len(candidates) < 2:
        raise RasterExtractionError("no aisle badge candidates found")
    ref_h = float(np.median([b["bbox"][3] - b["bbox"][1]
                             for b in candidates]))
    # The plan's model: badges form regular horizontal or vertical runs.
    # Both families are built over ALL candidates - an aisle column crosses
    # aisle rows, and the badge at the intersection belongs to both; the
    # weighted claims plus the global exact-1..N constraint decide which
    # numbering each shared badge serves.
    horizontal = [run for run in _runs_along(candidates, 0, .6 * ref_h)
                  if len(run) >= 2]
    vertical = [run for run in _runs_along(candidates, 1, .6 * ref_h)
                if len(run) >= 2]
    used = {id(candidate) for run in horizontal + vertical
            for candidate, _ in run}
    loose = [b for b in candidates if id(b) not in used]
    fitted, unfit_members = [], []
    for run in horizontal + vertical:
        axis = 0 if np.ptp([c[0] for _, c in run]) >= np.ptp(
            [c[1] for _, c in run]) else 1
        ordered = sorted(run, key=lambda item: item[1][axis])
        try:
            numbered = _complete_run([candidate for candidate, _ in ordered])
            fitted.append((ordered, numbered, axis, len(run)))
        except RasterExtractionError:
            unfit_members.extend(candidate for candidate, _ in ordered)
    in_fitted = {id(candidate) for ordered, _, _, _ in fitted
                 for candidate, _ in ordered}
    unresolved = loose + [candidate for candidate in unfit_members
                          if id(candidate) not in in_fitted]
    base = []           # (value, center, weight)
    # A badge whose box was never detected leaves a value-and-pitch
    # consistent gap BETWEEN two runs of the same corridor; bridge it with
    # phantom claims at the interpolated positions.
    for first_index, (ordered_a, numbered_a, axis, weight_a) in enumerate(fitted):
        along_a = sorted(numbered_a, key=lambda item: item[1][axis])
        gaps_a = [b[1][axis] - a[1][axis]
                  for a, b in zip(along_a, along_a[1:])]
        pitch = float(np.median(gaps_a)) if gaps_a else 0.0
        if pitch <= 0:
            continue
        cross = 1 - axis
        # Loose read badges act as one-member partners: a run end and a
        # read badge two-to-four pitches away pin the values between them.
        partners = [(sorted(numbered_b, key=lambda item: item[1][axis]),
                     weight_b)
                    for ordered_b, numbered_b, axis_b, weight_b
                    in fitted[first_index + 1:] if axis_b == axis]
        partners += [([(int(badge["text"]),
                        ((badge["bbox"][0] + badge["bbox"][2]) / 2,
                         (badge["bbox"][1] + badge["bbox"][3]) / 2))], 1)
                     for badge in unresolved
                     if badge.get("text", "").isdigit()]
        for along_b, weight_b in partners:
            for end, start in ((along_a[-1], along_b[0]),
                               (along_b[-1], along_a[0])):
                gap = start[1][axis] - end[1][axis]
                if gap <= 0:
                    continue
                if abs(start[1][cross] - end[1][cross]) > .8 * ref_h:
                    continue
                delta = start[0] - end[0]
                if not 2 <= abs(delta) <= 4:
                    continue
                if abs(gap - abs(delta) * pitch) > .3 * abs(delta) * pitch:
                    continue
                step = 1 if delta > 0 else -1
                for k in range(1, abs(delta)):
                    t = k / abs(delta)
                    base.append((end[0] + k * step,
                                 (end[1][0] + t * (start[1][0] - end[1][0]),
                                  end[1][1] + t * (start[1][1] - end[1][1])),
                                 min(weight_a, weight_b)))
    ambiguous = []      # [(fitted claim), (direct-read claim)] per member
    for ordered, numbered, axis, weight in fitted:
        for (candidate, _), (value, center) in zip(ordered, numbered):
            text = candidate.get("text", "")
            if text.isdigit() and int(text) != value:
                # Either a misread digit inside the run (the fit is right)
                # or a stray badge from an adjacent corridor absorbed into
                # the row (the read is right).  Locally undecidable; the
                # global exact-1..N constraint picks the interpretation.
                ambiguous.append([(value, center, weight),
                                  (int(text), center, 1)])
            else:
                base.append((value, center, weight))
        # Interior hole fills appended by _complete_run beyond the members.
        base.extend((value, center, weight)
                    for value, center in numbered[len(ordered):])
        # An unread badge beyond the run's end continues the same regular
        # numbering (its digit was simply illegible); walk outward up to
        # three pitch steps, alignment enforced at each one.
        along = sorted(numbered, key=lambda item: item[1][axis])
        gaps = [b[1][axis] - a[1][axis] for a, b in zip(along, along[1:])]
        pitch = float(np.median(gaps)) if gaps else 0.0
        if pitch <= 0 or len(along) < 2:
            continue
        cross = 1 - axis
        for end, direction in ((along[-1], 1), (along[0], -1)):
            value_step = (1 if along[-1][0] > along[0][0] else -1) * direction
            cursor = end
            for _ in range(3):
                target = cursor[1][axis] + direction * pitch
                match = next(
                    (badge for badge in unresolved
                     if abs((badge["bbox"][axis]
                             + badge["bbox"][axis + 2]) / 2 - target)
                     <= .3 * pitch
                     and abs((badge["bbox"][cross]
                              + badge["bbox"][cross + 2]) / 2
                             - cursor[1][cross]) <= .8 * ref_h), None)
                if match is None:
                    break
                center = ((match["bbox"][0] + match["bbox"][2]) / 2,
                          (match["bbox"][1] + match["bbox"][3]) / 2)
                cursor = (cursor[0] + value_step, center)
                text = match.get("text", "")
                if text.isdigit() and int(text) != cursor[0]:
                    # The run's regular spacing and this badge's own digit
                    # disagree.  Degraded scans misread digits as often as
                    # they drop them, so let the global 1..N search decide.
                    ambiguous.append([(cursor[0], center, weight),
                                      (int(text), center, 1)])
                else:
                    base.append((cursor[0], center, weight))
    for badge in candidates:
        if id(badge) not in in_fitted and badge.get("text", "").isdigit():
            box = badge["bbox"]
            base.append((int(badge["text"]),
                         ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), 1))
    # Last resort for a badge lost at a corridor corner: it belongs to no
    # shared row or column, but value-adjacent READ badges sit one pitch
    # to each side and pin it.  Only values nothing else claims are filled
    # this way - competing with real evidence would multiply readings.
    claimed = ({value for value, _, _ in base}
               | {value for pair in ambiguous for value, _, _ in pair})
    unclaimed = set(range(1, max(claimed, default=0) + 1)) - claimed
    pitches = []
    for _, numbered, axis, _ in fitted:
        along = sorted(numbered, key=lambda item: item[1][axis])
        pitches.extend(b[1][axis] - a[1][axis]
                       for a, b in zip(along, along[1:]))
    pitch_ref = float(np.median(pitches)) if pitches else 0.0
    if unclaimed and pitch_ref > 0:
        reads = [(int(b["text"]), ((b["bbox"][0] + b["bbox"][2]) / 2,
                                   (b["bbox"][1] + b["bbox"][3]) / 2))
                 for b in candidates if b.get("text", "").isdigit()]
        for first, (value_a, center_a) in enumerate(reads):
            for value_b, center_b in reads[first + 1:]:
                delta = value_b - value_a
                if not 2 <= abs(delta) <= 3:
                    continue
                step = 1 if delta > 0 else -1
                between = {value_a + k * step for k in range(1, abs(delta))}
                if not between <= unclaimed:
                    continue
                distance = math.hypot(center_b[0] - center_a[0],
                                      center_b[1] - center_a[1])
                if distance > (abs(delta) + 1.2) * pitch_ref:
                    continue
                for k in range(1, abs(delta)):
                    t = k / abs(delta)
                    base.append((value_a + k * step,
                                 (center_a[0] + t * (center_b[0] - center_a[0]),
                                  center_a[1] + t * (center_b[1] - center_a[1])),
                                 1))
    if len(ambiguous) > 8:
        raise RasterExtractionError(
            f"{len(ambiguous)} contested aisle badge digits")
    reads = [(int(b["text"]), ((b["bbox"][0] + b["bbox"][2]) / 2,
                               (b["bbox"][1] + b["bbox"][3]) / 2))
             for b in candidates if b.get("text", "").isdigit()]

    def agreement(placed):
        return sum(1 for value, center in reads
                   if value in placed and np.hypot(
                       placed[value][1][0] - center[0],
                       placed[value][1][1] - center[1]) <= 6)

    outcomes = {}
    for combo in range(1 << len(ambiguous)):
        chosen = [pair[(combo >> i) & 1] for i, pair in enumerate(ambiguous)]
        placed = _resolve_aisles(base + chosen)
        if placed is not None:
            key = tuple(sorted((value, round(center[0]), round(center[1]))
                               for value, (_, center) in placed.items()))
            outcomes[key] = placed
    if len(outcomes) > 1:
        # Competing complete numberings: OCR reads are evidence, so the
        # interpretation that honors the most read digits in place wins;
        # a tie stays ambiguous.
        scored = sorted(((agreement(placed), key) for key, placed
                         in outcomes.items()), reverse=True)
        if scored[0][0] > scored[1][0]:
            outcomes = {scored[0][1]: outcomes[scored[0][1]]}
    if len(outcomes) != 1:
        claimed = ({value for value, _, _ in base}
                   | {value for pair in ambiguous for value, _, _ in pair})
        missing = sorted(set(range(1, max(claimed, default=0) + 1)) - claimed)
        raise RasterExtractionError(
            "aisle badges do not form exact 1..N set "
            f"({len(outcomes)} consistent interpretations; "
            f"unclaimed: {missing})")
    placed = outcomes.popitem()[1]
    return {f"AISLE {value}": [center[0], center[1]]
            for value, (_, center) in sorted(placed.items())}


def _resolve_aisles(claims_list):
    """Resolve weighted claims into an exact 1..N placement, or None."""
    claims = {}
    for value, center, weight in claims_list:
        claims.setdefault(value, []).append((weight, center))
    placed, deferred = {}, []
    for value in sorted(claims, key=lambda v: -max(w for w, _ in claims[v])):
        options = claims[value]
        top = max(weight for weight, _ in options)
        winners = [center for weight, center in options if weight == top]
        if any(np.hypot(a[0] - b[0], a[1] - b[1]) > 10
               for a in winners for b in winners):
            deferred.append((value, top, winners))
        else:
            placed[value] = (top, winners[0])
    # Equal-support claims at different spots: aisle v is printed beside
    # v-1/v+1, so the instance nearest an already-placed neighbor wins;
    # with no placed neighbor the numbering stays ambiguous.
    while deferred:
        progressed, remaining = False, []
        for value, top, winners in deferred:
            neighbors = [placed[v][1] for v in (value - 1, value + 1)
                         if v in placed]
            if neighbors:
                placed[value] = (top, min(
                    winners, key=lambda c: min(
                        np.hypot(c[0] - n[0], c[1] - n[1])
                        for n in neighbors)))
                progressed = True
            else:
                remaining.append((value, top, winners))
        deferred = remaining
        if not progressed:
            return None
    values = sorted(placed)
    if values != list(range(1, len(values) + 1)) or len(values) < 20:
        return None
    return placed


def _erase_boxes(mask, boxes, pad):
    for x0, y0, x1, y1 in boxes:
        cv2.rectangle(mask, (max(0, round(x0) - pad), max(0, round(y0) - pad)),
                      (min(mask.shape[1] - 1, round(x1) + pad),
                       min(mask.shape[0] - 1, round(y1) + pad)), 0, -1)


def _thin(mask, limit=4):
    """Keep only stroke-width ink.  Blur fuses unread text into 5..7 px
    smear bands; real drawn lines stay within ~4 px of background even
    after degradation, so a distance-transform cut separates them."""
    distance = cv2.distanceTransform((mask > 0).astype(np.uint8),
                                     cv2.DIST_L2, 3)
    return ((mask > 0) & (distance <= limit)).astype(np.uint8) * 255


def _artwork_mask(image, gray, hsv, threshold):
    """Colored logo/banner artwork committed as real vector drawings.

    Guides draw vendor banners, service-counter logos, and colored kiosks
    as artwork, so their strokes are part of the committed differential
    truth.  Red needs care: department LABEL text is also red, and only
    OCR knows where it is - but OCR misses degrade with blur.  Shape is
    the robust signal: label text is sparse glyph work while banners are
    solid fills, so only large mostly-solid red components count.  The
    gradient keeps stroke edges rather than filled interiors, matching
    how the truth mask draws outlines.
    """
    scale = min(image.size) / 1700
    colored = (hsv[..., 1] > 70) & (hsv[..., 2] > 80) & (gray < threshold)
    red = _red_mask(image) > 0
    red_components = (colored & red).astype(np.uint8) * 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats(red_components)
    solid = np.zeros(count, bool)
    for label in range(1, count):
        _, _, width, height, area = stats[label]
        if (area >= 900 * scale * scale
                and area / max(1, width * height) >= .75):
            solid[label] = True
    kept = cv2.bitwise_or((colored & ~red).astype(np.uint8) * 255,
                          solid[labels].astype(np.uint8) * 255)
    kept = cv2.morphologyEx(kept, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return cv2.morphologyEx(kept, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))


def _structural_mask(image, words, threshold, badges=()):
    rgb = np.asarray(image)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    scale = min(image.size) / 1700
    sharp = _sharpness(image) >= 600
    # Erasing an OCR box deletes structure under it, and Tesseract emits
    # low-confidence boxes over plain linework; on sharp scans only trust
    # confident words.  Blurred scans erase everything OCR offers - real
    # blurred text is exactly what their morphology cannot reject.
    erasable = ([word for word in words if word["confidence"] >= .35]
                if sharp else words)
    neutral = rgb.max(axis=2) - rgb.min(axis=2) < 18
    light = ((gray >= 100) & (gray < threshold) & neutral).astype(np.uint8) * 255
    dark = ((gray < 100) & neutral).astype(np.uint8) * 255
    if not sharp:
        light, dark = _thin(light), _thin(dark)
    # Aisle badges are map annotations at the corridor mouth, never
    # structure, so they are erased before the long-run snapshot is taken -
    # a solid badge is wide enough to survive the line openings and would
    # otherwise wall off the very aisle it labels.
    for badge in badges:
        x0, y0, x1, y1 = [round(v) for v in badge["bbox"]]
        for plane in (dark, light):
            cv2.rectangle(plane, (max(0, x0 - 3), max(0, y0 - 3)),
                          (min(plane.shape[1] - 1, x1 + 3),
                           min(plane.shape[0] - 1, y1 + 3)), 0, -1)
    # The long-run restoration below reads these snapshots, so they must
    # keep the ink that WORD boxes cover (printed labels sit on top of real
    # shelf lines) while excluding the annotations erased above.
    raw_light = light.copy()
    raw_dark = dark.copy()
    # Delete OCR glyph boxes before line extraction.  Red labels have already
    # been excluded by the neutral-color test and are kept in OCR records.
    # A red-dominated box is a department/vendor label read by the FULL-page
    # pass: its glyphs are not neutral ink, so erasing it would only delete
    # the structure printed beneath the label.
    red_px = _red_mask(image) > 0
    ink_px = (light > 0) | (dark > 0)
    for word in erasable:
        if word.get("region") == "red-label":
            continue
        x0, y0, x1, y1 = [round(v) for v in word["bbox"]]
        y0c, y1c = max(0, y0), min(dark.shape[0], y1 + 1)
        x0c, x1c = max(0, x0), min(dark.shape[1], x1 + 1)
        red_dominated = (
            y1c > y0c and x1c > x0c
            and red_px[y0c:y1c, x0c:x1c].sum()
            >= max(1, ink_px[y0c:y1c, x0c:x1c].sum()))
        if not red_dominated:
            cv2.rectangle(dark, (max(0, x0 - 1), max(0, y0 - 1)),
                          (min(dark.shape[1] - 1, x1 + 1),
                           min(dark.shape[0] - 1, y1 + 1)), 0, -1)
        # Red glyphs keep a light-gray compression halo that reads as ink,
        # so the light plane is erased even for red-dominated labels; the
        # dark structure printed beneath the label survives.
        if word.get("region") != "shelf-band":
            cv2.rectangle(light, (max(0, x0 - 1), max(0, y0 - 1)),
                          (min(light.shape[1] - 1, x1 + 1),
                           min(light.shape[0] - 1, y1 + 1)), 0, -1)
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
    kept = (horizontal | vertical | diagonal | light_horizontal
            | light_vertical | dark_horizontal | dark_vertical) > 0
    kept |= _artwork_mask(image, gray, hsv, threshold) > 0
    # Dark colored ink below label brightness (maroon logo rings, navy
    # counters) is structure the neutral test would otherwise drop; bright
    # red label text stays out via the value ceiling.
    dark_colored = ((gray < 100) & ~neutral
                    & (hsv[..., 2] < 130)).astype(np.uint8) * 255
    _erase_boxes(dark_colored, [word["bbox"] for word in erasable
                                if word.get("region") != "red-label"], 1)
    kept |= dark_colored > 0
    # Small rotated fixtures draw closed thin outlines whose diagonal edges
    # are too short for the global Hough vote.  A closed outline is unlike
    # text in every regime: both bbox sides are fixture-sized and the
    # stroke encloses mostly empty interior.
    residual = ((gray < threshold) & neutral).astype(np.uint8) * 255
    _erase_boxes(residual, [word["bbox"] for word in erasable], 2)
    # Badge hexagons were erased from the line masks; restoring them here
    # would wall off the very aisle mouths they mark.
    _erase_boxes(residual, [badge["bbox"] for badge in badges], 3)
    residual = cv2.morphologyEx(residual, cv2.MORPH_CLOSE,
                                np.ones((3, 3), np.uint8))
    thin_ink = _thin(residual) > 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        thin_ink.astype(np.uint8))
    outline = np.zeros(count, bool)
    for label in range(1, count):
        _, _, width, height, area = stats[label]
        if not (min(width, height) >= 24
                and area / max(1, width * height) <= .5):
            continue
        # Aisle badges are hexagon outlines in this size class; they are
        # annotations, not structure, and production erases them by their
        # OCR boxes.  This mask must reject them by shape as well so the
        # badge-free differential measurement matches.
        if (10 * scale <= height <= 26 * scale
                and 13 * scale <= width <= 48 * scale
                and 1.1 <= width / max(1, height) <= 2.5):
            continue
        outline[label] = True
    kept |= thin_ink & outline[labels]
    if sharp:
        # Directional openings also drop rounded fixture corners, arcs, and
        # the short stubs OCR erasure cut.  On crisp scans, restore
        # stroke-width ink adjacent to kept structure; text glyphs sit
        # farther away.  Blur fuses text into the same band, so blurred
        # scans skip this.
        near_kept = cv2.dilate(kept.astype(np.uint8),
                               np.ones((17, 17), np.uint8)) > 0
        kept |= thin_ink & near_kept
    return cv2.morphologyEx(kept.astype(np.uint8) * 255,
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


def extract_page(page, config=None, backend="tesseract", artifact_dir=None):
    """Extract normal routing geometry from one full-page image map.

    Tesseract is the production backend: on the differential corpus it reads
    roughly 700 words per page against Apple Vision's 400, which is the
    difference between passing and failing the positioned-label gate.
    "auto" and "vision" stay callable for benchmark comparison.
    """
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
            def doors_at_bottom(entry):
                doors = [word for word in entry[4]
                         if word.get("region") == "red-label"
                         and _canonical_label(word["text"])
                         in ("ENTRANCE", "EXIT")]
                if not doors:
                    return None
                mean = sum((word["bbox"][1] + word["bbox"][3]) / 2
                           for word in doors) / len(doors)
                return mean > entry[3].height / 2

            oriented.sort(key=lambda item: item[0], reverse=True)
            best = oriented[0]
            # Label aspect cannot tell 0 from 180 (both horizontal), and
            # OCR reads upside-down digits, so the flip is settled by the
            # guides' layout convention: doors are labeled on the customer
            # (bottom) edge.  Aisle assembly must also succeed - an
            # upside-down page usually cannot form an exact 1..N set.
            opposite = next((entry for entry in oriented[1:]
                             if (entry[1] - best[1]) % 360 == 180), None)
            if opposite is not None and doors_at_bottom(best) is False \
                    and doors_at_bottom(opposite):
                best = opposite
            badges = _badge_candidates(best[3], choice, best[1])
            try:
                aisles = _aisle_anchors(badges)
            except RasterExtractionError:
                if opposite is None or best is opposite:
                    raise
                badges = _badge_candidates(opposite[3], choice, opposite[1])
                aisles = _aisle_anchors(badges)
                best = opposite
            _, rotation, skew, image, words = best
            anchors = _doorway_entrances({**_major_anchors(
                [word for word in words if word.get("region") == "red-label"]),
                                          **aisles})
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
            # Guides may label every door "Entrance" but mark fewer exits
            # (store 24 draws a second entrance as artwork), so only the
            # presence of each is a hard invariant.
            if not entrance_n or not exit_n:
                raise RasterExtractionError(
                    f"{choice}: entrance/exit labels missing "
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
