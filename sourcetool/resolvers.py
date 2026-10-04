"""Resolvers: turn the words of an ask into a source parameter — for EVERY parameter kind, one format,
one matcher (docs/RESOLVERS.md).

A resolver is a table of entries, all in one shape:

    {"id": "<resolver>:<key>", "resolver": "<name>", "kind": "<param kind>", "key": "<value a source gets>",
     "name": "Melbourne", "aliases": ["melbourne fl", ...], "lat": 28.1, "lon": -80.6, "state": "FL",
     "attrs": {...}, "rank": 0}

Sources point a parameter at a resolver (`access.params[].resolver` + `.field`), so "tides for Melbourne FL"
becomes place -> nearest tide stations -> the station id the NOAA source needs — and the same code resolves
an airport, a ticker, a team, a ZIP or a bike-share system.

`python -m sourcetool resolvers harvest` rebuilds every table from open, keyless sources.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from urllib.parse import quote
from xml.etree import ElementTree

from .common import ROOT, get, get_json, read_jsonl, uncache, write_jsonl

RES = ROOT / "resolvers"

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands",
    "AS": "American Samoa", "MP": "Northern Mariana Islands",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}

CURRENCIES = [("USD", "US dollar", "dollar"), ("EUR", "Euro", "euro"), ("JPY", "Japanese yen", "yen"),
              ("GBP", "British pound", "pound"), ("CNY", "Chinese yuan", "renminbi"),
              ("CAD", "Canadian dollar", "loonie"), ("AUD", "Australian dollar", "aussie dollar"),
              ("CHF", "Swiss franc", "franc"), ("MXN", "Mexican peso", "peso"), ("INR", "Indian rupee", "rupee"),
              ("KRW", "South Korean won", "won"), ("BRL", "Brazilian real", "real"), ("SEK", "Swedish krona", "krona"),
              ("NOK", "Norwegian krone", "krone"), ("DKK", "Danish krone", "danish krone"),
              ("NZD", "New Zealand dollar", "kiwi"), ("SGD", "Singapore dollar", "singapore dollar"),
              ("HKD", "Hong Kong dollar", "hong kong dollar"), ("ZAR", "South African rand", "rand"),
              ("TRY", "Turkish lira", "lira"), ("PLN", "Polish zloty", "zloty"), ("ILS", "Israeli shekel", "shekel"),
              ("THB", "Thai baht", "baht"), ("CZK", "Czech koruna", "koruna"), ("HUF", "Hungarian forint", "forint"),
              ("PHP", "Philippine peso", "philippine peso"), ("IDR", "Indonesian rupiah", "rupiah"),
              ("MYR", "Malaysian ringgit", "ringgit"), ("ISK", "Icelandic krona", "icelandic krona"),
              ("RON", "Romanian leu", "leu"), ("BGN", "Bulgarian lev", "lev")]

_PLACE_SUFFIX = re.compile(r"\s+(city|town|village|borough|CDP|municipality|city and borough|urban county|"
                           r"consolidated government.*|metro government.*|unified government.*)$", re.I)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("&", " and "))).strip()


def _e(resolver, kind, key, name, aliases=(), lat=None, lon=None, state="", attrs=None, rank=0):
    al = sorted({norm(a) for a in (name, *aliases) if a and norm(a)})
    return {"id": f"{resolver}:{key}", "resolver": resolver, "kind": kind, "key": str(key), "name": name,
            "aliases": al, "lat": lat, "lon": lon, "state": state, "attrs": attrs or {}, "rank": rank}


# ------------------------------------------------------------------------------------------ harvesters
def _gazetteer(url: str) -> list[dict]:
    _, body, _ = get(url, cache_hours=720, api=True)
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if r]


def _place_population() -> dict[str, int]:
    """Census population estimates for every incorporated place (GEOID -> 2024 estimate)."""
    _, body, _ = get("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/"
                     "sub-est2024.csv", cache_hours=720, api=True)
    pop = {}
    for r in csv.DictReader(io.StringIO(body.decode("latin-1"))):
        if r["SUMLEV"] in ("162", "170", "172"):
            pop[r["STATE"] + r["PLACE"]] = max(pop.get(r["STATE"] + r["PLACE"], 0), int(r["POPESTIMATE2024"] or 0))
    return pop


# A one-word short form that is an everyday or landscape word is never a place alias: "Lake City" is not "Lake"
# (whose "<short> <state>" form, "lake michigan", named Lake City MI for the lake), "Mountain City" is not the
# "mountain" of "Mammoth Mountain", "Atlantic City" is not the ocean. Reviewed from every one-word short form the
# 2024 gazetteer yields (the lowercase words of a dictionary among them, minus names people do say alone:
# Butte, Anaconda, Bessemer, Calumet...). The full name, with and without its state, always stays.
SHORT_FORM_STOP = frozenset("""
atlantic basin bay beach beaver bell bird bluff boulder bridge brown buffalo bullhead canyon cascade cathedral cave
cedar cement center central chain challenge chase circle citrus clay coal coffee college commerce copper corral
cottage cotton coulee cove crescent crook cross crown crystal dale dell delta diamond dodge dow dunes eagle
electric elk elm empire fairview fall falls farmer fifty floral ford forest fountain garden gas gate golden grace
granite grant green grove hide highland hill holiday horizon iron island jersey junction king kingdom lake
lakeside league leisure liberty little lost lumber maple marathon marble marine mass mentor midland midway midwest
mill mineral mobile mound mountain national neck new north oak ocean oil orange orchard ore pacific palm panama
paramount park pearl pick pine pines plain plant pleasant plum poplar prairie promise put queen rainbow raisin
rapid rapids ray redwood reed republican rising rock rose royal rush sale saline sand security sierra silver
skyline southwest spring springs standard star sterling stone story strong sugar sun sunnyside surf tell temple
timberline top tower traverse tri tunnel twin union universal university valley west white whites willow windfall
wood""".split())


def place_short_forms(name: str) -> set[str]:
    """The shorter names people say for a Census place: "Boise City" -> "Boise", "Nashville-Davidson" ->
    "Nashville", "Louisville/Jefferson County" -> "Louisville". Never a bare state name ("Oklahoma City" keeps
    its City), a code-length fragment ("Hi-Nella" is not "Hi") or an everyday word (SHORT_FORM_STOP)."""
    short = {re.split(r"[-/]", name)[0].strip(), re.sub(r"\s+City$", "", name),
             re.sub(r"^(Urban|Village of|Town of|City of)\s+", "", name)}
    return {x for x in short if x and x != name and norm(x) not in _STATE_BY_NAME
            and not (" " not in norm(x) and (len(norm(x)) < 4 or norm(x) in SHORT_FORM_STOP))}


def _refine_place(rows: list[dict]) -> list[dict]:
    """Today's alias rules over an already harvested place table (no gazetteer pull): the aliases an older harvest
    made from a short form place_short_forms now refuses are removed (a reviewed nickname is never touched),
    the reviewed POIs are added and the coast marks are set (_add_coast: Natural Earth, cached). h_place's own
    output passes through unchanged."""
    for p in rows:
        if p["attrs"].get("type") == "area":
            continue
        sname = US_STATES.get(p["state"], p["state"])
        old = {x for x in (re.split(r"[-/]", p["name"])[0].strip(), re.sub(r"\s+City$", "", p["name"]),
                           re.sub(r"^(Urban|Village of|Town of|City of)\s+", "", p["name"])) if x and x != p["name"]}
        drop = {norm(f"{x} {s}") for x in old - place_short_forms(p["name"]) for s in ("", p["state"], sname)}
        keep = set(p["attrs"].get("nicknames") or [])
        p["aliases"] = sorted(a for a in p["aliases"] if a not in drop or a in keep)
    _add_pois(rows)
    _add_coast(rows)
    return rows


def _add_pois(places: list[dict]) -> None:
    """Reviewed points of interest people name instead of their town (`resolvers/place_pois.json`: ski areas),
    added like a reviewed nickname: an alias of the town that serves them and listed in attrs.nicknames, so
    "snow forecast Mammoth Mountain" is Mammoth Lakes, CA on its own. Idempotent."""
    reviewed = json.loads((RES / "place_pois.json").read_text())
    for e in reviewed["entries"]:
        named = [p for p in places if p["state"] == e["state"] and norm(e["place"]) in p["aliases"]
                 and p["attrs"].get("type") != "area"]
        if not named:
            raise ValueError(f"place_pois.json: no place {e['place']!r} in {e['state']}")
        p = max(named, key=lambda p: p["attrs"].get("pop") or 0)
        said = {norm(n) for n in e["names"]}
        if clash := sorted(a for q in places if q is not p for a in q["aliases"] if a in said):
            raise ValueError(f"place_pois.json: {clash} already name another place")
        p["aliases"] = sorted({*p["aliases"], *said})
        p["attrs"]["nicknames"] = sorted({*p["attrs"].get("nicknames", []), *said})


def h_place() -> list[dict]:
    out = []
    pop = _place_population()
    gnis: dict[str, str] = {}  # GNIS feature id (the gazetteer's ANSICODE) -> GEOID, for Wikidata's nicknames
    for r in _gazetteer("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
                        "2024_Gaz_place_national.zip"):
        if r.get("ANSICODE", "").isdigit():
            gnis[str(int(r["ANSICODE"]))] = r["GEOID"]
        st, full = r["USPS"], r["NAME"]
        name = _PLACE_SUFFIX.sub("", re.sub(r"\s*\(balance\)$", "", full)).strip()
        typ = full[len(name):].strip().lower() or "place"
        sname = US_STATES.get(st, st)
        short = place_short_forms(name)
        forms = [name, *short]
        out.append(_e("place", "place", r["GEOID"], name,
                      [f"{f} {x}" for f in forms for x in (st, sname)] + sorted(short),
                      float(r["INTPTLAT"]), float(r["INTPTLONG"]), st,
                      {"type": typ, "land_km2": round(float(r["ALAND"]) / 1e6, 1), "pop": pop.get(r["GEOID"], 0)},
                      rank=round(math.log10(max(pop.get(r["GEOID"], 0), 1)), 2)))
    _add_nicknames(out, gnis)
    _add_pois(out)
    _add_coast(out)
    return out


# ------------------------------------------------------------------------------------------ place coast
# Natural Earth (public domain), pinned to a release tag so a re-run reads the same shapes
NATURAL_EARTH = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/{}.geojson"
COAST_KM = 25.0  # a place whose point is this close to a shoreline is on that shore
TIDAL_KM = 150.0  # ... an ocean shoreline only where NOAA predicts tides this close (not the St. Lawrence above tide)
GREAT_LAKES = ("Lake Superior", "Lake Michigan", "Lake Huron", "Lake Erie", "Lake Ontario", "Lake Saint Clair")
_CELL = 0.25  # degrees: the grid the shoreline points are bucketed in (0.25 deg of latitude is 27.8 km > COAST_KM)


def _ne_rings(layer: str, names: tuple[str, ...] = ()) -> list[list[list[float]]]:
    """The [lon, lat] rings and lines of a Natural Earth layer (only the features named, when names are given)."""
    status, body, _ = get(NATURAL_EARTH.format(layer), cache_hours=24 * 365, api=True)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for Natural Earth {layer}")
    out = []
    for f in json.loads(body)["features"]:
        if names and (f["properties"] or {}).get("name") not in names:
            continue
        g = f["geometry"]
        parts = {"LineString": [[g["coordinates"]]], "MultiLineString": [g["coordinates"]],
                 "Polygon": [g["coordinates"]], "MultiPolygon": g["coordinates"]}[g["type"]]
        out.extend(ring for part in parts for ring in part)
    return out


def _shore_grid(rings: list) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Every shoreline point at most ~1 km apart (long straight segments are filled in), by grid cell."""
    from .resolve import km  # resolve imports this module
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            n = max(1, int(km(y1, x1, y2, x2)))
            for k in range(n):
                lat, lon = y1 + (y2 - y1) * k / n, x1 + (x2 - x1) * k / n
                grid.setdefault((math.floor(lat / _CELL), math.floor(lon / _CELL)), []).append((lat, lon))
    return grid


