"""H-E-B catalog and Atlas-map helpers, for whichever store is selected."""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager

log = logging.getLogger("grocer.heb")
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse
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


class HEBBusyError(HEBConnectionError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__("H-E-B is busy; retry shortly")


SUPPORTED_STORES = (
    6, 14, 16, 24, 25, 26, 28, 31, 38, 39,
    178, 183, 189, 224, 265, 269, 333, 370, 373,
    659, 790, 811,
)
SEARCH_TTL = 300
PLACEMENT_TTL = 24 * 60 * 60
CACHE_MISS = object()


class HEBStorage:
    """Small durable store for anonymous browser state and catalog caches."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS store_states (
                    store_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    atlas_digest TEXT NOT NULL,
                    verified_at REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    kind TEXT NOT NULL,
                    store_id INTEGER NOT NULL,
                    cache_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (kind, store_id, cache_key)
                )
            """)
        self.path.chmod(0o600)

    def state_record(self, store):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT state_json, atlas_digest, verified_at "
                "FROM store_states WHERE store_id = ?", (int(store),)
            ).fetchone()
        if not row:
            return None
        return {
            "state": json.loads(row[0]),
            "atlas_digest": row[1],
            "verified_at": row[2],
        }

    def save_state(self, store, state, atlas_digest):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO store_states "
                "(store_id, state_json, atlas_digest, verified_at) "
                "VALUES (?, ?, ?, ?)",
                (int(store), json.dumps(state, separators=(",", ":")),
                 atlas_digest, time.time()),
            )

    def delete_state(self, store):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM store_states WHERE store_id = ?",
                       (int(store),))

    def get_cache(self, kind, store, key, default=None):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT value_json, expires_at FROM cache "
                "WHERE kind = ? AND store_id = ? AND cache_key = ?",
                (kind, int(store), str(key)),
            ).fetchone()
            if not row:
                return default
            if row[1] <= time.time():
                db.execute(
                    "DELETE FROM cache WHERE kind = ? AND store_id = ? "
                    "AND cache_key = ?", (kind, int(store), str(key)))
                return default
        return json.loads(row[0])

    def save_cache(self, kind, store, key, value, ttl):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO cache "
                "(kind, store_id, cache_key, value_json, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, int(store), str(key),
                 json.dumps(value, separators=(",", ":")), time.time() + ttl),
            )

    def clear_cache(self, kind=None):
        with sqlite3.connect(self.path) as db:
            if kind is None:
                db.execute("DELETE FROM cache")
            else:
                db.execute("DELETE FROM cache WHERE kind = ?", (kind,))

    def cache_count(self, kind, store):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM cache WHERE expires_at <= ?", (time.time(),))
            return db.execute(
                "SELECT COUNT(*) FROM cache WHERE kind = ? AND store_id = ?",
                (kind, int(store)),
            ).fetchone()[0]


