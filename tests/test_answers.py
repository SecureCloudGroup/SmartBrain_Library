"""answers: the schema (answer_problems), the check against a sample (check_sample), and build's merge."""
from __future__ import annotations

import json

import pytest

from sourcetool.answers import answer_problems, check_sample, load_answers, params_of
from sourcetool.build import merge_answers
from sourcetool.schema import validate_record


def value(name="temp", **kw):
    a = {"name": name, "label": "Temperature", "kind": "value", "primary": True,
         "words": ["temperature", "temp", "degrees"], "path": "current.temperature_2m", "type": "number"}
    a.update(kw)
    return a


def rows(**kw):
    a = {"name": "quakes", "label": "Earthquakes", "kind": "list", "primary": True,
         "words": ["earthquakes", "quakes", "shaking"], "path": "features",
         "row": [{"path": "properties.mag", "label": "Magnitude", "type": "number"},
                 {"path": "properties.place", "label": "Place", "type": "text"}]}
    a.update(kw)
    return a


def cols(**kw):
    a = {"name": "daily", "label": "Daily forecast", "kind": "columns", "primary": True,
         "words": ["forecast", "this week", "daily"],
         "columns": [{"path": "daily.time", "label": "Day", "type": "date"},  # a day column IS a date (v1.3 types every element)
                     {"path": "daily.temperature_2m_max", "label": "High", "type": "number",
                      "unit_path": "daily_units.temperature_2m_max"}]}
    a.update(kw)
    return a


def bad(answers, needle, params=()):
    errs = answer_problems(answers, params)
    assert any(needle in e for e in errs), errs


# --- answer_problems --------------------------------------------------------------------------------

def test_well_formed_answers_pass():
    assert answer_problems([value(), value("humidity", primary=False, unit="%", path="current.rh")]) == []
    assert answer_problems([rows(newest_first=True, may_be_empty=True)]) == []
    assert answer_problems([cols(limit=7), value("code", primary=False, codes="wmo_weather",
                                                  path="current.weather_code")]) == []
    assert answer_problems([value(type="count", path="activeStorms")]) == []  # a bare top-level name
    assert answer_problems([value(type="time", path="properties.periods[0].startTime")]) == []


def test_list_shape_and_names():
    bad([], "non-empty list")
    bad("x", "non-empty list")
    bad([value(str(i)) for i in range(13)], "at most 12")
    bad([value(name="Temp")], "slug")
    bad([value(name="t" * 41)], "slug")
    bad([value(), value(primary=False)], "duplicate name")
    bad([value(extra=1)], "unknown keys")
    bad([value(kind="table")], "kind must be")


def test_label_and_words():
    bad([value(label="")], "label")
    bad([value(label="x" * 41)], "label")
    bad([value(words=["a", "b"])], "3-15")
    bad([value(words=[f"w{i}" for i in range(16)])], "3-15")
    bad([value(words=["Temp", "hot", "cold"])], "lowercase")
    bad([value(words=["temp", "", "cold"])], "lowercase")


def test_value_rules():
    bad([value(type="boolean")], "type must be")
    bad([value(unit="°F", unit_path="current_units.t")], "not both")
    bad([value(unit_path="current units")], "unit_path")
    bad([value(codes="beaufort")], "wmo_weather")
    bad([value(primary="yes")], "primary must be")


def test_paths():
    for p in ("a[*]", "a[1:2]", "a[-1]", "a..b", "0abc", "a[x]", "", "a.b.", "$.a"):
        bad([value(path=p)], "bad path")
    assert answer_problems([value(path="data[0][3]")]) == []  # a list of lists, as the app's engine reads
    assert answer_problems([value(path="daily.precipitation_probability_max[1]")]) == []
    assert answer_problems([value(path="data-set_1.x[12].y")]) == []


def test_list_rules():
    bad([rows(row=[])], "1-4 fields")
    bad([rows(row=[{"path": "a", "label": "A", "type": "text"}] * 5)], "1-4 fields")
    bad([rows(row=[{"path": "a", "label": "A", "type": "count"}])], "type must be")
    bad([rows(row=[{"path": "a", "label": "A", "type": "text", "codes": "wmo_weather"}])], "unknown keys")
    bad([rows(may_be_empty="yes")], "may_be_empty")
    bad([rows(limit=5)], "unknown keys")