def _cells(lat: float, lon: float, limit: float) -> list[tuple[int, int]]:
    """The grid cells that hold every point within limit km of (lat, lon)."""
    a, b = math.floor(lat / _CELL), math.floor(lon / _CELL)
    di = math.ceil(limit / (111.2 * _CELL))
    dj = math.ceil(limit / (111.2 * _CELL * max(math.cos(math.radians(lat)), 0.1)))
    return [(a + i, b + j) for i in range(-di, di + 1) for j in range(-dj, dj + 1)]


def _near_shore(grid: dict, lat: float, lon: float, limit: float) -> bool:
    from .resolve import km
    return any(km(lat, lon, y, x) <= limit for c in _cells(lat, lon, limit) for y, x in grid.get(c, ()))


def _on_land(bands: dict[int, list], lat: float, lon: float) -> bool:
    """Point in polygon (even-odd ray to the east) over the land rings that cross this latitude."""
    inside = False
    for (x1, y1), (x2, y2) in bands.get(math.floor(lat), ()):
        if (y1 > lat) != (y2 > lat) and x1 + (lat - y1) * (x2 - x1) / (y2 - y1) > lon:
            inside = not inside
    return inside


def _add_coast(places: list[dict]) -> None:
    """attrs.coastal: the place's point is on the ocean coast, in the sense of a source with coverage.water
    "ocean_coastal" (a marine model): within COAST_KM of Natural Earth's 10m ocean coastline, or out on the water
    itself (San Francisco's point lies off the Golden Gate), and that shoreline is tidal: a NOAA tide prediction
    station (resolvers/tide_station.jsonl) within TIDAL_KM. attrs.great_lakes: within COAST_KM of a Great Lake's
    shore (Natural Earth's 10m lakes) — a Great Lakes town is not on the ocean coast. Every place gets coastal
    true or false (the app refuses a marine source only for an explicit false); great_lakes is written only when
    true. Rivers that Natural Earth draws as coast up to the head of tide (the Potomac to Washington, the Delaware
    to Philadelphia) count as coast. Idempotent: the same shapes and stations give the same marks."""
    from .resolve import km
    coast = _shore_grid(_ne_rings("ne_10m_coastline"))
    lakes = _shore_grid(_ne_rings("ne_10m_lakes", GREAT_LAKES))
    bands: dict[int, list] = {}  # land edges by whole degree of latitude, for the "out on the water" test
    for ring in _ne_rings("ne_50m_land"):
        for e in zip(ring, ring[1:]):
            for band in range(math.floor(min(e[0][1], e[1][1])), math.floor(max(e[0][1], e[1][1])) + 1):
                bands.setdefault(band, []).append(e)
    tides = [(t["lat"], t["lon"]) for t in read_jsonl(RES / "tide_station.jsonl")]
    for p in places:
        lat, lon = p["lat"], p["lon"]
        ocean = (_near_shore(coast, lat, lon, COAST_KM)  # or on the water: a coast in reach and not on land
                 or (any(c in coast for c in _cells(lat, lon, 4 * COAST_KM)) and not _on_land(bands, lat, lon)))
        p["attrs"]["coastal"] = ocean and any(km(lat, lon, y, x) <= TIDAL_KM for y, x in tides)
        p["attrs"].pop("great_lakes", None)
        if _near_shore(lakes, lat, lon, COAST_KM):
            p["attrs"]["great_lakes"] = True


