import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from router import engine
from router.heb import (
    HEBClient,
    HEBConnectionError,
    extract_atlas_svg,
    extract_products,
    parse_atlas,
    resolve_placement,
)


def search_html(*products):
    data = {"props": {"pageProps": {"searchGridV2": {"items": list(products)}}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script>"
    )


def test_search_returns_real_store_product():
    html = search_html({
        "id": "1657904",
        "storeId": 659,
        "fullDisplayName": "Fresh Sweet Cob Corn - Texas-Size Pack, 8 ct",
        "brand": {"name": "Fresh"},
        "SKUs": [{"customerFriendlySize": "8 ct"}],
        "productImageUrls": [{"size": "SMALL", "url": "https://img/corn.jpg"}],
        "inventory": {"inventoryState": "IN_STOCK"},
        "productLocation": {"location": "In Produce on the Front Wall"},
    })

    assert extract_products(html, store_id=659) == [{
        "id": "1657904",
        "name": "Fresh Sweet Cob Corn - Texas-Size Pack, 8 ct",
        "brand": "Fresh",
        "size": "8 ct",
        "image_url": "https://img/corn.jpg",
        "inventory_state": "IN_STOCK",
        "location_label": "In Produce on the Front Wall",
        "selectable": True,
    }]


def test_search_ranking_ignores_merchandising_product_lists():
    ranked = {
        "id": "1",
        "storeId": 659,
        "displayName": "Ranked Milk",
        "inventory": {"inventoryState": "IN_STOCK"},
        "productLocation": {"location": "In Dairy"},
    }
    promoted = {**ranked, "id": "2", "displayName": "Promoted Milk"}
    data = {"props": {"pageProps": {
        "recommendations": {"items": [promoted]},
        "searchGridV2": {"items": [ranked]},
    }}}
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script>")

    assert [product["id"] for product in extract_products(html)] == ["1"]


def test_search_keeps_out_of_stock_product_disabled_and_filters_other_store():
    product = {
        "id": "1",
        "storeId": 659,
        "displayName": "H-E-B Whole Milk",
        "brand": {"name": "H-E-B"},
        "SKUs": [{"customerFriendlySize": "1 gal"}],
        "productImageUrls": [],
        "inventory": {"inventoryState": "OUT_OF_STOCK"},
        "productLocation": {"location": "In Dairy on the Back Wall"},
    }
    other_store = {**product, "id": "2", "storeId": 92}
    no_location = {
        **product,
        "id": "3",
        "inventory": {"inventoryState": "IN_STOCK"},
        "productLocation": None,
    }

    products = extract_products(
        search_html(product, other_store, no_location), store_id=659)

    assert [p["id"] for p in products] == ["1", "3"]
    assert products[0]["selectable"] is False
    assert products[1]["location_label"] is None
    assert products[1]["selectable"] is True


def test_search_rejects_challenge_and_malformed_pages():
    with pytest.raises(ValueError, match="connection required"):
        extract_products('<script src="/_Incapsula_Resource"></script>')
    with pytest.raises(ValueError, match="invalid H-E-B search response"):
        extract_products('<script id="__NEXT_DATA__">{nope}</script>')


def test_heb_client_caches_ranked_queries_for_five_minutes():
    client = HEBClient()
    client.map_ready = True
    calls = []
    product = {
        "id": "1",
        "storeId": 659,
        "displayName": "H-E-B Whole Milk",
        "SKUs": [{"customerFriendlySize": "1 gal"}],
        "inventory": {"inventoryState": "IN_STOCK"},
        "productLocation": {"location": "In Dairy"},
    }

    async def fake_fetch(url):
        calls.append(url)
        return search_html(product)

    client._fetch = fake_fetch

    first = asyncio.run(client.search(" Whole Milk "))
    second = asyncio.run(client.search("whole   milk"))

    assert first == second
    assert calls == ["/search?q=whole%20milk"]


def test_heb_client_fails_closed_when_atlas_structure_changes():
    client = HEBClient()
    product = {
        "id": "1",
        "storeId": 659,
        "displayName": "H-E-B Whole Milk",
        "SKUs": [{"customerFriendlySize": "1 gal"}],
        "inventory": {"inventoryState": "IN_STOCK"},
        "productLocation": {"location": "In Dairy"},
    }

    async def fake_fetch(url):
        if url.startswith("/search"):
            return search_html(product)
        return '<svg viewBox="0 0 10 10"></svg>'

    client._fetch = fake_fetch

    with pytest.raises(HEBConnectionError, match="map changed"):
        asyncio.run(client.confirm())
    assert client.status() == {
        "connected": True,
        "map_ready": False,
        "store_id": 659,
    }


def test_heb_client_refreshes_a_challenged_existing_browser():
    client = HEBClient()
    navigated = []

    class Page:
        def is_closed(self):
            return False

        async def goto(self, url, wait_until):
            navigated.append((url, wait_until))

    client._context = object()
    client._page = Page()

    assert asyncio.run(client.connect())["connected"] is False
    assert navigated == [("https://www.heb.com/", "domcontentloaded")]


def test_heb_client_launches_normal_chrome_instead_of_automation_mode():
    command = HEBClient()._chrome_command(9223)

    assert command[0].endswith(
        "Google Chrome.app/Contents/MacOS/Google Chrome")
    assert "--remote-debugging-port=9223" in command
    assert not any("enable-automation" in arg for arg in command)


def test_heb_client_reports_incapsula_error_15_as_reconnect_required(
        monkeypatch):
    client = HEBClient()
    client.connected = client.map_ready = True

    class Response:
        async def text(self):
            return '{"errorCode" : "15"}'

    class Page:
        async def goto(self, url, wait_until):
            return Response()

    async def no_wait(_):
        pass

    monkeypatch.setattr("router.heb.asyncio.sleep", no_wait)
    client._page = Page()

    with pytest.raises(HEBConnectionError, match="reconnect required"):
        asyncio.run(client._fetch("/search?q=milk"))
    assert client.status()["connected"] is False


def test_heb_client_navigates_instead_of_using_blocked_page_fetch():
    client = HEBClient()
    navigated = []

    class Response:
        async def text(self):
            return "normal H-E-B response"

    class Page:
        async def goto(self, url, wait_until):
            navigated.append((url, wait_until))
            return Response()

        async def evaluate(self, script, url):
            return '{"errorCode" : "15"}'

    client._page = Page()

    assert asyncio.run(client._fetch("/search?q=milk")) == (
        "normal H-E-B response")
    assert navigated == [(
        "https://www.heb.com/search?q=milk", "domcontentloaded")]


def test_heb_client_retries_a_transient_incapsula_page(monkeypatch):
    client = HEBClient()
    replies = iter(['{"errorCode" : "15"}', "normal H-E-B response"])
    navigated = []

    class Response:
        def __init__(self, text):
            self.body = text

        async def text(self):
            return self.body

    class Page:
        async def goto(self, url, wait_until):
            navigated.append(url)
            return Response(next(replies))

    async def no_wait(_):
        pass

    monkeypatch.setattr("router.heb.asyncio.sleep", no_wait)
    client._page = Page()

    assert asyncio.run(client._fetch("/search?q=milk")) == (
        "normal H-E-B response")
    assert len(navigated) == 2


def test_atlas_map_becomes_router_geometry_and_psa_index():
    svg = """
    <svg id="store-map" viewBox="10,20,100,80">
      <polygon class="combined-fixture" points="20,30 40,30 40,40 20,40"/>
      <text x="50" y="40" class="aisle-label" aisle="2">2</text>
      <polygon class="landmarker produce area" landmark-name="produce"
               approx-center-x="80" approx-center-y="50"
               points="75,45 85,45 85,55 75,55"/>
      <text x="75" y="45" class="area-label">PRODUCE</text>
      <polygon class="landmarker frontdoor" landmark-name="frontdoor"
               approx-center-x="60" approx-center-y="90" points="55,85 65,85 65,95"/>
      <polygon class="landmarker checkstands area" landmark-name="checkstands"
               approx-center-x="70" approx-center-y="80" points="65,75 75,75 75,85"/>
      <text x="35" y="32" class="psa" area="03" aisle="2" side="A" section="14"/>
    </svg>
    """

    atlas = parse_atlas(svg, scale=0.5)

    assert atlas["geometry"]["page"] == {"w": 50.0, "h": 40.0}
    assert atlas["geometry"]["fixtures"] == [[5.0, 5.0, 15.0, 10.0]]
    assert atlas["geometry"]["anchors"]["AISLE 2"] == [20.0, 10.0]
    assert atlas["geometry"]["anchors"]["PRODUCE"] == [35.0, 15.0]
    assert atlas["geometry"]["anchors"]["ENTRANCE"] == [25.0, 35.0]
    assert atlas["geometry"]["anchors"]["CHECKSTANDS"] == [30.0, 30.0]
    assert atlas["psas"]["03|2|A|14"] == [12.5, 6.0]


def test_product_uses_exact_pals_shelf_position():
    atlas = {
        "psas": {"03|2|A|14": [12.5, 6.0]},
        "geometry": {"anchors": {"AISLE 2": [20.0, 10.0]}},
    }
    pals = {"results": [{
        "psas": [{"area": "03", "aisle": "2", "side": "A", "section": "14"}],
        "approximateLocation": None,
        "subDepartmentName": "Pantry",
    }]}

    assert resolve_placement(pals, atlas, "Aisle 2") == {
        "point": [12.5, 6.0],
        "psa_key": "03|2|A|14",
        "group": "PSA:03:2",
        "approx": False,
        "location_label": "Aisle 2",
    }


def test_product_uses_first_pals_psa_present_in_the_atlas():
    atlas = {
        "psas": {"03|2|A|14": [12.5, 6.0]},
        "geometry": {"anchors": {"AISLE 2": [20.0, 10.0]}},
    }
    pals = {"results": [{
        "psas": [
            {"area": "03", "aisle": "2", "side": "A", "section": "13"},
            {"area": "03", "aisle": "2", "side": "A", "section": "14"},
        ],
    }]}

    assert resolve_placement(pals, atlas, "Aisle 2")["point"] == [12.5, 6.0]


def test_product_prefers_primary_pals_placement_over_alternate():
    atlas = {
        "psas": {
            "35|6|A|75": [30.0, 30.0],
            "01|13|A|11": [12.5, 6.0],
        },
        "geometry": {"anchors": {}},
    }
    pals = {"results": [{
        "psas": [
            {"area": "35", "aisle": 6, "side": "A", "section": 75,
             "type": 2},
            {"area": "01", "aisle": 13, "side": "A", "section": 11,
             "type": 1},
        ],
    }]}

    placement = resolve_placement(pals, atlas, "Aisle 13")
    assert placement["point"] == [12.5, 6.0]
    assert placement["group"] == "PSA:01:13"


def test_product_pins_at_the_shelf_its_label_names_not_the_display():
    # Store 811's tortillas: PAL lists the Tortilleria display first by type,
    # but the shopper-visible label says Aisle 4 — the shelf must win.
    atlas = {
        "psas": {
            "16|88|A|3": [397.0, 735.0],
            "01|4|B|10": [120.0, 60.0],
        },
        "geometry": {"anchors": {}},
    }
    pals = {"results": [{
        "psas": [
            {"area": "16", "aisle": 88, "side": "A", "section": 3, "type": 1},
            {"area": "01", "aisle": 4, "side": "B", "section": 10, "type": 2},
        ],
    }]}

    placement = resolve_placement(pals, atlas, "Aisle 4")
    assert placement["point"] == [120.0, 60.0]
    assert placement["group"] == "PSA:01:4"


def test_product_location_falls_back_without_silent_guessing():
    atlas = {
        "psas": {"03|2|A|14": [12.5, 6.0]},
        "geometry": {"anchors": {
            "AISLE 2": [20.0, 10.0],
            "PRODUCE": [32.5, 12.5],
            "DAIRY": [10.0, 10.0],
            "BEER & WINE": [12.0, 10.0],
            "CHECKSTANDS": [14.0, 10.0],
        }},
    }
    approximate = {"results": [{
        "psas": [],
        "approximateLocation": {
            "area": "03", "aisle": "2", "side": "A", "section": "14",
        },
        "subDepartmentName": None,
    }]}
    department_only = {"results": [{
        "psas": [],
        "approximateLocation": None,
        "subDepartmentName": "Produce-Fresh",
    }]}

    assert resolve_placement(approximate, atlas, "Aisle 2")["approx"] is True
    assert resolve_placement({}, atlas, "Aisle 2")["group"] == "ANCHOR:AISLE 2"
    assert resolve_placement(department_only, atlas, None)["group"] == "ANCHOR:PRODUCE"
    assert resolve_placement(
        {"results": [{"subDepartmentName": "Dairy - Milk"}]},
        atlas, "Aisle 2")["group"] == "ANCHOR:AISLE 2"
    assert resolve_placement(
        {}, atlas, "In Beer and Wine")["group"] == "ANCHOR:BEER & WINE"
    assert resolve_placement(
        {}, atlas, "At Checkstands")["group"] == "ANCHOR:CHECKSTANDS"
    assert resolve_placement({}, atlas, "Somewhere mysterious") is None


def test_saved_atlas_snapshot_is_the_current_659_layout():
    # The snapshot used to be a whole saved product page — 9.5 MB of Next.js
    # around one <svg id="store-map">. Only the map is evidence, so only the
    # map is kept, and extract_atlas_svg still reads it the same way.
    atlas = parse_atlas(extract_atlas_svg(
        Path("data/659-atlas/store-map.svg").read_text()))
    source = json.loads(Path("data/659-atlas/source.json").read_text())
    profile = np.load("data/659-atlas/profile.npz", allow_pickle=True)

    aisles = [a for a in atlas["geometry"]["anchors"] if a.startswith("AISLE ")]
    assert len(aisles) == 41
    assert (len(atlas["geometry"]["fixtures"])
            + len(atlas["geometry"]["fixture_polys"])) == 494
    assert {"ENTRANCE", "CHECKSTANDS", "PRODUCE"} <= set(
        atlas["geometry"]["anchors"])
    assert len(atlas["psas"]) > 2_600
    assert atlas["digest"] == source["sha256"]
    assert str(profile["source_sha256"]) == source["sha256"]


def test_atlas_profile_has_reachable_terminals_and_product_snaps():
    geometry = json.loads(Path("data/659-atlas/geometry.json").read_text())
    psas = json.loads(Path("data/659-atlas/psas.json").read_text())
    exclusions = json.loads(
        Path("data/659-atlas/exclusions.json").read_text())
    profile = np.load("data/659-atlas/profile.npz", allow_pickle=True)
    names = [str(name) for name in profile["names"]]
    cells = profile["cells"]
    free = profile["free"]
    cell = float(profile["cell"])
    entrance = tuple(int(value) for value in cells[names.index("ENTRANCE")])
    checkout = tuple(int(value) for value in cells[names.index("CHECKOUT")])
    reach, _ = engine.bfs(free, entrance)
    furniture = engine.build_grid({
        "page": geometry["page"],
        "boundary": None,
        "fixtures": geometry["fixtures"],
        "fixture_polys": geometry["fixture_polys"],
        "obstacle_paths": [],
    }, cell)

    assert len([name for name in geometry["anchors"]
                if name.startswith("AISLE ")]) == 41
    assert reach[checkout[1] * free.shape[1] + checkout[0]] >= 0
    for exclusion in exclusions:
        x0, y0, x1, y1 = exclusion["rect"]
        center = (int((x0 + x1) / 2 // cell), int((y0 + y1) / 2 // cell))
        assert not free[center[1], center[0]], exclusion["name"]
    current = entrance
    for point in list(psas.values())[::500]:
        snapped = engine.snap(free, reach, point, cell)
        assert reach[snapped[1] * free.shape[1] + snapped[0]] >= 0
        _, parents = engine.bfs(free, current)
        path = engine.string_pull(
            free, engine.trace(parents, free.shape[1], snapped, cell), cell)
        assert engine.path_is_legal(free, path, cell) == []
        assert engine.path_is_legal(furniture, path, cell) == []
        current = snapped


def test_concurrent_fetches_do_not_overlap_on_the_single_page():
    """Typeahead fires overlapping searches; a second goto() on the same page
    aborts the first, which used to surface as 'H-E-B reconnect required'."""
    client = HEBClient()
    client.connected = client.map_ready = True
    overlapping = []

    class Response:
        async def text(self):
            return "normal H-E-B response"

    class Page:
        in_flight = 0

        async def goto(self, url, wait_until):
            Page.in_flight += 1
            overlapping.append(Page.in_flight)
            await asyncio.sleep(0)
            Page.in_flight -= 1
            return Response()

    client._page = Page()

    async def race():
        return await asyncio.gather(*(
            client._fetch(f"/search?q=chicke{'n' * i}") for i in range(6)))

    assert len(asyncio.run(race())) == 6
    assert max(overlapping) == 1, f"navigations overlapped: {overlapping}"
