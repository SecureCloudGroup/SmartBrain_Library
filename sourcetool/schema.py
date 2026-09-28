"""The Library source record: fields, allowed values, and the validator every record must pass.

The same rules run in CI (on every PR), in `sourcetool validate`, and in the app when a user adds a
local source, so a record means the same thing everywhere.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .common import taxonomy

TIERS = ("curated", "provider_trusted", "harvested", "local")
AUTHORITY = ("official", "primary", "aggregator", "community")
ACCESS_KINDS = ("http_json", "http_csv", "http_xml", "rss", "atom", "gtfs", "gtfs_rt", "gbfs", "ics", "html",
                "image", "text", "docs_only", "internal")
AUTH = ("none", "free_key", "oauth", "user_account")
TERMS = ("public_domain", "open_license", "terms_allow", "unverified")
CADENCE = ("realtime", "minutes", "hourly", "daily", "weekly", "monthly", "quarterly", "annual", "irregular", "static")
VALIDATION = ("unvalidated", "ok", "degraded", "failed", "refused")

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
            u = urlsplit(url.replace("{", "").replace("}", ""))
            host_is_param = bool(re.fullmatch(r"\{[a-z_][a-z0-9_]*\}", urlsplit(url).netloc))
            if u.scheme != "https":
                errs.append("https only")
            # a {param} host is filled by a resolver whose values are checked when filled
            if not host_is_param and (not u.hostname or "." not in u.hostname
                                      or u.hostname.endswith((".local", ".internal"))):
                errs.append("public host only")
            if _SECRETISH.search(url) and "{" not in _SECRETISH.search(url).group(2):
                errs.append("url carries a credential; keys come from the user's own vault as a {param}")
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
    return errs


def classify(text: str, limit: int = 3) -> list[str]:
    """Keyword placement into category/subcategory (a draft for review, never the final word)."""
    t = taxonomy()
    low = " " + re.sub(r"[^a-z0-9.&+ ]+", " ", text.lower()) + " "
    scored = []
    for c in t["categories"]:
        for s in c["subcategories"]:
            hits = sum(1 for kw in s["keywords"] if f" {kw} " in low or (len(kw) > 5 and kw in low))
            if hits:
                scored.append((hits, f"{c['id']}/{s['id']}"))
    scored.sort(key=lambda x: -x[0])
    return [cid for _, cid in scored[:limit]]
