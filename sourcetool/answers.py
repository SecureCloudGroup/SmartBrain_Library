"""Source `answers`: which paths of a source's response answer which questions (spec v1).

An answer names a path into the response, a label, the words people use to ask for it and the type the
card shows. `answer_problems` is the schema (closed keys, types, limits); `check_sample` verifies the paths
against a real response. Authoring files live in `answers/<source_id>.json`; `sourcetool build` merges them
into the records.

  python -m sourcetool answers-check [ids...]   fetch each sample, run the check, print PASS/FAIL (live network)
  python -m sourcetool answers-generate         write code-generated answers for curated feeds and FRED series
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import ROOT, SOURCES, Refused, get, read_jsonl, taxonomy

ANSWERS = ROOT / "answers"
# the SmartBrain app checkout beside this one (its parsers give samples the shape the app sees)
APP = Path(os.environ.get("SMARTBRAIN_APP") or ROOT.parent / "SmartBrain_3000" / "app")
AUTHORING_UA = "SmartBrain-Library-authoring/1 info@securecloudgroup.com"

KINDS = ("value", "list", "columns")
VALUE_TYPES = ("number", "text", "time", "count")
ROW_TYPES = ("number", "text", "time")
CODES = ("wmo_weather",)
COMMON = {"name", "label", "words", "primary", "kind"}
KEYS = {"value": COMMON | {"path", "type", "unit", "unit_path", "codes"},
        "list": COMMON | {"path", "row", "newest_first", "may_be_empty"},
        "columns": COMMON | {"columns", "limit"}}
ROW_KEYS = {"path", "label", "type", "unit", "unit_path"}
COLUMN_KEYS = ROW_KEYS | {"codes"}
FILE_KEYS = {"source_id", "answers", "sample_url", "checked"}
MAX_ANSWERS = 10

_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_PATH = re.compile(r"[A-Za-z_][\w-]*(\[\d+\])?(\.[A-Za-z_][\w-]*(\[\d+\])?)*", re.ASCII)
_SEG = re.compile(r"([A-Za-z_][\w-]*)(?:\[(\d+)\])?", re.ASCII)
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- schema -----------------------------------------------------------------------------------------

def _label_problems(where: str, label) -> list[str]:
    if not isinstance(label, str) or not label.strip() or len(label) > 40:
        return [f"{where}: label must be 1-40 characters"]
    return []


def _unit_problems(where: str, d: dict) -> list[str]:
    errs = []
    if "unit" in d and "unit_path" in d:
        errs.append(f"{where}: unit or unit_path, not both")
    if "unit" in d and (not isinstance(d["unit"], str) or not d["unit"].strip() or len(d["unit"]) > 20):
        errs.append(f"{where}: unit must be a short string")
    if "unit_path" in d and not _path_ok(d["unit_path"]):
        errs.append(f"{where}: bad unit_path {d['unit_path']!r}")
    return errs


def _path_ok(p) -> bool:
    return isinstance(p, str) and bool(_PATH.fullmatch(p))


def _field_problems(where: str, f, keys: set[str], types: tuple) -> list[str]:
    """One list row or one column: {path, label, type, unit?, unit_path?(, codes?)}."""
    if not isinstance(f, dict):
        return [f"{where}: must be an object"]
    errs = [f"{where}: unknown keys {sorted(set(f) - keys)}"] if set(f) - keys else []
    if not _path_ok(f.get("path")):
        errs.append(f"{where}: bad path {f.get('path')!r}")
    errs += _label_problems(where, f.get("label"))
    if f.get("type") not in types:
        errs.append(f"{where}: type must be one of {'/'.join(types)}")
    if "codes" in f and f["codes"] not in CODES:
        errs.append(f"{where}: codes must be \"wmo_weather\"")
    return errs + _unit_problems(where, f)


def answer_problems(answers) -> list[str]:
    """Problems with a source's `answers` list; an empty list means it is well-formed."""
    if not isinstance(answers, list) or not answers:
        return ["answers must be a non-empty list"]
    if len(answers) > MAX_ANSWERS:
        return [f"at most {MAX_ANSWERS} answers per source"]
    errs: list[str] = []
    names: set[str] = set()
    primary = {"value": 0, "list": 0, "columns": 0}
    for i, a in enumerate(answers):
        if not isinstance(a, dict):
            errs.append(f"answers[{i}]: must be an object")
            continue
        name = a.get("name")
        w = f"answer {name if isinstance(name, str) else i}"
        kind = a.get("kind")
        if kind not in KINDS:
            errs.append(f"{w}: kind must be value/list/columns")
            continue
        if set(a) - KEYS[kind]:
            errs.append(f"{w}: unknown keys {sorted(set(a) - KEYS[kind])}")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            errs.append(f"{w}: name must be a slug [a-z][a-z0-9_]{{0,39}}")
        elif name in names:
            errs.append(f"{w}: duplicate name")
        names.add(name if isinstance(name, str) else "")
        errs += _label_problems(w, a.get("label"))
        words = a.get("words")
        if not isinstance(words, list) or not 3 <= len(words) <= 15:
            errs.append(f"{w}: words must be 3-15 entries")
        elif any(not isinstance(x, str) or not x.strip() or x != x.lower() or x != x.strip() or len(x) > 40
                 for x in words):
            errs.append(f"{w}: words must be lowercase words or short phrases (at most 40 characters)")
        if "primary" in a and not isinstance(a["primary"], bool):
            errs.append(f"{w}: primary must be true or false")
        if a.get("primary") is True:
            primary[kind] += 1
        if not _path_ok(a.get("path")) and kind != "columns":
            errs.append(f"{w}: bad path {a.get('path')!r}")
        if kind == "value":
            if a.get("type") not in VALUE_TYPES:
                errs.append(f"{w}: type must be one of {'/'.join(VALUE_TYPES)}")
            if "codes" in a and a["codes"] not in CODES:
                errs.append(f"{w}: codes must be \"wmo_weather\"")
            errs += _unit_problems(w, a)
        elif kind == "list":
            rows = a.get("row")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
                errs.append(f"{w}: row must hold 1-4 fields")
            else:
                for j, f in enumerate(rows):
                    errs += _field_problems(f"{w} row[{j}]", f, ROW_KEYS, ROW_TYPES)
            for flag in ("newest_first", "may_be_empty"):
                if flag in a and not isinstance(a[flag], bool):
                    errs.append(f"{w}: {flag} must be true or false")
        else:
            cols = a.get("columns")
            if not isinstance(cols, list) or not 2 <= len(cols) <= 4:
                errs.append(f"{w}: columns must hold 2-4 fields")
            else:
                for j, f in enumerate(cols):
                    errs += _field_problems(f"{w} columns[{j}]", f, COLUMN_KEYS, ROW_TYPES)
            lim = a.get("limit")
            if "limit" in a and (isinstance(lim, bool) or not isinstance(lim, int) or not 1 <= lim <= 100):
                errs.append(f"{w}: limit must be a whole number 1-100")
    rows_primary = primary["list"] + primary["columns"]
    if rows_primary and primary["value"]:
        errs.append("primary answers are either 1-4 values or exactly 1 list/columns, not both")
    elif rows_primary > 1:
        errs.append("at most one list/columns answer may be primary")
    elif primary["value"] > 4:
        errs.append("at most 4 value answers may be primary")
    elif not rows_primary and not primary["value"]:
        errs.append("one answer at least must be primary")
    return errs