def test_columns_rules():
    bad([cols(columns=cols()["columns"][:1])], "2-4 fields")
    bad([cols(columns=cols()["columns"] * 3)], "2-4 fields")
    bad([cols(limit=0)], "limit")
    bad([cols(limit=True)], "limit")
    c = cols()
    c["columns"][0]["codes"] = "other"
    bad([c], "wmo_weather")


def test_primary_limits():
    assert answer_problems([value(primary=False)]) == []  # primary is a maximum, not a minimum (v1.1)
    bad([value(str(c)) for c in "abcde"], "at most 4")
    bad([rows(), cols()], "at most one list")
    bad([rows(), value()], "not both")
    assert answer_problems([value(str(c)) for c in "abcd"]) == []


def test_a_quoted_key_reaches_what_plain_names_cannot():
    from sourcetool.answers import resolve
    doc = {"root": {"bsa": [{"description": {"#cdata-section": "Expect delays"}}]}}
    assert resolve(doc, 'root.bsa[0].description["#cdata-section"]') == "Expect delays"
    assert answer_problems([value(type="text", path='root.bsa[0].description["#cdata-section"]')]) == []
    for bad_path in ('a["x.y"]', 'a.["x"]', 'a["x"'):
        bad([value(path=bad_path)], "bad path")


def test_a_date_may_be_a_midnight_timestamp():
    from sourcetool.answers import type_ok
    assert type_ok("2026-04-23", "date") and type_ok("2026-04-23T00:00:00.000Z", "date")
    assert type_ok("2026-09-24T00:00:00", "date") and not type_ok("Sep 24", "date")
    assert not type_ok("2026-09-24T14:00", "date")  # a real clock time is a time


def test_utc_marks_a_zoneless_time_as_utc():
    assert answer_problems([value(type="time", utc=True)]) == []
    bad([value(type="text", utc=True)], "utc must be true or false, on a time only")
    bad([value(type="time", utc="yes")], "utc must be true or false, on a time only")


def test_validate_record_runs_answer_problems():
    r = {"id": "x-source", "name": "x", "description": "d", "provider": {"name": "P", "authority": "official"},
         "tier": "curated", "categories": ["weather/current"], "kinds": ["current_value"], "coverage": {"geo": "US"},
         "access": {"kind": "http_json", "url_template": "https://api.example.org/x", "params": [], "auth": "none"},
         "terms": {"status": "public_domain"}, "freshness": {"cadence": "hourly"}, "origin": {"by": "test"}}
    assert validate_record(r) == []
    assert validate_record({**r, "answers": [value()]}) == []
    assert any(e.startswith("answers:") for e in validate_record({**r, "answers": [value(label="")]}))
    # a {param} segment must name one of the record's own params
    assert any("not a parameter" in e for e in validate_record({**r, "answers": [value(path="rates.{quote}")]}))
    # the app never sends a fixed header (a JSON API that needs Accept served CSV in the app): refused
    fixed = {**r, "access": {**r["access"], "headers": {"Accept": "application/json"}}}
    assert any("fixed header" in e for e in validate_record(fixed))
    keyed = {**r, "access": {**r["access"], "headers": {"X-Api-Key": "{key}"},
                             "params": [{"name": "key", "kind": "key"}]}}
    assert not any("fixed header" in e for e in validate_record(keyed))
    r["access"] = {**r["access"], "url_template": "https://api.example.org/x?q={quote}",
                   "params": [{"name": "quote", "kind": "currency_pair", "example": "EUR"}]}
    assert validate_record({**r, "answers": [value(path="rates.{quote}")]}) == []


# --- v1.1: {param} segments, date, row filter ------------------------------------------------------------

