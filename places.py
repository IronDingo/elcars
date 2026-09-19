#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Places: find real local spots via OpenStreetMap.

"The eyes on the map." A lean, stdlib-only finder over OpenStreetMap — no key,
no card, no account. Nominatim geocodes an area (default: home) and Overpass
returns the real food/drink POIs there (name, kind, cuisine, hours-if-tagged).

Like spotify.py and files.py, it doesn't try to be clever: it hands a grounded,
real list of places back to the brain, which ranks them, layers its own
knowledge of which are well-regarded, and composes the answer or itinerary. So
nothing is invented — every name is a place that actually exists on the map —
and the "which are good" judgement is the model's, never a fake rating.

OSM has no ratings and its opening-hours tags are patchy, so this never claims a
star or a reliable "open now"; it grounds the *places*, the brain supplies taste.

Etiquette: a real User-Agent is sent (OSM blocks the default), calls are bounded,
and the area geocode is cached per session so we don't re-hit Nominatim.
"""

from __future__ import annotations

import concurrent.futures
import json
import urllib.parse
import urllib.request

HOME_AREA = "Dokki, Giza, Egypt"      # default when the user doesn't name a place
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [                 # public instances 504 under load; retry + fall back
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "ELCARS-voice-assistant/1.0 (personal use; OSM data © OpenStreetMap contributors)"
RADIUS_M = 1600                       # ~a neighbourhood's walkable core
MAX_POIS = 30                         # how many real places we hand the brain
TIMEOUT = 10                          # per-request network timeout (Nominatim geocode)
OVERPASS_TIMEOUT = 10                 # Overpass query + client cap; mirrors raced in parallel

# food/drink venue kinds we care about (OSM `amenity` values)
_AMENITIES = ["restaurant", "cafe", "fast_food", "bar", "pub", "ice_cream", "food_court"]

# query hint -> OSM cuisine/name tokens, so "pasta" also finds italian places etc.
_CUISINE_HINTS = {
    "pasta": ["italian", "pasta", "pizza"], "italian": ["italian", "pizza", "pasta"],
    "pizza": ["pizza", "italian"], "sushi": ["sushi", "japanese"],
    "japanese": ["japanese", "sushi", "ramen"], "burger": ["burger", "american"],
    "coffee": ["coffee", "cafe"], "cafe": ["coffee", "cafe"],
    "dessert": ["dessert", "ice_cream", "bakery"], "seafood": ["seafood", "fish"],
    "egyptian": ["egyptian", "koshary", "koshari"], "koshary": ["koshary", "egyptian"],
    "lebanese": ["lebanese"], "shawarma": ["shawarma", "kebab"],
    "kebab": ["kebab", "grill"], "grill": ["grill", "bbq", "steak"], "indian": ["indian"],
    "chinese": ["chinese", "asian"], "asian": ["asian", "chinese", "thai"],
    "vegan": ["vegan", "vegetarian"], "vegetarian": ["vegetarian", "vegan"],
    "breakfast": ["breakfast", "cafe"], "bakery": ["bakery", "pastry"],
}

_GEO_CACHE: dict[str, tuple[float, float]] = {}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _geocode(area: str) -> tuple[float, float] | None:
    """Area name -> (lat, lon) via Nominatim; cached per session."""
    key = area.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    params = urllib.parse.urlencode({"q": area, "format": "json", "limit": 1})
    try:
        data = json.loads(_get(f"{NOMINATIM}?{params}"))
    except Exception:
        return None
    if not data:
        return None
    coord = (float(data[0]["lat"]), float(data[0]["lon"]))
    _GEO_CACHE[key] = coord
    return coord


def _overpass(lat: float, lon: float) -> list[dict]:
    """Named food/drink POIs near (lat, lon). Fires all mirrors at once and takes the
    first to answer — public instances 504/stall under load, and a voice assistant
    can't wait on a slow one, so we race them under a hard cap instead of retrying."""
    amen = "|".join(_AMENITIES)
    q = (f'[out:json][timeout:{OVERPASS_TIMEOUT}];'
         f'(node["amenity"~"{amen}"]["name"](around:{RADIUS_M},{lat},{lon});'
         f' way["amenity"~"{amen}"]["name"](around:{RADIUS_M},{lat},{lon}););'
         f'out tags {MAX_POIS * 3};')
    payload = urllib.parse.urlencode({"data": q}).encode()

    def _hit(endpoint: str) -> list[dict]:
        req = urllib.request.Request(endpoint, data=payload, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT) as r:
            return json.loads(r.read()).get("elements", [])

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS))
    futures = [ex.submit(_hit, ep) for ep in OVERPASS_ENDPOINTS]
    result: list[dict] = []
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=OVERPASS_TIMEOUT + 2):
            try:
                elements = fut.result()
            except Exception:
                continue          # this mirror errored/504'd — let a faster one answer
            if elements:
                result = elements
                break
    except concurrent.futures.TimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return result


