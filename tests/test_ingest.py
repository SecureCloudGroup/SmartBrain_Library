"""`sourcetool ingest` against a fake queue: vote aggregation, suggestions, duplicates, refusals."""
from __future__ import annotations

import copy
import json

import pytest

from sourcetool.common import read_jsonl, write_jsonl
from sourcetool.ingest import apply


def _record(sid: str, url: str) -> dict:
    return {"id": sid, "name": sid, "description": "d", "provider": {"name": "P", "authority": "official"},
            "tier": "curated", "categories": ["weather/current"], "kinds": ["current_value"],
            "coverage": {"geo": "US"}, "access": {"kind": "http_json", "url_template": url, "params": [],
                                                  "auth": "none", "headers": {}},
            "terms": {"status": "public_domain"}, "freshness": {"cadence": "hourly"}, "origin": {"by": "test"},
            "votes": {"yes": 2, "no": 1}}


def _suggested(name: str, url: str) -> dict:
    return {"name": name, "description": "Water temperature.", "provider": {"name": "Harbor", "authority": "community"},
            "categories": ["weather/current"], "kinds": ["current_value"], "coverage": {"geo": "US-CA"},
            "access": {"kind": "http_json", "url_template": url, "auth": "none", "headers": {},
                       "params": [{"name": "station", "kind": "station", "example": None, "required": True}]},
            "terms": {"status": "unverified"}, "freshness": {"cadence": "hourly"}}


def vote(i, sid, verdict):
    return {"id": i, "kind": "vote", "day": "2026-09-28", "payload": {"source_id": sid, "verdict": verdict,
                                                                        "app_version": "0.24.0"}}


def suggestion(i, record, via):
    return {"id": i, "kind": "suggestion", "day": "2026-09-28",
            "payload": {"record": record, "via": via, "app_version": "0.24.0"}}


@pytest.fixture
def sources(tmp_path):
    write_jsonl(tmp_path / "curated" / "core.jsonl", [_record("noaa-tides", "https://api.tides.gov/{station}"),
                                                      _record("nws-alerts", "https://api.weather.gov/alerts")])
    return tmp_path


def test_votes_aggregate_per_source(sources):
    items = [vote(1, "noaa-tides", "yes"), vote(2, "noaa-tides", "yes"), vote(3, "noaa-tides", "no"),
             vote(4, "nws-alerts", "broken"), vote(5, "gone-source", "yes"), vote(6, "Bad Id!", "yes"),
             vote(7, "nws-alerts", "maybe")]
    s = apply(items, sources=sources, probe=False, today="2026-09-28")
    rows = {r["id"]: r for r in read_jsonl(sources / "curated" / "core.jsonl")}
    assert rows["noaa-tides"]["votes"] == {"yes": 4, "no": 2}
    assert rows["nws-alerts"]["votes"] == {"yes": 2, "no": 2}  # broken counts against the source
    assert s["broken"] == ["nws-alerts"] and s["unknown_ids"] == {"gone-source": 1}
    assert s["counts"] == {"rejected": 2, "votes": 4}


def test_new_suggestions_are_written_merged_and_voted(sources):
    rec = _suggested("Harbor temperature", "https://api.harbordata.org/v1/temp?station={station}")
    items = [suggestion(1, rec, "form"), suggestion(2, copy.deepcopy(rec), "yes"),
             suggestion(3, copy.deepcopy(rec), "yes")]
    s = apply(items, sources=sources, probe=False, today="2026-09-28")
    out = read_jsonl(sources / "suggested" / "2026-09-28.jsonl")
    assert len(out) == 1
    r = out[0]
    assert r["id"] == "api-harbordata-org-harbor-temperature" and s["new"] == [r["id"]]
    assert r["tier"] == "harvested"
    assert r["origin"] == {"by": "user-suggestion", "at": "2026-09-28", "via": "form"}
    assert r["votes"] == {"yes": 2, "no": 0}
    assert s["counts"] == {"new suggestions": 1, "duplicate suggestions": 2}


def test_a_suggestion_of_a_known_source_is_a_yes_for_it(sources):
    known = _suggested("Tides", "https://api.tides.gov/{station}")
    s = apply([suggestion(1, known, "yes"), suggestion(2, copy.deepcopy(known), "form")], sources=sources,
              probe=False, today="2026-09-28")
    rows = {r["id"]: r for r in read_jsonl(sources / "curated" / "core.jsonl")}
    assert rows["noaa-tides"]["votes"] == {"yes": 3, "no": 1}
    assert not (sources / "suggested").exists()
    assert s["counts"] == {"duplicate suggestions": 1, "votes": 1}


def test_ingest_checks_every_item_again(sources):
    bad = _suggested("Leaky", "https://api.harbordata.org/v1/temp?station={station}&api_key=abc123")
    valued = _suggested("Valued", "https://api.harbordata.org/v2/temp?station={station}")
    valued["access"]["params"][0]["example"] = "8723214"
    private = _suggested("Mine", "https://192.168.1.4/temp?station={station}")
    s = apply([suggestion(1, bad, "yes"), suggestion(2, valued, "form"), suggestion(3, private, "form"),
               suggestion(4, _suggested("Ok", "https://x.org/{station}"), "telepathy"),
               {"id": 5, "kind": "mystery", "payload": {}}], sources=sources, probe=False, today="2026-09-28")
    assert s["counts"] == {"rejected": 5}
    assert not (sources / "suggested").exists()


def test_a_clashing_id_gets_a_suffix(sources):
    a = _suggested("Temp", "https://api.harbordata.org/a/{station}")
    b = _suggested("Temp", "https://api.harbordata.org/b/{station}")
    s = apply([suggestion(1, a, "form"), suggestion(2, b, "form")], sources=sources, probe=False,
              today="2026-09-28")
    assert s["new"] == ["api-harbordata-org-temp", "api-harbordata-org-temp-2"]


def test_dry_run_writes_nothing(sources):
    before = (sources / "curated" / "core.jsonl").read_text()
    apply([vote(1, "noaa-tides", "yes"), suggestion(2, _suggested("T", "https://x.org/{station}"), "form")],
          sources=sources, probe=False, write=False, today="2026-09-28")
    assert (sources / "curated" / "core.jsonl").read_text() == before
    assert not (sources / "suggested").exists()


def test_written_suggestions_pass_the_schema(sources):
    from sourcetool.schema import validate_record
    apply([suggestion(1, _suggested("Harbor", "https://api.harbordata.org/{station}"), "form")], sources=sources,
          probe=False, today="2026-09-28")
    for r in read_jsonl(sources / "suggested" / "2026-09-28.jsonl"):
        assert validate_record(r) == [], json.dumps(r)
