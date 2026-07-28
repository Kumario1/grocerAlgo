import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys

import cv2
import fitz
import numpy as np
import pytest
from PIL import Image

from router import raster
from router import engine
from raster_benchmark import pixel_metrics, render, truth_mask


def test_tesseract_tsv_is_normalized():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
           "left\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t91.5\tProduce\n"
           "5\t1\t1\t1\t1\t2\t45\t20\t7\t12\t-1\t\n")

    assert raster.parse_tesseract_tsv(tsv, orientation=90) == [{
        "text": "Produce", "confidence": .915,
        "bbox": [10.0, 20.0, 40.0, 32.0], "orientation": 90,
        "backend": "tesseract",
    }]


def test_tesseract_tsv_does_not_treat_ocr_quotes_as_csv_syntax():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
           "left\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t1\t2\t3\t4\t80\t\"Baby\n"
           "5\t1\t1\t1\t1\t2\t5\t6\t7\t8\t90\tWater\n")

    assert [word["text"] for word in raster.parse_tesseract_tsv(tsv)] == [
        '"Baby', "Water"]


def test_vision_phrase_is_split_into_positioned_words():
    records = raster.normalize_vision_phrase(
        "Business Center", .94, [10, 20, 110, 40], 90)

    assert [record["text"] for record in records] == ["Business", "Center"]
    assert records[0]["bbox"] == [10.0, 20.0, 60.0, 40.0]
    assert records[1]["bbox"] == [60.0, 20.0, 110.0, 40.0]
    assert all(record["backend"] == "vision" for record in records)


def test_overlapping_orientation_words_are_deduplicated_for_coverage():
    words = [
        {"text": "Coffee", "confidence": .8, "bbox": [10, 20, 40, 30],
         "orientation": 0, "backend": "vision"},
        {"text": "COFFEE", "confidence": .9, "bbox": [11, 20, 41, 30],
         "orientation": 90, "backend": "vision", "region": "shelf-band"},
        {"text": "Coffee", "confidence": .7, "bbox": [80, 20, 110, 30],
         "orientation": 0, "backend": "vision"},
    ]

    deduped = raster._dedupe_words(words)

    assert len(deduped) == 2
    assert max(word["confidence"] for word in deduped) == .9


def test_rotated_ocr_boxes_return_to_normalized_image_coordinates():
    assert raster.unrotate_bbox([10, 20, 20, 40], 90, (100, 50)) == [
        60.0, 10.0, 80.0, 20.0]
    assert raster.unrotate_bbox([10, 20, 20, 40], 270, (100, 50)) == [
        20.0, 30.0, 40.0, 40.0]


def test_aisle_sequence_fills_only_a_unique_regular_run():
    badges = [
        {"bbox": [10 + i * 20, 50, 20 + i * 20, 62],
         "text": str(value) if value else "", "confidence": .9}
        for i, value in enumerate((1, None, 3, None, 5))
    ]

    anchors = raster.reconstruct_aisle_sequence(badges)

    assert list(anchors) == [f"AISLE {i}" for i in range(1, 6)]
    assert anchors["AISLE 2"] == [35.0, 56.0]


def test_aisle_sequence_rejects_ambiguous_values():
    badges = [
        {"bbox": [10 + i * 20, 50, 20 + i * 20, 62],
         "text": text, "confidence": .9}
        for i, text in enumerate(("1", "", "4", "", "5"))
    ]

    with pytest.raises(raster.RasterExtractionError, match="unique aisle run"):
        raster.reconstruct_aisle_sequence(badges)


def test_boundary_discovery_closes_gap_without_emitting_door_icon(tmp_path):
    mask = np.zeros((180, 240), np.uint8)
    cv2.rectangle(mask, (30, 25), (210, 150), 255, 2)
    mask[148:153, 100:140] = 0                  # customer opening
    cv2.circle(mask, (120, 165), 10, 255, 2)   # exterior entrance icon

    boundary = raster.discover_boundary(mask)

    assert len(boundary) >= 4
    assert min(x for x, _ in boundary) <= 32
    assert max(x for x, _ in boundary) >= 208
    assert max(y for _, y in boundary) <= 152