def _describe(el: dict) -> dict:
    t = el.get("tags", {})
    return {
        "name": (t.get("name") or "").strip(),
        "kind": (t.get("amenity") or "").replace("_", " "),
        "cuisine": (t.get("cuisine") or "").replace("_", " ").replace(";", ", "),
        "hours": (t.get("opening_hours") or "").strip(),
        "street": (t.get("addr:street") or "").strip(),
    }


def _tokens(query: str) -> list[str]:
    toks = set()
    for word in query.lower().replace(",", " ").split():
        toks.add(word)
        toks.update(_CUISINE_HINTS.get(word, []))
    return [t for t in toks if t]


def _rank(pois: list[dict], query: str) -> list[dict]:
    """Query matches first (by name/cuisine/kind), rest kept as fallback — never dropped."""
    toks = _tokens(query)
    if not toks:
        return pois
    def hit(p):
        hay = f"{p['name']} {p['cuisine']} {p['kind']}".lower()
        return any(t in hay for t in toks)
    return [p for p in pois if hit(p)] + [p for p in pois if not hit(p)]


def find(query: str = "", area: str = "") -> tuple[str, bool]:
    """Find real venues in `area` (default home) for the brain to reason over.
    Returns a grounded list; the brain ranks it, adds its own knowledge of which
    are well-regarded, and composes. Mirrors files.open_file's data-back contract."""
    where = (area or "").strip() or HOME_AREA
    coord = _geocode(where if "," in where else f"{where}, Cairo, Egypt")
    if not coord:
        return f"I couldn't find {where} on the map.", False

    pois = [p for p in (_describe(e) for e in _overpass(*coord)) if p["name"]]
    if not pois:
        return f"I couldn't find any places around {where}.", False

    seen, uniq = set(), []
    for p in _rank(pois, query):
        k = p["name"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    uniq = uniq[:MAX_POIS]

    lines = []
    for p in uniq:
        bits = [p["name"]]
        detail = ", ".join(x for x in (p["cuisine"] or p["kind"], p["street"]) if x)
        if detail:
            bits.append(f"({detail})")
        if p["hours"]:
            bits.append(f"hours: {p['hours']}")
        lines.append(" ".join(bits))

    ask = (f"Real places in {where} from OpenStreetMap"
           + (f" matching '{query}'" if query else "")
           + f" ({len(uniq)}): " + " · ".join(lines) + ". "
           "These all genuinely exist. Pick the best few for what the user asked, using "
           "your own knowledge of which are well-regarded — but only vouch for a place you "
           "actually recognise, never invent a rating, and never name a place that isn't in "
           "this list. Then give a short, natural spoken answer or itinerary.")
    return ask, True


if __name__ == "__main__":     # quick manual check: python places.py [query] [area]
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "pasta"
    a = sys.argv[2] if len(sys.argv) > 2 else ""
    text, ok = find(q, a)
    print("OK" if ok else "MISS")
    print(text)
