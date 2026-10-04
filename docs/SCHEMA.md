# Source record schema (v1, answers spec v1.2)

Each source is one JSON object per line in `sources/**/*.jsonl`. Users' suggestions arrive through the Library API and `sourcetool ingest` writes them to `sources/suggested/<date>.jsonl` with tier `harvested` and origin `{by: "user-suggestion"}`. `sourcetool/schema.py` holds the rules, and CI, `sourcetool` and the app all run the same validator.

| Field | Meaning |
|---|---|
| `id` | Lowercase slug. It is stable forever; a retired source gets `replaced_by` (the id of the source that replaces it), never a reused id. `sourcetool build` leaves retired sources out of the pack. |
| `name`, `description` | Human-readable. |
| `provider` | `{id, name, url, authority: official\|primary\|aggregator\|community, wikidata?}` |
| `tier` | `curated`, `provider_trusted`, `harvested` or `local` (user-only, never published). |
| `categories` | One or more `category/subcategory` ids from `taxonomy/taxonomy.json`. |
| `kinds` | The question kinds the source answers: `current_value`, `forecast`, `next_event`, `schedule`, `result` (a finished or live game/event outcome: scores, winners), `latest_items`, `ranking`, `trend`, `status`, `alerts`, `count`, `lookup`, `map`, `image`, `text_brief`, `compare`. Each declared kind must be served by one of the source's answers (see Lints). |
| `coverage` | `{geo: "US" \| "US-NY" \| "global" \| "local", entity: "what it covers", water?: "ocean_coastal"}`. `water: "ocean_coastal"` says the source has data only at sea and on the coast (a marine model): the app refuses a place whose point is inland. Absent means no claim. |
| `access.kind` | `http_json`, `http_csv`, `http_xml`, `rss`, `atom`, `gtfs`, `gtfs_rt`, `gbfs`, `ics`, `html`, `image`, `text`, `docs_only` (the API is documented but the endpoint pattern still needs a recipe) or `internal` (computed or on-device). |
| `access.url_template` | An https URL with `{param}` slots. A `{param}` host is allowed only when a resolver list supplies it. |
| `access.params[]` | `{name, kind, example, required}`. `kind` comes from the taxonomy `param_kinds`, or is `key` for the user's own key. `example` is what `validate` probes with. |
| `access.auth` | `none`, `free_key` (the user's own free key, from their vault), `oauth` or `user_account`. |
| `access.headers` | Header templates; `{key}` is filled from the user's vault. Only a key slot is sent: the app never sends a fixed header (`Accept: application/json`), so `check` refuses one — use an address or format that needs none. |
| `access.params[].fill` | How the parameter gets its value: `resolver` (a resolver table + field), `clock`, `vault_key`, `source` (another source's answer), `text` (as typed), `default`, or `gap` (named reason; the card must ask). See `sourcetool/fills.py`. A `source` fill is a **same-host helper chain**: `{from: source, source: <helper id>, path: <json path>}` names a helper record (role `helper`) on the SAME host, keyless, whose own parameters fill from resolvers the ask already satisfies (`nws-forecast` takes `office`, `grid_x`, `grid_y` from `nws-points` at the place's lat/lon, both on api.weather.gov). The app fetches the helper first, reads each path into its parameter, then fetches the source; the user's one consent covers both fetches. A cross-host or keyed helper is never chained (the card names it as a gap). |
| `access.entity_filters` | For a national feed that covers every entity (e.g. FAA status for every airport): the resolver whose entity the card keeps after fetching. |
| `access.contact_ua` | `true` when the provider refuses requests without a contact email in the User-Agent (SEC, www.bls.gov). The app adds the user's own email for these sources only (ruling R11). |
| `terms` | `{status: public_domain\|open_license\|terms_allow\|unverified, note, terms_url}` |
| `freshness.cadence` | `realtime`, `minutes`, `hourly`, `daily`, `weekly`, `monthly`, `quarterly`, `annual`, `irregular` or `static`. |
| `examples[]` | Asks this source answers. They are indexed for lookup. |
| `validation` | `{status: ok\|degraded\|failed\|refused\|unvalidated, checked_at, http, robots: allow\|disallow, note}` |
| `votes` | `{yes, no}`, aggregated from users' Yes/No answers through the Library API. |
| `signals` | Harvest evidence: monthly views, last data update, column names. |
| `origin` | Who or what created the record, and when. |
| `answers` | Curated sources: which paths of the response answer which questions, so the app builds cards without guessing paths. Authored in `answers/<id>.json` (`{source_id, answers, sample_url, checked}`) and merged into the record by `sourcetool build`. A source has 1 to 12 answers, each `{name, label, words, primary?, kind: value\|list\|columns, ...}`; a path segment may be a whole `{param}` of the record, a key the plain names can't spell is quoted (`description["#cdata-section"]`), and a list may `filter` its rows by one or by a short fixed value the response uses (`"Final"`). A `time` whose values carry no zone is shown as the source's local clock; `"utc": true` says those zoneless values are UTC. Spec v1.2 adds what an answer delivers (below). The closed keys, types and limits are in `sourcetool/answers.py` (`answer_problems`), and every path must resolve in a real sample (`check_sample`, run live by `sourcetool answers-check`). |

### Answers spec v1.2: what an answer delivers

The words say how people ask; these keys say what the answer holds, so the app picks by the asked window and measure, not by shared words alone. All are optional and closed.

| Key | On | Meaning |
|---|---|---|
| `window` | value | The stretch of time the value is about: `now`, `today`, `tonight`, `tomorrow`, or `latest` (the newest published reading, not tied to the clock: a FRED close, a CPI release, a version, standings). |
| `measure` | value | The quantity it reports, one of `temperature`, `feels_like`, `precip_chance`, `precip_amount`, `conditions`, `thunderstorm`, `snow`, `wind`, `humidity`, `waves`, `swell`, `wave_direction`, `water_temp`, `alerts`, `kp`, `uv`, `air_quality`, `tide`, `sunrise`, `sunset`. |
| `axis` | list, columns | `{cell, step}`: the rows are indexed by local date/time, so a card can cut them to the asked window on every refresh. `cell` is one of the answer's own `time`/`date` fields (the column path `daily.time`, or the row-relative list path `startTime`); `step` is `day`, `hour` or `period` (named periods: "Tonight", "Tuesday"). Every cell in the sample must be an ISO date or date-time. |
| `tbd_if` | a `time` value or `time` list row field | `{path, equals}`: the time is a placeholder when `path` (from the response root for a value, row-relative for a row) equals `equals` (`true`, or a short fixed value such as `"TBA"`). The card shows the date and "time TBD". `answers-check` flags a time whose sample carries a TBD/TBA flag beside it and declares no `tbd_if`. |

Example (MLB postseason game times are placeholders until the league sets them):

```json
{"name": "next_game_time", "label": "Game time", "kind": "value", "type": "time", "words": ["game time", "first pitch", "when"],
 "path": "dates[0].games[0].gameDate", "tbd_if": {"path": "dates[0].games[0].status.startTimeTBD", "equals": true}}
```

### Taxonomy keys

A subcategory may declare `expects`: the components a complete answer holds (closed names from the `measure` list plus `observed_time`, `wave_period`, `score`, `opponent`, `start_time`, `venue`, `rank`, `record`), so a card that lacks one says so ("this source doesn't report: wind"). Its `policy` may declare `measure`: `{<station resolver>: <column the station must report>}` (`water/water_temperature`: `{"buoy": "WTMP"}`), and a nearest-station fill then skips stations whose `attrs.measures` lack that column. `build` writes both into the pack: `library_taxonomy.policy` holds the policy (with its `measure`) and the subcategory's `expects`.

### Lints

`answers-check --lint` (offline, never fetches) and the test suite also check the data's promises:
- every declared `kinds` entry is served by one of the record's answers (`next_event` needs a time/date value or a dated list; `schedule` and `trend` a dated list; `result` a number/text value or a list; `latest_items`/`ranking` a list; `alerts` a list; `status` a text value or a list; `count` a count or a list);
- a `count` answer never lists `any …` words (an existence question takes the list, which may be empty);
- every subcategory keyword is spoken to (whole words, plurals folded) by at least one curated source filed there, in its own name, description, examples or answers.

Rules that reject a record:
- non-https URLs;
- private or `.local` hosts, and private, loopback or link-local IP addresses;
- a credential embedded in the URL (a key parameter or `user:password@`);
- unknown categories, kinds or parameter kinds;
- undeclared `{params}`;
- duplicate ids;
- malformed `answers`, or an `answers/` file whose id has no record;
- a `coverage.water` other than `ocean_coastal`.

`check` and `build` also refuse a taxonomy that isn't well-formed (`schema.taxonomy_problems`): unknown subcategory or policy keys, kinds or params outside the vocabularies, a keyword classify can never match, an `expects` name outside the closed list, or a `policy.measure` that names a resolver the policy doesn't use.

### Example asks (locate v2)

`asks/` holds everyday phrasings of what a source or a subcategory answers. The app embeds them with the user's own embedder: per-subcategory centroids route an ask, and an ask's best match among a source's asks (with its capability card) ranks the source. They are written from the record and the taxonomy alone, never from a labeled or blind ask set.

| File | Line | Pack table |
|---|---|---|
| `asks/sources.jsonl` | `{"source_id": "...", "asks": [10 strings]}`, one per offerable curated source (not failed, refused, a helper or retired) | `library_source_asks(source_id VARCHAR, ask VARCHAR)` |
| `asks/routes.jsonl` | `{"route": "<category>/<subcategory>", "asks": [12 strings]}`, one per subcategory, including those no source serves yet | `library_route_asks(category VARCHAR, subcategory VARCHAR, ask VARCHAR)` |

`check` (`schema.load_asks`) refuses an unknown source id or route, a line repeated, other than exactly 10 or 12 asks, an ask over 80 characters, a repeated ask within a line, an ask equal to one of the record's own `examples` (case and spacing ignored), and a missing line for any offerable curated source or any subcategory. `build` loads both files and refuses the same problems except coverage.
