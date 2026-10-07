"""`sourcetool answers-recheck [--since DAYS] [ID ...]`: re-fetch every answered source's live sample and run
the same check as `answers-check`, then write the result INTO the answers file so `checked` means something.

PASS → `checked` = today; `last_failure` and `status` are dropped (a recovered source is served again).
FAIL → the first time: `last_failure` = today. A second failure on a LATER day: `status` = "drifted" — `build`
holds the file back and the app offers the source as a link until a later recheck passes.
SKIP (a keyed source, a kind the check cannot parse) leaves the file alone.

Each file keeps its own formatting (indent, final newline). `--since DAYS` rechecks only files whose `checked`
date is at least that many days old. Exit 1 when any file failed or drifted, so the weekly job turns red and
pushes the changed files for a PR. Phase 2a of the Round 19 plan (2026-10-06): the drift detection the
authoring loop was missing — before this, `checked` was never updated after a file was created.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

from .answers import ANSWERS, Failed, Skip, _records, check_sample, fetch_sample, load_answers, params_of

MAX_PROBLEMS_SHOWN = 3
NETWORK_RETRY_S = 2.0  # one retry for a connection-level failure: a transient is not drift evidence


def _today() -> str:
    return _dt.date.today().isoformat()


def read_file(path: Path) -> tuple[dict, int, bool]:
    """(data, indent, ends_with_newline) — the formatting is kept so a recheck diff is one or two lines."""
    text = path.read_text()
    lines = text.splitlines()
    indent = (len(lines[1]) - len(lines[1].lstrip())) if len(lines) > 1 else 1
    return json.loads(text), max(indent, 1), text.endswith("\n")


def write_file(path: Path, data: dict, indent: int, newline: bool) -> None:
    assert isinstance(data, dict) and indent >= 1, "args required"
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if newline else ""))


def apply_result(data: dict, ok: bool, today: str) -> str:
    """Advance the file's recheck state; returns the verdict word: pass | recovered | fail | drifted."""
    assert isinstance(data, dict) and isinstance(today, str), "args required"
    if ok:
        verdict = "recovered" if data.get("status") == "drifted" else "pass"
        data["checked"] = today
        data.pop("last_failure", None)
        data.pop("status", None)
        return verdict
    last = data.get("last_failure")
    data["last_failure"] = today
    if data.get("status") == "drifted" or (isinstance(last, str) and last < today):
        data["status"] = "drifted"
        return "drifted"
    return "fail"


def _fetch_with_retry(fetch, rec: dict, *, pause: float = NETWORK_RETRY_S):
    """One retry when the fetch itself failed (connection, timeout); an HTTP status, a robots refusal or a
    parse failure is the source's answer and is not retried."""
    assert callable(fetch) and isinstance(rec, dict), "args required"
    for attempt in range(2):  # bounded: at most one retry
        try:
            return fetch(rec, cache_hours=0)
        except Failed as e:
            if attempt == 0 and str(e).startswith("fetch failed"):
                time.sleep(pause)
                continue
            raise
    raise AssertionError("unreachable: the loop returns or raises")


def _due(data: dict, since_days: int, today: str) -> bool:
    if since_days <= 0:
        return True
    checked = _dt.date.fromisoformat(data["checked"])
    return (_dt.date.fromisoformat(today) - checked).days >= since_days


def run(ids: list[str], since_days: int = 0, *, directory: Path = ANSWERS, fetch=fetch_sample,
        today: str | None = None) -> int:
    """Recheck the given ids (all answered sources when empty). Prints one line per file and a summary."""
    assert isinstance(ids, list) and since_days >= 0, "args required"
    today = today or _today()
    recs = _records()
    loaded, errs = load_answers({k: params_of(r) for k, r in recs.items()}, directory, include_drifted=True)
    for e in errs:
        print(f"INVALID {e}")
    counts = {"pass": 0, "recovered": 0, "fail": 0, "drifted": 0, "skip": 0, "not_due": 0}
    for sid in ids or sorted(loaded):
        if sid not in loaded:
            print(f"FAIL {sid}: no valid answers file")
            counts["fail"] += 1
            continue
        path = directory / f"{sid}.json"
        data, indent, newline = read_file(path)
        if not _due(data, since_days, today):
            counts["not_due"] += 1
            continue
        try:
            _, sample = _fetch_with_retry(fetch, recs[sid])
        except Skip as e:
            print(f"SKIP {sid}: {e}")
            counts["skip"] += 1
            continue
        except Failed as e:
            problems = [str(e)]
        else:
            problems = check_sample(loaded[sid], sample, params_of(recs[sid]))
        verdict = apply_result(data, not problems, today)
        counts[verdict] += 1
        write_file(path, data, indent, newline)
        detail = "; ".join(problems[:MAX_PROBLEMS_SHOWN]) if problems else f"{len(loaded[sid])} answers"
        print(f"{verdict.upper()} {sid}: {detail}")
    print(f"recheck {today}: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v)
          + f" | {len(errs)} invalid files")
    return 1 if counts["fail"] or counts["drifted"] or errs else 0


def main(args: list[str]) -> int:
    since = 0
    ids: list[str] = []
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a == "--since":
            assert rest and rest[0].isdigit(), "usage: answers-recheck [--since DAYS] [ID ...]"
            since = int(rest.pop(0))
        else:
            ids.append(a)
    return run(ids, since)
