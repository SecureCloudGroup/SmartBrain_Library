"""answers-recheck: PASS stamps checked, a first failure is remembered, a second on a later day marks the file
drifted, a pass recovers it; drifted files are held back from build and the lints but still seen by the live
check; file formatting survives a rewrite. No network: the fetch is a fake."""
from __future__ import annotations

import json

from sourcetool import recheck
from sourcetool.answers import Failed, Skip, load_answers
from sourcetool.build import merge_answers


def _value(**kw):
    a = {"name": "temp", "label": "Temperature", "kind": "value", "primary": True, "type": "number",
         "path": "current.temperature_2m", "words": ["temperature", "temp", "how hot"], "window": "now"}
    a.update(kw)
    return a


def _write(d, sid, indent=2, newline=True, **extra):
    data = {"source_id": sid, "answers": [_value()], "sample_url": "https://ex.test/x", "checked": "2026-09-01", **extra}
    (d / f"{sid}.json").write_text(json.dumps(data, indent=indent) + ("\n" if newline else ""))


def _records(*sids):
    return {s: {"id": s, "tier": "curated", "access": {"kind": "http_json", "params": []}} for s in sids}


def test_apply_result_transitions():
    d = {"checked": "2026-09-01"}
    assert recheck.apply_result(d, False, "2026-10-06") == "fail" and d["last_failure"] == "2026-10-06"
    assert recheck.apply_result(d, False, "2026-10-06") == "fail"            # same day again: not yet drift
    assert recheck.apply_result(d, False, "2026-10-13") == "drifted" and d["status"] == "drifted"
    assert recheck.apply_result(d, False, "2026-10-20") == "drifted"         # stays drifted while failing
    assert recheck.apply_result(d, True, "2026-10-27") == "recovered"
    assert d == {"checked": "2026-10-27"}                                    # keys dropped on recovery
    assert recheck.apply_result(d, True, "2026-11-03") == "pass"


def test_run_writes_verdicts_and_keeps_formatting(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "ok-src", indent=1)
    _write(tmp_path, "bad-src", indent=2, newline=False)
    _write(tmp_path, "keyed-src")
    monkeypatch.setattr(recheck, "_records", lambda: _records("ok-src", "bad-src", "keyed-src"))

    def fetch(rec, cache_hours):
        assert cache_hours == 0, "a recheck never reads the sample cache"
        if rec["id"] == "ok-src":
            return "https://ex.test/x", {"current": {"temperature_2m": 71.3}}
        if rec["id"] == "bad-src":
            raise Failed("HTTP 500")
        raise Skip("needs the user's key")

    rc = recheck.run([], 0, directory=tmp_path, fetch=fetch, today="2026-10-06")
    out = capsys.readouterr().out
    assert rc == 1 and "PASS ok-src" in out and "FAIL bad-src: HTTP 500" in out and "SKIP keyed-src" in out
    ok = (tmp_path / "ok-src.json").read_text()
    assert ok.startswith('{\n "source_id"') and ok.endswith("\n")        # indent 1 + newline kept
    assert json.loads(ok)["checked"] == "2026-10-06"
    bad = (tmp_path / "bad-src.json").read_text()
    assert bad.startswith('{\n  "source_id"') and not bad.endswith("\n")  # indent 2, no newline kept
    assert json.loads(bad)["last_failure"] == "2026-10-06" and "status" not in json.loads(bad)
    assert json.loads((tmp_path / "keyed-src.json").read_text())["checked"] == "2026-09-01"

    rc = recheck.run(["bad-src"], 0, directory=tmp_path, fetch=fetch, today="2026-10-13")
    assert rc == 1 and json.loads((tmp_path / "bad-src.json").read_text())["status"] == "drifted"
    assert "DRIFTED bad-src" in capsys.readouterr().out


def test_since_skips_recently_checked_files(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "ok-src", checked="2026-10-05")
    monkeypatch.setattr(recheck, "_records", lambda: _records("ok-src"))
    calls = []
    rc = recheck.run([], 6, directory=tmp_path, fetch=lambda rec, cache_hours: calls.append(rec) or ("u", {}),
                     today="2026-10-06")
    assert rc == 0 and not calls and "not_due 1" in capsys.readouterr().out


def test_drifted_files_are_held_back_but_still_loadable(tmp_path):
    _write(tmp_path, "src-a")
    _write(tmp_path, "src-b", status="drifted", last_failure="2026-10-01")
    params = {"src-a": {}, "src-b": {}}
    loaded, errs = load_answers(params, tmp_path)
    assert errs == [] and set(loaded) == {"src-a"}
    loaded, _ = load_answers(params, tmp_path, include_drifted=True)
    assert set(loaded) == {"src-a", "src-b"}
    recs = [{"id": "src-a", "access": {}}, {"id": "src-b", "access": {}}]
    assert merge_answers(recs, tmp_path) == 1 and "answers" not in recs[1]


def test_recheck_keys_are_validated(tmp_path):
    _write(tmp_path, "src-a", status="broken")
    _write(tmp_path, "src-b", last_failure="yesterday")
    _write(tmp_path, "src-c", note="x")
    _, errs = load_answers({"src-a": {}, "src-b": {}, "src-c": {}}, tmp_path)
    assert any("status must be one of drifted" in e for e in errs)
    assert any("last_failure must be YYYY-MM-DD" in e for e in errs)
    assert any("src-c.json: must hold exactly" in e for e in errs)


def test_missing_app_parsers_skip_instead_of_failing(monkeypatch):
    """No SmartBrain_3000 checkout beside the Library (CI, a bare machine): a csv/feed/xml/text sample is a SKIP,
    never a FAIL that could drift a healthy source."""
    import sys
    import types

    from sourcetool import answers as answers_mod
    stub = types.ModuleType("smartbrain_3000")  # a package without `formats`, like an older installed app
    monkeypatch.setitem(sys.modules, "smartbrain_3000", stub)
    monkeypatch.delitem(sys.modules, "smartbrain_3000.formats", raising=False)
    try:
        answers_mod._formats()
    except Skip as e:
        assert "parsers are missing" in str(e)
    else:
        raise AssertionError("expected Skip")


def test_connection_failures_are_retried_once(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "flaky-src")
    _write(tmp_path, "dead-src")
    monkeypatch.setattr(recheck, "_records", lambda: _records("flaky-src", "dead-src"))
    monkeypatch.setattr(recheck, "NETWORK_RETRY_S", 0.0)
    seen = {"flaky-src": 0, "dead-src": 0}

    def fetch(rec, cache_hours):
        seen[rec["id"]] += 1
        if rec["id"] == "flaky-src" and seen[rec["id"]] == 1:
            raise Failed("fetch failed: ConnectError")
        if rec["id"] == "dead-src":
            raise Failed("HTTP 404")  # the source's own answer: no retry
        return "https://ex.test/x", {"current": {"temperature_2m": 71.3}}

    rc = recheck.run([], 0, directory=tmp_path, fetch=fetch, today="2026-10-06")
    out = capsys.readouterr().out
    assert rc == 1 and "PASS flaky-src" in out and "FAIL dead-src: HTTP 404" in out
    assert seen == {"flaky-src": 2, "dead-src": 1}
