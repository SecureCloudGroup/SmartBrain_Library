"""What the Library API accepts from the app (docs/PLAN.md sections 3-4), checked the same way by the
service on the VPS (`service/app.py`) and again by `sourcetool ingest` on the operator's side.

A vote names a Library source by id. A suggestion is a new source record with its parameter VALUES
removed: the template and the parameter kinds are kept, the user's station, ticker or place are not.
Nothing the client says about trust is kept: the tier, votes, validation, terms status, authority and
origin are set here, and the id is derived from the record, never taken from the client.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .common import slug
from .schema import _ID, url_problems, validate_record

VERDICTS = ("yes", "no", "broken")
VIA = ("form", "yes")
APP_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")

# fields a suggestion may carry, and the ones the server owns (silently dropped, then set here)
ACCEPTED = {"name", "description", "provider", "categories", "kinds", "coverage", "access", "terms", "freshness",
            "examples", "notes"}
SERVER_OWNED = {"id", "tier", "votes", "validation", "signals", "origin", "role", "audience"}
ACCESS_KEYS = {"kind", "url_template", "docs_url", "params", "auth", "headers", "contact_ua"}
PARAM_KEYS = {"name", "kind", "required", "example"}
_PARAM_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,39}$")
_SECRET_HEADER = re.compile(r"auth|token|key|secret|password|session|cookie", re.I)

MAX_RECORD_BYTES = 12_000
MAX_STR = 2048
MAX_LIST = 32
LIMITS = {"name": 200, "description": 1000, "notes": 1000}


def vote_problems(source_id: object, verdict: object) -> list[str]:
    errs = []
    if not isinstance(source_id, str) or not _ID.match(source_id):
        errs.append("source_id must be a Library id (a lowercase slug of at most 120 characters)")
    if verdict not in VERDICTS:
        errs.append(f"verdict must be one of {', '.join(VERDICTS)}")
    return errs


def _walk(v: object, depth: int = 0) -> list[str]:
    if depth > 5:
        return ["record nests too deeply"]
    if isinstance(v, str):
        return [f"a text field is longer than {MAX_STR} characters"] if len(v) > MAX_STR else []
    if isinstance(v, list):
        return ([f"a list is longer than {MAX_LIST} items"] if len(v) > MAX_LIST else []) + \
            [e for x in v for e in _walk(x, depth + 1)]
    if isinstance(v, dict):
        return [e for k, x in v.items() for e in _walk(k, depth + 1) + _walk(x, depth + 1)]
    if v is None or isinstance(v, (bool, int, float)):
        return []
    return ["unsupported value"]


def _shape_problems(r: dict) -> list[str]:
    """Types first, so the schema rules never meet a value of the wrong type."""
    errs = []
    for k in ("name", "description"):
        if not isinstance(r.get(k), str) or not r[k].strip():
            errs.append(f"{k} is required text")
    for k, n in LIMITS.items():
        if isinstance(r.get(k), str) and len(r[k]) > n:
            errs.append(f"{k} is longer than {n} characters")
    for k in ("provider", "coverage", "access", "terms", "freshness"):
        if not isinstance(r.get(k), dict):
            errs.append(f"{k} must be an object")
    if isinstance(r.get("provider"), dict) and not isinstance(r["provider"].get("name"), str):
        errs.append("provider.name is required text")
    for k in ("categories", "kinds", "examples"):
        if k in r and (not isinstance(r[k], list) or not all(isinstance(x, str) for x in r[k])):
            errs.append(f"{k} must be a list of text")
    if "notes" in r and not isinstance(r["notes"], str):
        errs.append("notes must be text")
    a = r.get("access")
    if isinstance(a, dict):
        if set(a) - ACCESS_KEYS:
            errs.append(f"unknown access fields {sorted(set(a) - ACCESS_KEYS)}")
        for k in ("url_template", "docs_url"):
            if k in a and not isinstance(a[k], str):
                errs.append(f"access.{k} must be text")
        if not isinstance(a.get("params", []), list) or not all(isinstance(q, dict) for q in a.get("params", [])):
            errs.append("access.params must be a list of objects")
        h = a.get("headers", {})
        if not isinstance(h, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in h.items()):
            errs.append("access.headers must map names to text")
    return errs


def _access_problems(a: dict) -> list[str]:
    errs = []
    for q in a.get("params", []):
        if set(q) - PARAM_KEYS:
            errs.append(f"unknown param fields {sorted(set(q) - PARAM_KEYS)}")
        if not isinstance(q.get("name"), str) or not _PARAM_NAME.match(q["name"]):
            errs.append("param name must be a short lowercase identifier")
        if q.get("example") not in (None, ""):
            errs.append(f"param {q.get('name')!r} carries a value; parameter values are removed before sending")
    for name, value in a.get("headers", {}).items():
        if _SECRET_HEADER.search(name) and "{key}" not in value:
            errs.append(f"header {name!r} carries a credential; keys come from the user's own vault as {{key}}")
    if a.get("docs_url") and a.get("url_template"):
        errs += [f"docs_url: {e}" for e in url_problems(a["docs_url"])]
    return errs


def suggestion_id(r: dict) -> str:
    """A stable id from where the source lives and what it is called (never the client's own id)."""
    a = r["access"]
    url = (a.get("url_template") or a.get("docs_url") or "").replace("{", "").replace("}", "")
    try:
        host = urlsplit(url).hostname
    except ValueError:  # a malformed address: validate_record names the problem
        host = ""
    sid = slug(host or "", r["name"])[:120].strip("-")
    return sid if _ID.match(sid) else slug("suggested", sid)[:120].strip("-")


def clean_suggestion(record: object) -> tuple[dict, list[str]]:
    """(the record as the Library would hold it, problems). An empty problem list means accepted."""
    if not isinstance(record, dict):
        return {}, ["record must be an object"]
    errs = _walk(record)
    if len(json.dumps(record)) > MAX_RECORD_BYTES:
        errs.append(f"record is larger than {MAX_RECORD_BYTES} bytes")
    unknown = set(record) - ACCEPTED - SERVER_OWNED
    if unknown:
        errs.append(f"unknown fields {sorted(unknown)}")
    rec = {k: v for k, v in record.items() if k in ACCEPTED}
    errs += _shape_problems(rec)
    if errs:
        return {}, errs
    errs += _access_problems(rec["access"])
    if rec["provider"].get("url"):
        errs += [f"provider.url: {e}" for e in url_problems(str(rec["provider"]["url"]))]
    # trust is decided in review, never by the client
    rec["provider"] = {**rec["provider"], "authority": "community"}
    rec["terms"] = {**rec["terms"], "status": "unverified"}
    rec["access"] = {**rec["access"], "params": [{**q, "example": None} for q in rec["access"].get("params", [])]}
    rec.setdefault("examples", [])
    rec.setdefault("notes", "")
    rec.update(tier="harvested", origin={"by": "user-suggestion"}, validation={"status": "unvalidated"},
               votes={"yes": 0, "no": 0})
    rec["id"] = suggestion_id(rec)
    errs += validate_record(rec)
    return (rec, []) if not errs else ({}, errs)