# ------------------------------------------------------------------------------------------ place nicknames
_NICKNAME_QUERY = """SELECT ?gnis ?nick WHERE {{ ?item wdt:{prop} ?nick . FILTER(LANGMATCHES(LANG(?nick), "en"))
  ?item wdt:P590 ?gnis . }}"""


def _wikidata_nicknames() -> list[tuple[str, str]]:
    """(GNIS id, nickname) from Wikidata: English short names (P1813: "NYC", "LA") and nicknames (P1449:
    "Big Apple", "NOLA") of items that carry a GNIS id (P590), the id the Census gazetteer carries as
    ANSICODE. Joining on it keeps exactly the items that ARE a Census place (not a team, county or
    neighbourhood of the same name), which is stricter than a city/town class filter."""
    out = []
    for prop in ("P1813", "P1449"):
        url = "https://query.wikidata.org/sparql?format=json&query=" + quote(_NICKNAME_QUERY.format(prop=prop))
        headers = {"Accept": "application/sparql-results+json"}
        _, body, _ = get(url, cache_hours=720, api=True, headers=headers)
        try:
            rows = json.loads(body)["results"]["bindings"]
        except ValueError:  # the query service cut the answer off at its time limit: never keep that
            uncache(url, headers)
            raise
        out += [(b["gnis"]["value"], b["nick"]["value"]) for b in rows]
    return out


def _nickname_core(nick: str) -> str:
    """The form of a Wikidata nickname people type, or "" when it would misfire as a place word.

    Kept: codes written in capitals ("NYC", "L.A.", "NOLA"), single words of 5+ letters ("Philly", "Vegas"),
    and phrases of up to three words ("Big Apple", "Mile High City"), with a leading "the" dropped (the
    matcher ignores "the" anyway). Dropped: slogans (4+ words), anything with a digit ("The 313"), short
    everyday words ("Jeff", "The Hub", "The Land"), and state names."""
    raw = re.sub(r"^\s*the\s+", "", nick.strip().strip('"'), flags=re.I)
    core = norm(raw)
    words = core.split()
    if not words or len(words) > 3 or re.search(r"\d", core) or core in _STATE_BY_NAME:
        return ""
    code = re.sub(r"[^A-Za-z]", "", raw)
    if len(words) == 1 and not (code.isupper() and 2 <= len(code) <= 5) and len(core) < 5:
        return ""
    from .resolve import _common_words
    return "" if len(words) == 1 and core in _common_words() else core


def _add_nicknames(places: list[dict], gnis: dict[str, str]) -> None:
    """Nicknames and short names become extra aliases on their place (docs/RESOLVERS.md, "Place nicknames").

    Two sources: Wikidata (filtered by ``_nickname_core``, minus the reviewed file's ``exclude`` list) and the
    reviewed ``entries`` in ``resolvers/place_nicknames.json`` for the common ones Wikidata lacks (it has no
    nickname for Chicago).
    A Wikidata nickname never shadows a real place's own name: "Frisco" is Frisco, Texas, not San Francisco,
    and "Queen City" is a town in Texas and Missouri, so neither is added."""
    reviewed = json.loads((RES / "place_nicknames.json").read_text())
    refused = {norm(n) for n in reviewed["exclude"]}
    by_key = {p["key"]: p for p in places}
    taken: set[str] = {a for p in places for a in p["aliases"]}
    for gid, nick in _wikidata_nicknames():
        p = by_key.get(gnis.get(gid, ""))
        core = _nickname_core(nick)
        if p and core and core not in taken and norm(nick) not in refused:
            p["aliases"] = sorted({*p["aliases"], core})
    for e in reviewed["entries"]:
        named = [p for p in places if p["state"] == e["state"] and norm(e["place"]) in p["aliases"]]
        if not named:
            raise ValueError(f"place_nicknames.json: no place {e['place']!r} in {e['state']}")
        p = max(named, key=lambda p: p["attrs"].get("pop") or 0)
        p["aliases"] = sorted({*p["aliases"], *(norm(n) for n in e["nicknames"])})
        # reviewed nicknames are deliberate place names: the app takes "Tahoe weather" as a place without "in"
        p["attrs"]["nicknames"] = sorted({*p["attrs"].get("nicknames", []), *(norm(n) for n in e["nicknames"])})
    for pt in reviewed.get("points", []):  # an area with no Census place in it: its own entry at a representative point
        places.append(_e("place", "place", "area-" + norm(pt["name"]).replace(" ", "-"), pt["name"], pt["aliases"],
                         pt["lat"], pt["lon"], pt["state"],
                         {"type": "area", "pop": 0, "nicknames": sorted(norm(a) for a in pt["aliases"])}))


def h_zip() -> list[dict]:
    return [_e("zip", "zip", r["GEOID"], r["GEOID"], [], float(r["INTPTLAT"]), float(r["INTPTLONG"]))
            for r in _gazetteer("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
                                "2024_Gaz_zcta_national.zip")]


def h_county() -> list[dict]:
    out = []
    for r in _gazetteer("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
                        "2024_Gaz_counties_national.zip"):
        st = r["USPS"]
        base = re.sub(r"\s+(County|Parish|Borough|Census Area|Municipality|city)$", "", r["NAME"])
        out.append(_e("county", "county", r["GEOID"], r["NAME"], [f"{base} county {st}", f"{r['NAME']} {st}"],
                      float(r["INTPTLAT"]), float(r["INTPTLONG"]), st))
    return out


# Weekly retail gasoline price areas (EIA's Gasoline and Diesel Fuel Update, eia.gov/petroleum/gasdiesel): EIA
# publishes nine states on their own; every other state is priced by its PADD sub-region (PADD map:
# eia.gov/petroleum/marketing/monthly/pdf/paddmap.pdf). `eia_gas_area` is the EIA API duoarea, the state's own
# when EIA has it; `fred_gas_series` is FRED's keyless copy of the state's PADD region, given only where EIA has
# no state series (FRED mirrors the national and five PADD series, never a state).
EIA_GAS_STATES = ("CA", "CO", "FL", "MA", "MN", "NY", "OH", "TX", "WA")
_PADD = {"R1X": ("CT ME MA NH RI VT", "GASREGECW"), "R1Y": ("DE DC MD NJ NY PA", "GASREGECW"),
         "R1Z": ("FL GA NC SC VA WV", "GASREGECW"), "R20": ("IL IN IA KS KY MI MN MO NE ND SD OH OK TN WI", "GASREGMWW"),
         "R30": ("AL AR LA MS NM TX", "GASREGGCW"), "R40": ("CO ID MT UT WY", "GASREGRMW"),
         "R5XCA": ("AK AZ HI NV OR WA", "GASREGWCW"), "R50": ("CA", "GASREGWCW")}