def test_param_segments():
    ps = {"quote": "EUR", "coin": "bitcoin"}
    for p in ("rates.{quote}", "{coin}.usd", "{coin}.usd_24h_change", "data.{coin}[0].price"):
        assert answer_problems([value(path=p)], ps) == [], p
    for p in ("rates.x{quote}", "rates.{quote}x", "rates.{Quote}", "rates.{}", "{coin}{quote}"):
        bad([value(path=p)], "bad path", ps)
    bad([value(path="rates.{base}")], "{base} is not a parameter", ps)
    bad([value(unit_path="units.{base}")], "{base} is not a parameter", ps)
    bad([rows(row=[{"path": "{base}.v", "label": "V", "type": "number"}])], "{base} is not a parameter", ps)
    sample = {"rates": {"EUR": "0.91"}, "bitcoin": {"usd": 65000}, "units": {"EUR": "EUR"}}
    assert check_sample([value(path="rates.{quote}", unit_path="units.{quote}"),
                         value("px", path="{coin}.usd")], sample, ps) == []
    assert check_sample([value(path="rates.{quote}")], sample, {"quote": "JPY"})
    assert check_sample([value(path="rates.{quote}")], sample, {})  # no example: nothing to stand for


def test_date_type():
    assert answer_problems([value(type="date", path="rows[0].observation_date")]) == []
    assert answer_problems([rows(row=[{"path": "d", "label": "Date", "type": "date"}])]) == []
    c = cols()
    c["columns"][0]["type"] = "date"
    assert answer_problems([c]) == []
    assert chk(value(type="date", path="current.date")) == []
    for p in ("current.time", "current.epoch", "current.name"):  # a timestamp is not a date-only value
        assert chk(value(type="date", path=p)), p


def test_row_filter():
    flt = {"path": "ARPT", "equals": "{airport}"}
    ps = {"airport": "JFK"}
    assert answer_problems([rows(filter=flt)], ps) == []
    bad([rows(filter=flt)], "{airport} is not a parameter")
    assert answer_problems([rows(filter={"path": "status", "equals": "Final"})], ps) == []  # a fixed status word
    bad([rows(filter={"path": "ARPT", "equals": "{JFK"})], "equals must be a {param} or a short fixed value", ps)
    bad([rows(filter={"path": "ARPT", "equals": ""})], "equals must be a {param} or a short fixed value", ps)
    bad([rows(filter={"path": "ARPT"})], "filter must be", ps)
    bad([rows(filter={**flt, "op": "eq"})], "filter must be", ps)
    bad([rows(filter={"path": "a[*]", "equals": "{airport}"})], "bad filter path", ps)
    bad([value(filter=flt)], "unknown keys", ps)
    sample = {"status": [{"ARPT": "LGA", "delay": "true"}, {"ARPT": "JFK", "delay": "15"}]}
    a = rows(path="status", filter=flt, row=[{"path": "delay", "label": "Delay", "type": "number"}])
    assert check_sample([a], sample, ps) == []       # the first row AFTER the filter is JFK's
    assert check_sample([{**a, "filter": {**flt}}], sample, {"airport": "SFO"})  # nothing left: empty
    assert check_sample([{**a, "may_be_empty": True}], sample, {"airport": "SFO"}) == []
    assert check_sample([a], sample, {})             # no example value for the filter


# --- check_sample -------------------------------------------------------------------------------------

SAMPLE = {"current": {"temperature_2m": 71.3, "rh": "64", "neg": "-6.904", "time": "2026-09-28T14:00",
                      "epoch": 1790000000, "rfc": "Tue, 29 Sep 2026 01:00:00 GMT",
                      "rfc_short": "29 Sep 2026 01:00 -0400", "rfc_bad": "Tue, 29 Foo 2026 01:00:00 GMT",
                      "prose": "Updated Tue, 29 Sep 2026 01:00:00 GMT by staff", "epoch_ms": "1790000000000", "date": "2026-09-28", "name": "iss",
                      "blank": " ", "flag": True, "small": 12},
          "current_units": {"temperature_2m": "°F"},
          "features": [{"properties": {"mag": 4.2, "place": "10km N of Ridgecrest", "time": 1790000000000}}],
          "activeStorms": [], "alerts": [],
          "daily": {"time": ["2026-09-28", "2026-09-29"], "temperature_2m_max": [80, 78], "short": [1]},
          "daily_units": {"temperature_2m_max": "°F"}}


