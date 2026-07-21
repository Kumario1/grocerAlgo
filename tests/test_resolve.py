from router.resolve import normalize, resolve

DIR = {"Milk": ["AISLE 40"], "Tortilla Chips": ["AISLE 7"],
       "Toothpicks": ["AISLE 19", "AISLE 28"], "Pet Supplies": ["AISLE 33"]}

def test_normalize_strips_quantities():
    assert normalize("2x milk 2%") == "milk 2%"
    assert normalize("3 lbs bananas") == "bananas"
    assert normalize("milk") == "milk"

def test_exact_and_fuzzy_match():
    matched, unmatched = resolve(["milk", "tortila chips"], DIR)
    assert [m["entry"] for m in matched] == ["Milk", "Tortilla Chips"]
    assert unmatched == []

def test_multi_anchor_entry_passes_all_anchors():
    matched, _ = resolve(["toothpicks"], DIR)
    assert matched[0]["anchors"] == ["AISLE 19", "AISLE 28"]

def test_unmatched_gets_top3_suggestions_never_dropped():
    matched, unmatched = resolve(["flux capacitor"], DIR)
    assert matched == []
    assert unmatched[0]["query"] == "flux capacitor"
    assert 1 <= len(unmatched[0]["suggestions"]) <= 3

def test_case_insensitive_short_words():
    # without a lowercasing processor, ratio("rice","Rice") = 75 < threshold
    matched, unmatched = resolve(["rice"], {"Rice": ["AISLE 22"]})
    assert matched and matched[0]["entry"] == "Rice"
