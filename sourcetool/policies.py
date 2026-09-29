"""Source policy for EVERY subcategory: whom to trust, how fresh, how the parameters must match, when to
cross-check and when to ask. `python -m sourcetool policies` writes them into taxonomy/taxonomy.json;
`sourcetool check` refuses a taxonomy where any subcategory lacks one.

Fields
  prefer      authority order, most trusted first (ranking weights a source by its place in this list)
  match       "geo" (the entity is a place: resolve place -> nearest entry), "name" (resolve by name),
              or "none" (no entity: a feed, a national series)
  resolvers   the resolver tables the subcategory's parameters use, in order (place first for geo)
  max_km      geo only: farther than this is not "the same place" (an honest miss, never a stretch)
  differ_on   geo only: attributes that make two nearby entries different answers (then ask)
  max_age     how old the data may be before the card calls itself stale ("15m", "1h", "1d", "7d", "40d", "1y")
  cross_check a numeric headline is compared with a second source when the card is built
"""
from __future__ import annotations

import json

from .common import TAXONOMY

OFFICIAL = ["official", "primary", "aggregator", "community"]
PRIMARY = ["primary", "official", "aggregator", "community"]

# category defaults
DEFAULTS = {
    "weather":  {"prefer": OFFICIAL, "match": "geo", "resolvers": ["place"], "max_km": 40, "max_age": "1h"},
    "hazards":  {"prefer": OFFICIAL, "match": "geo", "resolvers": ["place", "us_state"], "max_km": 200,
                 "max_age": "1h"},
    "water":    {"prefer": OFFICIAL, "match": "geo", "resolvers": ["place"], "max_km": 30, "max_age": "1h",
                 "differ_on": ["water"]},
    "sky":      {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_age": "1d"},
    "markets":  {"prefer": PRIMARY, "match": "name", "resolvers": ["ticker"], "max_age": "15m", "cross_check": True},
    "economy":  {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_age": "40d"},
    "energy":   {"prefer": OFFICIAL, "match": "geo", "resolvers": ["us_state"], "max_age": "1d"},
    "sports":   {"prefer": PRIMARY, "match": "name",
                 "resolvers": ["team_mlb", "team_nhl", "team_espn", "sports_league"], "max_age": "15m"},
    "news":     {"prefer": PRIMARY, "match": "none", "resolvers": [], "max_age": "1h"},
    "travel":   {"prefer": OFFICIAL, "match": "name", "resolvers": ["airport"], "max_age": "15m"},
    "health":   {"prefer": OFFICIAL, "match": "none", "resolvers": ["us_state"], "max_age": "7d"},
    "civic":    {"prefer": OFFICIAL, "match": "geo", "resolvers": ["place", "county", "us_state"], "max_km": 50,
                 "max_age": "1d"},
    "tech":     {"prefer": PRIMARY, "match": "name", "resolvers": [], "max_age": "15m"},
    "culture":  {"prefer": PRIMARY, "match": "none", "resolvers": [], "max_age": "1d"},
    "shopping": {"prefer": PRIMARY, "match": "name", "resolvers": [], "max_age": "1d", "cross_check": True},
    "science":  {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_age": "7d"},
    "time":     {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_age": "1y"},
    "personal": {"prefer": PRIMARY, "match": "none", "resolvers": [], "max_age": "15m"},
}

# subcategory overrides (only what differs from the category default)
OVERRIDES = {
    "weather/forecast": {"max_km": 25},
    "weather/current": {"resolvers": ["place", "airport"], "max_km": 40, "cross_check": True},
    "weather/alerts": {"resolvers": ["place", "county", "us_state"], "max_age": "15m"},
    "weather/air_quality": {"resolvers": ["zip", "place"], "max_km": 25},
    "weather/pollen": {"resolvers": ["zip", "place"]},
    "weather/uv": {"resolvers": ["zip", "place"], "max_age": "1d"},
    "weather/radar_imagery": {"resolvers": ["place", "radar_site"], "max_km": 250, "max_age": "15m"},
    "weather/climate_records": {"max_age": "1d"},
    "weather/drought": {"resolvers": ["us_state", "county"], "max_age": "7d"},
    "hazards/earthquakes": {"max_km": 300, "max_age": "15m"},
    "hazards/tropical_storms": {"match": "none", "resolvers": [], "max_age": "1h"},
    "hazards/floods_rivers": {"resolvers": ["place", "nwps_gauge"], "max_km": 25, "differ_on": ["river"]},
    "hazards/volcanoes": {"match": "none", "resolvers": []},
    "hazards/tsunami": {"match": "none", "resolvers": [], "max_age": "15m"},
    "hazards/space_weather": {"match": "none", "resolvers": []},
    "hazards/emergencies": {"match": "name", "resolvers": ["us_state"], "max_age": "1d"},
    # the tide_station table keeps secondary stations only 50 km from a primary one (resolvers.h_tide_station):
    # 30 km + 50 km keeps every place that had a station within 30 km covered
    "water/tides": {"resolvers": ["place", "tide_station"], "max_km": 80, "max_age": "1d"},
    "water/water_levels": {"resolvers": ["place", "tide_station"], "max_km": 80, "max_age": "15m"},
    "water/surf_waves": {"resolvers": ["place", "buoy"], "max_km": 80, "differ_on": []},
    "water/water_temperature": {"resolvers": ["place", "tide_station", "buoy"], "max_km": 80},
    "water/marine_forecast": {"max_km": 60, "differ_on": []},
    "water/beach_quality": {"max_age": "1d", "differ_on": []},
    "water/currents": {"max_age": "1d"},
    "sky/sun_moon": {"match": "geo", "resolvers": ["place"], "max_km": 100},
    "sky/iss": {"max_age": "15m"},
    "markets/indices": {"resolvers": ["ticker"]},
    "markets/crypto": {"resolvers": ["crypto"], "max_age": "15m"},
    "markets/fx": {"resolvers": ["currency"], "max_age": "1d"},
    "markets/commodities": {"match": "none", "resolvers": [], "max_age": "1d"},
    "markets/rates_bonds": {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_age": "1d",
                            "cross_check": False},
    "markets/filings": {"prefer": OFFICIAL, "max_age": "1d", "cross_check": False},
    "markets/funds": {"max_age": "1d"},
    "markets/market_status": {"prefer": OFFICIAL, "match": "none", "resolvers": [], "cross_check": False},
    "economy/jobs": {"resolvers": ["us_state"]},
    "economy/housing": {"match": "geo", "resolvers": ["place", "us_state"], "max_km": 50},
    "economy/fuel_prices": {"match": "geo", "resolvers": ["us_state"], "max_age": "7d", "cross_check": True},
    "economy/food_prices": {"cross_check": True},
    "economy/economic_calendar": {"max_age": "7d"},
    "energy/grid": {"max_age": "1h"},
    "energy/outages": {"resolvers": ["us_state", "county"], "max_age": "1h"},
    "energy/ev_charging": {"resolvers": ["place", "zip"], "max_km": 25},
    "energy/oil_gas_supply": {"match": "none", "resolvers": [], "max_age": "7d"},
    "sports/standings": {"max_age": "1d"},
    "sports/player_stats": {"max_age": "1d"},
    "sports/college": {"resolvers": ["team_espn", "sports_league"], "max_age": "1h"},
    "sports/motorsport_golf_other": {"resolvers": ["team_espn"]},
    "news/local_news": {"match": "geo", "resolvers": ["place", "us_state"], "max_km": 80},
    # a place named with "news" asks for that place's news: a national feed is not an answer to "Seattle news"
    "news/headlines": {"match": "geo", "resolvers": ["place", "us_state"], "max_km": 80},
    "news/podcasts": {"max_age": "1d"},
    "news/fact_checks": {"max_age": "1d"},
    "travel/flights": {"prefer": PRIMARY, "resolvers": ["airport"], "max_age": "15m"},
    "travel/transit": {"match": "geo", "resolvers": ["place"], "max_km": 50, "max_age": "15m"},
    "travel/transit_alerts": {"match": "geo", "resolvers": ["place"], "max_km": 50},
    "travel/traffic_roads": {"match": "geo", "resolvers": ["place", "us_state"], "max_km": 50},
    "travel/rail": {"match": "none", "resolvers": []},
    "travel/border_waits": {"match": "none", "resolvers": [], "max_age": "1h"},
    "travel/parks_travel": {"match": "name", "resolvers": [], "max_age": "1d"},
    "travel/bikeshare": {"match": "geo", "resolvers": ["place"], "max_km": 30},
    "health/disease_surveillance": {"cross_check": True},
    "health/hospitals": {"match": "geo", "resolvers": ["place", "zip"], "max_km": 50, "max_age": "40d"},
    "health/nutrition": {"match": "name", "resolvers": [], "max_age": "1y"},
    "civic/legislation": {"match": "none", "resolvers": []},
    "civic/elections": {"match": "name", "resolvers": ["us_state", "county"]},
    "civic/courts": {"match": "none", "resolvers": []},
    "civic/regulations": {"match": "name", "resolvers": ["fr_agency"]},
    "civic/gov_contracts": {"match": "name", "resolvers": ["spending_agency"], "max_age": "7d"},
    "civic/schools": {"max_age": "1d"},
    "tech/service_status": {"resolvers": ["statuspage"], "max_age": "15m"},
    "tech/repos_releases": {"max_age": "1h"},
    "tech/packages": {"max_age": "1d"},
    "tech/security_advisories": {"prefer": OFFICIAL, "match": "none", "max_age": "1d"},
    "tech/tech_news": {"match": "none", "max_age": "1h"},
    "tech/domains_web": {"max_age": "1d"},
    "tech/ai_models": {"match": "none", "max_age": "1d"},
    "culture/events": {"match": "geo", "resolvers": ["place"], "max_km": 50},
    "culture/museums_art": {"max_age": "40d"},
    "shopping/deals": {"match": "none", "cross_check": False, "max_age": "1h"},
    "science/nature_wildlife": {"match": "geo", "resolvers": ["place"], "max_km": 25},
    "science/environment_data": {"max_age": "40d"},
    "time/time_zones": {"match": "name", "resolvers": ["place"], "max_age": "15m"},
    "time/countdowns": {"max_age": "15m"},
}

BASE = {"prefer": OFFICIAL, "match": "none", "resolvers": [], "max_km": None, "differ_on": [], "max_age": "1d",
        "cross_check": False, "ask_if_ambiguous": True}


def policy_for(cat: str, sub: str) -> dict:
    p = {**BASE, **DEFAULTS.get(cat, {}), **OVERRIDES.get(f"{cat}/{sub}", {})}
    if p["match"] != "geo":
        p["max_km"], p["differ_on"] = None, []
    return p


def apply() -> int:
    t = json.loads(TAXONOMY.read_text())
    n = 0
    for c in t["categories"]:
        for s in c["subcategories"]:
            s["policy"] = policy_for(c["id"], s["id"])
            n += 1
    unknown = set(OVERRIDES) - {f"{c['id']}/{s['id']}" for c in t["categories"] for s in c["subcategories"]}
    assert not unknown, f"overrides for unknown subcategories: {sorted(unknown)}"
    TAXONOMY.write_text(json.dumps(t, indent=1, ensure_ascii=False) + "\n")
    return n
