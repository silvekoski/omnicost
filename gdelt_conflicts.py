# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
GDELT conflict overlay — pulls recent CAMEO-coded conflict events from GDELT 2.0
and scores how close they sit to each datacenter region.

Why the raw files and not the REST API:
  The GEO 2.0 endpoint (api/v2/geo/geo) now 404s and the DOC 2.0 endpoint is
  aggressively rate-limited (HTTP 429 after a couple of hits). The raw 15-minute
  event exports at data.gdeltproject.org/gdeltv2/ are small (~45 KB zipped),
  unauthenticated, un-throttled, and carry exactly what a proximity map needs:
  a CAMEO QuadClass/EventRootCode plus ActionGeo lat/long for every event.

We keep only QuadClass 4 ("material conflict" — coerce / assault / fight / mass
violence, CAMEO root codes 17-20), geolocated, and score each event's severity
from its Goldstein scale (boosted for armed clashes and mass violence). Then for
every region anchor in region_data.json we report how many conflicts fall inside
a radius and how close the nearest serious one is.

The whole thing is wrapped in a short-lived in-memory cache (GDELT publishes a new
file every 15 min, so re-fetching more often than that buys nothing).

Run standalone to sanity-check:  uv run --python 3.12 gdelt_conflicts.py
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REGION_DATA = HERE / "region_data.json"

GDELT_BASE = "http://data.gdeltproject.org/gdeltv2"
LASTUPDATE = f"{GDELT_BASE}/lastupdate.txt"

# ---- GDELT 2.0 export column indices (0-based, 61 tab-separated fields) ----
C_EVENT_ID      = 0
C_DAY           = 1
C_ROOT_CODE     = 28   # CAMEO EventRootCode
C_QUADCLASS     = 29   # 1 verbal-coop, 2 material-coop, 3 verbal-conflict, 4 material-conflict
C_GOLDSTEIN     = 30   # -10..+10, negative = conflictual
C_NUM_MENTIONS  = 31
C_NUM_SOURCES   = 32
C_NUM_ARTICLES  = 33
C_AVG_TONE      = 34
C_GEO_FULLNAME  = 52   # ActionGeo_Fullname
C_GEO_COUNTRY   = 53   # ActionGeo_CountryCode
C_GEO_LAT       = 56   # ActionGeo_Lat
C_GEO_LONG      = 57   # ActionGeo_Long
C_DATEADDED     = 59
C_SOURCEURL     = 60

# CAMEO root codes that live inside QuadClass 4, with human labels + map colours.
ROOT_LABELS = {
    "17": "Coercion",
    "18": "Assault",
    "19": "Armed clash",
    "20": "Mass violence",
}
ROOT_FALLBACK = "Material conflict"