def _gas_attrs(st: str) -> dict:
    for area, (states, fred) in _PADD.items():
        if st in states.split():
            return ({"eia_gas_area": f"S{st}"} if st in EIA_GAS_STATES
                    else {"eia_gas_area": area, "fred_gas_series": fred})
    return {}


def h_us_state() -> list[dict]:
    """The fixed list, with each state's FIPS code (the US Drought Monitor's area id) and its bounding box
    (a regional USGS earthquake query), from the Census TIGERweb state boundaries (public domain)."""
    d = get_json("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0/query"
                 "?where=1%3D1&outFields=STUSAB,GEOID&returnGeometry=true&maxAllowableOffset=0.01"
                 "&geometryPrecision=3&outSR=4326&f=json", cache_hours=720, api=True)
    attrs = {}
    for f in d["features"]:
        pts = [p for ring in f["geometry"]["rings"] for p in ring]
        lats, lons = [p[1] for p in pts], [p[0] for p in pts]
        if max(lons) - min(lons) > 180:  # Alaska's Aleutians cross the date line: west of it counts below -180
            lons = [x - 360 if x > 0 else x for x in lons]
        attrs[f["attributes"]["STUSAB"]] = {"fips": f["attributes"]["GEOID"], "min_lat": min(lats),
                                            "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)}
    return [_e("us_state", "us_state", k, v, [k], state=k, attrs={**(attrs.get(k) or {}), **_gas_attrs(k)})
            for k, v in US_STATES.items()]


def h_airport() -> list[dict]:
    _, body, _ = get("https://davidmegginson.github.io/ourairports-data/airports.csv", cache_hours=720, api=True)
    out = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))):
        if r["iso_country"] != "US" or r["type"] not in ("large_airport", "medium_airport", "small_airport"):
            continue
        iata, icao = r.get("iata_code", "").strip(), (r.get("icao_code") or r.get("gps_code") or "").strip()
        if not (iata or icao):
            continue
        st = r["iso_region"].split("-")[-1]
        muni = r.get("municipality", "")
        # what people call it: "O'Hare", "Hartsfield-Jackson", "Logan" — the name minus its city and the
        # generic words, plus every keyword OurAirports lists
        core = re.sub(r"\b(international|intl|regional|municipal|county|airport|airfield|field|air|terminal)\b",
                      " ", r["name"], flags=re.I)
        for word in norm(muni).split():  # the city's own words, anywhere in the name
            core = re.sub(rf"\b{re.escape(word)}\b", " ", core, flags=re.I)
        # a hub is often called by its town ("Dulles"): offered for large/medium airports, size breaks ties
        town = muni if muni and r["type"] in ("large_airport", "medium_airport") else ""
        aliases = [a for a in (iata, icao, f"{muni} airport" if muni else "", core.strip(), town,
                               *r.get("keywords", "").split(",")) if a and len(norm(a)) > 2]
        out.append(_e("airport", "airport", iata or icao, r["name"], aliases, float(r["latitude_deg"]),
                      float(r["longitude_deg"]), st, {"iata": iata, "icao": icao, "type": r["type"], "city": muni},
                      rank={"large_airport": 3, "medium_airport": 2}.get(r["type"], 1)))
    return out


PRIMARY_TIDE_KM = 50  # a secondary station this close to a primary one is left out (the primary answers there)


def h_tide_station() -> list[dict]:
    """NOAA's tide prediction stations, primary stations first: a city resolves to the nearest station, so a
    city ringed by small creek stations (Savannah: 1-ft tides up the Ogeechee) would never reach the station
    that serves it (Fort Pulaski, 7-8 ft). A station is PRIMARY when other stations' predictions are made from
    it (a reference station) or when it is an active water-level station (it also measures water temperature
    and levels). Secondary stations are kept only where no primary station is within PRIMARY_TIDE_KM, so the
    coast keeps its coverage (the tides policy searches max_km >= 30 + PRIMARY_TIDE_KM)."""
    from .resolve import km  # resolve imports this module
    base = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type="
    d = get_json(base + "tidepredictions&expand=tidepredoffsets", cache_hours=720, api=True)
    levels = {s["id"] for s in get_json(base + "waterlevels", cache_hours=720, api=True)["stations"]}
    refs = {s.get("reference_id") for s in d["stations"] if s.get("type") == "S"}
    primary = [s for s in d["stations"] if s["id"] in refs or s["id"] in levels]
    out = []
    for s in d["stations"]:
        lat, lon = float(s["lat"]), float(s["lng"])
        is_primary = s["id"] in refs or s["id"] in levels
        if not is_primary and min(km(lat, lon, float(p["lat"]), float(p["lng"])) for p in primary) <= PRIMARY_TIDE_KM:
            continue
        name = s.get("name", "")
        place, _, water = name.partition(",")
        out.append(_e("tide_station", "station", s["id"], name, [place], lat, lon,
                      s.get("state") or "", {"water": water.strip(), "place": place.strip(),
                                             "type": s.get("type", ""), "primary": is_primary},
                      rank=3 if is_primary else 2 if s.get("type") == "R" else 1))
    return out


# Named US lakes, bays, sounds and gulfs, so "Lake Michigan water temp Milwaukee" reads a lake and a city, not
# Lake City MI. The point is a representative one on the water (for a near() search), not a centroid. A body whose
# name is also a Census place or a reviewed place nickname (Green Bay, Lake George, Buzzards Bay, Lake Tahoe) is
# left out by h_water_body: the place keeps its name. Tampa Bay is left out too: it names teams and a metro.
WATER_BODIES = [
    ("Lake Superior", "lake", 47.7, -87.5, []), ("Lake Michigan", "lake", 44.0, -87.0, []),
    ("Lake Huron", "lake", 44.8, -82.4, []), ("Lake Erie", "lake", 42.2, -81.2, []),
    ("Lake Ontario", "lake", 43.7, -77.9, []), ("Lake St. Clair", "lake", 42.45, -82.68, ["lake saint clair"]),
    ("Lake Champlain", "lake", 44.53, -73.33, []), ("Great Salt Lake", "lake", 41.1, -112.5, []),
    ("Lake Okeechobee", "lake", 26.95, -80.8, []), ("Lake Pontchartrain", "lake", 30.18, -90.1, []),
    ("Lake Mead", "lake", 36.25, -114.4, []), ("Lake Powell", "lake", 37.07, -111.24, []),
    ("Lake Tahoe", "lake", 39.09, -120.04, []), ("Lake Winnebago", "lake", 44.0, -88.42, []),
    ("Lake Washington", "lake", 47.62, -122.26, []), ("Flathead Lake", "lake", 47.88, -114.1, []),
    ("Lake Lanier", "lake", 34.2, -83.95, ["lake sidney lanier"]),
    ("Chesapeake Bay", "bay", 38.1, -76.2, []), ("Delaware Bay", "bay", 39.1, -75.2, []),
    ("San Francisco Bay", "bay", 37.7, -122.28, ["sf bay"]), ("Monterey Bay", "bay", 36.8, -121.95, []),
    ("Galveston Bay", "bay", 29.5, -94.85, []), ("Mobile Bay", "bay", 30.45, -88.0, []),
    ("Biscayne Bay", "bay", 25.6, -80.2, []), ("Cape Cod Bay", "bay", 41.85, -70.3, []),
    ("Massachusetts Bay", "bay", 42.35, -70.7, []), ("Narragansett Bay", "bay", 41.6, -71.35, []),
    ("Santa Monica Bay", "bay", 33.9, -118.6, []), ("San Diego Bay", "bay", 32.67, -117.15, []),
    ("Saginaw Bay", "bay", 43.9, -83.5, []), ("Grand Traverse Bay", "bay", 44.9, -85.55, []),
    ("Puget Sound", "sound", 47.6, -122.45, []), ("Long Island Sound", "sound", 41.1, -72.9, []),
    ("Gulf of Mexico", "gulf", 25.0, -90.0, ["gulf of america"]), ("Gulf of Maine", "gulf", 43.0, -68.5, []),
]


