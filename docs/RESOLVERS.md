# Resolvers, fills and source policy

These three general mechanisms turn the words of *any* ask into a working source.

## Resolvers: words to a parameter value
A resolver is a table of entries, one per line in `resolvers/<name>.jsonl`, all in the same shape:
`{id, resolver, kind, key, name, aliases[], lat, lon, state, attrs{}, rank}`.

`python -m sourcetool resolvers` rebuilds them all from open, keyless data:

| Resolver | Entries | Open source |
|---|---|---|
| place | 32,334 | Census gazetteer plus population estimates; nicknames from Wikidata and `resolvers/place_nicknames.json` (with well-known areas); reviewed ski areas from `resolvers/place_pois.json`; coast marks from Natural Earth and NOAA tide stations (see Place coast) |
| zip | 33,791 | Census ZIP code areas (ZCTAs) |
| county | 3,222 | Census gazetteer |
| us_state | 56 | fixed list, with each state's FIPS code and bounding box from Census TIGERweb, and its weekly gas-price area (EIA's state series or the state's PADD region) |
| airport | 13,491 | OurAirports |
| tide_station | 1,181 | NOAA CO-OPS: primary stations (a reference for other stations' predictions, or an active water-level station), plus secondary stations only where no primary is within 50 km |
| buoy | 893 | NOAA NDBC: stations reporting now (`latest_obs.txt`) that have a realtime2 file, keyed by NDBC's exact id, each with the columns it reports (`attrs.measures`: WTMP, WVHT...) |
| water_body | 32 | reviewed list of US lakes, bays, sounds and gulfs (a representative point each); a name that is also a place's is left out |
| radar_site | NEXRAD sites | NWS |
| ticker | 13,243 | Nasdaq symbol directories plus the S&P 500 list (popularity, CIK); never a market word as an alias ("nasdaq", "market", "russell": `TICKER_STOP`), except a company's own symbol |
| crypto | 21,655 | CoinGecko (popularity from market cap) |
| kraken_pair | Kraken USD pairs | Kraken |
| currency | 31 | ISO 4217 |
| metal | 4 | fixed list: gold, silver, platinum, palladium (gold-api.com symbols, priced per troy ounce) |
| launch_site / launch_provider | 4 sites, 5 providers | fixed lists of Launch Library 2 location and provider ids (US launch sites; SpaceX, Rocket Lab, ULA, Blue Origin, Firefly) |
| team_espn / team_mlb / team_nhl | teams | ESPN, MLB, NHL; `team_espn` pro teams, and the college teams TheSportsDB lists under their school, carry TheSportsDB's team id (`attrs.tsdb_id`) |
| sports_league | 15 leagues | TheSportsDB league ids (NFL, NBA, WNBA, MLB, NHL, MLS, NWSL, college football and basketball, top European soccer) |
| soccer_competition | competition codes | football-data.org |
| statuspage | 30 services | curated |
| official_site | subjects | each subject's own domains (`attrs.domains`): every Library provider's url, every Statuspage host, and the reviewed `resolvers/official_sites.json` (slack-status.com is Slack's) |
| geyser | 13 geysers | GeyserTimes (ODbL): Yellowstone geysers with predictions, `attrs.gt_id`, plus `attrs.nps_id` for the six the park predicts |
| local_news | 147 feeds, 52 metros | reviewed list `resolvers/local_news_feeds.json`: each metro's newsrooms (local TV, public radio, the daily paper) by their public RSS/Atom feed, found by the names people call the metro |
| fr_agency / spending_agency | federal agencies | Federal Register, USAspending |
| nwps_gauge | river gauges | NOAA NWPS (pending: rate-limited during harvest) |

The single matcher is `sourcetool/resolve.py`. It works in three ways:
- **`by_name`** looks for a phrase match on a name or alias. It scores by the distinctive words in the phrase, agreement with a state the ask names, and context words (sport, league, exchange). Popularity is weighted per resolver. Among equal matches, one that is at least 20 times more populous wins. A short code counts only when it's typed like a code, unless it's also the thing's name.
- **`by_name(many=True)`** returns every distinct thing named, in order: a currency pair, or "compare X and Y".
- **`near`** returns the nearest entries within the policy's distance. When a near rival differs in what it measures (`differ_on`, such as a different body of water or river), the result is *ambiguous*, which means **ask the user**. When nothing is in range, it's an honest *none*.

`python -m sourcetool resolvers --refine place ticker team_espn team_mlb team_nhl` applies today's alias rules to the committed
tables without re-pulling their upstream lists (the same functions the full harvest applies).

### Place short forms
A place is also found by the shorter name people say ("Boise City" is "Boise", "Winston-Salem" is "Winston"),
with and without its state. A one-word short form that is an everyday or landscape word is never kept
(`SHORT_FORM_STOP`): "Lake City" is not "Lake", so "Lake Michigan" never names Lake City, MI, and "Mountain City"
is not the "Mountain" of "Mammoth Mountain". A named water body ("Lake Michigan", `water_body`) is masked out of
the ask before places and states are matched, unless a longer place name holds it ("Lake Michigan Beach").

### Place nicknames
People say "NYC", "LA", "Philly", "Vegas", "NOLA", "Chi-town" or "the Big Apple". These are extra aliases on the place:
- **Wikidata**: English short names (P1813) and nicknames (P1449) of items with a GNIS id (P590), joined to the Census place by the gazetteer's GNIS code (ANSICODE). The join keeps only items that are a Census place. A nickname is kept when it is a code typed in capitals ("NYC", "L.A."), a single word of five or more letters ("Philly"), or a phrase of up to three words ("Mile High City"); slogans, anything with a digit, short everyday words ("Jeff", "The Hub") and state names are dropped. A Wikidata nickname never shadows another place's own name ("Frisco" stays Frisco, Texas).
- **Reviewed list** `resolvers/place_nicknames.json`: the common ones Wikidata lacks (it has none for Chicago), plus an `exclude` list for Wikidata nicknames we refuse.
- **Areas that are not Census places** ("Outer Banks", "Tahoe", "Cape Cod", "Bay Area", "Twin Cities") are reviewed nicknames of the place that represents them: the area's main town, the one its forecasts name. An area with no Census place in it ("Big Sur") is a `points` entry: its own place at a representative point.
  Reviewed nicknames are also listed in the place's `attrs.nicknames`: the app takes one as the ask's place on its
  own ("Tahoe weather"), while a small town's plain name still needs "in" / "at" / "near" / "for" / "around".

### Place coast
Every place carries `attrs.coastal` (true or false): whether its point is on the ocean coast, in the sense of a
source whose `coverage.water` is `ocean_coastal` (a marine model such as Open-Meteo marine). The app refuses such a
source for a place marked false ("waves in Duluth, GA"). A place is coastal when both hold:
- its point is within 25 km of Natural Earth's 1:10m ocean coastline, or lies out on the water itself (not on
  Natural Earth's 1:50m land: San Francisco's Census point is off the Golden Gate); and
- that coast is tidal: a NOAA CO-OPS tide prediction station (`tide_station`) is within 150 km. This drops the
  St. Lawrence above tide (Massena, NY), which Natural Earth draws as coast.

A place within 25 km of a Great Lake's shore (Superior, Michigan, Huron, Erie, Ontario, and St. Clair; Natural Earth's
1:10m lakes) carries `attrs.great_lakes: true` instead: Duluth, MN and Chicago are on a Great Lake, not the ocean
coast. Natural Earth (public domain) is read from the `v5.1.2` tag of `nvkelso/natural-earth-vector`, so the marks
are reproducible; `resolvers --refine place` re-applies them. Known limit: tidal rivers that Natural Earth draws as
coast up to the head of tide count as coast (Washington, DC on the Potomac; Philadelphia on the Delaware).