# GDELT's automated CAMEO coder badly misreads soft-news content: a sports headline
# ("clash", "thrash", "demolish"), a movie/history piece (war themes), or a lifestyle
# story ("kill", "battle") routinely gets coded as a max-severity armed clash. None of
# that is a real datacenter risk, so we drop any event whose source URL lives in an
# entertainment / sports / lifestyle section. Matched case-insensitively as substrings.
NOISE_URL_MARKERS = (
    # section paths
    "/etimes/", "/entertainment/", "/lifestyle/", "/life-style/", "/trending/", "/viral/",
    "/celebrity/", "/celeb/", "/gossip/", "/showbiz/", "/bollywood/", "/hollywood/",
    "/movies/", "/movie-", "/film/", "/films/", "/tv-show", "/web-series/", "/music/",
    "/sports/", "/sport/", "/cricket/", "/football/", "/soccer/", "/nfl/", "/nba/", "/mlb/",
    "/tennis/", "/golf/", "/boxing/", "/ufc/", "/wwe/", "/wrestling/", "/rugby/", "/hockey/",
    "/gaming/", "/games/", "/esports/", "/recipe", "/recipes/", "/food/", "/foods/",
    "/travel/", "/horoscope", "/astrology", "/zodiac", "/numerology",
    "/photos/", "/photo-gallery", "/web-stories/", "/webstories/", "/gallery/", "/schedule/",
    "/fashion/", "/beauty/", "/wellness/", "/relationships/", "/dating/", "/parenting/",
    "/books/", "/history/", "/spirituality/", "/quiz", "/jokes", "/memes", "/royal",
    # soft-news domains
    "tvguide", "tv-guide", "imdb.", "rottentomatoes", "espn.", "goal.com", "billboard.",
    # history / period pieces (GDELT codes war-history articles as live armed clashes)
    "world-war", "wwii", "napoleon", "ancient-", "medieval", "roman-empire",
    "american-revolution", "revolutionary-war", "th-century", "centuries-old", "this-day-in",
    "-in-history", "on-this-day", "restoration-of-notre",
    # entertainment / sports slug giveaways  (note: avoid bare words that hit real news)
    "box-office", "-trailer", "-episode", "-season-", "-recap", "spoiler", "movie-review",
    "film-review", "tv-review", "red-carpet", "match-report", "full-match", "-movie-",
    "netflix", "coronation-street", "eastenders", "emmerdale", "soap-",
    "scorecard", "-playoff", "transfer-news", "-fixture", "live-score", "box-score",
)


def _is_noise_url(url: str) -> bool:
    u = url.lower()
    return any(m in u for m in NOISE_URL_MARKERS)


# Defaults (overridable per request).
DEFAULT_HOURS = 24          # how far back to look
DEFAULT_RADIUS_KM = 1000    # "near a region" threshold
MAX_HOURS = 72
MAX_POINTS = 450            # cap plotted points; risk counts still use the full set
# "Major" = a genuinely geopolitical-scale conflict, which is what should drive a
# datacenter's risk level. Empirically there's a sharp cliff in the GDELT feed: ~1000
# events/day land at 10-19 articles (mostly widely-syndicated local crime) but only ~65
# clear 20 — and those are the real ones (Tehran clashes, Ukraine strikes, etc.). Mass
# violence (CAMEO root 20) is always major regardless of how many outlets ran it.
MAJOR_ARTICLES = 20
MAJOR_MIN_SEVERITY = 0.5
CACHE_TTL_SEC = 20 * 60     # GDELT cadence is 15 min; 20 min cache is plenty
CONCURRENCY = 12

EARTH_R_KM = 6371.0


# --------------------------------------------------------------------------- #
# geo helpers
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def severity(goldstein: float, root: str) -> float:
    """0..1 — how violent. Goldstein drives it; armed clash / mass violence floor it high."""
    s = max(0.0, min(1.0, (-goldstein) / 10.0))
    if root == "19":
        s = max(s, 0.72)
    if root == "20":
        s = max(s, 0.9)
    return round(s, 3)


def is_major(e: dict) -> bool:
    """A geopolitical-scale conflict — drives the per-region risk level. Either mass
    violence, or violent AND widely reported (the article gate filters local crime)."""
    if e["root"] == "20":
        return True
    return e["articles"] >= MAJOR_ARTICLES and e["severity"] >= MAJOR_MIN_SEVERITY


# --------------------------------------------------------------------------- #
# fetch + parse
# --------------------------------------------------------------------------- #
def _load_geos() -> dict:
    """Region anchors are the single source of truth in region_data.json."""
    if not REGION_DATA.exists():
        return {}
    return json.loads(REGION_DATA.read_text()).get("geos", {})


async def _latest_timestamp(client: httpx.AsyncClient) -> str:
    """Newest published 15-min slot, e.g. '20260524094500', from lastupdate.txt."""
    r = await client.get(LASTUPDATE, timeout=20)
    r.raise_for_status()
    m = re.search(r"(\d{14})\.export\.CSV\.zip", r.text)
    if not m:
        raise RuntimeError("could not parse lastupdate.txt")
    return m.group(1)


