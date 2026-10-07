# Sources without answers

One line per source: id — why no answers file was written.

- nyt-sports — the feed answers HTTP 200 with zero items (2026-09-28); nothing to verify a list answer against.
- politifact — the feed starts with blank lines before `<?xml`; the app's feed parser refuses it ("XML or text declaration not at start of entity"), so no card can read it until the parser strips leading whitespace.
- powerball-home — an html page (the jackpot, cash value and next draw are page text); the page door reads it, and its readings are in the record's examples.

## Held back (records not published)

One line per record the 2026-09-29 round wanted but did not publish: id — why.

- subreddit-top-rss — reddit.com robots.txt disallows every path for every agent (`Disallow: /`); a hard refusal excludes the source.
- aws-health-currentevents — the AWS site terms bar robots and data mining, and the Health Dashboard documents no public API; terms unclear, not published.
- megamillions-home — the jackpot is filled by script from a POST service (no readable page text), and the site publishes no terms of use to check; Mega Millions numbers come from data.ny.gov (ny-lottery-megamillions) instead.
- thesportsdb-league-round — a round's results for one team need an any-of row filter (home OR away team), which answers spec v1.2 does not have; waits on that schema.

## 2026-10-06 — drafting round (answers-draft; details in answers/_drafts/REVIEW.md)
- ercot-supply-demand — the current reading sits at a moving index of an oldest-first series; no `last` row address in the spec (draft held: lists only)
- treasury-mts — rows mix line items under one record date; which line is "total receipts" needs a filter the sample does not make obvious (draft held)
- usaspending-awards-agency — "latest populated fiscal year" is not expressible (index 0 is the just-begun FY with nulls); goes stale once the new year fills (draft held)
- met-search — endpoint answers HTTP 410 Gone
- coingecko-chart — positional pair rows `[epoch, price]`; no row path can address an element
- pubmed-search — an id list and a count; no row cells
- swpc-aurora — 65k `[lon, lat, prob]` triples; no forecast answer is expressible
- nws-marine-zone — a plain-text product; only a `text` value is expressible and the record declares `forecast`
- giss-global-temp — numbers like `-.19` and `***` placeholders fail the number rule
- nsidc-arctic-ice — padded numeric cells and a units row under the header
- treasury-yield-curve — US dates `MM/DD/YYYY` are not a `date`
- gml-co2-daily, gml-co2-monthly — year/month(/day) in separate columns, oldest-first, `-9.99` placeholders