Two matcher rules keep short nicknames honest:
- A **two-letter** place alias counts only when typed in capitals: "LA weather" is Los Angeles, the word "la" never is.
- A capitalised **state code that is also a place nickname** ("LA", "DC") names that place only when no other place is named. "LA weather" is Los Angeles; "Lafayette LA" is Lafayette, Louisiana, because another place is named and the code is its state.

In source lookup, a code typed in capitals that names the ask's place ("NYC weather") is the place, not a ticker, unless the ask has a ticker cue ("NYC stock").

### Team abbreviations and nicknames
A team's abbreviation is an alias ("NYY", "PHI"), except one that is an everyday word and not a word of the team's
own name (`TEAM_ABBREV_STOP`): Winthrop's "WIN" would make "did the dbacks win" a Winthrop question, Minnesota's
"MIN" would claim "min temperature". The nicknames fans say ("D-backs", "A's", "Niners", "Habs") come from the
reviewed list `resolvers/team_nicknames.json`: each entry names a league and the club name the tables already
alias, so one entry reaches the team in `team_espn` and in the league's own table, as an alias and in
`attrs.nicknames`. A nickname that is an everyday word ("Pens", "Caps", "Wolves") or that two teams share
("Bucs", "Hawks", "Bolts") is left out.

Every result is `resolved`, `ambiguous` (the card asks one question, offering the candidates) or `none`. Nothing is guessed silently.

