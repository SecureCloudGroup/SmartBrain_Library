"""The SmartBrain Library API (docs/PLAN.md section 4, operator ruling R7): the app submits votes and
suggestions here instead of through Git; the operator's `sourcetool ingest` pulls the queue.

Run from the repository root, so the service checks records with the same `sourcetool` rules as CI:

    uvicorn --factory service.app:create_app --host 127.0.0.1 --port 8095 --no-access-log --no-server-header

Privacy (docs/POLICY.md): no accounts, cookies or install ids. The queue stores only what the app
sends (a source id and verdict, or a source record with parameter values removed), the app version and
the day. Client IPs live only in the in-memory rate limiter and are never written or logged. The
service never fetches a submitted URL.

Configuration (environment):
  LIBRARY_DB               SQLite queue file (default: service/data/queue.sqlite3)
  LIBRARY_MANIFEST         JSON file with the current pack: {"tag", "url", "sha256", "bytes"}
  LIBRARY_ADMIN_TOKEN      bearer token for /admin (at least 32 characters; unset = admin disabled)
  LIBRARY_TRUSTED_PROXIES  comma-separated proxy addresses or networks whose X-Forwarded-For is
                           believed (default 127.0.0.1; behind the VPS's Caddy container: 172.16.0.0/12)
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sourcetool.submission import APP_VERSION, clean_suggestion, vote_problems

PREFIX = "/library/v1"
MAX_BODY = 16 * 1024
MAX_QUEUE = 100_000  # rows waiting for ingest; beyond this the service refuses new ones (503)
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-resource-policy", b"same-origin"),
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Settings:
    db: Path
    manifest: Path | None = None
    admin_token: str = ""
    trusted_proxies: frozenset[str] = frozenset({"127.0.0.1"})
    votes_per_hour: int = 30
    suggestions_per_hour: int = 10
    max_queue: int = MAX_QUEUE

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(db=Path(env.get("LIBRARY_DB") or Path(__file__).parent / "data" / "queue.sqlite3"),
                   manifest=Path(env["LIBRARY_MANIFEST"]) if env.get("LIBRARY_MANIFEST") else None,
                   admin_token=env.get("LIBRARY_ADMIN_TOKEN", ""),
                   trusted_proxies=frozenset(p.strip() for p in env.get("LIBRARY_TRUSTED_PROXIES", "127.0.0.1")
                                             .split(",") if p.strip()))


# ------------------------------------------------------------------------------------------ request models
_VERSION = APP_VERSION.pattern


class Vote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]{1,119}$")
    verdict: Literal["yes", "no", "broken"]
    app_version: str = Field(max_length=32, pattern=_VERSION)


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    record: dict[str, Any]
    via: Literal["form", "yes"]
    app_version: str = Field(max_length=32, pattern=_VERSION)


class Ack(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    through: int = Field(ge=0)


# ------------------------------------------------------------------------------------------ pieces
class RateLimiter:
    """Per-client token buckets, in memory only (a restart forgets every address)."""

    def __init__(self) -> None:
        self._b: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, client: str, bucket: str, per_hour: int) -> float:
        """0 when allowed; otherwise the seconds until one more request is allowed."""
        rate = per_hour / 3600.0
        now = time.monotonic()
        with self._lock:
            if len(self._b) > 50_000:  # forget clients whose bucket has refilled
                self._b = {k: v for k, v in self._b.items() if v[0] + (now - v[1]) * rate < per_hour}
            tokens, at = self._b.get((client, bucket), (float(per_hour), now))
            tokens = min(float(per_hour), tokens + (now - at) * rate)
            if tokens < 1.0:
                self._b[(client, bucket)] = (tokens, now)
                return (1.0 - tokens) / rate
            self._b[(client, bucket)] = (tokens - 1.0, now)
            return 0.0


class Queue:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "kind TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL)")
        self._lock = threading.Lock()

    def add(self, kind: str, payload: dict, limit: int) -> bool:
        with self._lock:
            if self._db.execute("SELECT count(*) FROM queue").fetchone()[0] >= limit:
                return False
            # the day only: no time of day that could be matched against anything else
            self._db.execute("INSERT INTO queue(kind, day, payload) VALUES (?, ?, ?)",
                             (kind, time.strftime("%Y-%m-%d", time.gmtime()), json.dumps(payload, sort_keys=True)))
            return True

    def items(self, since: int, limit: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, kind, day, payload FROM queue WHERE id > ? ORDER BY id LIMIT ?",
                                    (since, limit)).fetchall()
        return [{"id": i, "kind": k, "day": d, "payload": json.loads(p)} for i, k, d, p in rows]

    def ack(self, through: int) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM queue WHERE id <= ?", (through,)).rowcount

    def ok(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1").fetchone() == (1,)


def client_address(scope: dict, trusted: list) -> str:
    """The caller's address: the peer, or the rightmost X-Forwarded-For entry when the peer is our proxy
    (Caddy sets that entry to the address it saw, so a client cannot choose it)."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    try:
        ours = any(ipaddress.ip_address(peer) in net for net in trusted)
    except ValueError:
        ours = False
    if ours:
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-for":
                last = v.decode("latin-1").split(",")[-1].strip()
                if last:
                    return last
    return peer