# --- the check against a real response -------------------------------------------------------------

_MISSING = object()


def resolve(data, path: str):
    """Walk `a.b[0].c` through dicts and list indexes; _MISSING when any step is absent."""
    cur = data
    for seg in path.split("."):
        m = _SEG.fullmatch(seg)
        if not m or not isinstance(cur, dict) or m.group(1) not in cur:
            return _MISSING
        cur = cur[m.group(1)]
        if m.group(2) is not None:
            n = int(m.group(2))
            if not isinstance(cur, list) or n >= len(cur):
                return _MISSING
            cur = cur[n]
    return cur


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float)) or (isinstance(v, str) and bool(_NUMBER.fullmatch(v)))


def type_ok(v, typ: str) -> bool:
    """Does value `v` hold what `typ` promises (spec v1, "The check")."""
    if typ == "number":
        return _is_number(v)
    if typ == "text":
        return isinstance(v, str) and bool(v.strip())
    if typ == "time":
        if isinstance(v, str) and _ISO_TIME.match(v):
            return True
        return _is_number(v) and float(v) > 1e8
    if typ == "count":
        return isinstance(v, list)
    return False


def _show(v) -> str:
    s = json.dumps(v, ensure_ascii=False) if v is not _MISSING else "missing"
    return s if len(s) <= 60 else s[:57] + "..."


