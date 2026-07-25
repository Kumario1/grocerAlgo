"""H-E-B catalog and Atlas-map helpers, for whichever store is selected."""
import asyncio
import hashlib
import json
import logging
import re
import socket
import subprocess
import time

log = logging.getLogger("grocer.heb")
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


def write_atlas(directory, store, atlas, page, scale=0.18):
    """Persist a parsed Atlas as the three files the app reads.

    source.json's sha256 is the fail-closed gate: the app refuses to place
    products when the live drawing no longer matches the one its calibration
    was measured against.
    """
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / "geometry.json").write_text(
        json.dumps(atlas["geometry"], indent=1) + "\n")
    (out / "psas.json").write_text(json.dumps(atlas["psas"], indent=1) + "\n")
    (out / "source.json").write_text(json.dumps({
        "kind": "heb-atlas",
        "store": str(store),
        "scale": scale,
        "sha256": atlas["digest"],
        "page": page,
    }, indent=1) + "\n")
    return out


def resolve_placement(pals, atlas, location_label=None):
    """Resolve H-E-B placement metadata to an Atlas point."""
    results = pals.get("results") or []
    result = results[0] if results else {}
    labelled = re.search(r"\baisle\s+(\d+)\b", location_label or "", re.I)

    def rank(psa):
        # A product can be placed twice — its aisle shelf and a department
        # display (store 811's Tortilleria). The shopper's label names the
        # shelf, so a placement that agrees with it outranks PAL type order.
        aisle = str(psa.get("aisle", ""))
        agrees = labelled and aisle.isdigit() and int(aisle) == int(labelled[1])
        return (not agrees, {1: 0, None: 1}.get(psa.get("type"), 2))

    psas = sorted(result.get("psas") or [], key=rank)
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
                "psa_key": key,
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
            "psa_key": None,
            "group": f"ANCHOR:{anchor}",
            "approx": True,
            "location_label": location_label or result.get("subDepartmentName"),
        }
    return None


class HEBConnectionError(RuntimeError):
    pass


