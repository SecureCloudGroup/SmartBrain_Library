"""The Library source record: fields, allowed values, and the validator every record must pass.

The same rules run in CI (on every PR), in `sourcetool validate`, and in the app when a user adds a
local source, so a record means the same thing everywhere.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from .answers import answer_problems
from .common import ROOT, taxonomy

TIERS = ("curated", "provider_trusted", "harvested", "local")
AUTHORITY = ("official", "primary", "aggregator", "community")
ACCESS_KINDS = ("http_json", "http_csv", "http_xml", "rss", "atom", "gtfs", "gtfs_rt", "gbfs", "ics", "html",
                "image", "text", "docs_only", "internal")
AUTH = ("none", "free_key", "oauth", "user_account")
TERMS = ("public_domain", "open_license", "terms_allow", "unverified")
CADENCE = ("realtime", "minutes", "hourly", "daily", "weekly", "monthly", "quarterly", "annual", "irregular", "static")
VALIDATION = ("unvalidated", "ok", "degraded", "failed", "refused")
WATER = ("ocean_coastal",)  # coverage.water: the source has data only at sea and on the coast (open-meteo-marine)

REQUIRED = ("id", "name", "description", "provider", "tier", "categories", "kinds", "coverage", "access", "terms",
            "freshness", "origin")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,119}$")
_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_SECRETISH = re.compile(r"(api[_-]?key|apikey|token|secret|password|access_key)=([^&{}\s]+)", re.I)


def _cat_ids() -> tuple[set[str], set[str], set[str]]:
    t = taxonomy()
    cats = {f"{c['id']}/{s['id']}" for c in t["categories"] for s in c["subcategories"]}
    return cats, set(t["question_kinds"]), set(t["param_kinds"])


_CATS, _KINDS, _PKINDS = _cat_ids()


def url_problems(url: str) -> list[str]:
    """https, a public host, and no credential in the address."""
    errs = []
    try:
        u = urlsplit(url.replace("{", "").replace("}", ""))
        host_is_param = bool(re.fullmatch(r"\{[a-z_][a-z0-9_]*\}", urlsplit(url).netloc))
    except ValueError:
        return ["not a valid address"]
    if u.scheme != "https":
        errs.append("https only")
    # a {param} host is filled by a resolver whose values are checked when filled
    if not host_is_param and (not u.hostname or "." not in u.hostname or _private_ip(u.hostname)
                              or u.hostname.endswith((".local", ".internal", ".localhost", ".home.arpa"))):
        errs.append("public host only")
    if (_SECRETISH.search(url) and "{" not in _SECRETISH.search(url).group(2)) or "@" in urlsplit(url).netloc:
        errs.append("url carries a credential; keys come from the user's own vault as a {param}")
    return errs


def _private_ip(host: str) -> bool:
    """An address typed as an IP that is not on the public internet (10.x, 192.168.x, 127.x, 169.254.x...)."""
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False  # a host name, not an IP literal


def _is_ip_literal(host: str) -> bool:
    """``host`` is an IP address (any family), not a DNS name."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def resolver_host_value_problems(record: dict, *, inline_rows: dict | None = None) -> list[str]:
    """Every resolver value filling a host-position ``{param}`` must itself be a safe host — no credential
    (``@``), explicit port, query/fragment, or IP literal — else the inflated URL smuggles a target past
    the url_template check (F3 2026-10-04).

    ``inline_rows`` is a {resolver_name: [rows]} map tests use to supply resolver rows without a file."""
    assert isinstance(record, dict), "record required"
    assert inline_rows is None or isinstance(inline_rows, dict), "inline_rows must be a dict or None"
    access = record.get("access") or {}
    template = access.get("url_template") or ""
    if not template:
        return []
    try:
        host_tok = urlsplit(template).netloc
    except ValueError:
        return []
    match = re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", host_tok)
    if match is None:
        return []
    param_name = match.group(1)
    fill = next((p.get("fill") or {} for p in (access.get("params") or [])
                 if p.get("name") == param_name), {})
    if (fill.get("from") or "") != "resolver":
        return []
    name = fill.get("resolver") or ""
    field = fill.get("field") or "key"
    rows = _resolver_rows(name, inline_rows)
    bad: list[str] = []
    seen = 0
    for row in rows[:5000]:  # bounded scan
        seen += 1
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        bad += [f"{name}.{field}={value!r}: {m}" for m in _host_value_problems(value)]
    assert seen or not rows, "rows must be scanned"
    return bad


