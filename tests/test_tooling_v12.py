"""sourcetool after the root-cause round: the taxonomy gate in check and build, the offline lint command, policies
that reproduce taxonomy.json, the app's own fetch identity and decode in validation and answers-check, resolver
readings sampled in validation, and the lookup's candidate set (an entity lifts, never gates). No network."""
from __future__ import annotations

import copy
import json
import random

import duckdb
import pytest

from sourcetool import answers as answers_mod
from sourcetool import build as build_mod
from sourcetool import validate as validate_mod
from sourcetool.common import APP_USER_AGENT, JSON_ACCEPT, SOURCES, decode_body, probe_headers, read_jsonl, taxonomy
from sourcetool.policies import policy_for
from sourcetool.resolvers import RES


def _rec(sid: str) -> dict:
    return next(r for r in read_jsonl(SOURCES / "curated" / "core.jsonl") if r["id"] == sid)


# --- taxonomy: policies reproduce it; check and build refuse a broken one ---------------------------------

def test_policies_reproduce_every_subcategory_policy():
    t = taxonomy()
    diff = [f"{c['id']}/{s['id']}" for c in t["categories"] for s in c["subcategories"]
            if policy_for(c["id"], s["id"]) != s["policy"]]
    assert not diff, diff


def _broken_taxonomy() -> dict:
    t = copy.deepcopy(taxonomy())
    t["categories"][0]["subcategories"][0]["expects"] = ["wave height"]
    return t


def test_check_refuses_a_taxonomy_with_problems(monkeypatch, capsys):
    import sourcetool.__main__ as cli
    monkeypatch.setattr(cli, "taxonomy", _broken_taxonomy)
    assert cli._check_taxonomy_and_resolvers() >= 1
    assert "expects" in capsys.readouterr().out


def test_build_refuses_a_taxonomy_with_problems(monkeypatch):
    monkeypatch.setattr(build_mod, "taxonomy", _broken_taxonomy)
    monkeypatch.setattr(build_mod, "DB", None)  # never reached: the gate comes first
    with pytest.raises(SystemExit, match="taxonomy"):
        build_mod.build()


# --- the offline lint command ---------------------------------------------------------------------------

def test_lint_report_names_record_and_keyword_lints():
    rec = {"id": "x", "tier": "curated", "kinds": ["schedule"], "categories": ["sports/schedules"], "name": "X",
           "description": "", "examples": [],
           "answers": [{"name": "n", "kind": "value", "type": "count", "path": "n", "label": "N", "words": ["any"]}]}
    out = answers_mod.lint_report([rec])
    assert any("kind schedule has no answer" in o for o in out)
    assert any("count answer n claims 'any'" in o for o in out)
    assert any(o.startswith("sports/schedules: no curated source filed here speaks to") for o in out)


def test_answers_check_lint_is_offline(monkeypatch, capsys):
    def no_fetch(*_a, **_k):
        raise AssertionError("--lint never fetches")
    monkeypatch.setattr(answers_mod, "fetch_sample", no_fetch)
    rc = answers_mod.cmd_check(["--lint"])
    out = capsys.readouterr().out
    assert rc == 0, out  # the gate: the repo's own data passes its lints (kinds served, utc, keyword baseline)
    assert "lint" in out.splitlines()[-1]


# --- the app's identity and decode ----------------------------------------------------------------------

def test_probe_headers_are_the_apps():
    h = probe_headers("http_json")
    assert h["User-Agent"] == APP_USER_AGENT == "SmartBrain/0.24.1 (+https://smartbrain.securecloudgroup.com)"
    assert h["Accept"] == JSON_ACCEPT == "application/json, text/plain;q=0.5, */*;q=0.1"
    assert probe_headers("rss")["Accept"].startswith("text/html")  # the app's page/feed reader
    assert probe_headers("http_json", {"accept": "application/geo+json"}) == {
        "User-Agent": APP_USER_AGENT, "accept": "application/geo+json"}  # a record's own Accept wins


@pytest.mark.parametrize("body,ctype", [
    ("﻿{\"a\": \"é\"}".encode("utf-16-le"), "application/json"),
    ('{"a": "é"}'.encode("utf-16-le"), "application/json;charset=utf-16"),
    ('{"a": "é"}'.encode("utf-16-be"), "application/json; charset=UTF-16BE"),
    ('{"a": "é"}'.encode(), "application/json;charset=ISO-8859-1"),  # a wrong 8-bit header over UTF-8
    ("﻿{\"a\": \"é\"}".encode(), ""),
    ('{"a": "é"}'.encode("latin-1"), "text/plain; charset=iso-8859-1"),
])
def test_decode_body_bom_then_charset_then_utf8(body, ctype):
    assert json.loads(decode_body(body, ctype)) == {"a": "é"}


def test_fetch_sample_decodes_a_utf16_json_feed(monkeypatch):
    rec = {"id": "x", "access": {"kind": "http_json", "auth": "none", "url_template": "https://h.example/x",
                                 "params": [], "docs_url": "https://h.example/docs"}}
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw.get("headers") or {})
        return 200, '{"ok": true}'.encode("utf-16-le"), "application/json;charset=utf-16"
    monkeypatch.setattr(answers_mod, "get", fake_get)
    assert answers_mod.fetch_sample(rec) == ("https://h.example/x", {"ok": True})
    assert seen["User-Agent"] == APP_USER_AGENT and seen["Accept"] == JSON_ACCEPT


# --- validation: the app's identity, and a sample of resolver readings ---------------------------------

