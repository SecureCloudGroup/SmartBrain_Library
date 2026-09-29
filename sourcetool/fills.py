"""How every source parameter gets its value — one declared `fill` per parameter, for ALL sources.

    {"from": "resolver", "resolver": "tide_station", "field": "key"}      a resolver table (resolve.py)
    {"from": "clock", "format": "%Y%m%d", "offset_days": 0}              today / a window, in the card's zone
    {"from": "vault_key"}                                                  the user's own key for this host
    {"from": "source", "source": "nws-points", "path": "properties.gridX"} another source's answer
    {"from": "text", "pattern": "..."}                                     the word(s) as the user typed them
    {"from": "default", "value": "..."}                                    a sensible default, shown on the card
    {"from": "gap", "reason": "..."}                                       no way yet — the card must ask

`python -m sourcetool fills` writes them and prints coverage; `check` refuses a curated param without one.
"""
from __future__ import annotations

import collections

from .common import SOURCES, read_jsonl, write_jsonl

R = lambda resolver, field="key", **kw: {"from": "resolver", "resolver": resolver, "field": field, **kw}  # noqa: E731
CLOCK = lambda fmt, days=0: {"from": "clock", "format": fmt, "offset_days": days}  # noqa: E731
DEFAULT = lambda v: {"from": "default", "value": v}  # noqa: E731
GAP = lambda why: {"from": "gap", "reason": why}  # noqa: E731

# (provider, param name) -> fill, for the parameters whose meaning depends on the provider
BY_PROVIDER = {
    ("coops", "station"): R("tide_station"),
    ("ndbc", "buoy"): R("buoy"),
    ("nws", "station"): R("airport", "attrs.icao"),
    ("nws", "radar"): R("radar_site", near=True),
    ("nws", "office"): {"from": "source", "source": "nws-points", "path": "properties.gridId"},
    ("nws", "grid_x"): {"from": "source", "source": "nws-points", "path": "properties.gridX"},
    ("nws", "grid_y"): {"from": "source", "source": "nws-points", "path": "properties.gridY"},
    ("nws", "zone_prefix"): GAP("no marine-zone resolver yet"),
    ("nws", "zone"): GAP("no marine-zone resolver yet"),
    ("ncei", "station"): GAP("no GHCN station resolver yet"),
    ("noaa-water", "gauge"): R("nwps_gauge", near=True),
    ("usgs", "site"): GAP("no USGS gauge resolver yet"),
    ("aviationweather", "airport"): R("airport", "attrs.icao"),
    ("coingecko", "coin"): R("crypto"),
    ("coinbase", "pair"): R("crypto", "attrs.symbol", format="{UPPER}-USD"),
    ("kraken", "pair"): R("kraken_pair"),
    ("goldapi", "metal"): R("metal"),
    # a launch site or provider the ask names narrows the list; none named = every upcoming launch (empty filter)
    ("thespacedevs", "site"): R("launch_site", fallback=DEFAULT("")),
    ("thespacedevs", "provider"): R("launch_provider", fallback=DEFAULT("")),
    ("frankfurter", "base"): R("currency", many_index=0),
    ("frankfurter", "quote"): R("currency", many_index=1),
    ("mlb", "team"): R("team_mlb"),
    ("mlb", "since"): CLOCK("%Y-%m-%d", -7),
    ("nhl", "team"): R("team_nhl"),
    ("espn", "sport"): R("team_espn", "attrs.sport", fallback=DEFAULT("football")),
    ("espn", "league"): R("team_espn", "attrs.league", fallback=DEFAULT("nfl")),
    ("football-data", "competition"): R("soccer_competition"),
    ("thesportsdb", "league_id"): R("sports_league"),
    ("thesportsdb", "table_league"): R("sports_league", "attrs.table_id"),
    ("thesportsdb", "team"): R("team_espn", "attrs.tsdb_id"),
    ("statuspage", "status_host"): R("statuspage"),
    ("local-newsrooms", "feed"): R("local_news"),  # the named metro's newsroom feeds (https://{feed})
    ("sec", "cik"): R("ticker", "attrs.cik", fallback=GAP("CIK known for S&P 500 companies only; others need SEC's mapping (contact User-Agent, R11)")),
    ("sec", "form"): DEFAULT("8-K"),
    ("eia", "ba"): GAP("no balancing-authority resolver yet"),
    # the state's own weekly gas price where EIA has one, else its PADD region; no state named: the US average
    ("eia", "area"): R("us_state", "attrs.eia_gas_area", fallback=DEFAULT("NUS")),
    ("fred", "gas_series"): R("us_state", "attrs.fred_gas_series"),  # a state EIA has no series for: its region
    ("usgs", "radius_km"): DEFAULT("200"),
    ("usgs", "min_mag"): DEFAULT("2.5"),
    ("coingecko", "days"): DEFAULT("30"),
    ("fec", "office"): DEFAULT("S"),
    ("federalregister", "agency"): R("fr_agency"),
    ("usaspending", "toptier_code"): R("spending_agency"),
    ("congress", "congress"): DEFAULT("119"),
    ("congress", "bill_type"): {"from": "text", "pattern": r"\b(hr|s|hjres|sjres)\b"},
    ("congress", "number"): {"from": "text", "pattern": r"\b(?:hr|s)\s*(\d{1,5})\b"},
    ("amtraker", "train"): {"from": "text", "pattern": r"\b(?:train|amtrak|number|no\.?|#)\s*#?\s*(\d{1,4})\b"},
    ("wikimedia", "yyyy"): CLOCK("%Y"), ("wikimedia", "mm"): CLOCK("%m"), ("wikimedia", "dd"): CLOCK("%d"),
    ("coops", "begin"): CLOCK("%Y%m%d"),
    ("usdm", "state"): R("us_state", "attrs.fips"),  # the Drought Monitor's area id is the state FIPS code
    ("usdm", "start"): CLOCK("%-m/%-d/%Y", -56), ("usdm", "end"): CLOCK("%-m/%-d/%Y"),
    # a state's bounding box: a regional USGS earthquake query ("earthquakes in Alaska")
    ("usgs", "min_lat"): R("us_state", "attrs.min_lat"), ("usgs", "max_lat"): R("us_state", "attrs.max_lat"),
    ("usgs", "min_lon"): R("us_state", "attrs.min_lon"), ("usgs", "max_lon"): R("us_state", "attrs.max_lon"),
    ("nyt", "list"): DEFAULT("hardcover-fiction"), ("nytapi", "list"): DEFAULT("hardcover-fiction"),
    ("usno", "tz"): GAP("a place's UTC offset needs a time-zone lookup (places carry no zone yet)"),
    ("cms", "city"): R("place", "name", format="{UPPER}"),
    ("ticketmaster", "city"): R("place", "name"),
    ("wttr", "place"): R("place", "name"),
    ("met", "topic"): {"from": "text"}, ("arxiv", "category"): DEFAULT("cs.AI"),
    ("dockerhub", "namespace"): DEFAULT("library"), ("dockerhub", "image"): {"from": "text"},
    ("steam", "appid"): GAP("no Steam app-id resolver yet"),
    ("apple", "app_id"): {"from": "source", "source": "itunes-app-search", "path": "results.0.trackId"},
    ("nps", "park"): GAP("no national-park-code resolver yet"),
    ("census", "variables"): GAP("no Census variable resolver yet"),
    ("fred", "series"): GAP("no FRED series resolver yet (the curated FRED sources cover the common series)"),
    ("bls", "series"): GAP("no BLS series resolver yet (the curated FRED sources cover the common series)"),
}

