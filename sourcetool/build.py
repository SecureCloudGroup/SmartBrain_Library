"""`sourcetool build`: compile every record into build/library.duckdb, and `lookup` against it.

The search index is plain SQL (a term table), not DuckDB's FTS extension: the app must look sources up
offline, and FTS is a runtime-downloaded extension. The same tables and the same scoring are what the app
loads into its own DuckDB (see docs/APP_INTEGRATION.md).
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter

import duckdb

from .common import BUILD, SOURCES, read_jsonl, taxonomy
from .schema import classify, load_asks

DB = BUILD / "library.duckdb"
_STOP = set("a an the of in on at for to and or is are was be by with from as it its this that what whats "
            "how when where who which my me i show get give tell today now current latest near about into "
            "per vs".split())
TIER_BONUS = {"curated": 1.0, "local": 1.2, "provider_trusted": 0.6, "harvested": 0.3}
VALID_BONUS = {"ok": 0.6, "unvalidated": 0.0, "degraded": -0.4, "failed": -2.0, "refused": -2.0}
AUTH_BONUS = {"official": 0.4, "primary": 0.2, "aggregator": 0.0, "community": -0.1}


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+]*", text.lower()) if t not in _STOP and len(t) > 1]


def _resolver_names() -> dict[str, str]:
    """param name -> the names a resolver list covers (e.g. status_host -> 'GitHub Cloudflare ...')."""
    out: dict[str, str] = {}
    for f in sorted((BUILD.parent / "resolvers").glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("param"):
            out[d["param"]] = " ".join(e.get("service") or e.get("name") or "" for e in d.get("entries", []))
    return out


_RESOLVED = None


def _fields(r: dict) -> list[tuple[str, float]]:
    global _RESOLVED
    if _RESOLVED is None:
        _RESOLVED = _resolver_names()
    resolved = " ".join(_RESOLVED.get(p["name"], "") for p in r["access"].get("params", []))
    t = taxonomy()
    labels = {f"{c['id']}/{s['id']}": f"{c['label']} {s['label']} {' '.join(s['keywords'])}"
              for c in t["categories"] for s in c["subcategories"]}
    return [(r["name"], 3.0), (" ".join(r.get("examples", [])), 2.5), (r["provider"]["name"], 1.5),
            (" ".join(labels.get(c, "") for c in r["categories"]), 1.0), (r["description"], 1.0),
            (r["coverage"].get("entity", ""), 1.5), (resolved, 2.0)]


def build() -> str:
    from .schema import taxonomy_problems
    if problems := taxonomy_problems(taxonomy()):  # before anything is written: the old build stays usable
        raise SystemExit("taxonomy refused:\n  " + "\n  ".join(problems))
    t0 = time.time()
    BUILD.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    recs = [r for d in ("curated", "harvested", "suggested") for f in sorted((SOURCES / d).glob("*.jsonl"))
            for r in read_jsonl(f) if not r.get("replaced_by")]  # a retired source never competes again
    n_answers = merge_answers(recs)
    src_asks, route_asks, problems = load_asks(recs, taxonomy(), require_all=False)  # coverage is check's gate
    if problems:
        raise SystemExit("asks refused:\n  " + "\n  ".join(problems))
    con = duckdb.connect(str(DB))
    con.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR, provider_name VARCHAR,
        authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR, access_kind VARCHAR, url_template VARCHAR,
        docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR,
        robots VARCHAR, votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON, kinds VARCHAR[],
        role VARCHAR, audience VARCHAR)""")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, kinds VARCHAR[], "
                "params VARCHAR[], keywords VARCHAR[], policy JSON)")
    con.execute("CREATE TABLE library_resolver_entries(id VARCHAR PRIMARY KEY, resolver VARCHAR, kind VARCHAR, "
                "key VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, state VARCHAR, attrs JSON, rank DOUBLE)")
    con.execute("CREATE TABLE library_resolver_aliases(alias VARCHAR, entry_id VARCHAR, partial BOOLEAN)")
    # which resolvers fill each source's parameters: an ask that names a team/ticker/airport/place
    # lifts the sources that take one
    con.execute("CREATE TABLE library_source_resolvers(source_id VARCHAR, resolver VARCHAR)")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    # example asks (locate v2): the app embeds them with its own embedder for routing and dense retrieval
    con.execute("CREATE TABLE library_source_asks(source_id VARCHAR, ask VARCHAR)")
    con.execute("CREATE TABLE library_route_asks(category VARCHAR, subcategory VARCHAR, ask VARCHAR)")
    rows, cats, term_rows, src_res = [], [], [], []
    df = Counter()
    per_rec_terms = []
    for r in recs:
        tf = Counter()
        for text, w in _fields(r):
            for tok in set(tokens(text)):
                tf[tok] += w
        per_rec_terms.append(tf)
        df.update(tf.keys())
    n = len(recs)
    for r, tf in zip(recs, per_rec_terms):
        v = r.get("validation", {})
        prior = (TIER_BONUS.get(r["tier"], 0) + VALID_BONUS.get(v.get("status", "unvalidated"), 0)
                 + AUTH_BONUS.get(r["provider"].get("authority"), 0) + 0.1 * math.log1p(r.get("votes", {}).get("yes", 0))
                 - (0.5 if r["terms"]["status"] == "unverified" else 0)
                 - (0.3 if r["access"]["kind"] == "docs_only" else 0)
                 + 0.05 * math.log1p((r.get("signals") or {}).get("views_month", 0)))
        rows.append((r["id"], r["name"], r["description"], r["provider"].get("id"), r["provider"]["name"],
                     r["provider"].get("authority"), r["tier"], r["coverage"]["geo"], r["coverage"].get("entity", ""),
                     r["access"]["kind"], r["access"].get("url_template", ""), r["access"].get("docs_url", ""),
                     r["access"]["auth"], r["terms"]["status"], r["freshness"]["cadence"],
                     v.get("status", "unvalidated"), v.get("robots", ""), r.get("votes", {}).get("yes", 0),
                     r.get("votes", {}).get("no", 0), prior, json.dumps(r), r.get("kinds", []), r.get("role", ""),
                     r.get("audience", "")))
        for c in r["categories"]:
            a, _, b = c.partition("/")
            cats.append((r["id"], a, b))
        takes = {p["fill"]["resolver"] for p in r["access"].get("params", [])
                 if (p.get("fill") or {}).get("from") == "resolver"}
        takes |= {f["resolver"] for f in r["access"].get("entity_filters", [])}
        for res in takes:
            src_res.append((r["id"], res))
        for tok, w in tf.items():
            term_rows.append((tok, r["id"], w * math.log(1 + n / df[tok])))
    con.executemany("INSERT INTO library_sources VALUES (" + ",".join("?" * 24) + ")", rows)
    con.executemany("INSERT INTO library_source_categories VALUES (?,?,?)", cats)
    _copy(con, "library_terms", term_rows)
    con.executemany("INSERT INTO library_source_resolvers VALUES (?,?)", src_res)
    for table, ask_rows in (("library_source_asks", src_asks), ("library_route_asks", route_asks)):
        if ask_rows:  # COPY refuses an empty file
            _copy(con, table, ask_rows)
    for c in taxonomy()["categories"]:
        for s in c["subcategories"]:
            con.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?,?)",
                        (c["id"], s["id"], f"{c['label']} › {s['label']}", s["kinds"], s["params"], s["keywords"],
                         json.dumps(pack_policy(s))))
    n_res = _load_resolvers(con)
    con.execute("INSERT INTO library_meta VALUES ('built_at', ?), ('records', ?), ('schema', '1'), "
                "('sources_with_answers', ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), str(n),
                                                str(n_answers)))
    con.execute("CREATE INDEX lt_term ON library_terms(term)")
    con.execute("CREATE INDEX lra_alias ON library_resolver_aliases(alias)")
    con.execute("CREATE INDEX lre_resolver ON library_resolver_entries(resolver)")
    con.execute("CREATE INDEX lsc_cat ON library_source_categories(category, subcategory)")
    con.close()
    return (f"built {DB.relative_to(BUILD.parent)}: {n} sources, {len(term_rows)} index terms, {n_res} resolver "
            f"entries in {time.time() - t0:.1f}s")