def h_water_body() -> list[dict]:
    """The reviewed list above, minus any body whose name or alias a place already has (run after `place`)."""
    taken = {a for p in read_jsonl(RES / "place.jsonl") for a in p["aliases"]}
    out = []
    for name, typ, lat, lon, alts in WATER_BODIES:
        e = _e("water_body", "place", norm(name).replace(" ", "-"), name, alts, lat, lon, attrs={"type": typ})
        if not set(e["aliases"]) & taken:
            out.append(e)
    return out


# NDBC's latest observation from every reporting station: a station missing here reports nothing now
NDBC_LATEST = "https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt"


def buoy_measures(head: list[str], row: list[str]) -> list[str]:
    """The columns a latest_obs.txt row has a reading in ("MM" is missing), after the station, position and time."""
    return [c for c, v in zip(head[8:], row[8:]) if v != "MM"]


def h_buoy() -> list[dict]:
    """NDBC stations that report now, keyed by the id exactly as NDBC writes it (realtime2/<ID>.txt is
    case-sensitive: a lowercase C-MAN id is a 404), each with the columns it reports (attrs.measures: WTMP water
    temperature, WVHT wave height...), so a fill can ask for the nearest station that measures the thing asked.
    A station with no realtime2 file (the Korean 221xx buoys are in latest_obs only) is dropped too: the
    source reads realtime2/<ID>.txt."""
    _, body, _ = get("https://www.ndbc.noaa.gov/activestations.xml", cache_hours=720, api=True)
    meta = {s.attrib["id"].upper(): s.attrib for s in ElementTree.fromstring(body).findall("station")}
    _, body, _ = get("https://www.ndbc.noaa.gov/data/realtime2/", cache_hours=12, api=True)
    realtime = set(re.findall(r'href="([A-Za-z0-9]+)\.txt"', body.decode("latin-1")))
    _, body, _ = get(NDBC_LATEST, cache_hours=12, api=True)
    lines = body.decode("latin-1").splitlines()
    head = lines[0].lstrip("#").split()
    out = []
    for line in lines[1:]:
        row = line.split()
        if line.startswith("#") or len(row) != len(head):
            continue
        sid, measures = row[0], buoy_measures(head, row)
        a = meta.get(sid.upper(), {})
        if not measures or sid not in realtime:
            continue
        out.append(_e("buoy", "station", sid, a.get("name", sid), [sid], float(a.get("lat", row[1])),
                      float(a.get("lon", row[2])), "",
                      {"owner": a.get("owner", ""), "type": a.get("type", ""), "met": a.get("met", ""),
                       "measures": measures}))
    return out


def _sp500() -> dict[str, str]:
    """S&P 500 members -> the common company name (open data, PDDL)."""
    _, body, _ = get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                     cache_hours=168, api=True)
    return {r["Symbol"].replace(".", "-"): (r["Security"], r.get("CIK", "")) for r in csv.DictReader(io.StringIO(body.decode()))}


_FIRST_WORD_STOP = {"american", "first", "united", "general", "national", "global", "new", "international",
                    "the", "royal", "western", "eastern", "southern", "northern", "pacific", "capital", "invesco",
                    "ishares", "vanguard", "spdr", "direxion", "proshares", "global", "select"}


# words that name a market, an index or an exchange, never one company: "how's the Nasdaq doing" is the index, not
# Nasdaq, Inc.; "the market" is not Market Technology Acquisition. A company keeps its full name ("nasdaq inc")
# and its symbol (the matcher wants a short symbol typed like a code: "DOW" is Dow Inc., "dow" alone is not).
TICKER_STOP = frozenset({"nasdaq", "market", "markets", "dow", "dow jones", "russell", "exchange", "stock exchange",
                         "nyse", "amex", "cboe", "stock", "stocks", "index", "composite", "s and p", "futures",
                         "wall street"})


def _ticker_aliases(sym: str, aliases: list[str]) -> list[str]:
    return [a for a in aliases if norm(a) not in TICKER_STOP or norm(a) == norm(sym)]


def _refine_ticker(rows: list[dict]) -> list[dict]:
    """TICKER_STOP over an already harvested ticker table (no network); h_ticker's own output is unchanged."""
    for r in rows:
        r["aliases"] = _ticker_aliases(r["key"], r["aliases"])
    return rows


def h_ticker() -> list[dict]:
    sp = _sp500()
    out = []
    for url, exch in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "NASDAQ"),
                      ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", None)):
        _, body, _ = get(url, cache_hours=168, api=True)
        for r in csv.DictReader(io.StringIO(body.decode("utf-8", "replace")), delimiter="|"):
            sym = r.get("Symbol") or r.get("ACT Symbol") or ""
            if not sym or sym.startswith("File Creation") or r.get("Test Issue") == "Y":
                continue
            name = re.sub(r"\s+-\s+.*$", "", r.get("Security Name", "")).strip()
            short = re.sub(r"\b(inc|corp|corporation|incorporated|co|ltd|plc|holdings?|class [a-z]|common stock|"
                           r"ordinary shares|the)\b\.?", " ", name, flags=re.I)
            common, cik = sp.get(sym.replace(".", "-"), ("", ""))
            first = norm(common or short).split()[:1]
            aliases = _ticker_aliases(sym, [sym, short, common] + (
                [first[0]] if first and len(first[0]) >= 4 and first[0] not in _FIRST_WORD_STOP else []))
            big = bool(common) or r.get("Market Category") == "Q"
            out.append(_e("ticker", "ticker", sym, name, aliases, attrs={
                "exchange": exch or {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX",
                                     "V": "IEX"}.get(r.get("Exchange", ""), r.get("Exchange", "")),
                "etf": r.get("ETF") == "Y", "sp500": bool(common), "cik": cik.zfill(10) if cik else ""},
                rank=(3 if common else 2 if big else 1) if r.get("ETF") != "Y" else 1))
    return out


def h_crypto() -> list[dict]:
    coins = get_json("https://api.coingecko.com/api/v3/coins/list", cache_hours=168, api=True)
    top = get_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc"
                   "&per_page=250&page=1", cache_hours=168, api=True)
    rank = {c["id"]: 1000 - (c.get("market_cap_rank") or 999) for c in top}
    return [_e("crypto", "crypto_asset", c["id"], c["name"], [c["symbol"]],
               attrs={"symbol": c["symbol"].upper()} if c.get("symbol") and " " not in c["symbol"] else {},
               rank=rank.get(c["id"], 0))
            for c in coins if c.get("id") and c.get("name")]


def h_currency() -> list[dict]:
    return [_e("currency", "currency_pair", code, name, [code, alias], rank=len(CURRENCIES) - i)
            for i, (code, name, alias) in enumerate(CURRENCIES)]


# precious metals priced per troy ounce (gold-api.com symbols; copper is priced per pound, so it is left out)
METALS = [("XAU", "Gold", ["xau"]), ("XAG", "Silver", ["xag"]), ("XPT", "Platinum", ["xpt"]),
          ("XPD", "Palladium", ["xpd"])]