def _resolver_rows(name: str, inline_rows: dict | None) -> list[dict]:
    """Resolver rows by name — ``inline_rows`` (tests) wins, else read the jsonl table."""
    assert isinstance(name, str), "name must be a string"
    if inline_rows is not None and name in inline_rows:
        return list(inline_rows[name])
    if not name:
        return []
    from .common import read_jsonl  # lazy: schema stays import-light
    path = ROOT / "resolvers" / f"{name}.jsonl"
    if not path.exists():
        return []
    return read_jsonl(path)


def _host_value_problems(value: str) -> list[str]:
    """Problems with a bare host-position value inflated as ``https://<value>``."""
    assert isinstance(value, str), "value must be a string"
    out = url_problems("https://" + value)
    if "@" in value.split("/", 1)[0]:
        out.append("userinfo in host")
    try:
        parsed = urlsplit("https://" + value)
        if parsed.port is not None:
            out.append("explicit port in host")
    except ValueError:
        out.append("not a valid address")
    first = value.split("/", 1)[0]
    if "#" in value or "?" in first:  # a path may carry its query (feeds: ?outputType=xml)
        out.append("query or fragment in host position")
    if _is_ip_literal(first):
        out.append("IP literal in host position")
    return list(dict.fromkeys(out))


def validate_record(r: dict) -> list[str]:
    """Return a list of problems; an empty list means the record is well-formed."""
    errs: list[str] = []
    for k in REQUIRED:
        if k not in r:
            errs.append(f"missing {k}")
    if errs:
        return errs
    if not _ID.match(r["id"]):
        errs.append("id must be a lowercase slug")
    if r["tier"] not in TIERS:
        errs.append(f"tier {r['tier']!r}")
    p = r["provider"]
    if not isinstance(p, dict) or not p.get("name") or p.get("authority") not in AUTHORITY:
        errs.append("provider needs name + authority")
    if not r["categories"] or any(c not in _CATS for c in r["categories"]):
        errs.append(f"unknown categories {[c for c in r['categories'] if c not in _CATS]}")
    if any(k not in _KINDS for k in r["kinds"]):
        errs.append(f"unknown kinds {r['kinds']}")
    if not r["coverage"].get("geo"):
        errs.append("coverage.geo required")
    if "water" in r["coverage"] and r["coverage"]["water"] not in WATER:
        errs.append(f"coverage.water must be one of {'/'.join(WATER)}")
    a = r["access"]
    if a.get("kind") not in ACCESS_KINDS:
        errs.append(f"access.kind {a.get('kind')!r}")
    if a.get("auth") not in AUTH:
        errs.append(f"access.auth {a.get('auth')!r}")
    url = a.get("url_template") or a.get("docs_url") or ""
    if a.get("kind") != "internal":
        if not url:
            errs.append("access needs url_template or docs_url")
        else:
            errs += url_problems(url)
    for name, value in (a.get("headers") or {}).items():  # the app sends only a key slot, never a fixed header
        if not _PARAM.search(str(value)):
            errs.append(f"access.headers.{name}: a fixed header is never sent by the app — use an address "
                        "or format that needs none")
    names = set(_PARAM.findall(a.get("url_template") or ""))
    declared = {q["name"] for q in a.get("params", [])}
    if names - declared:
        errs.append(f"undeclared url params {sorted(names - declared)}")
    for q in a.get("params", []):
        if q.get("kind") not in _PKINDS and q.get("kind") != "key":
            errs.append(f"param {q.get('name')} kind {q.get('kind')!r}")
    if r["terms"].get("status") not in TERMS:
        errs.append(f"terms.status {r['terms'].get('status')!r}")
    if r["freshness"].get("cadence") not in CADENCE:
        errs.append(f"freshness.cadence {r['freshness'].get('cadence')!r}")
    v = r.get("validation", {})
    if v and v.get("status") not in VALIDATION:
        errs.append(f"validation.status {v.get('status')!r}")
    if "answers" in r:
        errs += [f"answers: {e}" for e in answer_problems(r["answers"], declared)]
    return errs


def classify(text: str, limit: int = 3) -> list[str]:
    """Keyword placement into category/subcategory, exactly as the app places an ask (library_index.classify):
    the longest matched keyword wins ("temp" inside a matched "water temp" says nothing about weather), then
    the most keywords, ties in taxonomy order."""
    t = taxonomy()
    low = " " + re.sub(r"[^a-z0-9.&+ ]+", " ", (text or "").lower()) + " "
    matched = [(kw, f"{c['id']}/{s['id']}") for c in t["categories"] for s in c["subcategories"]
               for kw in s["keywords"] if f" {kw} " in low or (len(kw) > 5 and kw in low)]
    said = {kw for kw, _ in matched}
    counts: dict[str, int] = {}
    for kw, cid in matched:
        if not any(kw != longer and f" {kw} " in f" {longer} " for longer in said):
            counts[cid] = counts.get(cid, 0) + 1
    return [cid for cid, _ in sorted(counts.items(), key=lambda x: -x[1])[:limit]]


# --- taxonomy.json: its closed shape (build refuses a taxonomy with problems) ---------------------------

SUB_KEYS = {"id", "label", "kinds", "params", "keywords", "policy", "expects"}
POLICY_KEYS = {"prefer", "match", "resolvers", "max_km", "differ_on", "max_age", "cross_check", "ask_if_ambiguous",
               "measure"}
_KEYWORD = re.compile(r"[a-z0-9.&+]+(?: [a-z0-9.&+]+)*")  # what survives classify's own normalization
_COLUMN = re.compile(r"[A-Z][A-Z0-9]{1,7}")  # a station column the provider names (NDBC WTMP, WVHT)


def taxonomy_problems(t: dict) -> list[str]:
    """Problems with the taxonomy; an empty list means it is well-formed. `expects` names the components a
    complete answer holds (answers.COMPONENTS); `policy.measure` maps a station resolver the policy uses to
    the column a station must report to be offered (water temperature -> buoy WTMP)."""
    from .answers import COMPONENTS
    kinds, pkinds = set(t.get("question_kinds") or []), set(t.get("param_kinds") or [])
    errs, seen = [], set()
    for c in t.get("categories") or []:
        for s in c.get("subcategories") or []:
            cid = f"{c.get('id')}/{s.get('id')}"
            if cid in seen:
                errs.append(f"{cid}: duplicate subcategory")
            seen.add(cid)
            if set(s) - SUB_KEYS:
                errs.append(f"{cid}: unknown keys {sorted(set(s) - SUB_KEYS)}")
            if not s.get("kinds") or set(s["kinds"]) - kinds:
                errs.append(f"{cid}: kinds must come from question_kinds, got {s.get('kinds')}")
            if set(s.get("params") or []) - pkinds:
                errs.append(f"{cid}: unknown params {sorted(set(s['params']) - pkinds)}")
            kw = s.get("keywords")
            if not isinstance(kw, list) or any(not isinstance(k, str) or k != k.strip().lower() or not k for k in kw):
                errs.append(f"{cid}: keywords must be lowercase words or phrases")
            elif dead := [k for k in kw if not _KEYWORD.fullmatch(k)]:
                errs.append(f"{cid}: keywords {dead} never match (classify keeps only a-z 0-9 . & + and spaces)")
            if "expects" in s and (not isinstance(s["expects"], list) or not s["expects"]
                                   or set(s["expects"]) - set(COMPONENTS)
                                   or len(set(s["expects"])) != len(s["expects"])):
                errs.append(f"{cid}: expects must be distinct names from {'/'.join(COMPONENTS)}")
            pol = s.get("policy") or {}
            if set(pol) - POLICY_KEYS:
                errs.append(f"{cid}: unknown policy keys {sorted(set(pol) - POLICY_KEYS)}")
            m = pol.get("measure")
            if m is not None and (not isinstance(m, dict) or not m
                                  or any(r not in (pol.get("resolvers") or []) or not isinstance(col, str)
                                         or not _COLUMN.fullmatch(col) for r, col in m.items())):
                errs.append(f"{cid}: policy.measure must map a resolver of this policy to a station column (WTMP)")
    return errs