def _check_field(where: str, item, root, f: dict) -> list[str]:
    errs = []
    v = resolve(item, f["path"])
    if not type_ok(v, f["type"]):
        errs.append(f"{where}: {f['path']} is {_show(v)}, not {f['type']}")
    if "unit_path" in f and not isinstance(resolve(root, f["unit_path"]), str):
        errs.append(f"{where}: unit_path {f['unit_path']} is {_show(resolve(root, f['unit_path']))}, not text")
    return errs


def check_sample(answers: list, sample) -> list[str]:
    """Every path resolves in the sample with the type its answer promises. unit_path always resolves
    from the response root (units are response metadata, e.g. `daily_units.temperature_2m_max`)."""
    errs: list[str] = []
    for a in answers:
        w = f"answer {a['name']}"
        if a["kind"] == "value":
            errs += _check_field(w, sample, sample, a)
        elif a["kind"] == "list":
            items = resolve(sample, a["path"])
            if not isinstance(items, list):
                errs.append(f"{w}: {a['path']} is {_show(items)}, not a list")
            elif not items:
                if not a.get("may_be_empty"):
                    errs.append(f"{w}: {a['path']} is empty (set may_be_empty when that is a real answer)")
            else:
                for j, f in enumerate(a["row"]):
                    errs += _check_field(f"{w} row[{j}]", items[0], sample, f)
        else:
            lengths = set()
            for j, f in enumerate(a["columns"]):
                col = resolve(sample, f["path"])
                if not isinstance(col, list):
                    errs.append(f"{w} columns[{j}]: {f['path']} is {_show(col)}, not a list")
                    continue
                lengths.add(len(col))
                if "unit_path" in f and not isinstance(resolve(sample, f["unit_path"]), str):
                    errs.append(f"{w} columns[{j}]: unit_path {f['unit_path']} is not text")
            if len(lengths) > 1:
                errs.append(f"{w}: columns have different lengths {sorted(lengths)}")
            elif lengths == {0}:
                errs.append(f"{w}: columns are empty")
    return errs


# --- authoring files -------------------------------------------------------------------------------

