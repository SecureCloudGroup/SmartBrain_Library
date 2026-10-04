"""asks/: locate v2's example asks (schema.load_asks) and their pack tables (build). No network."""
from __future__ import annotations

import json

import duckdb
import pytest

from sourcetool import answers as answers_mod
from sourcetool import build as build_mod
from sourcetool import resolvers as resolvers_mod
from sourcetool import schema
from sourcetool.common import SOURCES, read_jsonl, taxonomy
from sourcetool.schema import load_asks

TAX = {"categories": [{"id": "weather", "subcategories": [{"id": "forecast"}, {"id": "pollen"}]}]}


def rec(sid: str, **kw) -> dict:
    r = {"id": sid, "name": sid, "description": f"{sid} source", "provider": {"id": "p", "name": "P",
         "authority": "official"}, "tier": "curated", "categories": ["weather/forecast"], "kinds": ["forecast"],
         "coverage": {"geo": "US"}, "access": {"kind": "http_json", "auth": "none",
                                               "url_template": f"https://api.example.org/{sid}"},
         "terms": {"status": "public_domain"}, "freshness": {"cadence": "hourly"}, "origin": {},
         "validation": {"status": "ok"}, "examples": ["weather tomorrow"]}
    r.update(kw)
    return r


def asks(n: int, stem: str = "ask") -> list[str]:
    return [f"{stem} {i}" for i in range(n)]


def write(d, sources=None, routes=None):
    if sources is not None:
        (d / "sources.jsonl").write_text("".join(json.dumps(x) + "\n" for x in sources))
    if routes is not None:
        (d / "routes.jsonl").write_text("".join(json.dumps(x) + "\n" for x in routes))


ROUTES = [{"route": "weather/forecast", "asks": asks(12, "forecast")},
          {"route": "weather/pollen", "asks": asks(12, "pollen")}]


def test_well_formed_asks_load_as_rows(tmp_path):
    write(tmp_path, [{"source_id": "a", "asks": asks(10)}], ROUTES)
    src, route, errs = load_asks([rec("a")], TAX, tmp_path)
    assert errs == []
    assert src[0] == ("a", "ask 0") and len(src) == 10
    assert route[0] == ("weather", "forecast", "forecast 0") and len(route) == 24


@pytest.mark.parametrize("line, needle", [
    ({"source_id": "nope", "asks": asks(10)}, "unknown source_id 'nope'"),
    ({"source_id": "a", "asks": asks(9)}, "9 asks, needs exactly 10"),
    ({"source_id": "a", "asks": asks(9) + ["x" * 81]}, "over 80 characters"),
    ({"source_id": "a", "asks": asks(9) + ["ASK  0"]}, "a repeated ask"),
    ({"source_id": "a", "asks": asks(9) + ["Weather  Tomorrow"]}, "copies the record's example"),
    ({"source_id": "a", "asks": asks(9) + [" padded"]}, "non-empty strings"),
    ({"source_id": "a", "asks": asks(10), "note": "x"}, "must hold exactly source_id and asks"),
])
def test_source_ask_problems(tmp_path, line, needle):
    write(tmp_path, [line], ROUTES)
    _, _, errs = load_asks([rec("a")], TAX, tmp_path, require_all=False)
    assert any(needle in e for e in errs), errs


def test_route_ask_problems(tmp_path):
    write(tmp_path, [], [{"route": "weather/radar", "asks": asks(12)}, {"route": "weather/pollen", "asks": asks(10)},
                         {"route": "weather/pollen", "asks": asks(12)}])
    errs = " | ".join(load_asks([], TAX, tmp_path, require_all=False)[2])
    for needle in ("unknown route 'weather/radar'", "10 asks, needs exactly 12", "weather/pollen appears twice"):
        assert needle in errs


def test_an_ask_may_reuse_another_sources_example(tmp_path):
    """Only the record's OWN examples are refused; asks of two sources may overlap."""
    write(tmp_path, [{"source_id": "a", "asks": asks(9) + ["rain tonight"]}], ROUTES)
    assert load_asks([rec("a"), rec("b", examples=["rain tonight"])], TAX, tmp_path, require_all=False)[2] == []


