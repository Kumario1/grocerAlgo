from pathlib import Path

import discover
from discover import city_slug, validate


def test_city_names_become_cdn_slugs():
    assert city_slug("  Cedar   Park ") == "cedar-park"
    assert city_slug("San-Antonio") == "san-antonio"


def test_full_page_scanned_store_guide_is_valid_input():
    assert validate("guide-cedar-park-265.pdf") is None


def test_explicit_city_never_reuses_another_citys_local_guide(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("guide-austin-265.pdf").touch()
    monkeypatch.setattr(discover, "probe", lambda url: "cedar-park" in url)
    monkeypatch.setattr(discover, "validate", lambda path: None)
    monkeypatch.setattr(
        discover.urllib.request, "urlretrieve",
        lambda _url, path: Path(path).write_bytes(b"pdf"))

    found = discover.discover("265", "Cedar Park")

    assert found == "guide-cedar-park-265.pdf"
    assert Path(found).exists()
    assert (Path("data/265/source.json").read_text()
            == '{"pdf": "guide-cedar-park-265.pdf"}')
