# SmartBrain Library

The SmartBrain Library is an open registry of **where the data lives**: weather, tides, markets, sports, transit, government data, and the other things people want on a live dashboard.

[SmartBrain](https://github.com/SecureCloudGroup/SmartBrain_3000)'s Neural Interface looks a request up in the Library **on the user's own machine**. That lookup takes milliseconds and uses no cloud search or AI service. SmartBrain then offers the best sources one at a time. When the user answers **Yes**, SmartBrain builds the card.

- **9,045 sources** (6,918 validated ok), US scope in v1:
  - 269 hand-curated and validated sources from 131 providers;
  - harvested from open catalogs: US open-data portals, the Mobility Database, GBFS, Census, Wikidata, APIs.guru and public-apis.
- **18 categories, 127 subcategories**, in `taxonomy/taxonomy.json`.
- Every source records:
  - who publishes it and how official it is;
  - its terms;
  - how fresh it is;
  - when it was last validated and what happened.

## Using SmartBrain? You don't need this repo

Browse and add sources from the **Library** page inside SmartBrain. When you tap **Yes** on a source, it counts as a good source. You can also suggest a new one from the app. No Git or GitHub account is needed.

## Maintainers

```bash
python3 -m venv .venv && .venv/bin/pip install httpx duckdb
.venv/bin/python -m sourcetool check          # schema-check every record (CI runs this)
.venv/bin/python -m sourcetool validate       # probe sources politely, record results
.venv/bin/python -m sourcetool harvest all    # refresh harvested catalogs
.venv/bin/python -m sourcetool build          # build/library.duckdb (+ search index)
.venv/bin/python -m sourcetool lookup "tides for Charleston Harbor today"
.venv/bin/python -m sourcetool coverage       # sources per subcategory, empty ones listed
.venv/bin/python -m sourcetool answers-check  # fetch each answers/ sample and verify its paths (live)
```

| Path | What |
|---|---|
| `taxonomy/` | categories, subcategories, question kinds, parameter kinds |
| `sources/curated/` | hand-curated records (JSONL, one per line) |
| `sources/harvested/` | records harvested from open catalogs (labelled, ranked lower) |
| `sources/suggested/` | users' suggestions, written by `sourcetool ingest` |
| `providers/` | provider records |
| `answers/` | which response paths answer which questions, per curated source (merged into records by `build`) |
| `resolvers/` | lists that turn words into parameters (status pages, stations…) |
| `sourcetool/` | the tool |
| `service/` | the Library API the app submits votes and suggestions to ([README](service/README.md)) |
| `docs/` | [PLAN](docs/PLAN.md), [POLICY](docs/POLICY.md), [SCHEMA](docs/SCHEMA.md) |

Only the maintainer merges changes to `main`. Changes from SmartBrain users arrive through the Library API, and are then reviewed and opened as PRs by the maintainer's tooling.

## License

The code and the records are licensed under the Elastic License 2.0 (see [LICENSE](LICENSE)). The data each source serves belongs to its provider and is governed by that provider's terms, which each record links to.