## Fills: how every source parameter gets its value
Each `access.params[]` entry carries a `fill`, written by `python -m sourcetool fills`:
- `resolver`: from a resolver table and field;
- `clock`: from the clock, in the card's time zone;
- `vault_key`: the user's own key;
- `source`: another source's answer (for example, NWS points to forecast office and grid);
- `text`: taken as the user typed it;
- `default`: a default value;
- `gap`: a named reason. The card asks.

`check` refuses a curated parameter without a fill.

## Source policy: one on every subcategory
`python -m sourcetool policies` writes a policy onto all 127 subcategories in `taxonomy/taxonomy.json`. Each policy states:
- which authority to prefer;
- how the entity must match (`geo`, `name` or `none`) and which resolvers to use;
- `max_km` and `differ_on`;
- the maximum data age;
- whether to cross-check against a second source;
- when to ask.

Lookup ranks authority by the asked subcategory's own trust order: official NOAA over a tide website, the market-data provider for stocks.

## Source lookup
`build.lookup` scores sources as follows:
- **Word relevance.** Location words are removed, and harvested sources need more evidence than curated ones.
- **Category match** from the taxonomy keywords.
- **The subcategory's trust order.**
- **Named subjects.** A team, ticker, coin, currency, airport, service, agency or competition narrows the candidates to the sources that take it. A subject counts only with a cue for its kind ("stock", a code typed in capitals, "airport", "down"…).
- **Location parameters.** Place, ZIP, state and county lift geo-capable sources in the asked category, through any geographic resolver chain.
- **Question kind.** Current value, trend, next event, ranking, alerts…
- **Specialist sources** (aviation, marine) rank lower unless the ask speaks to them.
- **Helper sources** (lookups that only fill other sources' parameters) are never offered as cards.

## Measurement
Resolver sets:

| Set | Size | Result |
|---|---|---|
| Development | 71 asks (11 nickname asks added) | 100% |
| Held-out | 44 asks | 82% on its first run, then 100% after general fixes |
| Sealed | 40 asks, run once | **90%** |

Lookup sets (top-1):

| Set | Size | Result |
|---|---|---|
| Development | 47 asks | 100% |
| Held-out | 40 asks | 85% on its first run, then 95% after general fixes |
| Sealed | 30 asks, run once | **100%** |

The asks were written by the Library's author, so real users' phrasing is the next test.

CI runs the development and held-out resolver sets as a gate, and reports the lookup sets.