class Guard:
    """Pure ASGI: body cap, JSON-only POSTs, security headers on every response."""

    def __init__(self, app, cap: int = MAX_BODY) -> None:
        self.app, self.cap = app, cap

    @staticmethod
    async def _reply(send, status: int, error: str) -> None:
        body = json.dumps({"error": error}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()), *SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.cap):
            return await self._reply(send, 413, "request body too large")
        if scope["method"] == "POST" and \
                headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            return await self._reply(send, 415, "send application/json")
        body, more = b"", True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > self.cap:
                return await self._reply(send, 413, "request body too large")
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), *SECURITY_HEADERS]}
            await send(msg)

        await self.app(scope, replay, send_with_headers)


# ------------------------------------------------------------------------------------------ the app
def create_app(settings: Settings | None = None) -> Guard:
    s = settings or Settings.from_env()
    queue = Queue(s.db)
    limiter = RateLimiter()
    proxies = [ipaddress.ip_network(p, strict=False) for p in s.trusted_proxies]
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def _limited(request: Request, bucket: str, per_hour: int) -> JSONResponse | None:
        wait = limiter.take(client_address(request.scope, proxies), bucket, per_hour)
        if wait:
            return JSONResponse({"error": "too many requests"}, status_code=429,
                                headers={"Retry-After": str(max(1, math.ceil(wait)))})
        return None

    def _admin(request: Request) -> JSONResponse | None:
        given = request.headers.get("authorization", "")
        want = f"Bearer {s.admin_token}"
        if len(s.admin_token) < 32 or not hmac.compare_digest(given.encode(), want.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return None

    @api.exception_handler(RequestValidationError)
    async def _invalid(_request, exc: RequestValidationError):
        # names what is wrong, never echoes the submitted values
        problems = [f"{'.'.join(str(x) for x in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": "invalid request", "problems": problems[:10]}, status_code=422)

    @api.get(f"{PREFIX}/healthz")
    def healthz():
        return {"ok": queue.ok()}

    @api.get(f"{PREFIX}/manifest")
    def manifest():
        try:
            m = json.loads(s.manifest.read_text()) if s.manifest else None
        except (OSError, ValueError):
            m = None
        good = (isinstance(m, dict) and isinstance(m.get("tag"), str) and isinstance(m.get("url"), str)
                and m["url"].startswith("https://") and isinstance(m.get("sha256"), str)
                and _SHA256.match(m["sha256"]) and isinstance(m.get("bytes"), int) and m["bytes"] > 0)
        if not good:
            return JSONResponse({"error": "no pack published"}, status_code=503)
        return {k: m[k] for k in ("tag", "url", "sha256", "bytes")}

    @api.post(f"{PREFIX}/votes", status_code=202)
    def votes(vote: Vote, request: Request):
        if (r := _limited(request, "votes", s.votes_per_hour)) is not None:
            return r
        problems = vote_problems(vote.source_id, vote.verdict)
        if problems:
            return JSONResponse({"error": "rejected", "problems": problems}, status_code=422)
        if not queue.add("vote", vote.model_dump(), s.max_queue):
            return JSONResponse({"error": "busy, try later"}, status_code=503, headers={"Retry-After": "3600"})
        return {"accepted": True}

    @api.post(f"{PREFIX}/suggestions", status_code=202)
    def suggestions(sug: Suggestion, request: Request):
        if (r := _limited(request, "suggestions", s.suggestions_per_hour)) is not None:
            return r
        record, problems = clean_suggestion(sug.record)
        if problems:
            return JSONResponse({"error": "rejected", "problems": problems[:20]}, status_code=422)
        if not queue.add("suggestion", {"record": record, "via": sug.via, "app_version": sug.app_version},
                         s.max_queue):
            return JSONResponse({"error": "busy, try later"}, status_code=503, headers={"Retry-After": "3600"})
        return {"accepted": True, "id": record["id"]}

    @api.get(f"{PREFIX}/admin/queue")
    def admin_queue(request: Request, since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
        if (r := _admin(request)) is not None:
            return r
        items = queue.items(since, limit)
        return {"items": items, "next": items[-1]["id"] if items else since}

    @api.post(f"{PREFIX}/admin/ack")
    def admin_ack(ack: Ack, request: Request):
        if (r := _admin(request)) is not None:
            return r
        return {"deleted": queue.ack(ack.through)}

    return Guard(api)