def chk(*answers):
    return check_sample(list(answers), SAMPLE)


def test_numbers_including_numeric_text():
    assert chk(value(), value("rh", path="current.rh"), value("neg", path="current.neg")) == []
    assert chk(value(path="current.name"))           # text is not a number
    assert chk(value(path="current.flag"))           # a boolean is not a number
    assert chk(value(path="current.temperature_2m", unit_path="current_units.temperature_2m")) == []
    assert chk(value(unit_path="current_units.nope"))


def test_text_and_time_forms():
    assert chk(value(type="text", path="current.name")) == []
    assert chk(value(type="text", path="current.blank"))
    assert chk(value(type="text", path="current.small"))
    for p in ("current.time", "current.epoch", "current.epoch_ms", "current.rfc", "current.rfc_short"):
        assert chk(value(type="time", path=p)) == [], p
    assert chk(value(type="time", path="current.rfc_bad"))  # RFC 2822 shape, impossible month
    assert chk(value(type="time", path="current.prose"))    # a sentence that merely holds a date
    assert chk(value(type="time", path="current.date"))    # a bare date has no time of day
    assert chk(value(type="time", path="current.small"))   # 12 is not an epoch


def test_top_level_list_is_wrapped_as_items():
    sample = [{"name": "a", "v": "1"}, {"name": "b", "v": "2"}]
    a = rows(path="items", row=[{"path": "name", "label": "Name", "type": "text"}])
    assert answer_problems([a]) == []
    assert check_sample([a, value("first", primary=False, path="items[0].v"),
                         value("n", primary=False, type="count", path="items")], sample) == []
    assert check_sample([rows(path="features")], sample)


def test_count():
    assert chk(value(type="count", path="activeStorms")) == []
    assert chk(value(type="count", path="current"))


def test_lists_and_empty():
    assert chk(rows()) == []
    assert chk(rows(path="alerts"))                   # empty without may_be_empty
    assert chk(rows(path="alerts", may_be_empty=True)) == []
    assert chk(rows(path="current"))                  # not a list
    assert chk(rows(row=[{"path": "properties.depth", "label": "Depth", "type": "number"}]))
    assert chk(rows(row=[{"path": "properties.time", "label": "When", "type": "time"}])) == []


def test_columns_lengths():
    assert chk(cols()) == []
    c = cols()
    c["columns"][1]["path"] = "daily.short"
    assert any("different lengths" in e for e in chk(c))
    c["columns"][1]["path"] = "daily.missing"
    assert any("not a list" in e for e in chk(c))


def test_missing_paths():
    for p in ("current.nope", "activeStorms.length", "features[3].properties", "current.temperature_2m.x", "daily[0]", "current[0]"):
        assert chk(value(path=p)), p


# --- files + build --------------------------------------------------------------------------------------

def write(d, sid, answers, **kw):
    doc = {"source_id": sid, "answers": answers, "sample_url": "https://api.example.org/x", "checked": "2026-09-28"}
    doc.update(kw)
    (d / f"{sid}.json").write_text(json.dumps(doc))


def test_build_merges_answers(tmp_path):
    write(tmp_path, "src-a", [value()])
    (tmp_path / "_skipped.md").write_text("- src-b — broken\n")
    recs = [{"id": "src-a", "access": {}}, {"id": "src-b", "access": {}}]
    assert merge_answers(recs, tmp_path) == 1
    assert recs[0]["answers"] == [value()] and "answers" not in recs[1]


