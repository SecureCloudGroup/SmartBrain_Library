"""Answers spec v1.5: a text cell's `codes` names a Library code table, taxonomy/codes/<name>.json — the closed list of
values the cell holds. The NWS alert event types (`nws_event`) sit on every NWS alert answer's `properties.event`
cell; a table sits on its own cell type only; `check_sample` reports a sample value outside the table; `check` and
`build` refuse a malformed table; `build` packs every table into library_codes(name, value)."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import duckdb
import pytest

import sourcetool.answers as answers_mod
import sourcetool.build as build_mod
from sourcetool.answers import answer_problems, check_sample, load_code_tables

FAMILIES = ("Flood Watch", "Flash Flood Warning", "Coastal Flood Advisory", "High Wind Warning", "Small Craft Advisory",
            "Hurricane Warning", "Red Flag Warning", "Winter Weather Advisory", "Air Quality Alert", "Tsunami Warning",
            "Tornado Warning", "Severe Thunderstorm Watch", "Heat Advisory", "Special Weather Statement",
            "Tropical Storm Warning", "Storm Surge Warning", "Extreme Wind Warning", "Winter Storm Watch")
NWS_ALERTS = ("nws-alerts-point", "nws-alerts-state", "nws-tropical-alerts-point")
EVENT_CELL = {"path": "properties.event", "label": "Alert", "type": "text", "codes": "nws_event"}
ACTUAL = {"path": "properties.status", "equals": "Actual"}  # NWS test / exercise alerts never become card rows


def alerts(**cell: object) -> dict:
    return {"name": "alerts", "label": "Active weather alerts", "kind": "list", "primary": True,
            "words": ["alerts", "warnings", "watches"], "path": "features", "may_be_empty": True,
            "row": [{**EVENT_CELL, **cell}]}


def first_alert(**kw: object) -> dict:
    return {"name": "first_alert", "label": "Alert", "kind": "value", "words": ["alert", "warning", "watch"],
            "path": "features[0].properties.event", "type": "text", "codes": "nws_event", **kw}


def event_columns() -> dict:
    return {"name": "events", "label": "Events", "kind": "columns", "words": ["events", "alerts", "warnings"],
            "columns": [{"path": "events", "label": "Event", "type": "text", "codes": "nws_event"},
                        {"path": "ends", "label": "Ends", "type": "time"}]}


def sample(*events: object) -> dict:
    return {"type": "FeatureCollection", "features": [{"properties": {"event": e}} for e in events]}


def write_table(directory: Path, name: str, **over: object) -> None:
    doc = {"description": "Test values.", "type": "text", "source_url": "https://example.org/types",
           "fetched": "2026-10-12", "values": ["Flood Watch", "Flood Warning"], **over}
    (directory / f"{name}.json").write_text(json.dumps(doc))


# --- the table ------------------------------------------------------------------------------------------

def test_the_nws_event_table_holds_the_alert_families() -> None:
    tables, problems = load_code_tables()
    assert problems == [] and set(tables) == {"nws_event"}
    values = tables["nws_event"]
    assert len(values) >= 100 and values == sorted(set(values))
    assert set(FAMILIES) <= set(values), sorted(set(FAMILIES) - set(values))
    last = [v.split()[-1] for v in values]
    assert min(last.count("Warning"), last.count("Watch"), last.count("Advisory")) >= 20
    assert {f"Coastal Flood {k}" for k in ("Watch", "Warning", "Advisory", "Statement")} <= set(values)
    assert "Test Message" in values  # NWS test alerts (status Test) carry it; /alerts/types lacks it (live 2026-10-12)


def test_every_nws_alert_answer_declares_the_event_table() -> None:
    alert_ids = sorted(sid for sid, r in answers_mod._records("curated").items()
                       if r["access"].get("url_template", "").startswith("https://api.weather.gov/alerts"))
    assert alert_ids == sorted(NWS_ALERTS)
    for sid in alert_ids:
        d = json.loads((answers_mod.ANSWERS / f"{sid}.json").read_text())
        cells = [c for a in d["answers"] for c in a.get("row") or [] if c["path"] == "properties.event"]
        assert cells and all(c == EVENT_CELL for c in cells), (sid, cells)
        lists = [a for a in d["answers"] if a["kind"] == "list"]
        assert lists and all(a.get("filter") == ACTUAL for a in lists), (sid, [a.get("filter") for a in lists])


def test_an_address_asks_only_for_event_types_nws_issues() -> None:
    """The tropical address asked for the retired "Hurricane Local Statement", which matches nothing (the API answers
    an unknown event name with no rows); its successor is "Tropical Cyclone Local Statement"."""
    values = set(load_code_tables()[0]["nws_event"])
    for sid in NWS_ALERTS:
        url = answers_mod._records("curated")[sid]["access"]["url_template"]
        asked = [e for v in parse_qs(urlsplit(url).query).get("event", []) for e in v.split(",")]
        assert set(asked) <= values, (sid, sorted(set(asked) - values))
        if sid == "nws-tropical-alerts-point":
            assert "Tropical Cyclone Local Statement" in asked and "Hurricane Local Statement" not in asked


# --- the schema -----------------------------------------------------------------------------------------

def test_a_code_table_sits_on_its_own_cell_type() -> None:
    assert answer_problems([alerts()]) == []                       # a text list row field (v1.5)
    assert answer_problems([first_alert()]) == []                  # a text value
    assert answer_problems([event_columns()]) == []                # a text column
    assert answer_problems([alerts(type="number", codes="wmo_weather")]) == []  # a weather code on a number row field
    for cell in ({"type": "number"}, {"codes": "wmo_weather"}, {"codes": "nws_events"}, {"codes": ["nws_event"]},
                 {"type": "time"}):
        errs = answer_problems([alerts(**cell)])
        assert any("codes must be" in e and "nws_event" in e for e in errs), (cell, errs)
    assert any("codes must be" in e for e in answer_problems([first_alert(type="count")]))


def test_a_cell_naming_a_missing_or_empty_table_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answers_mod, "CODE_TABLES", tmp_path)
    assert any("codes must be" in e and "(none)" in e for e in answer_problems([alerts()]))
    write_table(tmp_path, "nws_event", values=[])
    tables, problems = load_code_tables()
    assert tables == {} and any("values must be" in p for p in problems), problems
    assert any("codes must be" in e for e in answer_problems([alerts()]))


@pytest.mark.parametrize("over, needle", [
    ({"values": []}, "values must be"),
    ({"values": ["Flood Watch", "flood  WATCH"]}, "repeat"),
    ({"values": [" Flood Watch"]}, "values must be"),
    ({"values": ["x" * 81]}, "values must be"),
    ({"values": "Flood Watch"}, "values must be"),
    ({"type": "number"}, "type must be text"),
    ({"fetched": "Oct 12"}, "fetched"),
    ({"source_url": "http://example.org/types"}, "source_url"),
    ({"description": " "}, "description"),
    ({"extra": 1}, "exactly"),
])
def test_a_code_table_has_a_closed_shape(tmp_path: Path, over: dict, needle: str) -> None:
    write_table(tmp_path, "t", **over)
    tables, problems = load_code_tables(tmp_path)
    assert tables == {} and any(needle in p for p in problems), problems


def test_a_code_table_is_named_by_a_slug_that_is_no_number_table(tmp_path: Path) -> None:
    write_table(tmp_path, "wmo_weather")
    write_table(tmp_path, "Bad-Name")
    (tmp_path / "broken.json").write_text("{")
    write_table(tmp_path, "good")
    tables, problems = load_code_tables(tmp_path)
    assert tables == {"good": ["Flood Watch", "Flood Warning"]} and len(problems) == 3, problems


# --- the check against a sample -------------------------------------------------------------------------

def test_check_sample_reports_an_event_outside_the_table() -> None:
    assert check_sample([alerts()], sample("Flood Watch", "coastal flood  ADVISORY", "Special Weather Statement")) == []
    assert check_sample([alerts()], sample()) == []  # no alert right now: nothing to compare
    lacking = {"features": [{"properties": {"event": "Flood Watch"}}, {"properties": {}}, {"properties": {"event": None}}]}
    assert check_sample([alerts()], lacking) == []   # a row that lacks the cell is allowed
    errs = check_sample([alerts()], sample("Flood Watch", "Excessive Heat Warning", "Wind Chill Advisory",
                                           "Excessive Heat Warning"))
    assert len(errs) == 1 and "nws_event" in errs[0], errs
    assert '"Excessive Heat Warning", "Wind Chill Advisory", not in code table' in errs[0], errs


def test_check_sample_reads_a_value_and_a_column_too() -> None:
    assert check_sample([first_alert()], sample("Flood Watch")) == []
    assert "not in code table nws_event" in check_sample([first_alert()], sample("Volcano Party"))[0]
    ends = ["2026-10-12T10:00:00Z", "2026-10-12T11:00:00Z"]
    assert check_sample([event_columns()], {"events": ["Flood Watch", "Tornado Watch"], "ends": ends}) == []
    errs = check_sample([event_columns()], {"events": ["Flood Watch", "Bogus Warning"], "ends": ends})
    assert len(errs) == 1 and '"Bogus Warning"' in errs[0], errs


# --- check and build ------------------------------------------------------------------------------------

def test_check_reports_a_malformed_code_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    import sourcetool.__main__ as cli
    write_table(tmp_path, "t", type="number")
    monkeypatch.setattr(answers_mod, "CODE_TABLES", tmp_path)
    assert cli._check_taxonomy_and_resolvers() >= 1
    assert "codes/t.json: type must be text" in capsys.readouterr().out


def test_build_refuses_a_malformed_code_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_table(tmp_path, "t", values=[])
    monkeypatch.setattr(answers_mod, "CODE_TABLES", tmp_path)
    monkeypatch.setattr(build_mod, "DB", None)  # never reached: the gate comes first
    with pytest.raises(SystemExit, match="code tables refused"):
        build_mod.build()


def test_build_packs_every_code_table() -> None:
    tables, _ = load_code_tables()
    con = duckdb.connect()
    assert build_mod._load_codes(con, tables) == sum(len(v) for v in tables.values())
    got = [v for (v,) in con.execute("SELECT value FROM library_codes WHERE name = 'nws_event' ORDER BY value")
           .fetchall()]
    assert got == sorted(tables["nws_event"])
    assert build_mod._load_codes(duckdb.connect(), {}) == 0  # no table: an empty library_codes


def test_built_pack_holds_the_nws_event_table() -> None:
    if not build_mod.DB.exists():
        pytest.skip("no build/library.duckdb (run `python -m sourcetool build`)")
    con = duckdb.connect(str(build_mod.DB), read_only=True)
    n = con.execute("SELECT count(*) FROM library_codes WHERE name = 'nws_event'").fetchone()[0]
    con.close()
    assert n == len(load_code_tables()[0]["nws_event"])