# param kind -> fill, when the provider rule above doesn't apply
BY_KIND = {
    "zip": R("zip"),
    "us_state": R("us_state"),
    "county": R("county"),
    "ticker": R("ticker"),
    "fund": R("ticker"),
    "crypto_asset": R("crypto"),
    "currency_pair": R("currency"),
    "airport": R("airport"),
    "team": R("team_espn"),
    "service": R("statuspage"),
    "key": {"from": "vault_key"},
    "package": {"from": "text"},
    "repo": {"from": "text", "pattern": r"([\w.-]+)/([\w.-]+)"},
    "domain": {"from": "text", "pattern": r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b"},
    "flight": {"from": "text", "pattern": r"\b([A-Z]{2,3}\d{1,4})\b"},
    "topic": {"from": "text"},
    "title": {"from": "text"},
    "product": {"from": "text"},
    "person": {"from": "text"},
    "dataset": GAP("dataset parameters need a per-portal resolver"),
}


def _table_exists(resolver: str) -> bool:
    from .resolvers import RES
    return (RES / f"{resolver}.jsonl").exists()


def fill_for(record: dict, param: dict) -> dict:
    fill = _fill_rule(record, param)
    if fill["from"] == "resolver" and not _table_exists(fill["resolver"]):
        return GAP(f"the {fill['resolver']} resolver table is not harvested yet")
    return fill


def _fill_rule(record: dict, param: dict) -> dict:
    prov = record["provider"].get("id", "")
    name, kind = param["name"], param["kind"]
    if (prov, name) in BY_PROVIDER:
        return BY_PROVIDER[(prov, name)]
    if kind == "place":
        if name in ("lat", "lon"):
            return R("place", name)
        return R("place", "name")
    if kind == "date":
        return {"year": CLOCK("%Y"), "start": CLOCK("%Y-%m-%d", -30), "end": CLOCK("%Y-%m-%d"),
                "begin": CLOCK("%Y%m%d")}.get(name, CLOCK("%Y-%m-%d"))
    if kind in BY_KIND:
        return BY_KIND[kind]
    return GAP(f"no fill rule for a {kind} parameter named {name}")


def apply() -> dict:
    stats = collections.Counter()
    gaps = collections.Counter()
    for f in sorted(SOURCES.rglob("*.jsonl")):
        rows = read_jsonl(f)
        for r in rows:
            for p in r["access"].get("params", []):
                p["fill"] = fill_for(r, p)
                stats[p["fill"]["from"]] += 1
                if p["fill"]["from"] == "gap":
                    gaps[p["fill"]["reason"]] += 1
        write_jsonl(f, rows)
    return {"by_fill": dict(stats), "gaps": dict(gaps.most_common())}