def test_build_refuses_orphans_and_bad_answers(tmp_path):
    write(tmp_path, "gone", [value()])
    with pytest.raises(SystemExit, match="no record has id"):
        merge_answers([{"id": "src-a", "access": {}}], tmp_path)
    (tmp_path / "gone.json").unlink()
    write(tmp_path, "src-a", [value(label="")])
    with pytest.raises(SystemExit, match="label"):
        merge_answers([{"id": "src-a", "access": {}}], tmp_path)
    write(tmp_path, "src-a", [value(path="rates.{quote}")])
    with pytest.raises(SystemExit, match="not a parameter"):
        merge_answers([{"id": "src-a", "access": {}}], tmp_path)
    assert merge_answers([{"id": "src-a", "access": {"params": [{"name": "quote", "example": "EUR"}]}}],
                         tmp_path) == 1


def test_answer_files_are_closed(tmp_path):
    write(tmp_path, "src-a", [value()], note="x")
    write(tmp_path, "src-b", [value()], source_id="src-c")
    write(tmp_path, "src-d", [value()], checked="yesterday")
    (tmp_path / "src-e.json").write_text("{")
    loaded, errs = load_answers({k: {} for k in ("src-a", "src-b", "src-c", "src-d", "src-e")}, tmp_path)
    assert loaded == {}
    joined = " | ".join(errs)
    for needle in ("src-a.json: must hold exactly", "does not match the file name", "checked must be", "not JSON"):
        assert needle in joined


def test_generated_answers_are_well_formed():
    """Every committed answers file passes the schema and names a record (what `check` enforces in CI)."""
    from sourcetool.answers import ANSWERS, _records
    loaded, errs = load_answers({k: params_of(r) for k, r in _records().items()}, ANSWERS)
    assert errs == [] and loaded


def _fred(template, name="Unemployment rate (FRED)"):
    return {"name": name, "examples": [], "access": {"url_template": template}}


def test_fred_answers_carry_the_series_label_and_unit():
    """A FRED card reads "Unemployment rate | 4.1 %", never "Latest | 4.1"; pc1 series are named as rates."""
    from sourcetool.answers import fred_answers
    got = fred_answers(_fred("https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"),
                       {"columns": ["observation_date", "UNRATE"]})
    assert (got[0]["label"], got[0]["unit"]) == ("Unemployment rate", "%")
    assert got[2]["row"][1]["label"] == "Unemployment rate"
    core = fred_answers(_fred("https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPILFESL&transformation=pc1"),
                        {"columns": ["observation_date", "CPILFESL_PC1"]})
    assert core[0]["label"] == "Core inflation (year over year)" and core[0]["path"] == "rows[0].CPILFESL_PC1"
    with pytest.raises(KeyError):  # a series nobody named is not generated with a generic label
        fred_answers(_fred("https://fred.stlouisfed.org/graph/fredgraph.csv?id=NOPE"),
                     {"columns": ["observation_date", "NOPE"]})


def test_feed_answers_name_what_the_items_are():
    from sourcetool.answers import feed_answers
    sample = {"items": [{"title": "t", "published": "2026-09-28T10:00:00Z"}]}
    assert feed_answers({"categories": ["news/topic_news"]}, sample)[0]["label"] == "Headlines"
    assert feed_answers({"categories": ["science/papers"]}, sample)[0]["label"] == "Latest papers"


# --- v1.2: window, measure, axis, tbd_if; the `result` kind; coverage.water -------------------------------

def test_window_is_a_closed_enum_on_values():
    for w in ("now", "today", "tonight", "tomorrow", "latest"):
        assert answer_problems([value(window=w)]) == [], w
    for w in ("weekend", "Today", "", 1, None, "dow:sat"):
        bad([value(window=w)], "window must be one of")
    bad([rows(window="today")], "unknown keys")
    bad([cols(window="today")], "unknown keys")


def test_measure_is_a_closed_name_on_values():
    for m in ("temperature", "precip_chance", "thunderstorm", "waves", "water_temp", "kp", "sunset"):
        assert answer_problems([value(measure=m)]) == [], m
    for m in ("temp", "Temperature", ["temperature"], "", "wave_height"):
        bad([value(measure=m)], "measure must be one of")
    bad([rows(measure="temperature")], "unknown keys")