def pack_policy(sub: dict) -> dict:
    """A subcategory's library_taxonomy.policy: its policy (with `measure`) plus its `expects`, the one column the
    app reads a subcategory's facts from."""
    return {**(sub.get("policy") or {}), **({"expects": sub["expects"]} if sub.get("expects") else {})}


def merge_answers(recs: list[dict], directory=None) -> int:
    """Put each answers/<id>.json file's answers on its record; refuse orphans and malformed answers."""
    from .answers import ANSWERS, held_back, load_answers, params_of
    loaded, errs = load_answers({r["id"]: params_of(r) for r in recs}, directory or ANSWERS)
    if errs:
        raise SystemExit("answers files refused:\n  " + "\n  ".join(errs))
    held = held_back(directory or ANSWERS)
    if held:  # drifted since their last pass: the app offers these sources as links until a recheck passes
        print(f"{len(held)} answers files held back as drifted: {', '.join(held[:12])}{' …' if len(held) > 12 else ''}")
    for r in recs:
        if r["id"] in loaded:
            r["answers"] = loaded[r["id"]]
    return sum(1 for r in recs if r.get("answers"))


def _copy(con, table: str, rows: list[tuple]) -> None:
    """Bulk-load through a CSV (executemany is ~100x slower for 100k+ rows)."""
    import csv
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
        csv.writer(fh).writerows(rows)
        path = fh.name
    try:
        con.execute(f"COPY {table} FROM '{path}' (HEADER false)")
    finally:
        import os
        os.unlink(path)


