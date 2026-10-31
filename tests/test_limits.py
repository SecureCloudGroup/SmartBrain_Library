"""access.limits (2026-10-31): each source's documented free request limits, so the app can pick a source whose free
plan covers how often a card refreshes.

"NVDA stock price every 30 minutes" is 48 requests a day. Alpha Vantage's free key allows 25, so from about midday
the card would sit on its last value; Finnhub's free key allows 60 a minute. Until this field the Library said so only
in prose (``terms.note``), which no code reads. The numbers come from each provider's own terms, as the note quotes
them; Alpha Vantage's 1 request a second is its own limit message, captured on the operator's machine on 2026-10-31.

Offline. The last test reads the built pack and skips without one.
"""
from __future__ import annotations

import json
import re

import duckdb
import pytest

from sourcetool import build as build_mod
from sourcetool.common import SOURCES, read_jsonl
from sourcetool.schema import LIMIT_REQUESTS_MAX, LIMIT_SECONDS_MAX, LIMITS_MAX, limit_problems, validate_record
from sourcetool.submission import clean_suggestion

RECORDS = {r["id"]: r for r in read_jsonl(SOURCES / "curated" / "core.jsonl")}
DAY, HOUR, MINUTE = 86400, 3600, 60

# a request limit as a note names it: "25 requests/day", "1 request per 3 s", "60 req/min", "25 queries/day"
_NAMED = re.compile(r"\b(\d+) (?:requests?|req|queries|query|calls?) ?(?:/|per|a|an|each|every) ?(?:(\d+) ?)?"
                    r"(seconds?|sec|s|minutes?|min|hours?|h|days?|months?)\b", re.IGNORECASE)
_NO_BURST = re.compile(r"\bno multiple requests per second\b", re.IGNORECASE)  # gold-api's terms: 1 a second
_UNIT_S = {"s": 1, "sec": 1, "second": 1, "seconds": 1, "min": MINUTE, "minute": MINUTE, "minutes": MINUTE,
           "h": HOUR, "hour": HOUR, "hours": HOUR, "day": DAY, "days": DAY, "month": 31 * DAY, "months": 31 * DAY}


def _named(note: str) -> set[tuple[int, int]]:
    """The (requests, seconds) limits a terms note names."""
    named = {(int(n), int(k or 1) * _UNIT_S[unit.lower()]) for n, k, unit in _NAMED.findall(note)}
    return named | ({(1, 1)} if _NO_BURST.search(note) else set())


def _declared(record: dict) -> set[tuple[int, int]]:
    return {(w["requests"], w["seconds"]) for w in record["access"].get("limits") or []}


def test_the_note_reader_reads_each_way_a_note_names_a_limit() -> None:
    assert _named("free key, 25 requests/day") == {(25, DAY)}
    assert _named("API terms: 1 request per 3 s") == {(1, 3)}
    assert _named("public API; 1 req/s; User-Agent required") == {(1, 1)}
    assert _named("E-utilities: 3 req/s without key") == {(3, 1)}
    assert _named("v1 API keyless (25 queries/day)") == {(25, DAY)}
    assert _named("public REST API, 60 requests/hour unauthenticated") == {(60, HOUR)}
    assert _named("free key, 25 requests/day; 1 request/s burst limit") == {(25, DAY), (1, 1)}
    assert _named("terms allow any lawful use; no multiple requests per second") == {(1, 1)}
    assert _named("documented free API key 123 (30 requests/min)") == {(30, MINUTE)}
    assert _named("public API, rate-limited") == set()  # a limit with no number is unknown, not a guess


@pytest.mark.parametrize("limits", [
    [{"requests": 25, "seconds": DAY}],
    [{"requests": 1, "seconds": 1}, {"requests": 25, "seconds": DAY}],
    [{"requests": LIMIT_REQUESTS_MAX, "seconds": LIMIT_SECONDS_MAX}],
    [{"requests": n + 1, "seconds": s} for n, s in enumerate((1, MINUTE, HOUR, DAY))],
])
def test_a_documented_limit_passes(limits) -> None:
    assert limit_problems(limits) == []