def h_metal() -> list[dict]:
    return [_e("metal", "commodity", code, name, alts, rank=len(METALS) - i)
            for i, (code, name, alts) in enumerate(METALS)]


# Launch Library 2 location ids (lldev.thespacedevs.com/2.3.0/locations/) for the US launch sites people name
LAUNCH_SITES = [("12,27", "Cape Canaveral and Kennedy Space Center (Florida)",
                 ["cape canaveral", "canaveral", "kennedy space center", "kennedy", "ksc", "space coast", "florida"]),
                ("11", "Vandenberg (California)", ["vandenberg", "vandenberg sfb", "california"]),
                ("143", "Starbase (Texas)", ["starbase", "boca chica", "texas"]),
                ("21", "Wallops (Virginia)", ["wallops", "wallops island", "virginia"])]
# Launch Library 2 launch service provider ids (lldev.thespacedevs.com/2.3.0/agencies/)
LAUNCH_PROVIDERS = [("121", "SpaceX", ["space x", "falcon 9", "falcon heavy", "starship"]),
                    ("147", "Rocket Lab", ["rocketlab", "electron rocket"]),
                    ("124", "United Launch Alliance", ["ula", "vulcan", "atlas v"]),
                    ("141", "Blue Origin", ["new glenn", "new shepard"]),
                    ("265", "Firefly Aerospace", ["firefly"])]


def h_launch_site() -> list[dict]:
    return [_e("launch_site", "station", key, name, alts, rank=1) for key, name, alts in LAUNCH_SITES]


def h_launch_provider() -> list[dict]:
    return [_e("launch_provider", "organization", key, name, alts, rank=1) for key, name, alts in LAUNCH_PROVIDERS]


_ESPN_LEAGUES = [("football", "nfl"), ("basketball", "nba"), ("baseball", "mlb"), ("hockey", "nhl"),
                 ("basketball", "wnba"), ("soccer", "usa.1"), ("football", "college-football"),
                 ("basketball", "mens-college-basketball")]


# An everyday word is never a team alias unless it is a word of the team's own name: ESPN's abbreviation "WIN"
# made "did the dbacks win" Winthrop (live 2026-10-03), "MIN" would make "min temperature" Minnesota. Reviewed
# from every team abbreviation that is a lowercase dictionary word (PHI, CHI, TOR stay: nobody says them as
# words); the nickname words people would like but that are everyday words too ("cards", "pens") are here so
# team_nicknames.json can't add them.
TEAM_ABBREV_STOP = frozenset("""
and as ash bay bell ben bent bow buck cal cam can car cat col con dart day den fair for gen gram ham hard how ill
lip law long man mass me mil mile mill min mon more most no ore pit port rad rich rid row sam sea ship tar ten van
wag wash web wide win
birds bolts canes caps cards cats guards hawks jackets pack pens snakes sox wings wolves
""".split())


def _team_aliases(rows: list[dict]) -> list[dict]:
    """TEAM_ABBREV_STOP and the reviewed nicknames (resolvers/team_nicknames.json) over a team table: a nickname
    goes to the team of its league whose aliases have its club name, as an alias and in attrs.nicknames.
    Idempotent; a club the table doesn't list is skipped (team_nhl has no NBA)."""
    reviewed = json.loads((RES / "team_nicknames.json").read_text())["entries"]
    for t in rows:
        own = set(norm(t["name"]).split())
        said = {norm(n) for e in reviewed for n in e["nicknames"]
                if e["league"] == t["attrs"].get("league") and norm(e["club"]) in t["aliases"]}
        t["aliases"] = sorted({a for a in t["aliases"] if a not in TEAM_ABBREV_STOP or a in own} | said)
        if said:
            t["attrs"]["nicknames"] = sorted({*t["attrs"].get("nicknames", []), *said})
    return rows


