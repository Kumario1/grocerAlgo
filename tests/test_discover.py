from pathlib import Path

import pytest

import discover
from discover import city_slug, preflight, validate


def checks_for(path, store):
    """preflight over a repo-root guide, skipped when it isn't checked in."""
    if not Path(path).exists():
        pytest.skip(f"{path} is not in the repo root")
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(path)
    return preflight(doc, doc[1], store)


def test_city_names_become_cdn_slugs():
    assert city_slug("  Cedar   Park ") == "cedar-park"
    assert city_slug("San-Antonio") == "san-antonio"


def test_full_page_scanned_store_guide_is_valid_input():
    assert validate("guide-cedar-park-265.pdf", "265")[0] is None


def test_guide_drawn_before_a_remodel_is_flagged():
    flagged = checks_for("guide-austin-388.pdf", "388")
    assert flagged["guide_year"] == 2011
    assert flagged["stale_risk"] is True


def test_current_guide_carries_no_stale_flags():
    assert checks_for("guide-plano-790.pdf", "790") == {
        "guide_year": 2022, "flags": [], "stale_risk": False}


def test_explicit_city_never_reuses_another_citys_local_guide(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("guide-austin-265.pdf").touch()
    monkeypatch.setattr(discover, "probe", lambda url: "cedar-park" in url)
    monkeypatch.setattr(discover, "validate", lambda path, store: (None, {}))
    monkeypatch.setattr(
        discover.urllib.request, "urlretrieve",
        lambda _url, path: Path(path).write_bytes(b"pdf"))

    found = discover.discover("265", "Cedar Park")

    assert found == "guide-cedar-park-265.pdf"
    assert Path(found).exists()
    assert (Path("data/265/source.json").read_text()
            == '{"pdf": "guide-cedar-park-265.pdf"}')