def _load_resolvers(con) -> int:
    """Every resolver table, with its exact aliases and the leading-words partials (resolve.load's index)."""
    from .resolvers import RES
    n = 0
    for f in sorted(RES.glob("*.jsonl")):
        rows = read_jsonl(f)
        con.executemany("INSERT INTO library_resolver_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(r["id"], r["resolver"], r["kind"], r["key"], r["name"], r.get("lat"), r.get("lon"),
                          r.get("state") or "", json.dumps(r.get("attrs") or {}), float(r.get("rank") or 0))
                         for r in rows])
        al = []
        for r in rows:
            for a in r["aliases"]:
                w = a.split()
                if len(w) <= 6:
                    al.append((a, r["id"], False))
                al.extend((" ".join(w[:k]), r["id"], True) for k in range(2, min(len(w), 7)))
        _copy(con, "library_resolver_aliases", al)
        n += len(rows)
    return n


# an entity lifts the sources that take it, and a source whose declared subject (coverage.entity) the ask names
# gets the same lift; it never gates the rest out: a league-wide or index source takes no resolver ("MLB wild
# card standings", "how's the Nasdaq doing"). The app's library_index._LOOKUP_SQL is this query.
_LOOKUP = """
WITH q(term) AS (SELECT unnest(?::VARCHAR[])),
hits AS (SELECT t.source_id, sum(t.weight) AS rel, count(*) AS matched
         FROM library_terms t JOIN q ON t.term = q.term GROUP BY t.source_id),
catb AS (SELECT source_id, max(CASE WHEN category || '/' || subcategory IN (SELECT unnest(?::VARCHAR[])) THEN 2.5
                                   ELSE 0 END) AS cb
         FROM library_source_categories GROUP BY source_id),
pref(authority, pos) AS (SELECT unnest(?::VARCHAR[]), generate_subscripts(?::VARCHAR[], 1)),
ent AS (SELECT DISTINCT source_id FROM library_source_resolvers WHERE resolver IN (SELECT unnest(?::VARCHAR[]))),
geo AS (SELECT DISTINCT r.source_id FROM library_source_resolvers r JOIN catb c ON c.source_id = r.source_id
        WHERE c.cb > 0 AND r.resolver IN (SELECT unnest(?::VARCHAR[]))),
cand AS (SELECT source_id FROM ent UNION SELECT source_id FROM hits)
SELECT s.id, s.name, s.tier, s.validation_status, s.provider_name,
       coalesce(h.rel / (SELECT max(rel) FROM hits), 0) * 4
         * (CASE s.tier WHEN 'harvested' THEN 0.7 WHEN 'provider_trusted' THEN 0.9 ELSE 1.0 END)
         + coalesce(c.cb, 0) + s.prior
         - (CASE WHEN s.audience <> '' AND NOT list_contains(?::VARCHAR[], s.audience) THEN 1.5 ELSE 0 END)
         + coalesce((SELECT 0.6 - 0.3 * (p.pos - 1) FROM pref p WHERE p.authority = s.authority), 0)
         + (CASE WHEN s.id IN (SELECT source_id FROM ent) OR (s.entity <> '' AND strpos(?,
                 ' ' || trim(regexp_replace(lower(s.entity), '[^a-z0-9]+', ' ', 'g')) || ' ') > 0)
            THEN 3.5 ELSE 0 END)
         + (CASE WHEN s.id IN (SELECT source_id FROM geo) THEN 1.5 ELSE 0 END)
         + (CASE WHEN list_has_any(s.kinds, ?::VARCHAR[]) THEN 1.0 ELSE 0 END) AS score,
       (SELECT string_agg(category || '/' || subcategory, ',') FROM library_source_categories x WHERE x.source_id = s.id)
FROM cand JOIN library_sources s ON s.id = cand.source_id LEFT JOIN hits h ON h.source_id = s.id
     LEFT JOIN catb c ON c.source_id = s.id
WHERE s.validation_status NOT IN ('failed', 'refused') AND s.role <> 'helper'
ORDER BY score DESC, (s.auth <> 'none'), s.prior DESC, s.id LIMIT ?"""


