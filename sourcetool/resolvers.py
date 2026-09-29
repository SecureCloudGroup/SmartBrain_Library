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
        # the name people say: "Boise City" -> "Boise", "Nashville-Davidson metropolitan government" -> "Nashville",
        # "Louisville/Jefferson County" -> "Louisville" (never a bare state name: "Oklahoma City" keeps its City)
        short = {re.split(r"[-/]", name)[0].strip(), re.sub(r"\s+City$", "", name),
                 re.sub(r"^(Urban|Village of|Town of|City of)\s+", "", name)}
        short = {x for x in short if x and x != name and norm(x) not in _STATE_BY_NAME}
        forms = [name, *short]
        out.append(_e("place", "place", r["GEOID"], name,
                      [f"{f} {x}" for f in forms for x in (st, sname)] + sorted(short),
                      float(r["INTPTLAT"]), float(r["INTPTLONG"]), st,
                      {"type": typ, "land_km2": round(float(r["ALAND"]) / 1e6, 1), "pop": pop.get(r["GEOID"], 0)},
                      rank=round(math.log10(max(pop.get(r["GEOID"], 0), 1)), 2)))
    _add_nicknames(out, gnis)
    return out


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


def h_us_state() -> list[dict]:
    return [_e("us_state", "us_state", k, v, [k], state=k) for k, v in US_STATES.items()]


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


def h_tide_station() -> list[dict]:
    d = get_json("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=tidepredictions",
                 cache_hours=720, api=True)
    out = []
    for s in d["stations"]:
        name = s.get("name", "")
        place, _, water = name.partition(",")
        out.append(_e("tide_station", "station", s["id"], name, [place], float(s["lat"]), float(s["lng"]),
                      s.get("state") or "", {"water": water.strip(), "place": place.strip(),
                                             "type": s.get("type", "")}, rank=2 if s.get("type") == "R" else 1))
    return out


def h_buoy() -> list[dict]:
    _, body, _ = get("https://www.ndbc.noaa.gov/activestations.xml", cache_hours=720, api=True)
    out = []
    for s in ElementTree.fromstring(body).findall("station"):
        a = s.attrib
        out.append(_e("buoy", "station", a["id"], a.get("name", a["id"]), [a["id"]], float(a["lat"]), float(a["lon"]),
                      "", {"owner": a.get("owner", ""), "type": a.get("type", ""), "met": a.get("met", "")}))
    return out


def _sp500() -> dict[str, str]:
    """S&P 500 members -> the common company name (open data, PDDL)."""
    _, body, _ = get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                     cache_hours=168, api=True)
    return {r["Symbol"].replace(".", "-"): (r["Security"], r.get("CIK", "")) for r in csv.DictReader(io.StringIO(body.decode()))}


_FIRST_WORD_STOP = {"american", "first", "united", "general", "national", "global", "new", "international",
                    "the", "royal", "western", "eastern", "southern", "northern", "pacific", "capital", "invesco",
                    "ishares", "vanguard", "spdr", "direxion", "proshares", "global", "select"}


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
            aliases = [sym, short, common] + ([first[0]] if first and len(first[0]) >= 4
                                              and first[0] not in _FIRST_WORD_STOP else [])
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
    return [_e("crypto", "crypto_asset", c["id"], c["name"], [c["symbol"]], rank=rank.get(c["id"], 0))
            for c in coins if c.get("id") and c.get("name")]


def h_currency() -> list[dict]:
    return [_e("currency", "currency_pair", code, name, [code, alias], rank=len(CURRENCIES) - i)
            for i, (code, name, alias) in enumerate(CURRENCIES)]


_ESPN_LEAGUES = [("football", "nfl"), ("basketball", "nba"), ("baseball", "mlb"), ("hockey", "nhl"),
                 ("basketball", "wnba"), ("soccer", "usa.1"), ("football", "college-football"),
                 ("basketball", "mens-college-basketball")]


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
_TSDB_LEAGUE = {"nfl": "4391", "nba": "4387", "wnba": "4516", "mlb": "4424", "nhl": "4380", "usa.1": "4346"}


