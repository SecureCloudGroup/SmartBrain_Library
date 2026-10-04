"""Shared plumbing for sourcetool: paths, polite HTTP, record I/O.

Politeness rules (Library policy, see docs/POLICY.md):
- one honest User-Agent that names the project; never a browser disguise
- robots.txt is honored for every page we crawl or scrape; documented API/feed endpoints follow
  the provider's API terms instead (the robots result is still recorded; see docs/POLICY.md)
- at most one request per second per host, one connection per host
- every response is cached under .cache/ so re-runs do not re-hit providers
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "http"
SOURCES = ROOT / "sources"
TAXONOMY = ROOT / "taxonomy" / "taxonomy.json"
BUILD = ROOT / "build"

USER_AGENT = "SmartBrain-Library/0.1 (+https://github.com/SecureCloudGroup/SmartBrain_Library)"
MIN_GAP_S = 1.0

# The app's own fetch identity (SmartBrain_3000 netguard: its User-Agent, the JSON readers' Accept, the page and
# feed reader's Accept). `validate` and `answers-check` probe a source as the app will fetch it, so "validated
# ok" means the app gets the same answer: a content-negotiating API (Django REST Framework: Launch Library 2,
# jolpica, usaspending) serves its HTML page to a browser Accept. Harvesting keeps the Library's USER_AGENT.
APP_USER_AGENT = "SmartBrain/0.24.1 (+https://smartbrain.securecloudgroup.com)"
JSON_ACCEPT = "application/json, text/plain;q=0.5, */*;q=0.1"
PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
JSON_KINDS = ("http_json", "gbfs")


def probe_headers(kind: str, headers: dict | None = None) -> dict:
    """The headers the app sends for a source of this access kind, under the record's own headers (a record's
    Accept, in any case, wins; two Accepts are never sent)."""
    headers = dict(headers or {})
    out = {"User-Agent": APP_USER_AGENT}
    if not any(k.lower() == "accept" for k in headers):
        out["Accept"] = JSON_ACCEPT if kind in JSON_KINDS else PAGE_ACCEPT
    return {**out, **headers}


# a body as text the way the app decodes it (netguard.decode_body): a byte-order mark, then the declared
# charset, then UTF-8 with replacement (field 2026-09-28: AWS's status feed is application/json;charset=utf-16)
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_UTF16 = {"utf-16": None, "utf16": None, "utf-16le": "utf-16-le", "utf-16-le": "utf-16-le",
          "utf-16be": "utf-16-be", "utf-16-be": "utf-16-be"}
_EIGHT_BIT = {"latin-1": "latin-1", "latin1": "latin-1", "iso-8859-1": "latin-1", "iso8859-1": "latin-1",
              "windows-1252": "cp1252", "cp1252": "cp1252"}


def decode_body(content: bytes, content_type: str = "") -> str:
    """BOM -> declared charset -> UTF-8. A declared 8-bit charset over valid UTF-8 is a wrong header; UTF-16
    without a BOM takes its byte order from where the NULs sit. Never raises."""
    for bom, codec in _BOMS:
        if content.startswith(bom):
            return content[len(bom):].decode(codec, "replace")
    declared = ""
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            declared = value.strip().strip("'\"").lower()
    if declared in _UTF16 or (not declared and b"\x00" in content[:64]):
        head = content[:64]
        even = sum(1 for i in range(0, len(head), 2) if head[i] == 0)
        odd = sum(1 for i in range(1, len(head), 2) if head[i] == 0)
        order = _UTF16.get(declared) or (None if even == odd else "utf-16-be" if even > odd else "utf-16-le")
        if order:
            return content.decode(order, "replace")
    if declared in _EIGHT_BIT:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode(_EIGHT_BIT[declared], "replace")
    return content.decode("utf-8", "replace")

_last_hit: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_guard = threading.Lock()


class Refused(Exception):
    """robots.txt disallows the URL; the Library never fetches it."""


def _host_lock(host: str) -> threading.Lock:
    with _guard:
        return _host_locks.setdefault(host, threading.Lock())


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=90.0, follow_redirects=True,
                        trust_env=False)


def robots_allows(url: str) -> bool:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _raw_get(base + "/robots.txt", check_robots=False)
            if r.status_code >= 400:
                _robots[base] = None  # no robots.txt: everything allowed
            else:
                rp.parse(r.text.splitlines())
                _robots[base] = rp
        except httpx.HTTPError:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def _raw_get(url: str, *, check_robots: bool = True, headers: dict | None = None) -> httpx.Response:
    """check_robots=False only for robots.txt itself and documented API calls (see validate.py)."""
    host = urlsplit(url).netloc
    if check_robots and not robots_allows(url):
        raise Refused(url)
    with _host_lock(host):
        gap = time.monotonic() - _last_hit.get(host, 0.0)
        if gap < MIN_GAP_S:
            time.sleep(MIN_GAP_S - gap)
        try:
            with _client() as c:
                if (headers or {}).get("Range"):
                    # stream so a server that ignores Range still costs us only the first chunk
                    with c.stream("GET", url, headers=headers) as r:
                        want = int(headers["Range"].split("-")[1]) + 1
                        buf = b""
                        for chunk in r.iter_bytes():
                            buf += chunk
                            if len(buf) >= want:
                                break
                        r._content = buf[:want]
                        return r
                return c.get(url, headers=headers or {})
        finally:
            _last_hit[host] = time.monotonic()


def _cache_path(url: str, headers: dict | None) -> Path:
    key = hashlib.sha256((url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()
    return CACHE / key[:2] / key


def uncache(url: str, headers: dict | None = None) -> None:
    """Forget a cached answer that turned out unusable (a response cut off mid-body)."""
    cp = _cache_path(url, headers)
    cp.unlink(missing_ok=True)
    cp.with_suffix(".meta").unlink(missing_ok=True)


def get(url: str, *, cache_hours: float = 24.0, headers: dict | None = None, api: bool = False) -> tuple[int, bytes, str]:
    """Polite cached GET. Returns (status, body, content_type)."""
    cp = _cache_path(url, headers)
    if cp.exists() and (time.time() - cp.stat().st_mtime) < cache_hours * 3600:
        meta = json.loads((cp.with_suffix(".meta")).read_text())
        return meta["status"], cp.read_bytes(), meta["ctype"]
    for attempt in range(3):
        r = _raw_get(url, headers=headers, check_robots=not api)
        if r.status_code not in (429, 503):
            break
        # the provider asked us to slow down: wait what it says (capped), then try again
        try:
            wait = float(r.headers.get("retry-after", "") or 20 * (attempt + 1))
        except ValueError:
            wait = 20.0 * (attempt + 1)
        time.sleep(min(wait, 60.0))
    if r.status_code >= 400 and r.status_code not in (404, 410):
        return r.status_code, r.content, r.headers.get("content-type", "")  # never cache a transient error
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(r.content)
    cp.with_suffix(".meta").write_text(json.dumps({"status": r.status_code, "ctype": r.headers.get("content-type", ""),
                                                  "url": url, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    return r.status_code, r.content, r.headers.get("content-type", "")


def get_json(url: str, **kw):
    status, body, _ = get(url, **kw)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for {url}")
    return json.loads(body)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: r["id"])
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def taxonomy() -> dict:
    return json.loads(TAXONOMY.read_text())


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(*parts: str) -> str:
    import re
    s = "-".join(p for p in parts if p)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:120]