class HEBClient:
    """One persistent local browser session, for one store at a time.

    H-E-B's own session carries the selected store, so the app can serve one
    store at a time and no more. Each store gets its own Chrome profile
    directory, which is what lets a switch remember the store selection
    instead of asking for it again.
    """

    def __init__(self, store_id=659, profile_dir=None, source_path=None):
        self.store_id = int(store_id)
        self.profile_dir = profile_dir or f".heb-{self.store_id}"
        self.expected_digest = self._digest(source_path)
        self.connected = False
        self.map_ready = False
        self._playwright = self._browser = self._context = self._page = None
        self._chrome = None
        self._search_cache = {}
        self._placement_cache = {}
        # One page, one navigation at a time. Typeahead fires overlapping
        # /api/products calls, and a second page.goto() aborts the first —
        # which used to read as "H-E-B dropped us" and tear down the browser.
        self._nav = asyncio.Lock()

    def _digest(self, source_path=None):
        """The Atlas snapshot this store was calibrated against, if captured."""
        path = Path(source_path or f"data/{self.store_id}-atlas/source.json")
        try:
            return json.loads(path.read_text())["sha256"]
        except (FileNotFoundError, KeyError):
            return None

    def status(self, store=None):
        # A status poll for another store must never tear this session down;
        # it just reports that store as not connected, which it is.
        if store is not None and int(store) != self.store_id:
            return {"connected": False, "map_ready": False,
                    "store_id": int(store)}
        return {
            "connected": self.connected,
            "map_ready": self.map_ready,
            "store_id": self.store_id,
        }

    async def use(self, store):
        """Point the session at another store, closing the browser it had.

        Switching is deliberate — it is the user picking a different store —
        so it is the one place a live browser is allowed to be discarded.
        """
        store = int(store)
        if store == self.store_id:
            return
        await self.close()
        self.store_id = store
        self.profile_dir = f".heb-{store}"
        self.expected_digest = self._digest()
        # Both caches answer per store: a search is store-scoped and a
        # placement is a shelf in one building.
        self._search_cache.clear()
        self._placement_cache.clear()
        log.info("session switched to store %s", store)

    async def connect(self, store=None):
        if store is not None:
            await self.use(store)
        # Guarded by the same lock as navigation: two 503s racing a reconnect
        # used to launch two Chromes against one profile directory.
        async with self._nav:
            return await self._connect()

    async def _connect(self):
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
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self._chrome = subprocess.Popen(
                self._chrome_command(port),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._playwright = await async_playwright().start()
            last_error = None
            for _ in range(40):
                if self._chrome.poll() is not None:
                    break
                try:
                    self._browser = (
                        await self._playwright.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{port}"))
                    break
                except Exception as e:
                    last_error = e
                    await asyncio.sleep(0.25)
            if not self._browser:
                raise last_error or RuntimeError("Chrome closed before startup")
            self._context = self._browser.contexts[0]
            pages = self._context.pages
            self._page = next(
                (page for page in pages if page.url.startswith(
                    "https://www.heb.com")),
                pages[0] if pages else await self._context.new_page(),
            )
            if not self._page.url.startswith("https://www.heb.com"):
                await self._page.goto(
                    "https://www.heb.com/", wait_until="domcontentloaded")
        except Exception as e:
            log.error("connect failed: %s: %s", type(e).__name__, e)
            await self.close()
            raise HEBConnectionError(f"Could not open H-E-B: {e}") from e
        log.info("connect ok %s", self.status())
        return self.status()

    def _chrome_command(self, port):
        """Launch user-facing Chrome without Playwright automation switches."""
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            f"--user-data-dir={Path(self.profile_dir).resolve()}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.heb.com/",
        ]

    async def _fetch(self, url):
        async with self._nav:
            return await self._navigate(url)

    async def _navigate(self, url):
        if not self._page:
            raise HEBConnectionError("Connect H-E-B first")
        for attempt in range(3):
            started = time.monotonic()
            try:
                response = await self._page.goto(
                    f"https://www.heb.com{url}",
                    wait_until="domcontentloaded")
                text = await response.text()
            except Exception as e:
                log.warning("fetch %s attempt=%d failed after %dms: %s: %s",
                            url, attempt, (time.monotonic() - started) * 1000,
                            type(e).__name__, e)
                # A single navigation can fail transiently (renderer swap, a
                # slow redirect). Only a repeat failure means the session is
                # actually gone; dropping the browser on the first one is what
                # made the H-E-B connection look flaky.
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                log.error("disconnect: navigation to %s failed twice", url)
                await self.close()
                raise HEBConnectionError("H-E-B reconnect required") from e
            challenge = (
                "_Incapsula_Resource" in text
                or '"errorCode" : "15"' in text)
            if not challenge:
                log.info("fetch %s ok %dms %dB attempt=%d", url,
                         (time.monotonic() - started) * 1000, len(text), attempt)
                return text
            log.warning("fetch %s attempt=%d hit bot challenge", url, attempt)
            await asyncio.sleep(1)
        log.error("disconnect: %s stayed behind a bot challenge", url)
        self.connected = self.map_ready = False
        raise HEBConnectionError("H-E-B reconnect required")

    async def atlas_svg(self):
        """This store's live Atlas drawing."""
        svg = await self._fetch(
            f"/atlas/v1.0/image?locationNumber={self.store_id}&format=svg"
            "&style=none&label=false&drawPsas=true&drawAisleMarkers=true"
            "&landmarks=all&drawCombinedFixtures=true"
            "&drawDepartmentLabels=true&hidePartnerFixtures=true")
        if "<svg" not in svg:
            raise HEBConnectionError("H-E-B store map unavailable")
        return svg

    async def sees_store(self):
        """Whether the session's own catalog answers for this store."""
        html = await self._fetch("/search?q=milk")
        return bool(extract_products(html, self.store_id))

    async def confirm(self, store=None):
        if store is not None and int(store) != self.store_id:
            raise HEBConnectionError(f"Connect store #{store} first")
        if self.expected_digest is None:
            raise HEBConnectionError(
                f"store #{self.store_id} has no captured Atlas — run "
                f"capture_atlas.py {self.store_id}")
        if not await self.sees_store():
            raise HEBConnectionError(
                f"Select H-E-B #{self.store_id} in the opened browser")
        svg = await self.atlas_svg()
        self.connected = True
        # Fails closed: the calibration that places products was measured
        # against one drawing, so a changed drawing invalidates it.
        self.map_ready = parse_atlas(svg)["digest"] == self.expected_digest
        if not self.map_ready:
            raise HEBConnectionError(
                f"H-E-B's #{self.store_id} map changed; recapture and "
                "recalibrate the Atlas profile")
        return self.status()

    async def search(self, query, store=None):
        if store is not None and int(store) != self.store_id:
            raise HEBConnectionError(f"Connect store #{store} first")
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
        browser, playwright, chrome = (
            self._browser, self._playwright, self._chrome)
        self._playwright = self._browser = self._context = self._page = None
        self._chrome = None
        self.connected = self.map_ready = False
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass
        if chrome and chrome.poll() is None:
            chrome.terminate()
            try:
                await asyncio.to_thread(chrome.wait, 5)
            except subprocess.TimeoutExpired:
                chrome.kill()
                await asyncio.to_thread(chrome.wait)