@pytest.mark.parametrize("limits, problem", [
    (None, "must be a list of 1 to 4"),
    ([], "must be a list of 1 to 4"),
    ({"requests": 25, "seconds": DAY}, "must be a list of 1 to 4"),
    ([{"requests": n + 1, "seconds": n + 1} for n in range(LIMITS_MAX + 1)], "must be a list of 1 to 4"),
    ([{"requests": 25}], "must be {requests, seconds} and nothing else"),
    ([{"requests": 25, "seconds": DAY, "per": "key"}], "must be {requests, seconds} and nothing else"),
    (["25/day"], "must be {requests, seconds} and nothing else"),
    ([{"requests": 0, "seconds": DAY}], "requests must be a whole number"),
    ([{"requests": LIMIT_REQUESTS_MAX + 1, "seconds": DAY}], "requests must be a whole number"),
    ([{"requests": 2.5, "seconds": DAY}], "requests must be a whole number"),
    ([{"requests": "25", "seconds": DAY}], "requests must be a whole number"),
    ([{"requests": True, "seconds": DAY}], "requests must be a whole number"),
    ([{"requests": 25, "seconds": 0}], "seconds must be a whole number"),
    ([{"requests": 25, "seconds": LIMIT_SECONDS_MAX + 1}], "seconds must be a whole number"),
    ([{"requests": 25, "seconds": DAY}, {"requests": 30, "seconds": DAY}], "the same 86400 s window"),
])
def test_a_malformed_limit_is_refused_with_what_is_wrong(limits, problem) -> None:
    problems = limit_problems(limits)
    assert problems and any(problem in p for p in problems), problems


def test_a_record_is_checked_for_limits_only_when_it_has_them() -> None:
    record = json.loads(json.dumps(RECORDS["finnhub-quote"]))
    assert validate_record(record) == []
    del record["access"]["limits"]
    assert validate_record(record) == []  # unknown is allowed: most sources document no number
    record["access"]["limits"] = [{"requests": 60, "seconds": MINUTE}, {"requests": 30, "seconds": MINUTE}]
    assert validate_record(record) == ["access.limits[1]: another limit has the same 60 s window"]


def test_every_note_that_names_a_request_limit_carries_it_and_no_record_carries_one_its_note_does_not_name() -> None:
    # prose and data in step both ways: a note's number the field lacks is a limit the app can't see; a field the
    # note doesn't name is a number nobody can check against the provider's words
    drift = {sid: {"note": sorted(_named(r["terms"].get("note") or "")), "limits": sorted(_declared(r))}
             for sid, r in RECORDS.items() if _named(r["terms"].get("note") or "") != _declared(r)}
    assert not drift, drift
    assert sum(1 for r in RECORDS.values() if "limits" in r["access"]) == 21


def test_the_limits_that_decide_the_stock_card_are_the_providers_own() -> None:
    # the proof ask: 48 requests a day fits Finnhub's free key and not Alpha Vantage's
    for sid in ("alphavantage-quote", "alphavantage-daily"):
        assert _declared(RECORDS[sid]) == {(1, 1), (25, DAY)}, sid
    for sid in ("finnhub-quote", "finnhub-earnings-calendar"):
        assert _declared(RECORDS[sid]) == {(60, MINUTE)}, sid
    # the other windows long enough to bind a card that refreshes every few minutes
    assert _declared(RECORDS["bls-series-v1"]) == {(25, DAY)}
    assert all(_declared(RECORDS[s]) == {(15, HOUR)} for s in ("ll2-upcoming-events", "ll2-upcoming-launches"))
    assert all(_declared(RECORDS[s]) == {(60, HOUR)} for s in ("github-advisories", "github-issues", "github-repo"))


def test_a_limit_belongs_to_the_address_it_was_documented_for() -> None:
    # the BLS calendar is a file on www.bls.gov, not the v1 API; the releases feed is github.com, not
    # api.github.com: their notes named the API's limit, and neither carries it
    for sid, host in (("bls-release-calendar", "www.bls.gov"), ("github-releases-atom", "github.com")):
        assert RECORDS[sid]["access"]["url_template"].startswith(f"https://{host}/"), sid
        assert "limits" not in RECORDS[sid]["access"], sid
    for sid, host in (("bls-series-v1", "api.bls.gov"), ("github-repo", "api.github.com")):
        assert RECORDS[sid]["access"]["url_template"].startswith(f"https://{host}/"), sid


def test_a_users_suggestion_cannot_set_a_limit() -> None:
    # limits are the provider's words, read in review: a suggestion that claims one is refused, like any access
    # field review owns
    suggestion = {k: json.loads(json.dumps(RECORDS["finnhub-quote"][k]))
                  for k in ("name", "description", "provider", "categories", "kinds", "coverage", "access", "terms",
                            "freshness")}
    assert "limits" in suggestion["access"]
    _, problems = clean_suggestion(suggestion)
    assert "unknown access fields ['limits']" in problems, problems
    del suggestion["access"]["limits"]
    assert "unknown access fields ['limits']" not in clean_suggestion(suggestion)[1]


def test_built_pack_carries_each_records_limits() -> None:
    if not build_mod.DB.exists():
        pytest.skip("no build/library.duckdb (run `python -m sourcetool build`)")
    con = duckdb.connect(str(build_mod.DB), read_only=True)
    packed = {sid: json.loads(raw) for sid, raw in con.execute("SELECT id, record FROM library_sources").fetchall()
              if sid in RECORDS}
    con.close()
    want = {sid: r["access"]["limits"] for sid, r in RECORDS.items() if "limits" in r["access"]}
    assert {sid: packed[sid]["access"].get("limits") for sid in want} == want
