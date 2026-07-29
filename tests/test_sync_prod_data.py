"""dockerignore whitelist is derived from passing calibrations, not hand lists."""
import json
from pathlib import Path

import pytest

from scripts.sync_prod_data import (
    catalog_ids,
    parse_whitelisted,
    render_dockerignore,
    sync_dockerignore,
)


def _store(root: Path, store: int, *, verdict: str, profile: bool = True):
    data = root / "data" / str(store)
    atlas = root / "data" / f"{store}-atlas"
    data.mkdir(parents=True)
    atlas.mkdir(parents=True)
    if profile:
        (data / "profile.npz").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (atlas / "calibration.json").write_text(
        json.dumps({"verdict": verdict, "gates": {}}))


def test_catalog_ids_require_profile_and_pass(tmp_path):
    _store(tmp_path, 10, verdict="pass")
    _store(tmp_path, 11, verdict="fail")
    _store(tmp_path, 12, verdict="pass", profile=False)

    assert catalog_ids(tmp_path) == [10]


def test_render_lists_map_and_atlas_for_each_id():
    text = render_dockerignore([6, 24])

    assert text.startswith(".git\n")
    assert "data/*\n" in text
    for store in (6, 24):
        assert f"!data/{store}/\n" in text
        assert f"!data/{store}/**\n" in text
        assert f"!data/{store}-atlas/\n" in text
        assert f"!data/{store}-atlas/**\n" in text
    assert parse_whitelisted(text) == {6, 24}


def test_sync_refuses_to_shrink_without_force(tmp_path):
    _store(tmp_path, 6, verdict="pass")
    _store(tmp_path, 24, verdict="pass")
    sync_dockerignore(tmp_path)
    (tmp_path / "data" / "24-atlas" / "calibration.json").write_text(
        json.dumps({"verdict": "fail"}))

    with pytest.raises(SystemExit, match="refusing to drop"):
        sync_dockerignore(tmp_path)

    sync_dockerignore(tmp_path, force=True)
    assert parse_whitelisted((tmp_path / ".dockerignore").read_text()) == {6}