def test_check_wants_every_offerable_source_and_every_subcategory(tmp_path):
    recs = [rec("a"), rec("failed", validation={"status": "failed"}), rec("refused", validation={"status": "refused"}),
            rec("helper", role="helper"), rec("retired", replaced_by="a"), rec("harvested", tier="harvested"),
            rec("new", validation={"status": "unvalidated"})]
    write(tmp_path, [{"source_id": "a", "asks": asks(10)}], ROUTES[:1])
    errs = load_asks(recs, TAX, tmp_path)[2]
    assert sorted(errs) == ["routes.jsonl: no asks for weather/pollen", "sources.jsonl: no asks for new"]
    assert load_asks(recs, TAX, tmp_path, require_all=False)[2] == []  # build loads what is there


def test_a_retired_source_takes_its_asks_with_it(tmp_path):
    write(tmp_path, [{"source_id": "old", "asks": asks(10)}], ROUTES)
    assert "unknown source_id 'old'" in load_asks([rec("old", replaced_by="a"), rec("a")], TAX, tmp_path,
                                                   require_all=False)[2][0]


def test_missing_files(tmp_path):
    assert load_asks([], TAX, tmp_path, require_all=False) == ([], [], [])
    errs = load_asks([], TAX, tmp_path)[2]
    assert "sources.jsonl: missing" in errs and "routes.jsonl: missing" in errs


def test_committed_asks_pass_check():
    """asks/ covers every offerable curated source and every subcategory (what `check` enforces in CI)."""
    recs = [r for d in ("curated", "harvested", "suggested") for f in sorted((SOURCES / d).glob("*.jsonl"))
            for r in read_jsonl(f)]
    src, route, errs = load_asks(recs, taxonomy())
    assert errs == []
    assert len({s for s, _ in src}) == sum(schema.offerable(r) for r in recs)
    assert len(route) == 12 * sum(len(c["subcategories"]) for c in taxonomy()["categories"])


# --- build: a small fixture pack ------------------------------------------------------------------------

PLACE_ACCESS = {"kind": "http_json", "auth": "none", "url_template": "https://api.example.org/b?q={place}",
                "params": [{"name": "place", "kind": "place", "fill": {"from": "resolver", "resolver": "place"}}]}


@pytest.fixture
def pack(tmp_path, monkeypatch):
    for d in ("sources/curated", "build", "asks", "answers", "resolvers"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "sources/curated/core.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (rec("a"), rec("b", examples=[], access=PLACE_ACCESS))))
    monkeypatch.setattr(build_mod, "SOURCES", tmp_path / "sources")
    monkeypatch.setattr(build_mod, "BUILD", tmp_path / "build")
    monkeypatch.setattr(build_mod, "DB", tmp_path / "build" / "library.duckdb")
    monkeypatch.setattr(build_mod, "_RESOLVED", {})
    monkeypatch.setattr(answers_mod, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(resolvers_mod, "RES", tmp_path / "resolvers")
    monkeypatch.setattr(schema, "ASKS", tmp_path / "asks")
    return tmp_path


def test_build_loads_both_ask_tables(pack):
    routes = [{"route": f"{c['id']}/{s['id']}", "asks": asks(12, s["id"])}
              for c in taxonomy()["categories"] for s in c["subcategories"]][:2]
    write(pack / "asks", [{"source_id": "a", "asks": asks(9) + ["what's it like, \"outside\", tonight?"]}], routes)
    build_mod.build()
    con = duckdb.connect(str(pack / "build" / "library.duckdb"), read_only=True)
    rows = con.execute("SELECT source_id, ask FROM library_source_asks ORDER BY ask").fetchall()
    assert len(rows) == 10 and ("a", "what's it like, \"outside\", tonight?") in rows  # quotes and commas survive
    first = routes[0]["route"].split("/")
    assert con.execute("SELECT count(*) FROM library_route_asks WHERE category = ? AND subcategory = ?",
                       first).fetchone()[0] == 12
    assert con.execute("SELECT count(*) FROM library_route_asks").fetchone()[0] == 24


def test_build_without_asks_makes_empty_tables(pack):
    build_mod.build()
    con = duckdb.connect(str(pack / "build" / "library.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM library_source_asks").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM library_route_asks").fetchone()[0] == 0


def test_build_refuses_malformed_asks(pack):
    write(pack / "asks", [{"source_id": "a", "asks": asks(9) + ["weather tomorrow"]}])
    with pytest.raises(SystemExit, match="asks refused"):
        build_mod.build()
