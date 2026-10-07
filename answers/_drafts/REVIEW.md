# Drafts of 2026-10-06 — review notes (by eye against the live sample; the validator checks paths and types only)

Drafted by `sourcetool answers-draft` with the operator's claude CLI (sonnet). PROMOTE = re-verified live and moved
into answers/ by `answers-promote`; HOLD = stays a draft until the named gap is closed; the rejected files carry
the validator's reasons.

| source | verdict | note |
|---|---|---|
| nhl-club-schedule | PROMOTE | week schedule (date, start time UTC, away/home) + games count; redraft under the no-duplicate guide |
| openf1-sessions | PROMOTE | upcoming sessions with ISO start times (axis hour after review), circuit and country; session count |
| nvd-cves-keyword | PROMOTE | CVE list (id, published, status, description) + total; hand fix: `utc: true` on `cve.published` (NVD times are zoneless UTC) |
| fdc-food-search | PROMOTE | food matches (description, category, data type), nutrients of the first match, total hits; DEMO_KEY sample |
| sec-companyfacts | PROMOTE | total assets, cash, shares outstanding, public float as dated lists; hand fix: `newest_first` removed (arrays run oldest → newest) |
| ercot-supply-demand | HOLD | data[] runs oldest → newest and the current reading sits at a moving index the spec cannot address; the two "current" values were dropped (they showed midnight's row); needs a last-row addressing rule before a current card is honest |
| treasury-mts | HOLD | `data[0]` mixes line items (src_line 27 vs 14 share a record date); which line is "total receipts" needs a row filter the sample does not make obvious |
| usaspending-awards-agency | HOLD | the model picked `agency_data_by_year[1]` — correct today (index 0 is the just-begun FY2027 with null values) but "latest populated year" is not expressible and the index goes stale once FY2027 fills in; needs a first-non-null rule or a fiscal-year param |
| met-search | DEAD | the endpoint answers HTTP 410 Gone; the weekly validate will mark the record failed |
| coingecko-chart | REJECTED | `prices` is a list of [epoch, price] pairs; no row path can address a pair element (spec gap: positional rows) |
| pubmed-search | REJECTED | the response is an id list and a count; no row cells to type |
| swpc-aurora | REJECTED | root keys carry spaces ("Observation Time") and the data is 65k [lon, lat, prob] triples; no forecast answer is expressible |

## Rejected for spec or parser gaps (the drafter found the right data; the closed spec cannot express or type it)

| source | gap |
|---|---|
| giss-global-temp | numbers like `-.19` (no leading zero) fail the number rule `-?\d+(\.\d+)?`; placeholder `***` cells |
| nsidc-arctic-ice | padded numeric cells (`"     10.231"`) and a units row (`" 10^6 sq km"`) under the header |
| treasury-yield-curve | US dates `MM/DD/YYYY` are not a `date`; no ISO axis cell |
| gml-co2-daily / gml-co2-monthly | year/month/day in separate columns (no date cell), oldest-first with the oldest 500 kept, placeholder `-9.99` |
| coingecko-chart | rows are positional pairs `[epoch, price]`; no row path can address an element |
| pubmed-search | an id list and a count; no row cells |
| swpc-aurora | root keys with spaces; 65k `[lon, lat, prob]` triples |
| nws-marine-zone | plain-text product; only a `text` value is expressible and the record declares `forecast` |

Spec v1.3 candidates these imply (each needs the app's engine to agree before the Library declares it): tolerant number
parsing (leading-dot and padded strings), `date_format` for US dates, a `units_row` skip for CSVs, positional row
cells (`[0]`, `[1]`) for pair arrays, a composite date from year/month(/day) columns, quoted root keys with spaces
(the path grammar allows `["Observation Time"]` — the drafter did not use it; worth a guide line), and a `last`
row address for oldest-first series (also the ERCOT hold).