def test_structural_mask_never_walls_off_an_aisle_badge():
    # A solid badge is wide enough to survive the long-line openings, so
    # erasing it only from the pre-restoration planes let it come back as
    # structure and seal the corridor mouth it labels (store 388).
    image = np.full((200, 300, 3), 255, np.uint8)
    cv2.line(image, (20, 40), (280, 40), (40, 40, 40), 3)
    cv2.line(image, (20, 160), (280, 160), (40, 40, 40), 3)
    cv2.ellipse(image, (150, 100), (28, 15), 0, 0, 360, (20, 20, 20), -1)
    badge = {"bbox": [122, 85, 178, 115], "text": "7", "confidence": .9}

    mask = raster._structural_mask(Image.fromarray(image), [], 245, [badge])

    assert not mask[90:110, 130:170].any()
    assert mask[38:43, 100:200].any()


def test_structural_mask_keeps_diagonal_walls():
    image = np.full((120, 120, 3), 255, np.uint8)
    cv2.line(image, (15, 100), (105, 15), (70, 70, 70), 2)

    mask = raster._structural_mask(Image.fromarray(image), [], 245)

    assert mask[58:63, 58:63].any()


def test_tesseract_timeout_becomes_actionable_raster_error(monkeypatch):
    monkeypatch.setattr(raster.shutil, "which", lambda _: "/usr/bin/tesseract")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("tesseract", 120)

    monkeypatch.setattr(raster.subprocess, "run", timeout)

    with pytest.raises(raster.RasterExtractionError, match="timed out"):
        raster._run_tesseract(Image.new("RGB", (20, 20), "white"), 90)


