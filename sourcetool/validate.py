"""`sourcetool validate`: probe sources with their example parameters and record the result.

Checks, in order: robots.txt allows the URL; HTTP 200; the body parses as the declared format and is not
empty. A source that needs the user's own key and has no public demo key is left "unvalidated" (we never
hold user keys). Internal (on-device) sources are not probed.
"""
from __future__ import annotations

import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from .common import Refused, get, now_iso, robots_allows

# robots.txt is the crawler convention: it governs pages we would crawl or scrape (HTML, search
# results). Documented API and feed endpoints are governed by the provider's API terms, which each
# record carries in `terms`; many API hosts disallow "/" to keep search engines from indexing
# responses. The robots result is still recorded on every source (validation.robots) so the policy
# can be tightened by a filter. See docs/POLICY.md.
_API_KINDS = {"http_json", "http_csv", "http_xml", "rss", "atom", "gtfs", "gtfs_rt", "gbfs", "ics", "image", "text"}

_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def fill(template: str, params: list[dict]) -> str | None:
    ex = {p["name"]: p.get("example") for p in params}
    missing = [n for n in _PARAM.findall(template) if not ex.get(n)]
    if missing:
        return None
    host_param = re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", urlsplit(template).netloc)

    def sub(m):
        v = str(ex[m.group(1)])
        return v if (host_param and m.group(1) == host_param.group(1)) else quote(v, safe=",.-_:~")
    return _PARAM.sub(sub, template)


def _format_ok(kind: str, body: bytes, ctype: str) -> tuple[bool, str]:
    if not body.strip():
        return False, "empty body"
    try:
        if kind in ("http_json", "gbfs"):
            j = json.loads(body)
            return (bool(j), "json") if j not in ({}, []) else (False, "empty json")
        if kind in ("rss", "atom", "http_xml"):
            root = ElementTree.fromstring(body)
            items = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
            if kind in ("rss", "atom") and not items:
                return False, "feed has no items"
            return True, f"xml {len(items)} items"
        if kind == "http_csv":
            rows = list(csv.reader(io.StringIO(body.decode("utf-8", "replace"))))
            return (len(rows) >= 2, f"csv {len(rows)} rows")
        if kind == "ics":
            return (b"BEGIN:VCALENDAR" in body[:200], "ics")
        if kind == "image":
            return (ctype.startswith("image/"), ctype)
        if kind == "gtfs_rt":
            return (len(body) > 20, f"protobuf {len(body)}B")
        if kind == "text":
            return (len(body) > 20, f"text {len(body)}B")
        return True, kind
    except (ValueError, ElementTree.ParseError) as e:
        return False, f"parse: {type(e).__name__}"


def validate_one(r: dict) -> dict:
    a = r["access"]
    v = {"checked_at": now_iso()}
    if a["kind"] in ("internal", "docs_only"):
        return {**v, "status": "unvalidated", "note": "not probed (" + a["kind"] + ")"}
    if a.get("contact_ua"):
        return {**v, "status": "unvalidated", "note": "needs the user's contact email in the User-Agent (R11)"}
    tpl = a.get("url_template") or ""
    url = fill(tpl, a.get("params", []))
    if url is None:
        return {**v, "status": "unvalidated", "note": "needs a user value or key to probe"}
    headers = {}
    for hk, hv in (a.get("headers") or {}).items():
        if "{" in hv:
            return {**v, "status": "unvalidated", "note": "needs the user's key in a header"}
        headers[hk] = hv
    allowed = robots_allows(url)
    v["robots"] = "allow" if allowed else "disallow"
    if not allowed and (a["kind"] not in _API_KINDS or not r.get("access", {}).get("docs_url")):
        return {**v, "status": "refused", "note": "robots.txt disallows (not a documented API)"}
    if a["kind"] == "gtfs":  # static GTFS is a zip of tens of MB: read only the first 4 KB
        headers = {**headers, "Range": "bytes=0-4095"}
    try:
        status, body, ctype = get(url, cache_hours=12, headers=headers, api=True)
    except Refused:
        return {**v, "status": "refused", "note": "robots.txt disallows"}
    except Exception as e:  # network failure is a validation result, not a crash
        return {**v, "status": "failed", "note": f"{type(e).__name__}"}
    if status == 206 or (status == 200 and a["kind"] == "gtfs"):
        return {**v, "status": "ok" if body[:2] == b"PK" else "degraded", "http": status,
                "note": "zip" if body[:2] == b"PK" else "not a zip", "bytes": len(body)}
    if status != 200:
        return {**v, "status": "failed", "http": status}
    ok, note = _format_ok(a["kind"], body, ctype)
    return {**v, "status": "ok" if ok else "degraded", "http": status, "note": note, "bytes": len(body)}


def _interleave_by_host(records: list[dict]) -> list[dict]:
    """Round-robin across hosts so workers never queue behind one host's 1 req/s limit."""
    by_host: dict[str, list[dict]] = {}
    for r in records:
        host = urlsplit((r["access"].get("url_template") or "").replace("{", "").replace("}", "")).netloc
        by_host.setdefault(host, []).append(r)
    queues = list(by_host.values())
    out: list[dict] = []
    while queues:
        out.extend(q.pop(0) for q in queues)
        queues = [q for q in queues if q]
    return out


def validate_all(records: list[dict], workers: int = 16) -> list[dict]:
    order = _interleave_by_host(records)
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(validate_one, order))
    for r, v in zip(order, results):
        r["validation"] = v
    return records
