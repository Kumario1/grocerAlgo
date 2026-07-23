"""H-E-B #659 catalog and Atlas-map helpers."""
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree


class _NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "__NEXT_DATA__":
            self.capture = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def _search_grids(value):
    """Yield ranked H-E-B search item arrays, excluding merchandising lists."""
    if isinstance(value, dict):
        kind = str(value.get("type") or value.get("__typename") or "").lower()
        if kind == "searchgridv2" and isinstance(value.get("items"), list):
            yield value["items"]
        direct = value.get("searchGridV2")
        if isinstance(direct, dict) and isinstance(direct.get("items"), list):
            yield direct["items"]
        for key, child in value.items():
            if key != "searchGridV2":
                yield from _search_grids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _search_grids(child)


def _normalize_label(text):
    text = text.upper().replace("&", " AND ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_products(html, store_id=659):
    """Return the store-scoped product cards embedded in H-E-B Next.js HTML."""
    parser = _NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError("H-E-B connection required")
    try:
        data = json.loads("".join(parser.parts))
    except json.JSONDecodeError as e:
        raise ValueError("invalid H-E-B search response") from e
    grid = next(_search_grids(data), None)
    if grid is None:
        raise ValueError("invalid H-E-B search response")
    out, seen = [], set()
    for product in grid:
        if not isinstance(product, dict):
            continue
        pid = str(product.get("id", ""))
        name = product.get("fullDisplayName") or product.get("displayName")
        if (not pid or pid in seen
                or str(product.get("storeId")) != str(store_id)
                or not name):
            continue
        seen.add(pid)
        skus = product.get("SKUs") or [{}]
        images = product.get("productImageUrls") or []
        image = next((i.get("url") for i in images if i.get("size") == "SMALL"),
                     images[0].get("url") if images else None)
        inventory = (product.get("inventory") or {}).get("inventoryState")
        brand = product.get("brand")
        out.append({
            "id": pid,
            "name": name,
            "brand": brand.get("name") if isinstance(brand, dict) else brand,
            "size": skus[0].get("customerFriendlySize"),
            "image_url": image,
            "inventory_state": inventory,
            "location_label": (
                product.get("productLocation") or {}).get("location"),
            "selectable": inventory == "IN_STOCK",
        })
    return out


def extract_atlas_svg(html):
    """Extract the H-E-B store-map SVG from a rendered product page."""
    start = html.find('<svg id="store-map"')
    if start < 0:
        raise ValueError("H-E-B store map unavailable")
    end = html.find("</svg>", start)
    if end < 0:
        raise ValueError("invalid H-E-B store map")
    return html[start:end + len("</svg>")]


def parse_atlas(svg, scale=0.18, boundary=None):
    """Convert an H-E-B Atlas SVG into the router's geometry + PSA lookup."""
    root = ElementTree.fromstring(svg)
    x0, y0, width, height = map(
        float, re.split(r"[,\s]+", root.attrib["viewBox"].strip()))

    def point(x, y):
        return [round((float(x) - x0) * scale, 6),
                round((float(y) - y0) * scale, 6)]

    fixtures, fixture_polys, anchors, psas = [], [], {}, {}
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        classes = set(node.attrib.get("class", "").split())
        if tag == "polygon" and "combined-fixture" in classes:
            poly = [
                point(*raw.split(",")) for raw in node.attrib["points"].split()
            ]
            xs, ys = {p[0] for p in poly}, {p[1] for p in poly}
            if len(xs) == 2 and len(ys) == 2 and len(poly) in {4, 5}:
                fixtures.append([min(xs), min(ys), max(xs), max(ys)])
            else:
                fixture_polys.append(poly)
        elif tag == "text" and "aisle-label" in classes:
            anchors[f"AISLE {int(node.attrib['aisle'])}"] = point(
                node.attrib["x"], node.attrib["y"])
        elif tag == "text" and "area-label" in classes and (node.text or "").strip():
            anchors.setdefault((node.text or "").strip().upper(), point(
                node.attrib["x"], node.attrib["y"]))
        elif ("landmarker" in classes and node.attrib.get("landmark-name")
              and node.attrib.get("approx-center-x")
              and node.attrib.get("approx-center-y")):
            landmark = node.attrib["landmark-name"]
            name = {
                "frontdoor": "ENTRANCE",
                "checkstands": "CHECKSTANDS",
                "beer-and-wine": "BEER & WINE",
            }.get(landmark, landmark.replace("-", " ").upper())
            anchors[name] = point(node.attrib["approx-center-x"],
                                  node.attrib["approx-center-y"])
        elif tag == "text" and "psa" in classes:
            parts = [node.attrib.get(k) for k in ("area", "aisle", "side", "section")]
            if all(parts):
                psas["|".join(parts)] = point(node.attrib["x"], node.attrib["y"])

    scaled_boundary = None
    if boundary:
        scaled_boundary = [point(x, y) for x, y in boundary]
    geometry = {
        "page": {"w": width * scale, "h": height * scale},
        "boundary": scaled_boundary,
        "fixtures": fixtures,
        "fixture_polys": fixture_polys,
        "obstacle_paths": [],
        "anchors": anchors,
    }
    structure = {
        "page": geometry["page"],
        "fixtures": fixtures,
        "fixture_polys": fixture_polys,
        "anchors": anchors,
        "psas": psas,
    }
    digest = hashlib.sha256(json.dumps(
        structure, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"geometry": geometry, "psas": psas, "digest": digest}


def resolve_placement(pals, atlas, location_label=None):
    """Resolve H-E-B placement metadata to an Atlas point."""
    results = pals.get("results") or []
    result = results[0] if results else {}
    psas = result.get("psas") or []
    candidates = [(psa, False) for psa in psas]
    if result.get("approximateLocation"):
        candidates.append((result["approximateLocation"], True))
    for psa, approximate in candidates:
        key = "|".join(str(psa.get(k, "")) for k in
                       ("area", "aisle", "side", "section"))
        point = atlas["psas"].get(key)
        if point:
            return {
                "point": point,
                "group": f"PSA:{psa['area']}:{psa['aisle']}",
                "approx": approximate,
                "location_label": location_label,
            }

    anchors = atlas["geometry"]["anchors"]
    anchor = None
    if location_label:
        aisle = re.search(r"\baisle\s+(\d+)\b", location_label, re.I)
        if aisle and f"AISLE {int(aisle[1])}" in anchors:
            anchor = f"AISLE {int(aisle[1])}"
        else:
            label = _normalize_label(location_label)
            candidates = [name for name in anchors
                          if not name.startswith("AISLE ")
                          and name != "ENTRANCE"
                          and _normalize_label(name) in label]
            anchor = max(candidates, key=len) if candidates else None
            if not anchor and "CHECKOUT" in label and "CHECKSTANDS" in anchors:
                anchor = "CHECKSTANDS"
    if not anchor:
        department = (
            result.get("subDepartmentName") or "").split("-")[0].strip().upper()
        anchor = department if department in anchors else None
    if anchor:
        return {
            "point": anchors[anchor],
            "group": f"ANCHOR:{anchor}",
            "approx": True,
            "location_label": location_label or result.get("subDepartmentName"),
        }
    return None


class HEBConnectionError(RuntimeError):
    pass


class HEBClient:
    """One persistent local browser session for Lakeline H-E-B #659."""

    def __init__(self, store_id=659, profile_dir=".heb-659",
                 source_path="data/659-atlas/source.json"):
        self.store_id = store_id
        self.profile_dir = profile_dir
        self.expected_digest = json.loads(
            Path(source_path).read_text())["sha256"]
        self.connected = False
        self.map_ready = False
        self._playwright = self._context = self._page = None
        self._search_cache = {}
        self._placement_cache = {}

    def status(self):
        return {
            "connected": self.connected,
            "map_ready": self.map_ready,
            "store_id": self.store_id,
        }

    async def connect(self):
        if self._context:
            try:
                alive = self._page and not self._page.is_closed()
            except Exception:
                alive = False
            if alive:
                if not self.connected:
                    try:
                        await self._page.goto(
                            "https://www.heb.com/",
                            wait_until="domcontentloaded")
                    except Exception:
                        await self.close()
                    else:
                        return self.status()
                else:
                    return self.status()
            else:
                await self.close()
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._context = (
                await self._playwright.chromium.launch_persistent_context(
                    self.profile_dir, channel="chrome", headless=False))
            self._page = self._context.pages[0] if self._context.pages else (
                await self._context.new_page())
            await self._page.goto(
                "https://www.heb.com/", wait_until="domcontentloaded")
        except Exception as e:
            await self.close()
            raise HEBConnectionError(f"Could not open H-E-B: {e}") from e
        return self.status()

    async def _fetch(self, url):
        if not self._page:
            raise HEBConnectionError("Connect H-E-B first")
        try:
            text = await self._page.evaluate(
                """async url => {
                  const response = await fetch(url, {credentials: 'include'});
                  return await response.text();
                }""", url)
        except Exception as e:
            await self.close()
            raise HEBConnectionError("H-E-B reconnect required") from e
        if "_Incapsula_Resource" in text or '"errorCode" : "15"' in text:
            self.connected = self.map_ready = False
            raise HEBConnectionError("H-E-B reconnect required")
        return text

    async def confirm(self):
        html = await self._fetch("/search?q=milk")
        if not extract_products(html, self.store_id):
            raise HEBConnectionError(
                f"Select Lakeline H-E-B #{self.store_id} in the opened browser")
        svg = await self._fetch(
            "/atlas/v1.0/image?locationNumber=659&format=svg&style=none"
            "&label=false&drawPsas=true&drawAisleMarkers=true&landmarks=all"
            "&drawCombinedFixtures=true&drawDepartmentLabels=true"
            "&hidePartnerFixtures=true")
        if "<svg" not in svg:
            raise HEBConnectionError("H-E-B store map unavailable")
        self.connected = True
        self.map_ready = parse_atlas(svg)["digest"] == self.expected_digest
        if not self.map_ready:
            raise HEBConnectionError(
                "H-E-B's #659 map changed; rebuild and verify the Atlas profile")
        return self.status()

    async def search(self, query):
        if not self.map_ready:
            raise HEBConnectionError("Connect and verify H-E-B first")
        key = " ".join(query.lower().split())
        cached = self._search_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        products = extract_products(
            await self._fetch(f"/search?q={quote(key)}"),
            self.store_id)[:8]
        self.connected = True
        self._search_cache[key] = (time.monotonic(), products)
        return products

    async def locate(self, product_id, location_label, atlas):
        if not self.map_ready:
            raise HEBConnectionError("Connect and verify H-E-B first")
        if product_id in self._placement_cache:
            return self._placement_cache[product_id]
        text = await self._fetch(
            f"/pals/v2.0/location/store/{self.store_id}/products/{product_id}")
        try:
            pals = json.loads(text)
        except json.JSONDecodeError as e:
            raise HEBConnectionError("H-E-B product location unavailable") from e
        placement = resolve_placement(pals, atlas, location_label)
        self._placement_cache[product_id] = placement
        return placement

    async def close(self):
        context, playwright = self._context, self._playwright
        self._playwright = self._context = self._page = None
        self.connected = self.map_ready = False
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass
