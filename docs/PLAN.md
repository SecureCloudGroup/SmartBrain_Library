# SmartBrain Library: plan

The SmartBrain Library is the registry of **where the data lives** for the needs a very large number of people have.
SmartBrain's Neural Interface (NI) looks a user's ask up in it locally, in milliseconds, with no cloud API and no cloud model.
The Library then offers the best sources for the user to accept, one at a time, with **Yes / No**.

Scope for v1 is the **US**.

## Status (v1, 2026-09-28)

| | |
|---|---|
| Sources | **9,045**: 269 curated plus 8,776 harvested |
| Providers (curated) | 131 |
| Taxonomy | 18 categories, 127 subcategories, **none empty** (`taxonomy/taxonomy.json`) |
| Validation (all) | **6,918 ok**, 149 degraded, 299 failed, 1,679 unvalidated (the source needs a user key, or it is documented but not yet an endpoint recipe) |
| Curated validation | 230 ok, 4 degraded, 11 failed, 24 need a user key to probe |
| robots.txt on probed URLs | 7,265 allow, 101 disallow (all of them documented APIs; see POLICY) |
| Lookup | 7 ms median on 9,045 sources (plain DuckDB SQL, works offline) |

Harvested sources, by catalog:

| Catalog | Sources | Validation |
|---|---|---|
| Socrata US open data (the most-used city, state and federal datasets) | 5,094 | 5,057 ok |
| Wikidata feeds of US organizations | 1,308 | 980 ok |
| public-apis | 703 | docs only |
| Mobility Database (US transit) | 540 | 479 ok |
| APIs.guru | 525 | docs only |
| Census API datasets | 427 | need a free key |
| GBFS bike share | 179 | 172 ok |

## Operator rulings

| # | Ruling |
|---|---|
| R1 | The repo is **SmartBrain Library**, public. Only the operator merges and updates it. |
| R2 | Both tiers: curated and harvested (labelled). Users can **suggest** a source and add their own **local** source. |
| R3 | `sourcetool` may use a model on the publisher side (local, or frontier with consent) to draft classifications and descriptions for review. The user's machine never needs one. |
| R4 | Only sources whose terms allow automated personal use, for now. User-paid services may come later. |
| R5 | v1 = US. |
| R6 | **A Yes is a good source.** Every time a user taps Yes on a source, it is recorded as a good source, "because they said so". It is recorded locally at once and submitted to the Library as a vote. |
| R7 | **Submission is an API, not Git.** 99% of users do not know Git. The app submits through a Library API; only the operator's tooling touches GitHub. |
| R8 | **Expand the initial set as much as possible**, and store it as **DuckDB tables** for fast lookup. |
| R9 | The Library link on the NI page opens a **new Library page** where the user can see the Library and add to it (a form). |
| R10 | robots.txt governs crawling and scraping pages; the provider's API terms govern documented API calls. The robots result is recorded per source. |
| R11 | Sources that require a contact User-Agent (SEC, www.bls.gov) use the **user's own email**, entered once in the app. |
| R12 | Pack delivery v1: each app release **pins the pack's sha256** and downloads it from the Library repo's GitHub release. v2: Ed25519-signed packs with a separate Library publisher key, for updates between app releases. |

## 1. Data model

These are the Library records (`docs/SCHEMA.md`).

| Record | What it is |
|---|---|
| Provider | The organisation: authority tier (official, primary, aggregator, community), terms and attribution. |
| Source | An address pattern and its parameters. It carries its taxonomy placements, coverage (geo and entity), access (kind, auth, headers), terms status, freshness cadence, example asks, validation result, votes, and signals (usage, freshness, columns). |
| Resolver | A list that fills a parameter from user words. Examples: status-page hosts for "is GitHub down", tide stations, tickers, transit agencies. The names in a resolver list are indexed, so "openai status" finds the status-page family. |
| Local source | The same schema with tier `local`. It lives only in the user's vault. |

## 2. `sourcetool` (in this repo)

| Command | Job |
|---|---|
| `check` | Schema-check every record: https only, public host, no embedded credentials, known categories. CI runs it. |
| `validate` | Probe sources politely with their example parameters. It records status (ok, degraded, failed, refused, unvalidated), HTTP code, the robots.txt result and format checks. |
| `harvest NAME\|all` | Pull candidates from the open catalogs above. Records that fail the schema are dropped. Curated ids are never overwritten. |
| `build` | Compile `build/library.duckdb`: sources, categories, taxonomy and a term index for scoring. |
| `lookup "words"` | Look up a phrase against the build (the same SQL the app runs). |
| `coverage` | Count sources per subcategory and list the empty ones. |
| `ingest` (phase 2) | Pull the API submission queue, validate, aggregate votes, write records, and open a PR for the operator. |

`sourcetool` writes only to a checkout. Publishing is always a PR that the operator merges.

## 3. Yes = good source (R6)

When a user taps **Yes** on a source in the NI flow:

