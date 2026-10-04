"""Curated records and their declared answers under spec v1.2: every value says its window, every dated row
list says its axis, the records the root-cause round named carry what the fix needs, the new records are
there (and the held-back ones are not), and every answers file checks clean against its recorded sample.

Offline: recorded samples live in tests/fixtures/records/<id>.json ({url, fetched, sample}); they were
fetched once with the app's identity. `sourcetool answers-check <ids>` repeats the check live.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcetool.answers import (ANSWERS, AXIS_STEPS, MEASURES, WINDOWS, answer_lints, answer_problems,
                                check_sample, params_of)
from sourcetool.common import ROOT, SOURCES, read_jsonl
from sourcetool.schema import resolver_host_value_problems, validate_record

FIXTURES = ROOT / "tests" / "fixtures" / "records"
RECORDS = {r["id"]: r for r in read_jsonl(SOURCES / "curated" / "core.jsonl")}
FILES = {p.stem: json.loads(p.read_text()) for p in sorted(ANSWERS.glob("*.json"))}

NEW = ("nws-tropical-alerts-point", "mlb-wildcard-standings", "nps-geyser-predictions", "geysertimes-predictions",
       "slack-status-current", "powerball-home", "ny-lottery-powerball", "ny-lottery-megamillions", "ny-lotto",
       "ny-take5", "ny-numbers-win4", "ny-pick10", "ndbc-buoy-waves")
# held back: a hard refusal or unclear terms (subreddit, AWS), no readable source (Mega Millions jackpot), or a
# schema the record needs that v1.2 does not have (TheSportsDB round results need an any-of row filter)
HELD_BACK = ("subreddit-top-rss", "aws-health-currentevents", "megamillions-home", "thesportsdb-league-round")

# a dated list whose time cell is not an ISO date/time in the live response (RFC 2822 feed dates, epoch
# seconds, "HH:MM" clock strings), so a window cut can't read it: no axis, by design
_NON_ISO = "cell is not ISO in the live response"


def _answers(sid):
    return FILES[sid]["answers"]


def _by_name(sid, name):
    return next(a for a in _answers(sid) if a["name"] == name)


def _merged(sid):
    return {**RECORDS[sid], "answers": _answers(sid)}


def _dated(a):
    fields = a.get("row") or a.get("columns") or []
    return [f for f in fields if f.get("type") in ("time", "date")]


# --- the backfill -----------------------------------------------------------------------------------

def test_every_value_answer_declares_its_window():
    missing = [f"{sid}.{a['name']}" for sid, d in FILES.items() for a in d["answers"]
               if a["kind"] == "value" and a.get("window") not in WINDOWS]
    assert not missing, missing


def test_every_dated_row_answer_declares_an_axis_or_is_exempt():
    exempt = json.loads((FIXTURES / "axis_exempt.json").read_text())
    missing = [f"{sid}.{a['name']}" for sid, d in FILES.items() for a in d["answers"]
               if a["kind"] != "value" and _dated(a) and "axis" not in a
               and f"{sid}.{a['name']}" not in exempt]
    assert not missing, missing
    assert all(v for v in exempt.values()), "every exemption names its reason"


def test_axis_steps_follow_the_cell_type():
    for sid, d in FILES.items():
        for a in d["answers"]:
            if "axis" not in a:
                continue
            cell = next(f for f in _dated(a) if f["path"] == a["axis"]["cell"])
            assert a["axis"]["step"] in AXIS_STEPS
            if cell["type"] == "date":
                assert a["axis"]["step"] == "day", f"{sid}.{a['name']}: a date cell can only cut by day"


def test_answers_files_pass_the_schema_and_the_lints():
    # a kind the response can't serve in v1.2 shapes (non-ISO dates, per-row values) is listed with its reason
    exempt = json.loads((FIXTURES / "lint_exempt.json").read_text())
    problems = []
    for sid, d in FILES.items():
        if sid not in RECORDS:
            continue  # a harvested record's file: its own suite
        problems += [f"{sid}: {e}" for e in answer_problems(d["answers"], params_of(RECORDS[sid]))]
        problems += [e for e in answer_lints(_merged(sid)) if e.split(" has no")[0] not in exempt]
    assert not problems, problems
    assert all(v for v in exempt.values()), "every exemption names its reason"


def test_changed_and_new_records_are_well_formed():
    for sid in NEW + ("open-meteo-forecast", "open-meteo-marine", "mlb-standings", "nhl-standings-now"):
        assert validate_record(RECORDS[sid]) == [], sid


# F3 (review 2026-10-04): a resolver value that fills a host position must itself be a safe host — no
# credential (@), explicit port, query/fragment, or IP literal — else the inflated URL smuggles a target
# past the record-time check.
def test_resolver_host_check_catches_an_at_credential_port_and_fragment():
    rec = {"id": "fake", "access": {"url_template": "https://{feed}", "params": [
        {"name": "feed", "fill": {"from": "resolver", "resolver": "__inline__", "field": "key"}}]}}
    bad = resolver_host_value_problems(rec, inline_rows={"__inline__": [
        {"key": "news.example.com@evil"},
        {"key": "ok.example.com:81/x"},
        {"key": "ok.example.com?q=1"},
        {"key": "feeds.example.com/arc/outboundfeeds/rss/?outputType=xml"},
        {"key": "ok.example.com/path#frag"},
        {"key": "203.0.113.9"},
        {"key": "ok.example.com/rss"}]})
    tokens = ("@evil", ":81", "?q=1", "#frag", "203.0.113.9")
    hits = {tok for tok in tokens for b in bad if tok in b}
    assert hits == set(tokens), hits
    assert not [b for b in bad if "outputType" in b], bad  # a feed path keeps its query
    # A url_template without a host-position {param} is out of scope: no false positives.
    out_of_scope = {"id": "x", "access": {"url_template": "https://api.example.com/x?p={q}",
                                            "params": [{"name": "q", "fill": {"from": "text"}}]}}
    assert resolver_host_value_problems(out_of_scope) == []


# --- weather: tonight, phenomena, alerts --------------------------------------------------------------

def test_open_meteo_forecast_reads_hourly_conditions_and_tonight():
    url = RECORDS["open-meteo-forecast"]["access"]["url_template"]
    hourly = url.split("hourly=")[1].split("&")[0].split(",")
    assert "weather_code" in hourly and "precipitation_probability" in hourly
    tonight = _by_name("open-meteo-forecast", "tonight")
    assert tonight["kind"] == "columns" and tonight["axis"] == {"cell": "hourly.time", "step": "hour"}
    assert {"tonight", "overnight", "storm tonight"} <= set(tonight["words"])
    assert any(c.get("codes") == "wmo_weather" for c in tonight["columns"])
    assert _by_name("open-meteo-forecast", "high_today")["window"] == "today"
    assert _by_name("open-meteo-forecast", "rain_tomorrow")["window"] == "tomorrow"
    assert _by_name("open-meteo-forecast", "temperature")["window"] == "now"
    assert _by_name("open-meteo-forecast", "daily_forecast")["axis"] == {"cell": "daily.time", "step": "day"}


@pytest.mark.parametrize("sid", ["nws-forecast", "nws-forecast-hourly"])
def test_nws_forecasts_name_the_phenomena_they_report(sid):
    words = {w for a in _answers(sid) for w in a["words"]}
    assert {"thunderstorm", "hail", "fog", "frost", "sleet", "freezing rain", "snow"} <= words


def test_nws_forecast_periods_cut_to_tonight_and_today():
    periods = _by_name("nws-forecast", "periods")
    assert periods["axis"] == {"cell": "startTime", "step": "period"}
    assert periods["row"][0]["path"] == "startTime" or any(r["path"] == "startTime" for r in periods["row"])
    tonight = _by_name("nws-forecast", "tonight")
    assert tonight["filter"] == {"path": "name", "equals": "Tonight"} and tonight["may_be_empty"] is True
    assert tonight["axis"]["step"] == "period"
    today = _by_name("nws-forecast", "today")
    assert today["axis"]["step"] == "period" and "today" in today["words"]
    assert _by_name("nws-forecast-hourly", "hours")["axis"] == {"cell": "startTime", "step": "hour"}


def test_nws_alerts_point_names_the_hazards_it_carries():
    words = set(_by_name("nws-alerts-point", "alerts")["words"])
    assert {"tornado warning", "severe thunderstorm warning", "flood warning", "winter storm warning"} <= words


def test_count_answers_leave_existence_to_the_list():
    for sid in ("nhc-current-storms", "nws-alerts-point"):
        for a in _answers(sid):
            if a.get("type") == "count":
                assert not [w for w in a["words"] if w == "any" or w.startswith("any ")], (sid, a["words"])
    assert "any hurricanes" in _by_name("nhc-current-storms", "storms")["words"]


# --- sports: result kind, leagues, TBD times ----------------------------------------------------------

def test_result_sources_declare_the_result_kind():
    for sid in ("mlb-team-results", "mlb-schedule", "nhl-score-now", "thesportsdb-last-league"):
        assert "result" in RECORDS[sid]["kinds"], sid


def test_league_wide_sources_name_their_league():
    want = {"mlb-standings": "MLB", "mlb-wildcard-standings": "MLB", "mlb-schedule": "MLB", "nhl-score-now": "NHL",
            "nhl-standings-now": "NHL", "jolpica-f1-standings": "Formula 1", "jolpica-f1-next": "Formula 1"}
    for sid, league in want.items():
        assert RECORDS[sid]["coverage"]["entity"] == league, sid


def test_mlb_team_schedule_marks_tbd_times_and_names_the_game():
    tbd = {"path": "dates[0].games[0].status.startTimeTBD", "equals": True}
    assert _by_name("mlb-team-schedule", "next_game_time")["tbd_if"] == tbd
    upcoming = _by_name("mlb-team-schedule", "upcoming")
    start = next(r for r in upcoming["row"] if r["path"] == "games[0].gameDate")
    assert start["tbd_if"] == {"path": "games[0].status.startTimeTBD", "equals": True}
    # every game carries its series ("Regular Season", "NL Division Series"); `description` exists only on some
    assert _by_name("mlb-team-schedule", "next_series")["path"] == "dates[0].games[0].seriesDescription"
    # the league-wide day's games carry the same flag beside each game's start
    start = next(r for r in _by_name("mlb-schedule", "games")["row"] if r["path"] == "gameDate")
    assert start["tbd_if"] == {"path": "status.startTimeTBD", "equals": True}


# --- water: marine coverage, direction, swell, surf --------------------------------------------------

def test_open_meteo_marine_is_ocean_and_coast_only_and_reads_direction_and_swell():
    assert RECORDS["open-meteo-marine"]["coverage"]["water"] == "ocean_coastal"
    url = RECORDS["open-meteo-marine"]["access"]["url_template"]
    for field in ("wave_direction", "swell_wave_height", "swell_wave_period"):
        assert field in url
    assert _by_name("open-meteo-marine", "wave_direction")["measure"] == "wave_direction"
    assert _by_name("open-meteo-marine", "swell")["measure"] == "swell"
    assert _by_name("open-meteo-marine", "wave_height")["measure"] == "waves"
    assert _by_name("open-meteo-marine", "waves_tomorrow_noon")["window"] == "tomorrow"


def test_buoy_waves_come_from_the_wave_file_and_are_asked_for_surf():
    # the met file's newest row has no wave reading two times in three ("MM"); the .spec file's rows all do
    assert RECORDS["ndbc-buoy-waves"]["access"]["url_template"].endswith("/{buoy}.spec")
    assert _by_name("ndbc-buoy-waves", "wave_direction")["path"] == "rows[0].MWD"
    assert _by_name("ndbc-buoy-waves", "swell_height")["measure"] == "swell"
    assert {"surf", "waves"} <= set(_by_name("ndbc-buoy-waves", "wave_height")["words"])
    met = {a["path"] for a in _answers("ndbc-buoy-realtime")}
    assert not met & {"rows[0].WVHT", "rows[0].DPD", "rows[0].MWD"}  # replaced, not kept beside


# --- new and held-back records ------------------------------------------------------------------------

def test_new_records_are_curated_terms_checked_and_answered():
    for sid in NEW:
        r = RECORDS[sid]
        assert r["tier"] == "curated" and r["terms"]["status"] != "unverified", sid
        assert r["terms"].get("terms_url") and r["terms"].get("note"), sid
        if r["access"]["kind"] != "html":  # the page door reads html; its readings are in examples
            assert sid in FILES, sid


def test_held_back_records_are_not_published_and_say_why():
    skipped = (ANSWERS / "_skipped.md").read_text()
    for sid in HELD_BACK:
        assert sid not in RECORDS, sid
        assert f"- {sid} —" in skipped, sid


def test_geyser_sources_take_the_geyser_resolver():
    for sid in ("nps-geyser-predictions", "geysertimes-predictions"):
        fills = [p["fill"] for p in RECORDS[sid]["access"]["params"]]
        assert fills and all(f.get("resolver") == "geyser" for f in fills), sid


def _eval_rows():
    rows = []
    for name in ("lookup_records_asks", "lookup_records_holdout"):
        rows += json.loads((ROOT / "tests" / f"{name}.json").read_text())["cases"]
    return rows


def test_each_new_or_changed_record_has_at_least_15_lookup_rows():
    counts: dict[str, int] = {}
    for c in _eval_rows():
        for sid in c["ok"]:
            counts[sid] = counts.get(sid, 0) + 1
        assert set(c["ok"]) <= set(RECORDS), c
    for sid in NEW + ("open-meteo-forecast", "nws-forecast", "nws-alerts-point", "nhc-current-storms",
                      "mlb-team-results", "nhl-score-now", "thesportsdb-last-league", "mlb-standings",
                      "open-meteo-marine", "ndbc-buoy-realtime", "mlb-team-schedule"):
        assert counts.get(sid, 0) >= 15, (sid, counts.get(sid, 0))


# --- recorded samples ---------------------------------------------------------------------------------

def _fixture_ids():
    return sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.endswith("_exempt"))


@pytest.mark.parametrize("sid", _fixture_ids())
def test_answers_check_clean_against_the_recorded_sample(sid):
    fx = json.loads((FIXTURES / f"{sid}.json").read_text())
    source = fx.get("answers_of", sid)  # a shape fixture: another response of the same API and shape
    assert check_sample(_answers(source), fx["sample"], params_of(RECORDS[source])) == []


def test_every_new_or_changed_answers_file_has_a_recorded_sample():
    have = {json.loads((FIXTURES / f"{s}.json").read_text()).get("answers_of", s) for s in _fixture_ids()}
    want = {s for s in NEW if s in FILES} | {"open-meteo-forecast", "nws-forecast", "nws-forecast-hourly",
                                            "nws-alerts-point", "nhc-current-storms", "open-meteo-marine",
                                            "ndbc-buoy-realtime", "mlb-team-schedule", "mlb-schedule"}
    assert want <= have, sorted(want - have)


def test_measures_are_from_the_closed_list():
    for sid, d in FILES.items():
        for a in d["answers"]:
            if "measure" in a:
                assert a["measure"] in MEASURES, (sid, a["name"])
