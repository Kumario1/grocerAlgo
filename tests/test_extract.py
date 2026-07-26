import fitz
from PIL import Image

from extract import raster_experiment_enabled, stitch_open_boundary
from router.raster import is_raster_page


def test_raster_fallback_only_claims_image_only_maps():
    assert is_raster_page(fitz.open("guides/guide-cedar-park-265.pdf")[1])
    assert not is_raster_page(fitz.open("guides/guide-austin-659.pdf")[1])


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