1. **Locally, at once:** the source is saved to the user's local sources (sealed in their vault) with a yes count. It then ranks first for that user from then on.
2. **To the Library:** the app submits a vote to the Library API:
   - `{source_id | new_source_record, verdict: "yes", app_version}`;
   - for a Library source, only its id;
   - for a new source found on the web, the record with parameter **values removed** (the template and parameter kinds are kept, the user's station, ticker or place are not);
   - **never** the user's ask text, their location or any identifier.
3. **The Yes text says so:** "Yes, use this. It also tells the SmartBrain Library this is a good source." The user sees it before tapping.

No and "broken" reports go the same way, as votes against a source.

## 4. The Library API (R7)

This is a small service on the existing VPS (`smartbrain.securecloudgroup.com`) next to the landing page and broker. It reuses the VPS deploy and TLS.

| Endpoint | Purpose |
|---|---|
| `POST /library/v1/votes` | `{source_id, verdict: yes\|no\|broken}` for known sources |
| `POST /library/v1/suggestions` | A new source record (from the Library form or a Yes on a web source) |
| `GET /library/v1/manifest` | The latest signed pack: seq, sha256, size, url |
| `GET /library/v1/pack/{seq}` | The signed pack (the DuckDB file, or JSONL plus the build step in the app) |

Security:
- Anonymous: no account, no install id, no cookies.
- Rate-limited per IP, with a small proof-of-work on submissions.
- Size caps.
- Every payload runs through the same `validate_record` rules as CI: https, public host, no credentials, known categories.
- The service **never fetches** a submitted URL; `sourcetool ingest` validates on the operator's side.
- Submissions go into a queue (SQLite on the VPS).
- The only operator endpoint is authenticated (token held by the operator's machine) and reads that queue.

Flow: the queue feeds `sourcetool ingest` on the operator's side. It validates, aggregates votes per source, drafts a classification (R3) and opens a PR in this repo. The operator merges, a tag triggers a new signed pack, and the app fetches the manifest and installs the pack.

## 5. Distribution and trust

- Packs are signed with **Ed25519 over the exact bytes**. This reuses the vault machinery: TOFU pin of the publisher key, monotonic seq, rollback refused, and all-or-nothing install.
- The v1 pack is `library.duckdb` (about 34 MB with full records, about 8 MB compressed). Delta packs come later.
- The app ships with the pack current at release, so it works offline from first launch, and checks the manifest on the same cadence as vault updates.

## 6. In the app (R8, R9)

- **DuckDB tables:** `library_sources`, `library_source_categories`, `library_terms`, `library_taxonomy` and `library_meta`, loaded from the signed pack. They are plaintext: public catalog data holds no user content.
- **Local sources** are user data. They are sealed in the vault and indexed **in memory at unlock** (a user has tens or hundreds, not thousands). Scoring merges them with the Library results. So nothing about a user's interests sits in plaintext at rest.
- **Lookup** uses the SQL in `sourcetool/build.py` (`_LOOKUP`): term weights plus a category match from the taxonomy, plus a prior. The prior covers tier, validation, authority, votes, terms and usage.
- **The Library page** (`/ni/library`, R9):
  - browse by category and subcategory, search, and filter by tier and status;
  - each source shows its provider, authority, terms, cadence, last validation and votes;
  - an **Add a source** form: name, URL template or a plain URL, category, what it answers, and whether it needs a key. The same validator runs on the device.
  - The user can keep the source local only, or also **Suggest to the Library** (the API above).

  This page replaces the current "Global Library → Connect" sheet. Library templates move into this page as recipes over sources.

## 7. Phases

| Phase | Work | Status |
|---|---|---|
| A | Taxonomy v1, schema, `sourcetool` check, validate, harvest, build and lookup; curated US core; 7 harvesters; DuckDB build | **done** |
| B | Repo public and protected; CI (schema check plus validation of changed records); scheduled re-validation | this change |
| C | App: signed pack install, DuckDB tables, lookup in the NI finder, Yes recorded locally, the Library page and form | next (SmartBrain_3000 PR) |
| D | The Library API on the VPS, `sourcetool ingest`, vote aggregation, and the Yes vote plus suggestions from the app | after C |
| E | Model-drafted classification (R3) to fix keyword misplacements; more harvesters (state 511 and GTFS registries, NWS stations, NOAA stations as resolvers) | rolling |

## Known gaps (v1)

- Keyword classification misplaces some harvested records. For example, a noise-complaints dataset lands under weather. The R3 model pass fixes these in review.
- Some official sources require a contact email in the User-Agent: SEC EDGAR and www.bls.gov. The app needs a policy for that. For example, the user supplies an email, or these sources ship as `free_key`-style "needs your contact".
- Some subcategories are thin or empty in v1. `sourcetool coverage` lists them.
- **robots.txt vs API terms:** see `docs/POLICY.md`. Many documented public APIs (api.weather.gov, Open-Meteo, CoinGecko, Wikidata's query service) publish a crawler robots.txt that disallows `/`. We treat robots.txt as governing crawling and scraping, and API terms as governing documented API calls. Every source records its robots result so the policy can be tightened by a filter. **Ruled 2026-09-28: yes** (robots.txt governs crawling and scraping; API terms govern documented API calls).
