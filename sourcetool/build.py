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
from .schema import classify

DB = BUILD / "library.duckdb"
_STOP = set("a an the of in on at for to and or is are was be by with from as it its this that what whats "
            "how when where who which my me i show get give tell today now current latest near about into "
            "per vs".split())
TIER_BONUS = {"curated": 1.0, "local": 1.2, "provider_trusted": 0.6, "harvested": 0.3}
VALID_BONUS = {"ok": 0.6, "unvalidated": 0.0, "degraded": -0.4, "failed": -2.0, "refused": -2.0}
AUTH_BONUS = {"official": 0.4, "primary": 0.2, "aggregator": 0.0, "community": -0.1}


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+-]*", text.lower()) if t not in _STOP and len(t) > 1]


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
    t0 = time.time()
    BUILD.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    recs = [r for d in ("curated", "harvested") for f in sorted((SOURCES / d).glob("*.jsonl")) for r in read_jsonl(f)]
    con = duckdb.connect(str(DB))
    con.execute("""CREATE TABLE library_sources(
        id VARCHAR PRIMARY KEY, name VARCHAR, description VARCHAR, provider_id VARCHAR, provider_name VARCHAR,
        authority VARCHAR, tier VARCHAR, geo VARCHAR, entity VARCHAR, access_kind VARCHAR, url_template VARCHAR,
        docs_url VARCHAR, auth VARCHAR, terms_status VARCHAR, cadence VARCHAR, validation_status VARCHAR,
        robots VARCHAR, votes_yes INTEGER, votes_no INTEGER, prior DOUBLE, record JSON)""")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_taxonomy(category VARCHAR, subcategory VARCHAR, label VARCHAR, kinds VARCHAR[], "
                "params VARCHAR[], keywords VARCHAR[])")
    con.execute("CREATE TABLE library_meta(key VARCHAR, value VARCHAR)")
    rows, cats, term_rows = [], [], []
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
                     r.get("votes", {}).get("no", 0), prior, json.dumps(r)))
        for c in r["categories"]:
            a, _, b = c.partition("/")
            cats.append((r["id"], a, b))
        for tok, w in tf.items():
            term_rows.append((tok, r["id"], w * math.log(1 + n / df[tok])))
    con.executemany("INSERT INTO library_sources VALUES (" + ",".join("?" * 21) + ")", rows)
    con.executemany("INSERT INTO library_source_categories VALUES (?,?,?)", cats)
    con.executemany("INSERT INTO library_terms VALUES (?,?,?)", term_rows)
    for c in taxonomy()["categories"]:
        for s in c["subcategories"]:
            con.execute("INSERT INTO library_taxonomy VALUES (?,?,?,?,?,?)",
                        (c["id"], s["id"], f"{c['label']} › {s['label']}", s["kinds"], s["params"], s["keywords"]))
    con.execute("INSERT INTO library_meta VALUES ('built_at', ?), ('records', ?), ('schema', '1')",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), str(n)))
    con.execute("CREATE INDEX lt_term ON library_terms(term)")
    con.execute("CREATE INDEX lsc_cat ON library_source_categories(category, subcategory)")
    con.close()
    return f"built {DB.relative_to(BUILD.parent)}: {n} sources, {len(term_rows)} index terms in {time.time() - t0:.1f}s"


_LOOKUP = """
WITH q(term) AS (SELECT unnest(?::VARCHAR[])),
hits AS (SELECT t.source_id, sum(t.weight) AS rel, count(*) AS matched
         FROM library_terms t JOIN q ON t.term = q.term GROUP BY t.source_id),
catb AS (SELECT source_id, max(CASE WHEN category || '/' || subcategory IN (SELECT unnest(?::VARCHAR[])) THEN 1.5
                                   ELSE 0 END) AS cb
         FROM library_source_categories GROUP BY source_id)
SELECT s.id, s.name, s.tier, s.validation_status, s.provider_name,
       (h.rel / (SELECT max(rel) FROM hits)) * 4 + coalesce(c.cb, 0) + s.prior AS score,
       (SELECT string_agg(category || '/' || subcategory, ',') FROM library_source_categories x WHERE x.source_id = s.id)
FROM hits h JOIN library_sources s ON s.id = h.source_id LEFT JOIN catb c ON c.source_id = s.id
WHERE s.validation_status NOT IN ('failed', 'refused')
ORDER BY score DESC LIMIT ?"""


def lookup(ask: str, limit: int = 8) -> list[dict]:
    con = duckdb.connect(str(DB), read_only=True)
    t0 = time.perf_counter()
    rows = con.execute(_LOOKUP, [tokens(ask), classify(ask), limit]).fetchall()
    ms = (time.perf_counter() - t0) * 1000
    con.close()
    out = [{"id": r[0], "name": r[1], "tier": r[2], "validation": r[3], "provider": r[4], "score": r[5],
            "categories": r[6]} for r in rows]
    if out:
        out[0]["ms"] = ms
    return out