def test_axis_names_a_date_or_time_cell_of_its_rows():
    c = cols(axis={"cell": "daily.time", "step": "day"})
    assert answer_problems([c]) == []
    r = rows(row=[{"path": "startTime", "label": "When", "type": "time"},
                  {"path": "shortForecast", "label": "Forecast", "type": "text"}],
             axis={"cell": "startTime", "step": "period"})
    assert answer_problems([r]) == []
    bad([cols(axis={"cell": "daily.time", "step": "week"})], "axis step must be")
    bad([cols(axis={"cell": "daily.time"})], "axis must be {cell, step}")
    bad([cols(axis={"cell": "daily.time", "step": "day", "tz": "x"})], "axis must be {cell, step}")
    bad([cols(axis={"cell": "daily.temperature_2m_max", "step": "day"})], "axis cell must be one of its date/time")
    bad([cols(axis={"cell": "daily.nope", "step": "day"})], "axis cell must be one of its date/time")
    bad([rows(axis={"cell": "properties.place", "step": "hour"})], "axis cell must be one of its date/time")
    bad([value(axis={"cell": "current.time", "step": "hour"})], "unknown keys")


def test_tbd_if_is_on_a_time_value_or_time_row_cell():
    t = {"path": "dates[0].games[0].status.startTimeTBD", "equals": True}
    assert answer_problems([value(type="time", path="dates[0].games[0].gameDate", tbd_if=t)]) == []
    assert answer_problems([value(type="time", tbd_if={"path": "status", "equals": "TBD"})]) == []
    row = {"path": "gameDate", "label": "Time", "type": "time", "tbd_if": {"path": "status.startTimeTBD",
                                                                              "equals": True}}
    assert answer_problems([rows(row=[row])]) == []
    bad([value(type="number", tbd_if=t)], "tbd_if is on a time only")
    bad([value(type="time", tbd_if={"path": "x"})], "tbd_if must be {path, equals}")
    bad([value(type="time", tbd_if={"path": "x", "equals": False})], "tbd_if equals must be true or")
    bad([value(type="time", tbd_if={"path": "x", "equals": "{team}"})], "tbd_if equals must be true or")
    bad([value(type="time", tbd_if={"path": "a[*]", "equals": True})], "bad tbd_if path")
    c = cols()
    c["columns"][0]["tbd_if"] = {"path": "x", "equals": True}
    bad([c], "unknown keys")


def test_v12_keys_checked_against_the_sample():
    sample = {"daily": {"time": ["2026-09-28", "2026-09-29"], "t": [1, 2], "epoch": [1790000000, 1790086400]},
              "periods": [{"startTime": "2026-09-28T18:00:00-05:00", "name": "Tonight", "ok": True},
                          {"startTime": "Tomorrow", "name": "Tuesday", "ok": True}],
              "games": [{"gameDate": "2026-10-03T20:08:00Z", "status": {"startTimeTBD": True}}]}
    good = cols(axis={"cell": "daily.time", "step": "day"},
                columns=[{"path": "daily.time", "label": "Day", "type": "date"},
                         {"path": "daily.t", "label": "T", "type": "number"}])
    assert check_sample([good], sample) == []
    epoch = cols(axis={"cell": "daily.epoch", "step": "day"},
                 columns=[{"path": "daily.epoch", "label": "Day", "type": "time"},
                          {"path": "daily.t", "label": "T", "type": "number"}])
    assert any("not an ISO date/time" in e for e in check_sample([epoch], sample))
    per = rows(path="periods", row=[{"path": "startTime", "label": "When", "type": "time"}],
               axis={"cell": "startTime", "step": "period"})
    # every row's cell must be ISO, not only the first one the type check reads
    assert any("not an ISO date/time" in e for e in check_sample([per], sample))
    tbd = value(type="time", path="games[0].gameDate",
                tbd_if={"path": "games[0].status.startTimeTBD", "equals": True})
    assert check_sample([tbd], sample) == []
    gone = value(type="time", path="games[0].gameDate", tbd_if={"path": "games[0].status.nope", "equals": True})
    assert any("tbd_if" in e for e in check_sample([gone], sample))


