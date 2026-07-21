from router.directory import parse_aisle_raw, load_directory

def test_single_number():
    assert parse_aisle_raw("31") == ["AISLE 31"]

def test_range():
    assert parse_aisle_raw("33 - 35") == ["AISLE 33", "AISLE 34", "AISLE 35"]

def test_ampersand_and_comma_lists():
    assert parse_aisle_raw("1 & 2") == ["AISLE 1", "AISLE 2"]
    assert parse_aisle_raw("19, 28") == ["AISLE 19", "AISLE 28"]

def test_named_zone():
    assert parse_aisle_raw("Checkstands") == ["CHECKSTANDS"]

def test_zone_plus_number():
    assert parse_aisle_raw("Left Wall, 37") == ["LEFT WALL", "AISLE 37"]
    assert parse_aisle_raw("Checkstands, 19") == ["CHECKSTANDS", "AISLE 19"]

def test_load_directory_filters_unknown_anchors(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("item,aisle_raw\nIce,Checkstands\nWine,1 & 2\nWeird,Moon Base\n")
    d = load_directory(str(csv), {"CHECKSTANDS", "AISLE 1", "AISLE 2"})
    assert d["Ice"] == ["CHECKSTANDS"]
    assert d["Wine"] == ["AISLE 1", "AISLE 2"]
    assert "Weird" not in d                     # no known anchor -> excluded