# resolvers that name a specific thing; places are too common a word-match to decide the source on their own.
# A resolver whose names overlap ordinary place names (an airport called "Boise") counts only with a cue in
# the ask — a code typed like a code, or one of its words.
ENTITY_RESOLVERS = ("team_mlb", "team_nhl", "team_espn", "ticker", "crypto", "currency", "airport", "statuspage",
                    "soccer_competition", "fr_agency", "spending_agency")  # a ZIP is a location, not a subject
ENTITY_CUES = {
    "airport": {"airport", "airports", "flight", "flights", "delay", "delays", "delayed", "tsa", "ground", "gate",
                "departures", "arrivals", "runway"},
    "ticker": {"stock", "stocks", "share", "shares", "ticker", "trading", "earnings", "filings", "filing", "sec",
               "dividend", "market", "nasdaq", "nyse", "etf", "fund", "10k", "10q", "8k", "quote"},
    "crypto": {"crypto", "coin", "coins", "token", "tokens", "cryptocurrency", "btc", "eth", "blockchain"},
    "currency": {"exchange", "rate", "rates", "fx", "forex", "currency", "currencies", "convert", "conversion",
                 "to", "vs", "per"},
    "soccer_competition": {"table", "standings", "fixtures", "league", "match", "matches", "game", "games", "score",
                           "scores", "cup", "season"},
    "fr_agency": {"rule", "rules", "regulation", "regulations", "register", "notice", "notices", "federal",
                  "proposed", "comment", "comments"},
    "spending_agency": {"spending", "spent", "budget", "contract", "contracts", "award", "awards", "grant",
                        "grants", "obligations", "outlays"},
    "zip": set(),
    "team_espn": {"game", "games", "score", "scores", "schedule", "standings", "play", "plays", "won", "win", "lost",
                  "vs", "match", "season", "roster", "football", "basketball", "baseball", "hockey", "soccer"},
    "statuspage": {"down", "status", "outage", "outages", "incident", "working", "up"},
}


def _vocabulary_outside(subcategory: str) -> set[str]:
    """Every keyword word of every OTHER subcategory: a coin named "Rain" is not meant by "will it rain"."""
    out = set()
    for c in taxonomy()["categories"]:
        for sc in c["subcategories"]:
            if f"{c['id']}/{sc['id']}" != subcategory:
                for kw in sc["keywords"]:
                    out.update(kw.lower().split())
    return out


GEO_RESOLVERS = ("place", "zip", "county", "us_state", "tide_station", "buoy", "airport", "radar_site", "nwps_gauge")


def place_words(ask: str) -> tuple[set[str], bool]:
    """Words of the ask that name a location, and whether one was found. A location is a PARAMETER of the
    card, not a relevance word ("weather in Boise" is about weather, served for Boise). It counts only with
    evidence: a preposition before it ("in Boise"), a state in the ask, a ZIP, or a large city."""
    import re as _re

    from . import resolve
    from .resolvers import norm
    low = f" {norm(ask)} "
    if _re.search(r"\b\d{5}\b", ask or ""):
        return set(), True
    st = resolve.by_name("us_state", ask)
    state_words = {w for w in norm(st["best"]["name"]).split()} if st["status"] == "resolved" else set()
    r = resolve.by_name("place", ask)
    words: set[str] = set()
    if r["status"] != "none":
        for c in (r["candidates"] or [])[:3]:
            said = [a for a in c["aliases"] if f" {a} " in low]  # what the user actually typed ("Boise")
            if not said:
                continue
            nm = max(said, key=len)
            big = (c.get("attrs", {}).get("pop") or 0) >= 100_000
            prep = _re.search(rf" (in|at|near|for|around|of) {_re.escape(nm)} ", low)
            if big or prep or resolve.states_in(ask):
                words |= set(nm.split())
    return words | state_words, bool(words or state_words)


KIND_CUES = [  # the kind of question, from the ask's wording (first match wins; default: a current value)
    ("trend", r"\b(chart|history|historical|trend|over time|since|past \d+|last \d+|this year|over the)\b"),
    ("ranking", r"\b(top \d*|best|standings|table|ranking|rankings|leaders|leaderboard|most)\b"),
    ("next_event", r"\b(next|when is|when does|when will|upcoming|countdown)\b"),
    ("schedule", r"\b(schedule|calendar|fixtures|this week|tonight|lineup)\b"),
    ("alerts", r"\b(alert|alerts|warning|warnings|advisory|watch)\b"),
    ("latest_items", r"\b(latest|new|recent|headlines|news|feed)\b"),
    ("count", r"\b(how many|number of|count)\b"),
    ("status", r"\b(status|down|outage|open|closed|delays?|delayed)\b"),
    ("forecast", r"\b(forecast|tomorrow|will it|this weekend|next week)\b"),
]