def test_a_time_beside_a_tbd_flag_must_declare_tbd_if():
    sample = {"games": [{"gameDate": "2026-10-03T20:08:00Z", "status": {"startTimeTBD": False}}],
              "events": [{"strTimestamp": "2026-10-03T20:00:00", "strTime": "TBA"}],
              "plain": [{"when": "2026-10-03T20:00:00Z", "name": "x"}]}
    assert any("TBD/TBA" in e for e in check_sample([value(type="time", path="games[0].gameDate")], sample))
    ev = rows(path="events", row=[{"path": "strTimestamp", "label": "When", "type": "time"}])
    assert any("TBD/TBA" in e for e in check_sample([ev], sample))
    pl = rows(path="plain", row=[{"path": "when", "label": "When", "type": "time"}])
    assert check_sample([pl], sample) == []


def _rec(**kw):
    r = {"id": "x-source", "name": "x", "description": "d", "provider": {"name": "P", "authority": "official"},
         "tier": "curated", "categories": ["weather/current"], "kinds": ["current_value"], "coverage": {"geo": "US"},
         "access": {"kind": "http_json", "url_template": "https://api.example.org/x", "params": [], "auth": "none"},
         "terms": {"status": "public_domain"}, "freshness": {"cadence": "hourly"}, "origin": {"by": "test"}}
    r.update(kw)
    return r


def test_result_kind_and_water_coverage():
    assert validate_record(_rec(kinds=["result", "latest_items"])) == []
    assert validate_record(_rec(coverage={"geo": "US", "water": "ocean_coastal"})) == []
    assert any("coverage.water" in e for e in validate_record(_rec(coverage={"geo": "US", "water": "lakes"})))


# --- lints (the data's promises agree with its answers) ---------------------------------------------------

def test_count_answers_never_claim_any_words():
    from sourcetool.answers import answer_lints
    assert answer_lints(_rec(kinds=["count"], answers=[value(type="count", path="items")])) == []
    got = answer_lints(_rec(kinds=["count"], answers=[value(type="count", path="items",
                                                            words=["how many", "count", "any hurricanes"])]))
    assert any("any hurricanes" in e for e in got)


def test_every_declared_kind_is_covered_by_an_answer():
    from sourcetool.answers import answer_lints
    nxt = value("next_time", type="time", path="games[0].gameDate")
    sched = rows(path="games", row=[{"path": "gameDate", "label": "When", "type": "time"}])
    assert answer_lints(_rec(kinds=["next_event"], answers=[nxt])) == []
    got = answer_lints(_rec(kinds=["next_event", "schedule"], answers=[nxt]))
    assert any("schedule" in e for e in got) and not any("next_event" in e for e in got)
    assert answer_lints(_rec(kinds=["next_event", "schedule"], answers=[nxt, sched])) == []
    assert any("result" in e for e in answer_lints(_rec(kinds=["result"], answers=[nxt])))
    score = value("home_score", path="games[0].teams.home.score")
    assert answer_lints(_rec(kinds=["result"], answers=[score])) == []
    assert any("latest_items" in e for e in answer_lints(_rec(kinds=["latest_items"], answers=[value()])))
    # kinds that need no particular shape (lookup, map, image, text_brief, compare) are never flagged
    assert answer_lints(_rec(kinds=["lookup", "map"], answers=[value()])) == []
    assert answer_lints(_rec(kinds=["forecast"])) == []  # a record with no answers is not linted


def test_every_subcategory_keyword_is_spoken_by_a_source_filed_there():
    from sourcetool.answers import keyword_lints
    tax = {"categories": [{"id": "weather", "subcategories": [
        {"id": "forecast", "keywords": ["forecast", "thunderstorm", "hail"]},
        {"id": "empty", "keywords": ["nothing filed"]}]}]}
    recs = [_rec(categories=["weather/forecast"], name="Forecast", description="7-day forecast",
                 answers=[value(words=["thunderstorms", "rain", "storm"])]),
            _rec(id="harvested-x", tier="harvested", categories=["weather/forecast"], description="hail reports")]
    got = keyword_lints(recs, tax)
    assert got == ["weather/forecast: no curated source filed here speaks to 'hail'"]

