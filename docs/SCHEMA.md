# Source record schema (v1)

Each source is one JSON object per line in `sources/**/*.jsonl`. `sourcetool/schema.py` holds the rules, and CI, `sourcetool` and the app all run the same validator.

| Field | Meaning |
|---|---|
| `id` | Lowercase slug. It is stable forever; a retired source gets `replaced_by`, never a reused id. |
| `name`, `description` | Human-readable. |
| `provider` | `{id, name, url, authority: official\|primary\|aggregator\|community, wikidata?}` |
| `tier` | `curated`, `provider_trusted`, `harvested` or `local` (user-only, never published). |
| `categories` | One or more `category/subcategory` ids from `taxonomy/taxonomy.json`. |
| `kinds` | The question kinds the source answers: `current_value`, `forecast`, `next_event`, `schedule`, `latest_items`, `ranking`, `trend`, `status`, `alerts`, `count`, `lookup`, `map`, `image`, `text_brief`, `compare`. |
| `coverage` | `{geo: "US" \| "US-NY" \| "global" \| "local", entity: "what it covers"}` |
| `access.kind` | `http_json`, `http_csv`, `http_xml`, `rss`, `atom`, `gtfs`, `gtfs_rt`, `gbfs`, `ics`, `html`, `image`, `text`, `docs_only` (the API is documented but the endpoint pattern still needs a recipe) or `internal` (computed or on-device). |
| `access.url_template` | An https URL with `{param}` slots. A `{param}` host is allowed only when a resolver list supplies it. |
| `access.params[]` | `{name, kind, example, required}`. `kind` comes from the taxonomy `param_kinds`, or is `key` for the user's own key. `example` is what `validate` probes with. |
| `access.auth` | `none`, `free_key` (the user's own free key, from their vault), `oauth` or `user_account`. |
| `access.headers` | Header templates; `{key}` is filled from the user's vault. |
| `terms` | `{status: public_domain\|open_license\|terms_allow\|unverified, note, terms_url}` |
| `freshness.cadence` | `realtime`, `minutes`, `hourly`, `daily`, `weekly`, `monthly`, `quarterly`, `annual`, `irregular` or `static`. |
| `examples[]` | Asks this source answers. They are indexed for lookup. |
| `validation` | `{status: ok\|degraded\|failed\|refused\|unvalidated, checked_at, http, robots: allow\|disallow, note}` |
| `votes` | `{yes, no}`, aggregated from users' Yes/No answers through the Library API. |
| `signals` | Harvest evidence: monthly views, last data update, column names. |
| `origin` | Who or what created the record, and when. |

Rules that reject a record:
- non-https URLs;
- private or `.local` hosts;
- a credential embedded in the URL;
- unknown categories, kinds or parameter kinds;
- undeclared `{params}`;
- duplicate ids.