# --- asks/: example asks for locate v2 (the app embeds them for its router and dense retrieval) -----------

ASKS = ROOT / "asks"
SOURCE_ASKS, ROUTE_ASKS, ASK_MAX = 10, 12, 80


def offerable(r: dict) -> bool:
    """A curated source the app offers: not failed or refused, not a helper, not retired."""
    return (r.get("tier") == "curated" and (r.get("validation") or {}).get("status") not in ("failed", "refused")
            and r.get("role") != "helper" and not r.get("replaced_by"))


def _said(text: str) -> str:
    return " ".join(text.lower().split())


def _ask_file(path, key: str, n: int, known, examples: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """(key value, ask) pairs of one asks file, and its problems."""
    pairs, errs, seen = [], [], set()
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path.name}:{i}"
        try:
            doc = json.loads(line)
        except ValueError:
            errs.append(f"{where}: not JSON")
            continue
        if not isinstance(doc, dict) or set(doc) != {key, "asks"}:
            errs.append(f"{where}: must hold exactly {key} and asks")
            continue
        k, asks = doc[key], doc["asks"]
        if k not in known:
            errs.append(f"{where}: unknown {key} {k!r}")
            continue
        if k in seen:
            errs.append(f"{where}: {k} appears twice")
        seen.add(k)
        if not isinstance(asks, list) or any(not isinstance(a, str) or not a or a != a.strip() for a in asks):
            errs.append(f"{where}: {k}: asks must be non-empty strings without outer spaces")
            continue
        if len(asks) != n:
            errs.append(f"{where}: {k}: {len(asks)} asks, needs exactly {n}")
        errs += [f"{where}: {k}: over {ASK_MAX} characters: {a!r}" for a in asks if len(a) > ASK_MAX]
        if len({_said(a) for a in asks}) != len(asks):
            errs.append(f"{where}: {k}: a repeated ask")
        errs += [f"{where}: {k}: copies the record's example {a!r}" for a in asks
                 if _said(a) in examples.get(k, set())]
        pairs += [(k, a) for a in asks]
    return pairs, errs


def load_asks(recs: list[dict], tax: dict, directory=None, require_all: bool = True):
    """asks/sources.jsonl ({source_id, asks: 10 per source}) and asks/routes.jsonl ({route, asks: 12 per
    subcategory}) as table rows (source_id, ask) and (category, subcategory, ask), with their problems: an unknown
    source or route, the wrong count, an ask over 80 characters, a repeat, or one of the record's own examples
    (asks are new phrasings). require_all (check) also wants every offerable curated source and every
    subcategory covered; build loads what is there."""
    d = directory or ASKS
    live = [r for r in recs if not r.get("replaced_by")]  # a retired source's asks go with it
    examples = {r["id"]: {_said(e) for e in r.get("examples") or [] if isinstance(e, str)} for r in live}
    routes = [f"{c['id']}/{s['id']}" for c in tax["categories"] for s in c["subcategories"]]
    got, errs = {}, []
    for name, key, n, known in (("sources.jsonl", "source_id", SOURCE_ASKS, examples),
                                ("routes.jsonl", "route", ROUTE_ASKS, set(routes))):
        if not (d / name).exists():
            got[name] = []
            if require_all:
                errs.append(f"{name}: missing")
            continue
        got[name], e = _ask_file(d / name, key, n, known, examples if key == "source_id" else {})
        errs += e
    if require_all:
        have = {k for k, _ in got["sources.jsonl"]}
        errs += [f"sources.jsonl: no asks for {r['id']}" for r in live if offerable(r) and r["id"] not in have]
        have = {k for k, _ in got["routes.jsonl"]}
        errs += [f"routes.jsonl: no asks for {r}" for r in routes if r not in have]
    route_rows = [(*k.split("/", 1), a) for k, a in got["routes.jsonl"]]
    return got["sources.jsonl"], route_rows, errs