class HEBClient:
    """One normal Chrome with an isolated, persisted context per store."""

    def __init__(self, store_id=659, profile_dir=None, source_path=None,
                 database_path=None, runtime_dir=None,
                 allow_unsupported=False):
        self.store_id = int(store_id)
        self.allow_unsupported = allow_unsupported
        self.runtime_dir = Path(
            runtime_dir or os.environ.get("HEB_RUNTIME_DIR", "runtime"))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = self.runtime_dir / "chrome"
        self.storage = HEBStorage(
            database_path or self.runtime_dir / "heb.sqlite")
        self.expected_digest = self._digest(source_path=source_path)
        self._playwright = self._browser = None
        self._chrome = None
        self._contexts = {}
        self._pages = {}
        self._context = self._page = None  # compatibility for local scripts
        self._connected = set()
        self._manual_ready = set()
        self._failed = set()
        self._map_invalid = set()
        self._pending_verification = set()
        self._inflight = {}
        self._tier = min(3, max(1, int(os.environ.get("HEB_START_TIER", "1"))))
        self._pending_tier = None
        self._outcomes = []
        self.queue_timeout = float(os.environ.get("HEB_QUEUE_TIMEOUT", "5"))
        # ponytail: one global browser slot; split per store only when measured
        # queue pressure justifies parallel H-E-B traffic.
        self._nav = asyncio.Lock()

    @property
    def connected(self):
        return self.status(self.store_id)["connected"]

    @connected.setter
    def connected(self, value):
        (self._connected.add if value else self._connected.discard)(
            self.store_id)

    @property
    def map_ready(self):
        return self.status(self.store_id)["map_ready"]

    @map_ready.setter
    def map_ready(self, value):
        (self._manual_ready.add if value else self._manual_ready.discard)(
            self.store_id)

    def _digest(self, source_path=None, store=None):
        """The Atlas snapshot this store was calibrated against, if captured."""
        store = self.store_id if store is None else int(store)
        path = Path(source_path or f"data/{store}-atlas/source.json")
        try:
            return json.loads(path.read_text())["sha256"]
        except (FileNotFoundError, KeyError):
            return None

    def _store(self, store=None):
        store = self.store_id if store is None else int(store)
        if store not in SUPPORTED_STORES and not self.allow_unsupported:
            raise HEBConnectionError(f"store #{store} is not catalog-enabled")
        return store

    def _valid_state(self, store):
        record = self.storage.state_record(store)
        digest = self._digest(store=store)
        return bool(record and digest and record["atlas_digest"] == digest)

    def status(self, store=None):
        store = self.store_id if store is None else int(store)
        valid = (store in SUPPORTED_STORES or self.allow_unsupported
                 ) and self._valid_state(store)
        failed = store in self._failed
        pending = store in self._pending_verification
        return {
            "connected": (
                not failed and not pending
                and (valid or store in self._connected)
            ),
            "map_ready": (
                not failed and not pending and store not in self._map_invalid
                and (valid or store in self._manual_ready)
            ),
            "store_id": store,
        }

    def recovery_status(self, store=None):
        """Report durable state without touching Chrome or H-E-B."""
        store = self._store(store)
        status = self.status(store)
        return status | {
            "cache": {
                kind: self.storage.cache_count(kind, store)
                for kind in ("search", "placement", "located")
            },
        }

    async def use(self, store):
        """Retained for local scripts; switching no longer closes other stores."""
        self.store_id = self._store(store)
        self.expected_digest = self._digest(store=self.store_id)
        self._context = self._contexts.get(self.store_id)
        self._page = self._pages.get(self.store_id)

    async def connect(self, store=None, fresh=True):
        store = self._store(store)
        if fresh:
            self._pending_verification.add(store)
        try:
            async with self._slot():
                await self._apply_pending_tier()
                await self._drop_context(store)
                await self._ensure_context(store, load_saved=not fresh)
                await self._navigate_unlocked(store, "/")
        except Exception:
            self._pending_verification.discard(store)
            raise
        return self.status(store)

    async def select_store(self, store=None):
        """Select a store in an onboarding context without human browser clicks."""
        store = self._store(store)
        context = self._contexts.get(store)
        if not context:
            raise HEBConnectionError(f"Connect store #{store} first")
        await context.add_cookies([
            {"name": name, "value": value, "url": "https://www.heb.com/"}
            for name, value in (
                ("CURR_SESSION_STORE", str(store)),
                ("SHOPPING_STORE_ID", str(store)),
                ("USER_SELECT_STORE", "false"),
            )
        ])

    @asynccontextmanager
    async def _slot(self):
        try:
            await asyncio.wait_for(
                self._nav.acquire(), timeout=self.queue_timeout)
        except TimeoutError as e:
            raise HEBBusyError(self.queue_timeout) from e
        try:
            yield
        finally:
            self._nav.release()

    def _clear_chrome_locks(self):
        """A crashed Chrome on a persistent volume leaves locks that block CDP."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.profile_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _chrome_failure(self, port, last_error):
        exit_code = None if self._chrome is None else self._chrome.poll()
        log_path = self.runtime_dir / "chrome.log"
        detail = ""
        try:
            detail = log_path.read_text(errors="replace")[-1500:]
        except OSError:
            pass
        if exit_code is not None:
            return (
                f"Chrome exited before CDP on :{port} (exit {exit_code})"
                + (f":\n{detail.strip()}" if detail.strip() else "")
            )
        return (
            f"Chrome CDP on :{port} never accepted connections"
            + (f": {last_error}" if last_error else "")
            + (f"\n{detail.strip()}" if detail.strip() else "")
        )

    async def _ensure_browser(self):
        if self._browser:
            return
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            endpoint = (
                os.environ.get("HEB_CDP_URL") if self._tier == 3 else None)
            if endpoint:
                self._browser = (
                    await self._playwright.chromium.connect_over_cdp(endpoint))
                return
            self._clear_chrome_locks()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            log_path = self.runtime_dir / "chrome.log"
            log_handle = open(log_path, "ab", buffering=0)
            self._chrome = subprocess.Popen(
                self._chrome_command(port),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            log_handle.close()
            last_error = None
            for _ in range(80):
                if self._chrome.poll() is not None:
                    break
                try:
                    self._browser = (
                        await self._playwright.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{port}"))
                    return
                except Exception as e:
                    last_error = e
                    await asyncio.sleep(0.25)
            raise RuntimeError(self._chrome_failure(port, last_error))
        except Exception as e:
            log.error("connect failed: %s: %s", type(e).__name__, e)
            await self.close()
            raise HEBConnectionError(f"Could not open H-E-B: {e}") from e

    def _chrome_command(self, port):
        """Launch normal Chrome without Playwright's automation switches."""
        configured = os.environ.get("CHROME_PATH")
        executable = configured or next(filter(None, (
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium"),
        )), None)
        if not executable:
            raise HEBConnectionError(
                "Chrome not found; set CHROME_PATH to its executable")
        # Containers need --no-sandbox even when not root; without it Chrome
        # exits immediately and CDP returns ECONNREFUSED.
        command = [
            executable,
            f"--user-data-dir={Path(self.profile_dir).resolve()}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "about:blank",
        ]
        return command

    def _proxy(self):
        if self._tier != 2:
            return None
        server = os.environ.get("HEB_PROXY_SERVER")
        if not server:
            return None
        proxy = {"server": server}
        if os.environ.get("HEB_PROXY_USERNAME"):
            proxy["username"] = os.environ["HEB_PROXY_USERNAME"]
        if os.environ.get("HEB_PROXY_PASSWORD"):
            proxy["password"] = os.environ["HEB_PROXY_PASSWORD"]
        return proxy

    def _record_outcome(self, failed):
        """Escalate configured transports when the last 20 attempts exceed 5%."""
        self._outcomes = (self._outcomes + [bool(failed)])[-20:]
        if (self._pending_tier is not None or len(self._outcomes) < 20
                or sum(self._outcomes) / len(self._outcomes) <= .05):
            return
        if self._tier < 2 and os.environ.get("HEB_PROXY_SERVER"):
            self._pending_tier = 2
        elif self._tier < 3 and os.environ.get("HEB_CDP_URL"):
            self._pending_tier = 3
        if self._pending_tier:
            log.error("H-E-B failure rate %.0f%%; escalating tier %s -> %s",
                      100 * sum(self._outcomes) / len(self._outcomes),
                      self._tier, self._pending_tier)

    async def _apply_pending_tier(self):
        if self._pending_tier is None:
            return
        tier, self._pending_tier = self._pending_tier, None
        await self.close()
        self._tier = tier
        self._outcomes.clear()
        self._failed.clear()

    async def _ensure_context(self, store, load_saved=True):
        page = self._pages.get(store)
        if page:
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
            await self._drop_context(store)
        if store == self.store_id and self._context and self._page:
            self._contexts[store] = self._context
            self._pages[store] = self._page
            return self._page
        await self._ensure_browser()
        record = self.storage.state_record(store) if load_saved else None
        options = {}
        if record:
            options["storage_state"] = record["state"]
        proxy = self._proxy()
        if proxy:
            options["proxy"] = proxy
        context = await self._browser.new_context(**options)
        page = await context.new_page()
        self._contexts[store], self._pages[store] = context, page
        if store == self.store_id:
            self._context, self._page = context, page
        return page

    async def _drop_context(self, store):
        context = self._contexts.pop(store, None)
        self._pages.pop(store, None)
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if store == self.store_id:
            self._context = self._page = None

    async def _fetch(self, url, store=None):
        store = self._store(store)
        async with self._slot():
            await self._apply_pending_tier()
            await self._ensure_context(store)
            text = await self._navigate_unlocked(store, url)
            if self._valid_state(store):
                await self._persist_state(store)
            return text

    async def _navigate(self, url, store=None):
        return await self._fetch(url, store)

    async def _navigate_unlocked(self, store, url):
        page = self._pages.get(store)
        if not page:
            raise HEBConnectionError(f"Connect store #{store} first")
        for attempt in range(3):
            started = time.monotonic()
            try:
                response = await page.goto(
                    f"https://www.heb.com{url}",
                    wait_until="domcontentloaded")
                text = (await response.text() if response
                        else await page.content())
            except Exception as e:
                self._record_outcome(True)
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
                log.error("store %s navigation to %s failed", store, url)
                self._failed.add(store)
                await self._drop_context(store)
                raise HEBConnectionError("H-E-B reconnect required") from e
            challenge = (
                "_Incapsula_Resource" in text
                or '"errorCode" : "15"' in text)
            if not challenge:
                self._record_outcome(False)
                log.info("fetch %s ok %dms %dB attempt=%d", url,
                         (time.monotonic() - started) * 1000, len(text), attempt)
                self._failed.discard(store)
                self._connected.add(store)
                return text
            self._record_outcome(True)
            log.warning("fetch %s attempt=%d hit bot challenge", url, attempt)
            await asyncio.sleep(1)
        log.error("disconnect: %s stayed behind a bot challenge", url)
        self._failed.add(store)
        await self._drop_context(store)
        raise HEBConnectionError("H-E-B reconnect required")

    async def _persist_state(self, store):
        context = self._contexts.get(store)
        digest = self._digest(store=store)
        if not context or not digest:
            return
        state = await context.storage_state(indexed_db=True)
        self.storage.save_state(store, self._clean_state(state), digest)

    @staticmethod
    def _clean_state(state):
        """Keep H-E-B state only; never copy a whole Chrome profile."""
        cookies = []
        for cookie in state.get("cookies", []):
            domain = cookie.get("domain", "").lstrip(".")
            if domain == "heb.com" or domain.endswith(".heb.com"):
                cookies.append(cookie)
        origins = []
        for origin in state.get("origins", []):
            host = urlparse(origin.get("origin", "")).hostname or ""
            if host == "heb.com" or host.endswith(".heb.com"):
                origins.append(origin)
        return {"cookies": cookies, "origins": origins}

    async def atlas_svg(self, store=None):
        """This store's live Atlas drawing."""
        store = self._store(store)
        svg = await self._fetch(
            f"/atlas/v1.0/image?locationNumber={store}&format=svg"
            "&style=none&label=false&drawPsas=true&drawAisleMarkers=true"
            "&landmarks=all&drawCombinedFixtures=true"
            "&drawDepartmentLabels=true&hidePartnerFixtures=true", store)
        if "<svg" not in svg:
            raise HEBConnectionError("H-E-B store map unavailable")
        return svg

    async def sees_store(self, store=None):
        """Whether the session's own catalog answers for this store."""
        store = self._store(store)
        html = await self._fetch("/search?q=milk", store)
        return bool(extract_products(html, store))

    async def confirm(self, store=None):
        store = self._store(store)
        pending = store in self._pending_verification
        try:
            expected = self._digest(store=store)
            if expected is None:
                raise HEBConnectionError(
                    f"store #{store} has no captured Atlas — run "
                    f"capture_atlas.py {store}")
            if not await self.sees_store(store):
                raise HEBConnectionError(
                    f"Select H-E-B #{store} in the opened browser")
            svg = await self.atlas_svg(store)
            self._connected.add(store)
            # Fails closed: the calibration that places products was measured
            # against one drawing, so a changed drawing invalidates it.
            if parse_atlas(svg)["digest"] != expected:
                self._map_invalid.add(store)
                raise HEBConnectionError(
                    f"H-E-B's #{store} map changed; recapture and "
                    "recalibrate the Atlas profile")
            self._map_invalid.discard(store)
            self._failed.discard(store)
            await self._persist_state(store)
        except Exception:
            if pending:
                self._pending_verification.discard(store)
                await self._drop_context(store)
            raise
        self._pending_verification.discard(store)
        return self.status(store)

    async def import_state(self, store, state):
        """Validate an admin-supplied anonymous state before replacing one."""
        store = self._store(store)
        previous = self.storage.state_record(store)
        self.storage.save_state(store, self._clean_state(state), "")
        self._pending_verification.add(store)
        await self._drop_context(store)
        try:
            return await self.confirm(store)
        except Exception:
            if previous:
                self.storage.save_state(
                    store, previous["state"], previous["atlas_digest"])
            else:
                self.storage.delete_state(store)
            await self._drop_context(store)
            raise

    def export_state(self, store):
        store = self._store(store)
        record = self.storage.state_record(store)
        if not record or not self._valid_state(store):
            raise HEBConnectionError(f"store #{store} has no verified state")
        return record["state"]

    async def _coalesce(self, key, operation):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            self._inflight[key] = task

            def finished(done):
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def search(self, query, store=None):
        store = self._store(store)
        key = " ".join(query.lower().split())
        cached = self.storage.get_cache("search", store, key)
        if cached is not None:
            return cached
        if not self.status(store)["map_ready"]:
            raise HEBConnectionError(f"store #{store} needs admin bootstrap")

        async def fetch():
            cached_again = self.storage.get_cache("search", store, key)
            if cached_again is not None:
                return cached_again
            products = extract_products(
                await self._fetch(f"/search?q={quote(key)}", store),
                store)[:8]
            self.storage.save_cache(
                "search", store, key, products, SEARCH_TTL)
            return products

        return await self._coalesce(("search", store, key), fetch)

    async def locate(self, product_id, location_label, atlas, store=None):
        store = self._store(store)
        if not self.status(store)["map_ready"]:
            raise HEBConnectionError(f"store #{store} needs admin bootstrap")
        digest = self._digest(store=store) or ""
        key = f"{digest}:{product_id}:{location_label or ''}"
        cached = self.storage.get_cache(
            "placement", store, key, CACHE_MISS)
        if cached is not CACHE_MISS:
            return cached

        async def fetch():
            cached_again = self.storage.get_cache(
                "placement", store, key, CACHE_MISS)
            if cached_again is not CACHE_MISS:
                return cached_again
            text = await self._fetch(
                f"/pals/v2.0/location/store/{store}/products/{product_id}",
                store)
            try:
                pals = json.loads(text)
            except json.JSONDecodeError as e:
                raise HEBConnectionError(
                    "H-E-B product location unavailable") from e
            placement = resolve_placement(pals, atlas, location_label)
            self.storage.save_cache(
                "placement", store, key, placement, PLACEMENT_TTL)
            return placement

        return await self._coalesce(("placement", store, key), fetch)

    async def close(self):
        for store in list(self._contexts):
            if self._valid_state(store):
                try:
                    await self._persist_state(store)
                except Exception:
                    pass
        browser, playwright, chrome = (
            self._browser, self._playwright, self._chrome)
        for store in list(self._contexts):
            await self._drop_context(store)
        self._playwright = self._browser = self._context = self._page = None
        self._chrome = None
        self._connected.clear()
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
