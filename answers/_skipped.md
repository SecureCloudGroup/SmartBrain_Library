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
