# Library policy

## What gets in
- **Terms (operator ruling R4):** only sources whose terms allow automated personal use. Each record has a `terms.status`:
  - `public_domain`: for example US government works.
  - `open_license`: CC0, CC BY, ODbL, MIT and similar. The license is named in `terms.note`.
  - `terms_allow`: the provider's terms or published feed purpose allow personal automated use. The note says which.
  - `unverified`: harvested and not yet reviewed. It ranks lower and is labelled in the app.
- **User-paid services:** not in v1. Sources that need a **free** key are allowed. The key is the user's own, entered in the app, stored in their vault and bound to that provider's host. The Library never stores keys, and a record with a credential in its URL is rejected by the schema.
- **Safe content only.** Safe search is always on in SmartBrain, and the Library carries no adult or harmful-content sources.
- **Scope v1:** the US. This includes global providers that serve US users.

## How we fetch (sourcetool and the app alike)
- **One honest User-Agent** that names the project. We never disguise ourselves as a browser, never rotate identities, and never solve challenges or CAPTCHAs.
- **Politeness:** one connection per host, at most one request per second per host, and cached responses.
- **robots.txt vs API terms (operator ruling, 2026-09-28):**
  - `robots.txt` is the crawler convention. It **governs every page we crawl or scrape**: HTML pages and search results. We honor it without exception.
  - **Documented API and feed endpoints** are governed by the provider's **API terms**, which the record carries. Several public APIs publish a robots.txt that disallows `/`, to keep search engines from indexing responses, while documenting the same endpoints for programs. Examples: api.weather.gov, Open-Meteo, CoinGecko, sunrise-sunset.org, Wikidata's query service.
  - Every source records its `validation.robots` result (`allow` or `disallow`), so the operator can tighten this with a filter at any time.
- **Contact User-Agent (operator ruling, 2026-09-28):** a few official sources refuse requests without a contact email in the User-Agent (SEC EDGAR, www.bls.gov). For those sources only, the app adds the **user's own email**, which they enter once and can remove. Records mark this with `access.contact_ua: true`. The Library never collects that email.
- **Search engines are not sources.** Google, Bing and Brave search pages disallow automation, so the Library never scrapes them. Assisted search, where the user runs the search themselves, is the rules-compliant path.

## Privacy
- **No telemetry.** The app sends the Library only what the user chooses:
  - a **Yes / No / broken vote**: a source id, or a new source record with parameter values removed;
  - a **suggestion** from the Add-a-source form.
- **Never sent:** the user's ask text, location, keys, account data, or any install or device identifier.
- **Local sources** stay sealed in the user's vault unless the user chooses to suggest one.

## Review and publishing
- Only the operator merges into `main` and signs packs.
- `sourcetool` and the Library API open PRs; they never merge.
- A provider can ask for its sources to be removed. We remove them in the next pack and cards that use them get a switch offer.
