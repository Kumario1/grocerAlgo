#!/usr/bin/env python3
"""PDF discovery: store number -> downloaded + validated map PDF.

Usage: python3 discover.py <store> [city]

H-E-B publishes every store guide on its Scene7 CDN (no bot protection,
unlike heb.com itself) at a fixed URL:
    https://images.heb.com/is/content/HEBGrocery/Store%20Finder%20Layouts/
        guide-<city>-<store>.pdf
Misses are clean 404s, hits are application/pdf — so an unknown store's
city is found by probing the slug list below (austin first, then the
H-E-B footprint). Pass an explicit city to skip probing; spaces are
normalized to the CDN's hyphenated slug.

Output: guide-<city>-<store>.pdf in the repo root (the name the rest of
the pipeline resolves via router.derive.pdf_path), validated to actually
be a store map (>=2 pages; map page carries drawings and aisle badges).
Idempotent: an existing local guide for the requested city wins immediately.
"""
import glob
import json
import os
import sys
import urllib.request

BASE = ("https://images.heb.com/is/content/HEBGrocery/"
        "Store%20Finder%20Layouts")

# H-E-B footprint city slugs (single lowercase word or hyphenated), austin
# first. Confirmed live examples: austin, houston, plano, lubbock, odessa,
# elgin, hondo, taylor. Extend freely — a wrong slug costs one 404.
CITIES = [
    "austin", "houston", "san-antonio", "dallas", "fort-worth", "plano",
    "frisco", "mckinney", "allen", "katy", "round-rock", "pflugerville",
    "cedar-park", "leander", "georgetown", "hutto", "kyle", "buda",
    "san-marcos", "new-braunfels", "seguin", "schertz", "converse",
    "universal-city", "boerne", "bulverde", "kerrville", "fredericksburg",
    "temple", "belton", "killeen", "harker-heights", "waco", "bryan",
    "college-station", "corpus-christi", "victoria", "laredo", "mcallen",
    "edinburg", "mission", "pharr", "harlingen", "brownsville", "el-paso",
    "lubbock", "midland", "odessa", "abilene", "san-angelo", "amarillo",
    "wichita-falls", "burleson", "mansfield", "grand-prairie", "irving",
    "spring", "cypress", "humble", "tomball", "conroe", "magnolia",
    "pearland", "sugar-land", "richmond", "missouri-city", "baytown",
    "league-city", "friendswood", "webster", "alvin", "angleton",
    "lake-jackson", "rosenberg", "kingwood", "the-woodlands", "elgin",
    "taylor", "hondo", "uvalde", "del-rio", "eagle-pass", "bastrop",
    "lockhart", "gonzales", "cuero", "beeville", "alice", "kingsville",
    "portland", "rockport", "port-lavaca", "bay-city", "wharton",
    "brenham", "navasota", "huntsville", "lufkin", "nacogdoches",
    "palestine", "tyler", "longview", "texarkana", "sherman", "denton",
    "lewisville", "flower-mound", "rockwall", "waxahachie", "ennis",
    "corsicana", "cleburne", "granbury", "weatherford", "stephenville",
    "brownwood", "marble-falls", "burnet", "llano", "lakeway",
    "dripping-springs", "wimberley", "canyon-lake", "floresville",
    "pleasanton", "devine", "castroville",
]


def probe(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200 and "pdf" in r.headers.get(
                "Content-Type", "")
    except Exception:
        return False


def city_slug(city):
    return "-".join(city.lower().split())


def validate(path):
    """A real store guide: >=2 pages, map page dense with drawings and
    carrying a run of aisle badges."""
    import fitz
    doc = fitz.open(path)
    if len(doc) < 2:
        return "fewer than 2 pages (map is page 2)"
    page = doc[1]
    if not page.get_drawings() and page.get_images():
        images = page.get_image_info()
        if (len(images) == 1 and images[0]["width"] >= 1000
                and images[0]["height"] >= 1000
                and fitz.Rect(images[0]["bbox"]).get_area()
                >= .8 * page.rect.get_area()):
            return None
        return "raster map is not one high-resolution full-page image"
    if len(page.get_drawings()) < 100:
        return "map page has <100 drawings — not a store map?"
    badges = {int(t) for *_, t, _, _, _ in page.get_text("words")
              if t.isdigit() and 1 <= int(t) <= 60}
    if len(badges) < 15:
        return f"only {len(badges)} aisle badges on the map page"
    return None


def select(store, path):
    """Persist the validated guide so later stages never choose by glob."""
    directory = f"data/{store}"
    os.makedirs(directory, exist_ok=True)
    with open(f"{directory}/source.json", "w") as output:
        json.dump({"pdf": path}, output)


def discover(store, city=None):
    city = city_slug(city) if city else None
    existing = sorted(glob.glob(f"guide-*-{store}.pdf"))
    if city:
        exact = f"guide-{city}-{store}.pdf"
        existing = [exact] if exact in existing else []
    if len(existing) > 1:
        raise SystemExit(
            f"multiple local guides found for store {store}: {existing}; "
            "rerun with the store city")
    if len(existing) == 1:
        err = validate(existing[0])
        if err:
            raise SystemExit(f"local {existing[0]} failed validation: {err}")
        select(store, existing[0])
        print(f"already local: {existing[0]}")
        return existing[0]
    for slug in ([city] if city else CITIES):
        url = f"{BASE}/guide-{slug}-{store}.pdf"
        if not probe(url):
            continue
        path = f"guide-{slug}-{store}.pdf"
        print(f"found {url}")
        urllib.request.urlretrieve(url, path)
        err = validate(path)
        if err:
            raise SystemExit(f"downloaded {path} but it failed validation: "
                             f"{err}")
        select(store, path)
        print(f"downloaded + validated -> {path}")
        return path
    where = f"city '{city}'" if city else f"{len(CITIES)} known city slugs"
    raise SystemExit(
        f"no guide PDF found for store {store} under {where}.\n"
        f"Find the store's city on heb.com and rerun: "
        f"python3 discover.py {store} <city>")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 discover.py <store> [city]")
    discover(sys.argv[1], " ".join(sys.argv[2:]) or None)