def _step_back(ts: str, steps: int) -> list[str]:
    """`steps` timestamps at 15-min intervals ending at (and including) ts.
    GDELT timestamps are UTC; we do pure arithmetic so no tz/DST handling is needed."""
    base = datetime.strptime(ts, "%Y%m%d%H%M%S")
    return [(base - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S") for i in range(steps)]


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, ts: str) -> list[dict]:
    """Download one export ZIP and return its parsed material-conflict events."""
    url = f"{GDELT_BASE}/{ts}.export.CSV.zip"
    try:
        async with sem:
            r = await client.get(url, timeout=30)
        if r.status_code != 200:
            return []
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            raw = zf.read(zf.namelist()[0]).decode("latin-1")
    except (httpx.HTTPError, zipfile.BadZipFile, OSError):
        return []

    events = []
    for line in raw.splitlines():
        f = line.split("\t")
        if len(f) < 61 or f[C_QUADCLASS] != "4":
            continue
        if _is_noise_url(f[C_SOURCEURL]):       # entertainment/sports/lifestyle miscodes
            continue
        lat_s, lon_s = f[C_GEO_LAT], f[C_GEO_LONG]
        if not lat_s or not lon_s:
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
            gold = float(f[C_GOLDSTEIN] or 0)
            arts = int(f[C_NUM_ARTICLES] or 0)
            ment = int(f[C_NUM_MENTIONS] or 0)
        except ValueError:
            continue
        if lat == 0.0 and lon == 0.0:       # GDELT's "unknown" sentinel
            continue
        root = f[C_ROOT_CODE]
        events.append({
            "id": f[C_EVENT_ID],
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "place": f[C_GEO_FULLNAME] or "Unknown location",
            "country": f[C_GEO_COUNTRY],
            "root": root,
            "type": ROOT_LABELS.get(root, ROOT_FALLBACK),
            "goldstein": gold,
            "articles": arts,
            "mentions": ment,
            "severity": severity(gold, root),
            "day": f[C_DAY],
            "url": f[C_SOURCEURL],
        })
    return events


async def _collect_events(hours: int) -> tuple[list[dict], str]:
    steps = max(1, min(hours, MAX_HOURS)) * 4
    async with httpx.AsyncClient(headers={"User-Agent": "aiven-costcity-conflicts/1.0"}) as client:
        latest = await _latest_timestamp(client)
        slots = _step_back(latest, steps)
        sem = asyncio.Semaphore(CONCURRENCY)
        batches = await asyncio.gather(*(_fetch_one(client, sem, ts) for ts in slots))

    # Dedupe by event id (the same id can recur if GDELT revises a slot).
    by_id: dict[str, dict] = {}
    for batch in batches:
        for e in batch:
            by_id[e["id"]] = e
    return list(by_id.values()), latest


# --------------------------------------------------------------------------- #
# proximity scoring
# --------------------------------------------------------------------------- #
def _risk_level(count_within: int, count_major: int, nearest_major_km: float | None) -> str:
    if nearest_major_km is not None and (nearest_major_km <= 400 or count_major >= 8):
        return "high"
    if nearest_major_km is not None:        # a major conflict, but further out / isolated
        return "elevated"
    if count_within > 0:                    # only lower-level / local conflict nearby
        return "watch"
    return "clear"


def _datacenter_risk(events: list[dict], geos: dict, radius_km: float) -> list[dict]:
    out = []
    for key, g in geos.items():
        if g.get("synthetic"):          # "other" is non-geographic — skip
            continue
        glat, glon = g["lat"], g["lon"]
        within, major = 0, 0
        nearest = None
        nearest_major = None
        for e in events:
            d = haversine_km(glat, glon, e["lat"], e["lon"])
            if d > radius_km:
                continue
            within += 1
            if nearest is None or d < nearest[0]:
                nearest = (d, e)
            if is_major(e):
                major += 1
                if nearest_major is None or d < nearest_major[0]:
                    nearest_major = (d, e)
        rec = {
            "geo": key,
            "name": g["name"],
            "lat": glat,
            "lon": glon,
            "within": within,
            "major": major,
            "level": _risk_level(within, major, nearest_major[0] if nearest_major else None),
        }
        if nearest:
            d, e = nearest
            rec["nearest"] = {"km": round(d), "place": e["place"], "type": e["type"]}
        if nearest_major:
            d, e = nearest_major
            rec["nearest_major"] = {
                "km": round(d), "place": e["place"], "type": e["type"],
                "url": e["url"], "severity": e["severity"], "articles": e["articles"],
            }
        out.append(rec)
    # Worst first.
    order = {"high": 0, "elevated": 1, "watch": 2, "clear": 3}
    out.sort(key=lambda r: (order[r["level"]], -r["major"], -r["within"]))
    return out


def _to_geojson(events: list[dict]) -> dict:
    feats = []
    for e in events:
        props = {k: e[k] for k in
                 ("id", "place", "country", "type", "root", "goldstein",
                  "articles", "mentions", "severity", "day", "url")}
        props["major"] = is_major(e)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": feats}


def build_payload(events: list[dict], hours: int, radius_km: float, latest_ts: str | None) -> dict:
    geos = _load_geos()
    datacenters = _datacenter_risk(events, geos, radius_km)
    # Plot the most newsworthy events; risk counts above already used the full set.
    ranked = sorted(events, key=lambda e: (e["articles"], e["severity"]), reverse=True)
    plotted = ranked[:MAX_POINTS]
    return {
        "generated_at": int(time.time()),
        "source": "GDELT 2.0 Event Database (CAMEO QuadClass 4 — material conflict)",
        "window_hours": hours,
        "radius_km": radius_km,
        "latest_slot": latest_ts,
        "total_conflicts": len(events),
        "plotted": len(plotted),
        "conflicts": _to_geojson(plotted),
        "datacenters": datacenters,
    }


# --------------------------------------------------------------------------- #
# cached public entrypoint
# --------------------------------------------------------------------------- #
_cache: dict[tuple, tuple[float, dict]] = {}


async def get_conflicts(hours: int = DEFAULT_HOURS, radius_km: float = DEFAULT_RADIUS_KM,
                        force: bool = False) -> dict:
    hours = max(1, min(int(hours), MAX_HOURS))
    radius_km = max(50.0, min(float(radius_km), 8000.0))
    key = (hours, radius_km)
    now = time.monotonic()
    if not force and key in _cache and now - _cache[key][0] < CACHE_TTL_SEC:
        cached = dict(_cache[key][1])
        cached["cached"] = True
        return cached

    events, latest_ts = await _collect_events(hours)
    payload = build_payload(events, hours, radius_km, latest_ts)
    payload["cached"] = False
    _cache[key] = (now, payload)
    return payload


# --------------------------------------------------------------------------- #
# CLI sanity check
# --------------------------------------------------------------------------- #
def _main() -> None:
    t0 = time.time()
    data = asyncio.run(get_conflicts(force=True))
    dt = time.time() - t0
    print(f"fetched in {dt:.1f}s — {data['total_conflicts']} material-conflict events "
          f"(window {data['window_hours']}h), plotting {data['plotted']}")
    print(f"radius for 'near a region': {data['radius_km']} km\n")
    print(f"{'region':<10} {'level':<9} {'within':>6} {'major':>6}  nearest major conflict")
    for d in data["datacenters"]:
        nm = d.get("nearest_major")
        tail = f"{nm['km']} km — {nm['type']} @ {nm['place']} ({nm['articles']} articles)" if nm else "—"
        print(f"{d['name']:<10} {d['level']:<9} {d['within']:>6} {d['major']:>6}  {tail}")


if __name__ == "__main__":
    _main()