AUDIENCE_CUES = {
    "aviation": r"\b(aviation|airport|flight|flights|pilot|pilots|metar|taf|runway)\b",
    "marine": r"\b(marine|boat|boating|sailing|offshore|coastal waters|small craft|buoy|mariners?|surf|waves?)\b",
}


def audiences(ask: str) -> list[str]:
    """Specialist audiences the ask speaks to; a specialist source ranks lower for everyone else."""
    import re as _re
    return [a for a, rx in AUDIENCE_CUES.items() if _re.search(rx, (ask or "").lower())]


def question_kinds(ask: str) -> list[str]:
    import re as _re
    low = (ask or "").lower()
    found = [k for k, rx in KIND_CUES if _re.search(rx, low)]
    return found or ["current_value"]


def named_entities(ask: str) -> list[str]:
    import re as _re

    from . import resolve
    from .resolvers import norm
    words = set(norm(ask).split())
    # a code that names the ask's place ("NYC weather") is the place, not a ticker, unless a cue says so
    codes = set(_re.findall(r"\b[A-Z]{3,5}\b", ask or "")) - {w.upper() for w in place_words(ask)[0]}
    found = {}
    for res in ENTITY_RESOLVERS:
        r = resolve.by_name(res, ask)
        if r["status"] == "none":
            continue
        best = r["best"] or (r["candidates"] or [{}])[0]
        kind = res.replace("team_mlb", "team_espn").replace("team_nhl", "team_espn")
        cue = ENTITY_CUES.get(kind)
        typed_code = best.get("key", "").upper() in codes and kind in ("ticker", "crypto", "currency")
        if kind == "zip":
            typed_code = True  # five digits are never an accident
        well_known = (kind == "crypto" and (best.get("rank") or 0) > 0 and norm(best.get("name", "")) in norm(ask)
                      and norm(best.get("name", "")) not in _vocabulary_outside("markets/crypto"))
        if cue is not None and not (words & cue) and not typed_code and not well_known:
            continue  # "weather in Boise" names a city, not Boise's airport or Boise Cascade's stock
        found[res] = best
    # one word matching two kinds (NVDA the stock vs an unranked token called NVDA): the popular kind wins
    if "crypto" in found and "ticker" in found and not (found["crypto"].get("rank") or 0) > 0:
        found.pop("crypto")
    if "ticker" in found and "crypto" in found and (found["crypto"].get("rank") or 0) > 0 \
            and not found["ticker"].get("attrs", {}).get("sp500"):
        found.pop("ticker")
    return list(found)


def lookup(ask: str, limit: int = 8) -> list[dict]:
    con = duckdb.connect(str(DB), read_only=True)
    t0 = time.perf_counter()
    cats = classify(ask)
    prefer = []
    if cats:  # the asked subcategory's policy decides whom to trust (official NOAA over a tide website)
        cat, _, sub = cats[0].partition("/")
        row = con.execute("SELECT policy FROM library_taxonomy WHERE category = ? AND subcategory = ?",
                          [cat, sub]).fetchone()
        prefer = (json.loads(row[0]) if row and row[0] else {}).get("prefer", [])
    entities = named_entities(ask)
    pwords, has_place = place_words(ask)
    terms = [t for t in tokens(ask) if t not in pwords] or tokens(ask)
    geo = False
    if cats:
        row = con.execute("SELECT policy FROM library_taxonomy WHERE category = ? AND subcategory = ?",
                          [*cats[0].split("/", 1)]).fetchone()
        geo = bool(row and row[0] and json.loads(row[0]).get("match") == "geo")
    geo_lift = list(GEO_RESOLVERS) if (has_place and geo) else []
    from .resolvers import norm
    rows = con.execute(_LOOKUP, [terms, cats, prefer, prefer, entities, geo_lift, audiences(ask), f" {norm(ask)} ",
                                 question_kinds(ask), limit]).fetchall()
    ms = (time.perf_counter() - t0) * 1000
    con.close()
    out = [{"id": r[0], "name": r[1], "tier": r[2], "validation": r[3], "provider": r[4], "score": r[5],
            "categories": r[6]} for r in rows]
    if out:
        out[0]["ms"] = ms
    return out