def test_resolver_samples_fill_from_real_entries():
    rec = _rec("ndbc-buoy-realtime")
    urls = validate_mod.resolver_samples(rec, 3, random.Random(7))
    buoys = {e["key"] for e in read_jsonl(RES / "buoy.jsonl")}
    assert len(urls) == 3
    for u in urls:
        sid = u.rsplit("/", 1)[1].removesuffix(".txt")
        assert sid in buoys and sid == sid.upper()


def test_resolver_samples_fill_lat_and_lon_from_one_place():
    rec = next(r for r in read_jsonl(SOURCES / "curated" / "core.jsonl")
               if {(p.get("fill") or {}).get("field") for p in r["access"].get("params", [])} >= {"lat", "lon"}
               and r["access"]["kind"] == "http_json")
    places = {(str(e["lat"]), str(e["lon"])) for e in read_jsonl(RES / "place.jsonl")}
    names = {p["name"]: p for p in rec["access"]["params"]}
    lat = next(n for n, p in names.items() if p["fill"].get("field") == "lat")
    lon = next(n for n, p in names.items() if p["fill"].get("field") == "lon")
    tpl = dict(kv.split("=", 1) for kv in rec["access"]["url_template"].split("?", 1)[1].split("&"))
    key = {v.strip("{}"): k for k, v in tpl.items()}  # template query key of each {param}
    for url in validate_mod.resolver_samples(rec, 2, random.Random(1)):
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        assert (q[key[lat]], q[key[lon]]) in places, url


def test_validate_probes_as_the_app_and_samples_resolver_readings(monkeypatch):
    rec = copy.deepcopy(_rec("ndbc-buoy-realtime"))
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw.get("headers") or {}))
        ok = url.endswith("/41004.txt")
        return (200 if ok else 404), b"#YY MM DD\n2026 09 29\n" * 3, "text/plain"
    monkeypatch.setattr(validate_mod, "get", fake_get)
    monkeypatch.setattr(validate_mod, "robots_allows", lambda url: True)
    v = validate_mod.validate_one(rec, samples=2, rng=random.Random(3))
    assert all(h["User-Agent"] == APP_USER_AGENT for _, h in calls)
    assert len(calls) == 3  # the example, then two resolver readings
    assert v["status"] == "degraded" and v["samples"]["ok"] == 0 and len(v["samples"]["failed"]) == 2


# --- the lookup: an entity lifts the sources that take it, never gates the rest out ------------------------

def test_lookup_candidates_are_entity_sources_and_term_hits():
    con = duckdb.connect()
    con.execute("CREATE TABLE library_terms(term VARCHAR, source_id VARCHAR, weight DOUBLE)")
    con.execute("CREATE TABLE library_source_categories(source_id VARCHAR, category VARCHAR, subcategory VARCHAR)")
    con.execute("CREATE TABLE library_source_resolvers(source_id VARCHAR, resolver VARCHAR)")
    con.execute("CREATE TABLE library_sources(id VARCHAR, name VARCHAR, tier VARCHAR, validation_status VARCHAR, "
                "provider_name VARCHAR, prior DOUBLE, audience VARCHAR, authority VARCHAR, kinds VARCHAR[], "
                "role VARCHAR, auth VARCHAR, entity VARCHAR)")
    for sid, entity in (("team-src", ""), ("league-wide", "MLB"), ("other", "NHL")):
        con.execute("INSERT INTO library_sources VALUES (?, ?, 'curated', 'ok', 'p', 1.0, '', 'official', "
                    "['ranking'], '', 'none', ?)", [sid, sid, entity])
        con.execute("INSERT INTO library_source_categories VALUES (?, 'sports', 'standings')", [sid])
        con.execute("INSERT INTO library_terms VALUES ('standings', ?, 1.0)", [sid])
    con.execute("INSERT INTO library_source_resolvers VALUES ('team-src', 'team_mlb')")
    rows = con.execute(build_mod._LOOKUP, [["standings"], ["sports/standings"], [], [], ["team_mlb"], [], [],
                                           " mlb wild card standings ", ["ranking"], 8]).fetchall()
    got = {r[0]: r[5] for r in rows}
    assert set(got) == {"team-src", "league-wide", "other"}  # the entity no longer removes the league-wide one
    assert got["league-wide"] == got["team-src"] > got["other"]  # a declared subject the ask names lifts alike


# --- the pack carries each subcategory's expects and measure where the app reads its policy -----------------

def _sub(sid: str) -> dict:
    cat, _, sub = sid.partition("/")
    return next(s for c in taxonomy()["categories"] if c["id"] == cat for s in c["subcategories"] if s["id"] == sub)


def test_pack_policy_carries_expects_and_measure():
    surf = build_mod.pack_policy(_sub("water/surf_waves"))
    assert surf["expects"] == ["waves", "wave_period", "wave_direction", "wind"]
    assert surf["measure"] == {"buoy": "WVHT"} and surf["match"] == "geo"
    assert "expects" not in build_mod.pack_policy(_sub("tech/packages"))  # none declared: no key


def test_built_pack_has_surf_waves_expects():
    if not build_mod.DB.exists():
        pytest.skip("no build/library.duckdb (run `python -m sourcetool build`)")
    con = duckdb.connect(str(build_mod.DB), read_only=True)
    pol = json.loads(con.execute("SELECT policy FROM library_taxonomy WHERE category = 'water' "
                                 "AND subcategory = 'surf_waves'").fetchone()[0])
    con.close()
    assert "wind" in pol["expects"] and pol["measure"] == {"buoy": "WVHT"}