def load_answers(ids: set[str], directory: Path = ANSWERS) -> tuple[dict[str, list], list[str]]:
    """Read every answers/<id>.json: ({source_id: answers}, problems). A file whose id has no record, or
    whose answers fail the schema, is a problem (build refuses it)."""
    out: dict[str, list] = {}
    errs: list[str] = []
    for f in sorted(directory.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError as e:
            errs.append(f"{f.name}: not JSON ({e})")
            continue
        if not isinstance(d, dict) or set(d) != FILE_KEYS:
            errs.append(f"{f.name}: must hold exactly {sorted(FILE_KEYS)}")
            continue
        sid = d["source_id"]
        if sid != f.stem:
            errs.append(f"{f.name}: source_id {sid!r} does not match the file name")
            continue
        if sid not in ids:
            errs.append(f"{f.name}: no record has id {sid!r}")
            continue
        bad = answer_problems(d["answers"])
        if not isinstance(d["sample_url"], str) or not d["sample_url"].startswith("https://"):
            bad.append("sample_url must be the https URL fetched")
        if not isinstance(d["checked"], str) or not _DATE.fullmatch(d["checked"]):
            bad.append("checked must be YYYY-MM-DD")
        errs += [f"{f.name}: {e}" for e in bad]
        if not bad:
            out[sid] = d["answers"]
    return out, errs


def _records(tier: str | None = None) -> dict[str, dict]:
    dirs = ("curated", "harvested", "suggested")
    recs = {r["id"]: r for d in dirs for f in sorted((SOURCES / d).glob("*.jsonl")) for r in read_jsonl(f)}
    return {k: r for k, r in recs.items() if tier is None or r["tier"] == tier}


# --- live samples ----------------------------------------------------------------------------------

class Skip(Exception):
    """The source cannot be sampled without a user's own value or key."""


class Failed(Exception):
    """The sample could not be fetched or parsed."""


FORMAT = {"http_json": "json", "gbfs": "json", "http_csv": "csv", "rss": "feed", "atom": "feed",
          "http_xml": "xml", "text": "text"}


def _formats():
    """The app's parsers, so a sample has exactly the shape the app's cards see."""
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    try:
        from smartbrain_3000 import formats
    except ImportError as e:
        raise Failed(f"the app's parsers are missing ({APP}/smartbrain_3000/formats.py: {e}); "
                     "csv/feed/xml/text samples need a SmartBrain_3000 checkout") from None
    return formats


def sample_url(rec: dict) -> tuple[str, dict]:
    """The record's example URL and headers. Skip when it needs a user's key or value."""
    from .validate import fill
    a = rec["access"]
    if a["kind"] not in FORMAT:
        raise Skip(f"access.kind {a['kind']} has no parsed form")
    keys = [p for p in a.get("params", []) if p.get("kind") == "key"]
    if any(p.get("example") != "DEMO_KEY" for p in keys):
        raise Skip("needs the user's own key (no documented demo key)")
    if a["auth"] != "none" and not keys:
        raise Skip(f"needs the user's {a['auth']}")
    url = fill(a.get("url_template") or "", a.get("params", []))
    if url is None:
        raise Skip("a parameter has no example")
    headers = {}
    for hk, hv in (a.get("headers") or {}).items():
        if "{" in hv:
            raise Skip("needs the user's key in a header")
        headers[hk] = hv
    if a.get("contact_ua"):
        headers["User-Agent"] = AUTHORING_UA
    return url, headers


def fetch_sample(rec: dict, cache_hours: float = 12.0):
    """(url, parsed sample) — JSON, or the app's parsed form for csv/feed/xml/text."""
    from .validate import _API_KINDS
    url, headers = sample_url(rec)
    kind = rec["access"]["kind"]
    try:  # documented API/feed endpoints follow the provider's terms (as `validate` does); pages honor robots
        status, body, _ = get(url, cache_hours=cache_hours, headers=headers or None,
                              api=kind in _API_KINDS and bool(rec["access"].get("docs_url")))
    except Refused:
        raise Failed("robots.txt disallows") from None
    except Exception as e:  # network failure is a result, not a crash
        raise Failed(f"fetch failed: {type(e).__name__}") from None
    if status != 200:
        raise Failed(f"HTTP {status}")
    text = body.decode("utf-8-sig", "replace")
    fmt = FORMAT[kind]
    if fmt == "json":
        try:
            return url, json.loads(text)
        except ValueError:
            raise Failed("body is not JSON") from None
    formats = _formats()
    try:
        return url, getattr(formats, f"parse_{fmt}")(text)
    except formats.FormatError as e:
        raise Failed(f"{fmt} did not parse: {e}") from None


def cmd_check(ids: list[str]) -> int:
    recs = _records()
    loaded, errs = load_answers(set(recs))
    for e in errs:
        print(f"INVALID {e}")
    todo = ids or sorted(loaded)
    bad = len(errs)
    for sid in todo:
        if sid not in loaded:
            print(f"FAIL {sid}: no valid answers file")
            bad += 1
            continue
        try:
            _, sample = fetch_sample(recs[sid])
        except Skip as e:
            print(f"SKIP {sid}: {e}")
            continue
        except Failed as e:
            print(f"FAIL {sid}: {e}")
            bad += 1
            continue
        problems = check_sample(loaded[sid], sample)
        if problems:
            bad += 1
            print(f"FAIL {sid}: {'; '.join(problems)}")
        else:
            print(f"PASS {sid}: {len(loaded[sid])} answers")
    print(f"{len(todo)} checked, {bad} failing")
    return 1 if bad else 0


# --- code-generated answers (no model) -------------------------------------------------------------

# FRED's own units for the curated series whose unit is a plain percent or U.S. dollars (from each
# series' FRED page). Index levels, counts and millions/billions of dollars carry no unit.
FRED_UNITS = {"A191RL1Q225SBEA": "%", "APU0000702111": "USD", "APU0000708111": "USD", "APU0000709112": "USD",
              "APU0000717311": "USD", "DCOILWTICO": "USD", "DEXUSEU": "USD", "DGS10": "%", "DGS2": "%",
              "DHHNGSP": "USD", "FEDFUNDS": "%", "GASREGW": "USD", "MORTGAGE30US": "%", "MSPUS": "USD",
              "T10Y2Y": "%", "UNRATE": "%"}


def _words(*groups) -> list[str]:
    out: list[str] = []
    for g in groups:
        for w in g:
            w = " ".join(str(w).lower().split())
            if w and len(w) <= 40 and w not in out:
                out.append(w)
    return out[:15]


def feed_answers(rec: dict, sample: dict) -> list[dict]:
    t = taxonomy()
    kw = {f"{c['id']}/{s['id']}": s["keywords"] for c in t["categories"] for s in c["subcategories"]}
    words = _words(*[kw.get(c, []) for c in rec["categories"]])[:12]
    words = _words(words, ["headlines", "news", "latest"])
    row = [{"path": "title", "label": "Title", "type": "text"}]
    first = (sample.get("items") or [{}])[0]
    if type_ok(first.get("published"), "time"):
        row.append({"path": "published", "label": "Published", "type": "time"})
    elif type_ok(first.get("published"), "text"):
        row.append({"path": "published", "label": "Published", "type": "text"})
    return [{"name": "latest", "label": "Latest", "kind": "list", "primary": True, "words": words,
             "path": "items", "row": row, "may_be_empty": False}]


def fred_answers(rec: dict, sample: dict) -> list[dict]:
    date_col, value_col = sample["columns"][0], sample["columns"][1]
    series = parse_qs(urlsplit(rec["access"]["url_template"]).query).get("id", [""])[0]
    unit = {"unit": FRED_UNITS[series]} if series in FRED_UNITS else {}
    name = re.sub(r"\s*\(FRED\)\s*$", "", rec["name"])
    return [
        {"name": "latest", "label": "Latest", "kind": "value", "primary": True,
         "words": _words([name], rec.get("examples", []), ["latest", "current", "now", "today"]),
         "path": f"rows[0].{value_col}", "type": "number", **unit},
        {"name": "as_of", "label": "As of", "kind": "value",
         "words": ["as of", "when", "date", "last updated", "updated"],
         "path": f"rows[0].{date_col}", "type": "text"},
        {"name": "recent", "label": "Recent", "kind": "list",
         "words": ["recent", "history", "trend", "past", "over time", "chart", "last few"],
         "path": "rows", "row": [{"path": date_col, "label": "Date", "type": "text"},
                                 {"path": value_col, "label": name[:40].rstrip(), "type": "number", **unit}]},
    ]


def _is_fred_csv(rec: dict) -> bool:
    return rec["access"]["kind"] == "http_csv" and "fred.stlouisfed.org/graph/fredgraph.csv" in (
        rec["access"].get("url_template") or "")


def cmd_generate(_args) -> int:
    """Write answers/<id>.json for every curated feed and FRED series that has no file yet."""
    ANSWERS.mkdir(exist_ok=True)
    made = {"feed": 0, "fred": 0}
    kept = failed = 0
    for sid, rec in sorted(_records("curated").items()):
        if rec["access"]["kind"] in ("rss", "atom"):
            shape, build_fn = "feed", feed_answers
        elif _is_fred_csv(rec):
            shape, build_fn = "fred", fred_answers
        else:
            continue
        path = ANSWERS / f"{sid}.json"
        if path.exists():  # a hand-authored (or earlier) file is never overwritten
            kept += 1
            continue
        try:
            url, sample = fetch_sample(rec, cache_hours=24.0)
            answers = build_fn(rec, sample)
        except (Skip, Failed, KeyError, IndexError) as e:
            print(f"FAIL {sid}: {type(e).__name__}: {e}")
            failed += 1
            continue
        problems = answer_problems(answers) + check_sample(answers, sample)
        if problems:
            print(f"FAIL {sid}: {'; '.join(problems)}")
            failed += 1
            continue
        doc = {"source_id": sid, "answers": answers, "sample_url": url, "checked": time.strftime("%Y-%m-%d")}
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        made[shape] += 1
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"generated {made['feed']} feed + {made['fred']} FRED answer files (verified); "
          f"{kept} existing files kept; {failed} failed")
    return 1 if failed else 0