def test_failed_extraction_writes_available_diagnostics(tmp_path, monkeypatch):
    page = fitz.open("guides/guide-cedar-park-265.pdf")[1]
    monkeypatch.setattr(
        raster, "_ocr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            raster.RasterExtractionError("forced OCR failure")))

    with pytest.raises(raster.RasterExtractionError,
                       match=f"artifacts={tmp_path}"):
        raster.extract_page(page, {"rotation": 90}, backend="tesseract",
                            artifact_dir=tmp_path)

    assert (tmp_path / "original.png").exists()
    assert (tmp_path / "failure.json").exists()


def test_store_265_raster_config_is_calibration_not_manual_geometry():
    config = json.load(open("data/265/raster.json"))

    assert config == {"rotation": 90, "threshold": 245}


def test_raster_coverage_words_use_stored_positioned_ocr():
    class RasterPageMustNotBeRead:
        def get_text(self, *_args):
            raise AssertionError("raster coverage must use stored OCR")

    geom = {"source_kind": "raster", "ocr_words": [
        {"bbox": [1, 2, 3, 4], "text": "Coffee"},
        {"bbox": [5, 6, 7, 8], "text": "Tea"},
        {"bbox": [9, 10, 11, 12], "text": "Produce",
         "region": "red-label"},
    ]}

    assert raster.coverage_words(RasterPageMustNotBeRead(), geom) == [
        (1, 2, 3, 4, "Coffee", 0, 0, 0),
        (5, 6, 7, 8, "Tea", 1, 0, 1),
    ]


@pytest.mark.parametrize("store", ["24", "659", "388", "790"])
def test_clean_vector_rasterization_retains_structural_parity(store):
    """OCR-free canary: rasterizing a vector guide must not LOSE structure.

    Called without word or badge boxes, nothing erases printed text, so the
    mask keeps glyphs as structure and its precision is meaningfully lower
    than the production path's - only a floor worth asserting here.  Recall
    is the property this canary can hold to account, and the full OCR-fed
    precision/recall gates (>=.93/.93 clean) are enforced by
    raster_benchmark.py against the image-only PDF corpus.
    """
    image = render(store)
    expected = truth_mask(store, image.size)

    found = raster._structural_mask(image, [], 245) > 0
    precision, recall = pixel_metrics(expected, found)

    assert recall >= .95
    assert precision >= .85


@pytest.mark.parametrize("backend", ["vision", "tesseract"])
def test_store_265_ocr_backend_mechanical_contract(tmp_path, backend):
    if backend == "vision" and (
            sys.platform != "darwin" or importlib.util.find_spec("objc") is None):
        pytest.skip("Apple Vision bindings are unavailable")
    if backend == "tesseract" and not shutil.which("tesseract"):
        pytest.skip("Tesseract CLI is not installed")
    page = fitz.open("guides/guide-cedar-park-265.pdf")[1]
    config = json.load(open("data/265/raster.json"))

    geom = raster.extract_page(page, config, backend=backend,
                               artifact_dir=tmp_path)

    assert geom["source_kind"] == "raster"
    assert geom["rotation"] == 90
    assert sorted(int(k.split()[1]) for k in geom["anchors"]
                  if k.startswith("AISLE ")) == list(range(1, 26))
    for name in ("ENTRANCE", "EXIT", "CHECKSTANDS", "PRODUCE", "BAKERY",
                 "DELI", "SEAFOOD", "MARKET", "DAIRY", "TORTILLERIA",
                 "SUSHIYA", "FLORAL", "PHARMACY", "RESTROOMS",
                 "BUSINESS CENTER"):
        assert name in geom["anchors"]
    assert len(geom["boundary"]) >= 8
    assert len(geom["fixtures"]) + len(geom["fixture_polys"]) >= 75
    assert len(geom["obstacle_paths"]) >= 100
    assert len(geom["ocr_words"]) >= 150

    raw = engine.build_grid(geom)
    seed = engine.nearest_free(raw, geom["anchors"]["ENTRANCE"])
    reach, _ = engine.bfs(raw, seed)
    height, width = raw.shape
    for name, point in geom["anchors"].items():
        if name.startswith("AISLE "):
            x, y = int(point[0] // engine.CELL), int(point[1] // engine.CELL)
            assert raw[y, x] and reach[y * width + x] >= 0, name

    # Deterministic 35-label spot check spanning the left, center, and right
    # wings. Each OCR position must remain beside an extracted fixture.
    labels = {
        re.sub("[^A-Z]", "", word["text"].upper()): word
        for word in geom["ocr_words"]}
    expected = ("CHARCOAL MIXERS TEA COFFEE CANDY CHIPS COOKIES CRACKERS "
                "PEANUT BUTTER VINEGAR DRESSING BEANS SOUP PASTA GRAVY "
                "TORTILLAS PICKLES CONDIMENTS SNACKS CEREAL FLOUR SUGAR "
                "NAPKINS TISSUE BAGS STARCH AUTOMOTIVE DIAPERS COSMETICS "
                "FRAGRANCES DEODORANT GROOMING INCONTINENCE VITAMINS").split()
    boxes = list(geom["fixtures"])
    boxes += [[min(x for x, _ in poly), min(y for _, y in poly),
               max(x for x, _ in poly), max(y for _, y in poly)]
              for poly in geom["fixture_polys"]]
    boxes += [[min(a[0], b[0]), min(a[1], b[1]),
               max(a[0], b[0]), max(a[1], b[1])]
              for a, b in geom["obstacle_paths"]]
    for text in expected:
        assert text in labels
        box = labels[text]["bbox"]
        x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        distance = min(math.hypot(max(left - x, 0, x - right),
                                  max(top - y, 0, y - bottom))
                       for left, top, right, bottom in boxes)
        assert distance <= 16, (text, distance)


def test_store_265_auto_backend_retries_with_tesseract(
        tmp_path, monkeypatch):
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract CLI is not installed")
    page = fitz.open("guides/guide-cedar-park-265.pdf")[1]
    config = json.load(open("data/265/raster.json"))
    monkeypatch.setattr(
        raster, "_vision_words",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            raster.RasterExtractionError("Vision unavailable")))

    geom = raster.extract_page(page, config, backend="auto",
                               artifact_dir=tmp_path)

    assert geom["ocr_backend"] == "tesseract"
