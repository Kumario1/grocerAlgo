import pytest
import fitz
from PIL import Image

import extract
from extract import (
    load_boundary_override,
    raster_experiment_enabled,
    stitch_open_boundary,
)
from router.raster import is_raster_page


def test_raster_fallback_only_claims_image_only_maps():
    assert is_raster_page(fitz.open("guides/guide-cedar-park-265.pdf")[1])
    assert not is_raster_page(fitz.open("guides/guide-austin-659.pdf")[1])


@pytest.mark.parametrize("missing", ({15}, {13, 14}),
                         ids=("store-54", "store-68"))
def test_vector_extraction_allows_real_aisle_number_gaps(monkeypatch, missing):
    class ReachedDrawingExtraction(Exception):
        pass

    class Page:
        rect = fitz.Rect(0, 0, 100, 100)

        def get_text(self, kind):
            assert kind == "words"
            return [(n, n, n + 1, n + 1, str(n), 0, 0, 0)
                    for n in range(1, 38) if n not in missing]

        def get_drawings(self):
            raise ReachedDrawingExtraction

    monkeypatch.setattr(extract, "PDF", "unused")
    monkeypatch.setattr(extract.fitz, "open",
                        lambda _path: [None, Page()])
    monkeypatch.setattr(extract.raster, "is_raster_page",
                        lambda _page: False)

    with pytest.raises(ReachedDrawingExtraction):
        extract.extract()


def test_aisle_badges_exclude_curbside_slot_numbers_outside_floor():
    badges = {
        (n, 20): (n, n, 20, n + 1, 21) for n in range(1, 40)
    }
    badges.update({
        (110 + n, 20): (n, 110 + n, 20, 111 + n, 21)
        for n in range(1, 11)
    })

    anchors, aisles = extract.aisle_anchors_inside_boundary(
        badges, [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
        140, 100)

    assert aisles == list(range(1, 40))
    assert set(anchors) == {f"AISLE {n}" for n in range(1, 40)}


def test_raster_fallback_rejects_small_page_decoration(tmp_path):
    icon = tmp_path / "icon.png"
    Image.new("RGB", (20, 20), "black").save(icon)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(10, 10, 40, 40), filename=str(icon))

    assert not is_raster_page(page)


def test_raster_production_path_remains_disabled_until_benchmark_passes(
        monkeypatch):
    monkeypatch.delenv("GROCER_RASTER_EXPERIMENTAL", raising=False)
    assert not raster_experiment_enabled()

    monkeypatch.setenv("GROCER_RASTER_EXPERIMENTAL", "1")
    assert raster_experiment_enabled()


def test_stitches_open_perimeter_chains_at_wall_junction():
    chains = [
        [[40, 10], [10, 10], [10, 70], [80, 70], [80, 90]],
        [[120, 10], [120, 90], [20, 90]],
    ]

    assert stitch_open_boundary(chains, 120, 100) == [
        [40, 10], [10, 10], [10, 70], [80, 70], [80, 90],
        [120, 90], [120, 10], [40, 10],
    ]


def test_boundary_override_is_named_closed_and_store_sized(tmp_path):
    path = tmp_path / "boundary.json"
    path.write_text('{"name":"printed sales-floor perimeter",'
                    '"poly":[[10,10],[110,10],[110,90],[10,90]]}')

    assert load_boundary_override(path, 120, 100) == [
        [10.0, 10.0], [110.0, 10.0], [110.0, 90.0], [10.0, 90.0],
        [10.0, 10.0],
    ]
