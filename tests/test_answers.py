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
         "columns": [{"path": "daily.time", "label": "Day", "type": "time"},
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
