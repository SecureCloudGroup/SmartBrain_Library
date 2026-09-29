# Sources without answers

One line per source: id — why no answers file was written.

- nyt-sports — the feed answers HTTP 200 with zero items (2026-09-28); nothing to verify a list answer against.
- politifact — the feed starts with blank lines before `<?xml`; the app's feed parser refuses it ("XML or text declaration not at start of entity"), so no card can read it until the parser strips leading whitespace.