def _espn_teams() -> list[dict]:
    out = []
    for sport, league in _ESPN_LEAGUES:
        d = get_json(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams?limit=1000",
                     cache_hours=168, api=True)
        for t in (d.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", []):
            t = t["team"]
            out.append(_e("team_espn", "team", f"{sport}/{league}/{t['id']}", t["displayName"],
                          [t.get("abbreviation", ""), t.get("shortDisplayName", ""), t.get("nickname", ""),
                           t.get("name", ""), t.get("location", "")],
                          attrs={"sport": sport, "league": league, "espn_id": t["id"],
                                 "abbrev": t.get("abbreviation", "")},
                          rank=0 if league.startswith(("college", "mens-college")) else 1))
    return out


# ESPN league -> TheSportsDB league id, for the team ids TheSportsDB's team sources take (attrs.tsdb_id)
_TSDB_LEAGUE = {"nfl": "4391", "nba": "4387", "wnba": "4516", "mlb": "4424", "nhl": "4380", "usa.1": "4346",
                "college-football": "4479", "mens-college-basketball": "4607"}


def _add_tsdb_ids(teams: list[dict]) -> None:
    """TheSportsDB's id for each team, found by its name (searchteams.php; the free key allows 30 requests a
    minute, so one uncached search every 2.1 s) and kept only when the team found plays in the same league. A
    team that already has an id keeps it.

    A college team is listed under its school ("Alabama" = 136168, NCAA Division 1), so after ESPN's full name
    the search tries the school (the aliases the full name starts with, longest first), and a hit counts only
    when its name is one of the team's own aliases. The free search returns one team per name, often another
    sport's ("Ohio State" is the hockey team): such a team is an honest gap, never a guess. Two schools of a
    name ("Charlotte 49ers", "Charlotte Saints") find the same team: it goes to the one whose own name has
    TheSportsDB's other name for it (strTeamAlternate "49ers"); the other is left without an id."""
    import time

    from .common import _cache_path
    holder = {(t["attrs"].get("league"), t["attrs"]["tsdb_id"]): t for t in teams if t["attrs"].get("tsdb_id")}
    for t in teams:
        want = _TSDB_LEAGUE.get(t["attrs"].get("league", ""))
        if not want or t["attrs"].get("tsdb_id"):
            continue
        college = t["attrs"]["league"].startswith(("college", "mens-college"))
        # ESPN's name first, then the spellings TheSportsDB uses ("LA Clippers" -> "Los Angeles Clippers",
        # "Seattle Sounders FC" -> "Seattle Sounders"), then a college team's school
        names = [t["name"], re.sub(r"^LA ", "Los Angeles ", t["name"]), re.sub(r" FC$", "", t["name"])]
        if college:
            full = norm(t["name"])
            names += sorted((a for a in t["aliases"] if full.startswith(a + " ")), key=len, reverse=True)
        for name in dict.fromkeys(names):
            url = f"https://www.thesportsdb.com/api/v1/json/123/searchteams.php?t={quote(name.replace(' ', '_'))}"
            cached = _cache_path(url, None).exists()
            try:
                status, body, _ = get(url, cache_hours=720, api=True)
            except Exception:  # a timeout leaves this team without an id; a later run (cached) retries it
                break
            if not cached:
                time.sleep(2.1)
            if status != 200:
                raise RuntimeError(f"TheSportsDB HTTP {status} (searches so far are cached; run again later)")
            found = [x for x in (json.loads(body or b"{}") or {}).get("teams") or [] if x.get("idLeague") == want
                     and (not college or norm(x.get("strTeam") or "") in t["aliases"])]
            if found:
                key = (t["attrs"]["league"], found[0]["idTeam"])
                other = holder.get(key)
                if other is not None and other["name"] != t["name"]:
                    hit = found[0]
                    alt = set(norm(hit.get("strTeamAlternate") or "").split()) - set(norm(hit["strTeam"]).split())
                    if not alt or not alt <= set(norm(t["name"]).split()) or alt <= set(norm(other["name"]).split()):
                        break  # the team found is the other school's
                    del other["attrs"]["tsdb_id"]
                t["attrs"]["tsdb_id"] = found[0]["idTeam"]
                holder[key] = t
                break


def h_team_espn() -> list[dict]:
    try:
        out = _espn_teams()
    except RuntimeError:  # ESPN refuses SmartBrain's User-Agent since 2026-09-28: keep its last harvest
        out = read_jsonl(RES / "team_espn.jsonl")
    _add_tsdb_ids(out)
    return _team_aliases(out)


def h_team_mlb() -> list[dict]:
    d = get_json("https://statsapi.mlb.com/api/v1/teams?sportId=1", cache_hours=168, api=True)
    return _team_aliases([_e("team_mlb", "team", t["id"], t["name"],
                             [t.get("teamName", ""), t.get("abbreviation", ""), t.get("locationName", ""),
                              t.get("clubName", "")],
                             attrs={"league": "mlb", "abbrev": t.get("abbreviation", "")}, rank=1)
                          for t in d["teams"]])


def h_team_nhl() -> list[dict]:
    d = get_json("https://api-web.nhle.com/v1/standings/now", cache_hours=168, api=True)
    out = []
    for t in d.get("standings", []):
        ab = t["teamAbbrev"]["default"]
        nm = t["teamName"]["default"]
        out.append(_e("team_nhl", "team", ab, nm, [ab, t.get("teamCommonName", {}).get("default", ""),
                                                  t.get("placeName", {}).get("default", "")],
                      attrs={"league": "nhl", "abbrev": ab}, rank=1))
    return _team_aliases(out)


def h_statuspage() -> list[dict]:
    d = json.loads((RES / "statuspage_hosts.json").read_text())
    return [_e("statuspage", "service", e["host"], e["service"], [e["service"].split(" (")[0]], rank=1)
            for e in d["entries"]]


def _site(url_or_host: str) -> str:
    host = (url_or_host.split("://", 1)[-1].split("/", 1)[0]).lower()
    return host[4:] if host.startswith("www.") else host


def h_official_site() -> list[dict]:
    """A subject's own domains (attrs.domains), so a web result can be told first-party (slack-status.com for
    Slack) from an aggregator. Three open inputs, merged by name: every Library provider's url (not a project
    page on a code host: github.com/jolpica is not GitHub's), every Statuspage host, and the reviewed
    `resolvers/official_sites.json`."""
    subjects: list[dict] = []

    def add(name: str, aliases: list[str], domains: list[str]) -> None:
        said = {norm(a) for a in (name, name.split(" (")[0], *aliases) if norm(a)}
        e = next((s for s in subjects if s["said"] & said), None)
        if e is None:
            e = {"name": name, "said": set(), "domains": []}
            subjects.append(e)
        e["said"] |= said
        e["domains"] += [d for d in domains if d not in e["domains"]]

    for p in read_jsonl(ROOT / "providers" / "providers.jsonl"):
        url = p.get("url") or ""
        if url and not (_site(url) == "github.com" and url.rstrip("/").count("/") > 2):
            add(p["name"], [], [_site(url)])
    for s in json.loads((RES / "statuspage_hosts.json").read_text())["entries"]:
        add(s["service"], [], [_site(s["host"])])
    for r in json.loads((RES / "official_sites.json").read_text())["entries"]:
        add(r["name"], r["aliases"], [_site(d) for d in r["domains"]])
    return [_e("official_site", "domain", norm(s["name"]).replace(" ", "-"), s["name"], sorted(s["said"]),
               attrs={"domains": s["domains"]}, rank=1) for s in subjects]


# GeyserTimes ids (geysertimes.org/api/v5/geysers, ODbL) of the geysers with predictions, and the NPS ids of the
# six the park predicts (the nps-yell carto table's npmap_id)
GEYSERS = {"2": "ee5bf30e-4594-4bc0-91ce-452b603743ea", "5": "56165001-1957-4b70-8e02-9d09ad268fb0",
           "13": "9e28b793-f2c1-440f-a39f-3e62b74e2c9a", "4": "590211c7-8dd7-44be-83c3-88aa867a75c0",
           "7": "bf68ab69-5568-48c2-97d0-7debe3f30627", "16": "bdcc6284-9e03-4ee4-a98b-80413474ff96",
           "1": "", "3": "", "10": "", "14": "", "15": "", "163": "", "8": ""}


def h_geyser() -> list[dict]:
    """Yellowstone's predicted geysers, named "<name> Geyser" (never the bare name: "Grand", "Castle", "Lion" are
    words) plus "Old Faithful", with attrs.gt_id (GeyserTimes) and attrs.nps_id where the park predicts it."""
    d = get_json("https://www.geysertimes.org/api/v5/geysers", cache_hours=720, api=True)
    by_id = {str(g["id"]): g for g in d["geysers"]}
    out = []
    for gt, nps in GEYSERS.items():
        g = by_id[gt]
        attrs = {"gt_id": gt, **({"nps_id": nps} if nps else {}), "basin": g.get("groupName", "")}
        out.append(_e("geyser", "station", gt, f"{g['name']} Geyser", ["Old Faithful"] if gt == "2" else [],
                      float(g["latitude"]), float(g["longitude"]), "WY", attrs, rank=2 if nps else 1))
    return out


def h_local_news() -> list[dict]:
    """Each metro's local newsrooms from the reviewed list: one entry per outlet feed, named by the outlet and
    found by the names people call the metro; the first-listed outlet ranks first."""
    d = json.loads((RES / "local_news_feeds.json").read_text())
    out = []
    for m in d["entries"]:
        slug = norm(m["metro"]).replace(" ", "-")
        for i, o in enumerate(m["outlets"]):
            e = _e("local_news", "feed", o["feed"], o["name"], m["names"], state=m["state"],
                   attrs={"metro": m["metro"]}, rank=len(m["outlets"]) - i)
            e["aliases"] = sorted({norm(n) for n in m["names"]})  # the metro's names, not the outlet's
            e["id"] = f"local_news:{slug}:{i + 1}"
            out.append(e)
    return out


def h_fr_agency() -> list[dict]:
    d = get_json("https://www.federalregister.gov/api/v1/agencies", cache_hours=720, api=True)
    return [_e("fr_agency", "agency", a["slug"], a["name"], [a.get("short_name") or "", a.get("display_name") or ""],
               rank=1 if not a.get("parent_id") else 0) for a in d if a.get("slug")]


def h_spending_agency() -> list[dict]:
    d = get_json("https://api.usaspending.gov/api/v2/references/toptier_agencies/", cache_hours=720, api=True)
    return [_e("spending_agency", "agency", a["toptier_code"], a["agency_name"], [a.get("abbreviation") or ""],
               rank=1) for a in d["results"] if a.get("toptier_code")]


def h_radar_site() -> list[dict]:
    d = get_json("https://api.weather.gov/radar/stations", cache_hours=720, api=True)
    out = []
    for f in d["features"]:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"][:2]
        if p.get("stationType") != "WSR-88D":
            continue
        out.append(_e("radar_site", "station", p["id"], p["name"], [p["id"]], float(lat), float(lon),
                      attrs={"type": p.get("stationType", "")}))
    return out


# (code, name, the other names people say: each its own alias; never a bare "euro", which is the currency)
SOCCER = [("PL", "Premier League", ["epl", "english premier league"]),
          ("CL", "UEFA Champions League", ["champions league", "ucl"]),
          ("PD", "La Liga", ["laliga", "spanish league", "primera division"]),
          ("BL1", "Bundesliga", ["german bundesliga"]), ("SA", "Serie A", ["italian serie a"]),
          ("FL1", "Ligue 1", ["french ligue 1"]), ("DED", "Eredivisie", ["dutch eredivisie"]),
          ("PPL", "Primeira Liga", ["portuguese liga"]), ("ELC", "EFL Championship", ["english championship"]),
          ("BSA", "Brasileirao", ["brazilian serie a"]), ("EC", "European Championship", ["euros", "euro championship"]),
          ("WC", "FIFA World Cup", ["world cup"])]


def h_soccer_competition() -> list[dict]:
    return [_e("soccer_competition", "league", code, name, [code, *alts], rank=len(SOCCER) - i)
            for i, (code, name, alts) in enumerate(SOCCER)]


# TheSportsDB league ids (lookupleague.php) with the names people use; the harvest confirms each id live
SPORTS_LEAGUES = [
    ("4391", "nfl", "football", ["nfl", "national football league", "pro football"]),
    ("4387", "nba", "basketball", ["nba", "national basketball association", "pro basketball"]),
    ("4424", "mlb", "baseball", ["mlb", "major league baseball"]),
    ("4380", "nhl", "hockey", ["nhl", "national hockey league", "pro hockey"]),
    ("4479", "college-football", "football", ["college football", "ncaa football", "ncaaf", "cfb", "fbs"]),
    ("4607", "mens-college-basketball", "basketball",
     ["college basketball", "ncaa basketball", "ncaab", "mens college basketball", "march madness"]),
    ("4516", "wnba", "basketball", ["wnba", "womens national basketball association"]),
    ("4346", "mls", "soccer", ["mls", "major league soccer"]),
    ("4521", "nwsl", "soccer", ["nwsl", "national womens soccer league"]),
    ("4328", "premier-league", "soccer", ["premier league", "english premier league", "epl", "prem"]),
    ("4480", "champions-league", "soccer", ["champions league", "uefa champions league", "ucl"]),
    ("4335", "la-liga", "soccer", ["la liga", "laliga", "spanish league"]),
    ("4331", "bundesliga", "soccer", ["bundesliga", "german bundesliga"]),
    ("4332", "serie-a", "soccer", ["serie a", "italian serie a"]),
    ("4334", "ligue-1", "soccer", ["ligue 1", "french ligue 1"]),
]


def h_sports_league() -> list[dict]:
    out = []
    for i, (lid, slug, sport, said) in enumerate(SPORTS_LEAGUES):
        d = get_json(f"https://www.thesportsdb.com/api/v1/json/123/lookupleague.php?id={lid}", cache_hours=720,
                     api=True)
        lg = (d.get("leagues") or [{}])[0]
        if str(lg.get("idLeague")) != lid:
            raise RuntimeError(f"TheSportsDB league {lid} is gone")
        # the provider's own alternates, when they name a league and not a country or a word ("Women")
        alts = [a.strip() for a in (lg.get("strLeagueAlternate") or "").split(",")
                if len(a.split()) >= 2 and a.isascii()]
        # the provider keeps league tables for soccer leagues only (lookuptable.php), and none for the Champions
        # League's league phase (empty body, 2026-09-29): those leagues have no table id
        table = {"table_id": lid} if sport == "soccer" and slug != "champions-league" else {}
        out.append(_e("sports_league", "league", lid, lg.get("strLeague") or slug, [*said, *alts],
                      attrs={"sport": sport, "league": slug, **table}, rank=len(SPORTS_LEAGUES) - i))
    return out


def h_kraken_pair() -> list[dict]:
    d = get_json("https://api.kraken.com/0/public/AssetPairs", cache_hours=720, api=True)
    out = []
    for code, p in d["result"].items():
        ws = p.get("wsname", "")
        if not ws.endswith("/USD"):
            continue
        base = ws.split("/")[0].replace("XBT", "BTC")
        out.append(_e("kraken_pair", "crypto_asset", code, f"{base}/USD", [base, f"{base} usd"],
                      attrs={"base": base}, rank=1))
    return out


def h_nwps_gauge() -> list[dict]:
    gauges: dict[str, dict] = {}
    for x0 in range(-180, -60, 10):  # 10-degree longitude tiles: the whole-US request times out
        d = get_json(f"https://api.water.noaa.gov/nwps/v1/gauges?bbox.xmin={x0}&bbox.ymin=15&bbox.xmax={x0 + 10}"
                     f"&bbox.ymax=72&srid=EPSG_4326", cache_hours=720, api=True)
        for g in d.get("gauges", []):
            if g.get("lid") and g.get("latitude") is not None:
                gauges[g["lid"]] = g
    return [_e("nwps_gauge", "station", g["lid"], g.get("name", g["lid"]), [g["lid"]], g.get("latitude"),
               g.get("longitude"), (g.get("state") or {}).get("abbreviation", ""),
               {"river": (g.get("name") or "").split(" at ")[0]}) for g in gauges.values()]


HARVEST = {"place": h_place, "zip": h_zip, "county": h_county, "us_state": h_us_state, "airport": h_airport,
           "tide_station": h_tide_station, "buoy": h_buoy, "ticker": h_ticker, "crypto": h_crypto,
           "currency": h_currency, "team_espn": h_team_espn, "team_mlb": h_team_mlb, "team_nhl": h_team_nhl,
           "metal": h_metal, "launch_site": h_launch_site, "launch_provider": h_launch_provider,
           "statuspage": h_statuspage, "local_news": h_local_news, "fr_agency": h_fr_agency,
           "spending_agency": h_spending_agency,
           "radar_site": h_radar_site, "soccer_competition": h_soccer_competition, "kraken_pair": h_kraken_pair,
           "sports_league": h_sports_league,
           "nwps_gauge": h_nwps_gauge, "water_body": h_water_body, "official_site": h_official_site,
           "geyser": h_geyser}

# today's rules over the committed table, without re-pulling the upstream list (a targeted, reproducible
# regeneration: each function is what the harvester itself applies, so a full harvest gives the same rows)
REFINE = {"place": _refine_place, "ticker": _refine_ticker,
          "team_espn": lambda rows: (_add_tsdb_ids(rows), _team_aliases(rows))[1],  # ESPN refuses us
          "team_mlb": _team_aliases, "team_nhl": _team_aliases}


def harvest(names: list[str] | None = None, refine: bool = False) -> dict[str, int]:
    """Rebuild resolver tables (all by default). With refine, a table in REFINE is rebuilt from its committed
    rows by today's rules instead of from its upstream source."""
    counts = {}
    for name in names or list(HARVEST):
        try:
            rows = REFINE[name](read_jsonl(RES / f"{name}.jsonl")) if refine and name in REFINE else HARVEST[name]()
        except Exception as exc:  # a provider that refuses or rate-limits us: keep the old table, report it
            counts[name] = f"not harvested ({type(exc).__name__}: {str(exc)[:80]})"
            continue
        uniq = {r["id"]: r for r in rows}
        write_jsonl(RES / f"{name}.jsonl", list(uniq.values()))
        counts[name] = len(uniq)
    return counts
