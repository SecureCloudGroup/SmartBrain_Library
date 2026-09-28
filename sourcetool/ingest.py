"""`sourcetool ingest`: pull the Library API's queue (docs/PLAN.md section 4) into this checkout.

  * every item is checked again with the same rules the service ran (`sourcetool/submission.py`);
  * votes are added to each source's `votes` (a "broken" report counts as a No and is listed, so the
    operator can re-validate that source);
  * new suggestions go to `sources/suggested/<date>.jsonl` with tier `harvested` and
    origin `{by: "user-suggestion"}`, and are probed with `validate`; a suggestion of a source the Library
    already has (same address) counts as a Yes for it when it came from a Yes;
  * the queue is then acknowledged (cleared) up to the last item read.

The operator reviews the diff and opens the PR (`--commit` makes the branch and commit; it never pushes).

Environment: LIBRARY_API_URL (default https://smartbrain.securecloudgroup.com/library/v1) and
LIBRARY_ADMIN_TOKEN (the service's admin token).
"""
from __future__ import annotations

import collections
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .common import ROOT, SOURCES, USER_AGENT, read_jsonl, write_jsonl
from .submission import VIA, clean_suggestion, vote_problems

DEFAULT_API = "https://smartbrain.securecloudgroup.com/library/v1"
SOURCE_DIRS = ("curated", "harvested", "suggested")


def _address(r: dict) -> str:
    a = r.get("access", {})
    return (a.get("url_template") or a.get("docs_url") or "").strip().lower()


def fetch(api: str, token: str) -> list[dict]:
    """Every queued item, oldest first."""
    items: list[dict] = []
    since = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"}, timeout=60.0,
                      trust_env=False) as c:
        while True:
            r = c.get(f"{api}/admin/queue", params={"since": since, "limit": 1000})
            r.raise_for_status()
            page = r.json()
            if not page["items"]:
                return items
            items += page["items"]
            since = page["next"]


def acknowledge(api: str, token: str, through: int) -> int:
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"}, timeout=60.0,
                      trust_env=False) as c:
        r = c.post(f"{api}/admin/ack", json={"through": through})
        r.raise_for_status()
        return r.json()["deleted"]


def apply(items: list[dict], *, sources: Path = SOURCES, probe: bool = True, write: bool = True,
          today: str | None = None) -> dict:
    """Fold the queue into the source files. Returns a summary (what `ingest` prints)."""
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    files = {f: read_jsonl(f) for d in SOURCE_DIRS for f in sorted((sources / d).glob("*.jsonl"))}
    by_id = {r["id"]: (f, r) for f, rows in files.items() for r in rows}
    by_address = {_address(r): r["id"] for _, r in by_id.values() if _address(r)}
    tally: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    new: dict[str, dict] = {}  # address -> the new record
    s = collections.Counter()
    unknown: collections.Counter = collections.Counter()
    for item in items:
        p = item.get("payload") or {}
        if item.get("kind") == "vote":
            if vote_problems(p.get("source_id"), p.get("verdict")):
                s["rejected"] += 1
            elif p["source_id"] not in by_id:
                unknown[p["source_id"]] += 1
            else:
                tally[p["source_id"]][p["verdict"]] += 1
            continue
        rec, problems = clean_suggestion(p.get("record")) if item.get("kind") == "suggestion" else ({}, ["kind"])
        if problems or p.get("via") not in VIA:
            s["rejected"] += 1
            continue
        known = by_address.get(_address(rec))
        if known:  # the Library already has it: a Yes on it is a Yes for that source
            if p["via"] == "yes":
                tally[known]["yes"] += 1
            else:
                s["duplicate suggestions"] += 1
            continue
        first = new.setdefault(_address(rec), rec)
        if first is rec:
            rec["origin"] = {"by": "user-suggestion", "at": today, "via": p["via"]}
            s["new suggestions"] += 1
        else:
            s["duplicate suggestions"] += 1
        if p["via"] == "yes":
            first["votes"]["yes"] += 1

    changed: set[Path] = set()
    for sid, c in tally.items():
        f, r = by_id[sid]
        v = r.setdefault("votes", {"yes": 0, "no": 0})
        v["yes"] = v.get("yes", 0) + c["yes"]
        v["no"] = v.get("no", 0) + c["no"] + c["broken"]
        changed.add(f)
        s["votes"] += sum(c.values())
    taken = set(by_id)
    for rec in new.values():
        base, n = rec["id"], 2
        while rec["id"] in taken:  # a different source already holds this id
            rec["id"] = f"{base[:115]}-{n}"
            n += 1
        taken.add(rec["id"])
    if new and probe:
        from .validate import validate_all
        validate_all(list(new.values()))
    if write:
        for f in changed:
            write_jsonl(f, files[f])
        if new:
            out = sources / "suggested" / f"{today}.jsonl"
            write_jsonl(out, read_jsonl(out) + list(new.values()))
    return {"counts": dict(s), "new": sorted(new[a]["id"] for a in new),
            "broken": sorted(sid for sid, c in tally.items() if c["broken"]), "unknown_ids": dict(unknown),
            "files": sorted(str(f.relative_to(sources)) for f in changed)}


def _api() -> tuple[str, str]:
    api = os.environ.get("LIBRARY_API_URL", DEFAULT_API).rstrip("/")
    token = os.environ.get("LIBRARY_ADMIN_TOKEN", "")
    u = urlsplit(api)
    if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("LIBRARY_API_URL must be https (the admin token must never travel in the clear)")
    if not token:
        raise SystemExit("set LIBRARY_ADMIN_TOKEN (the service's admin token)")
    return api, token


def main(args: list[str]) -> int:
    api, token = _api()
    dry = "--dry-run" in args
    items = fetch(api, token)
    summary = apply(items, probe="--no-probe" not in args and not dry, write=not dry)
    print(f"{len(items)} queued items: {summary['counts']}")
    for k in ("new", "broken", "unknown_ids", "files"):
        if summary[k]:
            print(f"  {k}: {summary[k]}")
    if dry or not items:
        return 0
    print(f"acknowledged {acknowledge(api, token, max(i['id'] for i in items))} items on the service")
    if "--commit" in args:
        branch = f"ingest-{time.strftime('%Y-%m-%d', time.gmtime())}"
        subprocess.run(["git", "checkout", "-b", branch], cwd=ROOT, check=True)
        subprocess.run(["git", "add", "sources"], cwd=ROOT, check=True)
        subprocess.run(["git", "commit", "-m", f"Library ingest: {summary['counts']}"], cwd=ROOT, check=True)
        print(f"committed on {branch}; review it and open the PR")
    return 0