def _add_tsdb_ids(teams: list[dict]) -> None:
    """TheSportsDB's id for each pro team, found by its name (searchteams.php; the free key allows 30
    requests a minute, so one search every 2.1 s) and kept only when the team found plays in the same league."""
    import time
    for t in teams:
        want = _TSDB_LEAGUE.get(t["attrs"].get("league", ""))
        if not want:
            continue
        # ESPN's name first, then the spellings TheSportsDB uses ("LA Clippers" -> "Los Angeles Clippers",
        # "Seattle Sounders FC" -> "Seattle Sounders")
        names = [t["name"], re.sub(r"^LA ", "Los Angeles ", t["name"]), re.sub(r" FC$", "", t["name"])]
        for name in dict.fromkeys(names):
            url = f"https://www.thesportsdb.com/api/v1/json/123/searchteams.php?t={quote(name.replace(' ', '_'))}"
            body = get(url, cache_hours=720, api=True)[1]
            time.sleep(2.1)
            found = [x for x in (json.loads(body or b"{}") or {}).get("teams") or [] if x.get("idLeague") == want]
            if found:
                t["attrs"]["tsdb_id"] = found[0]["idTeam"]
                break


def h_team_espn() -> list[dict]:
    try:
        out = _espn_teams()
    except RuntimeError:  # ESPN refuses SmartBrain's User-Agent since 2026-09-28: keep its last harvest
        out = read_jsonl(RES / "team_espn.jsonl")
    _add_tsdb_ids(out)
    return out


def h_team_mlb() -> list[dict]:
    d = get_json("https://statsapi.mlb.com/api/v1/teams?sportId=1", cache_hours=168, api=True)
    return [_e("team_mlb", "team", t["id"], t["name"], [t.get("teamName", ""), t.get("abbreviation", ""),
                                                        t.get("locationName", ""), t.get("clubName", "")],
               attrs={"league": "mlb", "abbrev": t.get("abbreviation", "")}, rank=1) for t in d["teams"]]


def h_team_nhl() -> list[dict]:
    d = get_json("https://api-web.nhle.com/v1/standings/now", cache_hours=168, api=True)
    out = []
    for t in d.get("standings", []):
        ab = t["teamAbbrev"]["default"]
        nm = t["teamName"]["default"]
        out.append(_e("team_nhl", "team", ab, nm, [ab, t.get("teamCommonName", {}).get("default", ""),
                                                  t.get("placeName", {}).get("default", "")],
                      attrs={"league": "nhl", "abbrev": ab}, rank=1))
    return out


def h_statuspage() -> list[dict]:
    d = json.loads((RES / "statuspage_hosts.json").read_text())
    return [_e("statuspage", "service", e["host"], e["service"], [e["service"].split(" (")[0]], rank=1)
            for e in d["entries"]]


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


SOCCER = [("PL", "Premier League", "epl english premier league"), ("CL", "UEFA Champions League", "champions league ucl"),
          ("PD", "La Liga", "laliga spanish league primera division"), ("BL1", "Bundesliga", "german bundesliga"),
          ("SA", "Serie A", "italian serie a"), ("FL1", "Ligue 1", "french ligue 1"),
          ("DED", "Eredivisie", "dutch eredivisie"), ("PPL", "Primeira Liga", "portuguese liga"),
          ("ELC", "EFL Championship", "english championship"), ("BSA", "Brasileirao", "brazilian serie a"),
          ("EC", "European Championship", "euros euro"), ("WC", "FIFA World Cup", "world cup")]


def h_soccer_competition() -> list[dict]:
    return [_e("soccer_competition", "league", code, name, [code, alt], rank=len(SOCCER) - i)
            for i, (code, name, alt) in enumerate(SOCCER)]


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
           "statuspage": h_statuspage, "fr_agency": h_fr_agency, "spending_agency": h_spending_agency,
           "radar_site": h_radar_site, "soccer_competition": h_soccer_competition, "kraken_pair": h_kraken_pair,
           "sports_league": h_sports_league,
           "nwps_gauge": h_nwps_gauge}


def harvest(names: list[str] | None = None) -> dict[str, int]:
    counts = {}
    for name in names or list(HARVEST):
        try:
            rows = HARVEST[name]()
        except Exception as exc:  # a provider that refuses or rate-limits us: keep the old table, report it
            counts[name] = f"not harvested ({type(exc).__name__}: {str(exc)[:80]})"
            continue
        uniq = {r["id"]: r for r in rows}
        write_jsonl(RES / f"{name}.jsonl", list(uniq.values()))
        counts[name] = len(uniq)
    return counts
