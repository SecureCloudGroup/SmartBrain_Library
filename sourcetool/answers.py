"""Source `answers`: which paths of a source's response answer which questions (spec v1.1).

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
from email.utils import parsedate_tz
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .common import ROOT, SOURCES, Refused, decode_body, get, probe_headers, read_jsonl, taxonomy

ANSWERS = ROOT / "answers"
# the SmartBrain app checkout beside this one (its parsers give samples the shape the app sees)
APP = Path(os.environ.get("SMARTBRAIN_APP") or ROOT.parent / "SmartBrain_3000" / "app")
AUTHORING_UA = "SmartBrain-Library-authoring/1 info@securecloudgroup.com"

KINDS = ("value", "list", "columns")
VALUE_TYPES = ("number", "text", "time", "date", "count")
ROW_TYPES = ("number", "text", "time", "date")
CODES = ("wmo_weather",)
# v1.2: what a value delivers. `window` is the stretch of time it is about ("latest" = the newest published
# reading, not tied to the clock); `measure` is the quantity it reports. Both closed (docs/SCHEMA.md).
WINDOWS = ("now", "today", "tonight", "tomorrow", "latest")
MEASURES = ("temperature", "feels_like", "precip_chance", "precip_amount", "conditions", "thunderstorm", "snow",
            "wind", "humidity", "waves", "swell", "wave_direction", "water_temp", "alerts", "kp", "uv",
            "air_quality", "tide", "sunrise", "sunset")
AXIS_STEPS = ("day", "hour", "period")  # rows indexed by local date/time: a day, an hour, a named period
# taxonomy `expects`: the components a complete answer for a subcategory holds (the measures, plus what a
# score, a schedule, a table or an observation carries), so a card that lacks one says so
COMPONENTS = MEASURES + ("observed_time", "wave_period", "score", "opponent", "start_time", "venue", "rank",
                         "record")
COMMON = {"name", "label", "words", "primary", "kind"}
KEYS = {"value": COMMON | {"path", "type", "unit", "unit_path", "codes", "utc", "window", "measure", "tbd_if"},
        "list": COMMON | {"path", "row", "newest_first", "may_be_empty", "filter", "axis"},
        "columns": COMMON | {"columns", "limit", "axis"}}
ROW_KEYS = {"path", "label", "type", "unit", "unit_path", "utc", "tbd_if"}  # utc: zoneless times are UTC
COLUMN_KEYS = (ROW_KEYS - {"tbd_if"}) | {"codes"}
FILE_KEYS = {"source_id", "answers", "sample_url", "checked"}
MAX_ANSWERS = 12

_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
# a segment is a key or a whole `{param}` (filled from the record's params), optionally indexed: rates.{quote}
# data[0][3] (a list of lists); description["#cdata-section"] (a key the plain names can't spell, as the app
# reads it: quoted, no dot, quote or control character inside)
_SUB_SRC = r'(?:\[\d+\]|\["[^".\x00-\x1f]{1,200}"\])'
_SEG_SRC = rf"(?:[A-Za-z_][\w-]*|\{{[a-z_][a-z0-9_]*\}}){_SUB_SRC}*"
_PATH = re.compile(rf"{_SEG_SRC}(?:\.{_SEG_SRC})*", re.ASCII)
_SEG = re.compile(rf"(?:([A-Za-z_][\w-]*)|\{{([a-z_][a-z0-9_]*)\}})({_SUB_SRC}*)", re.ASCII)
_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# a date-only value: YYYY-MM-DD, or a MIDNIGHT timestamp whose day is the value ("2026-04-23T00:00:00.000Z";
# the app shows its day as written). A timestamp with a real clock time is a time, never a date.
_DATE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
# RFC 2822, as RSS publishes dates: "Tue, 29 Sep 2026 01:00:00 GMT" (weekday optional, seconds optional)
_RFC2822 = re.compile(r"(?:[A-Za-z]{3}, *)?\d{1,2} [A-Za-z]{3} \d{2,4} \d{2}:\d{2}(?::\d{2})?(?: +\S+)?")


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
    return errs + _unit_problems(where, f) + _utc_problems(where, f) + _tbd_problems(where, f)


def _tbd_problems(where: str, f: dict) -> list[str]:
    """`tbd_if: {path, equals}` says a time is a placeholder when `path` equals `equals` (MLB's
    status.startTimeTBD true): the card shows the date and "time TBD". Time only."""
    if "tbd_if" not in f:
        return []
    t = f["tbd_if"]
    if f.get("type") != "time":
        return [f"{where}: tbd_if is on a time only"]
    if not isinstance(t, dict) or set(t) != {"path", "equals"}:
        return [f"{where}: tbd_if must be {{path, equals}}"]
    errs = [] if _path_ok(t["path"]) else [f"{where}: bad tbd_if path {t['path']!r}"]
    if not (t["equals"] is True or (isinstance(t["equals"], str) and _FILTER_LITERAL.fullmatch(t["equals"]))):
        errs.append(f"{where}: tbd_if equals must be true or a short fixed value")
    return errs


def _axis_problems(w: str, a: dict) -> list[str]:
    """`axis: {cell, step}` says the rows are indexed by local date/time, so a card can cut them to a window.
    `cell` is one of the answer's own date/time fields (a column path, or a row-relative list path)."""
    if "axis" not in a:
        return []
    ax = a["axis"]
    if not isinstance(ax, dict) or set(ax) != {"cell", "step"}:
        return [f"{w}: axis must be {{cell, step}}"]
    errs = [] if ax["step"] in AXIS_STEPS else [f"{w}: axis step must be {'/'.join(AXIS_STEPS)}"]
    fields = a.get("row") if a.get("kind") == "list" else a.get("columns")
    cells = {f.get("path") for f in fields or [] if isinstance(f, dict) and f.get("type") in ("time", "date")}
    if ax["cell"] not in cells:
        errs.append(f"{w}: axis cell must be one of its date/time fields {sorted(c for c in cells if c)}")
    return errs


def _param_problems(w: str, a: dict, params) -> list[str]:
    """Every `{param}` an answer names (in any path or filter) is one of the record's access.params."""
    found = [a.get("path"), a.get("unit_path")]
    for f in (a.get("row") or []) + (a.get("columns") or []) + [a.get("filter"), a.get("tbd_if")]:
        if isinstance(f, dict):
            found += [f.get("path"), f.get("unit_path"), f.get("equals")]
            if isinstance(f.get("tbd_if"), dict):
                found.append(f["tbd_if"].get("path"))
    names = {n for x in found if isinstance(x, str) for n in _PARAM.findall(x)}
    return [f"{w}: {{{n}}} is not a parameter of this source" for n in sorted(names - set(params))]


# a fixed filter value: a status word the response uses ("Final"), never a user's value
_FILTER_LITERAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,39}")


def _filter_problems(w: str, flt) -> list[str]:
    if not isinstance(flt, dict) or set(flt) != {"path", "equals"}:
        return [f"{w}: filter must be {{path, equals}}"]
    errs = [] if _path_ok(flt["path"]) else [f"{w}: bad filter path {flt['path']!r}"]
    if not isinstance(flt["equals"], str) or not (_PARAM.fullmatch(flt["equals"])
                                                  or _FILTER_LITERAL.fullmatch(flt["equals"])):
        errs.append(f"{w}: filter equals must be a {{param}} or a short fixed value")
    return errs


def _utc_problems(where: str, f: dict) -> list[str]:
    """`utc: true` says a time's zoneless values are UTC (TheSportsDB strTimestamp); time only."""
    if "utc" not in f:
        return []
    if not isinstance(f["utc"], bool) or f.get("type") != "time":
        return [f"{where}: utc must be true or false, on a time only"]
    return []


def answer_problems(answers, params=()) -> list[str]:
    """Problems with a source's `answers` list; an empty list means it is well-formed. `params` are the
    names of the record's access.params (the only names a `{param}` path segment or filter may use)."""
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
            errs += _unit_problems(w, a) + _utc_problems(w, a) + _tbd_problems(w, a)
            if "window" in a and a["window"] not in WINDOWS:
                errs.append(f"{w}: window must be one of {'/'.join(WINDOWS)}")
            if "measure" in a and a["measure"] not in MEASURES:
                errs.append(f"{w}: measure must be one of {'/'.join(MEASURES)}")
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
            if "filter" in a:
                errs += _filter_problems(w, a["filter"])
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
        if kind != "value":
            errs += _axis_problems(w, a)
        errs += _param_problems(w, a, params)
    rows_primary = primary["list"] + primary["columns"]
    if rows_primary and primary["value"]:
        errs.append("primary answers are either 1-4 values or exactly 1 list/columns, not both")
    elif rows_primary > 1:
        errs.append("at most one list/columns answer may be primary")
    elif primary["value"] > 4:
        errs.append("at most 4 value answers may be primary")
    return errs


# --- lints: a record's promises agree with its answers (no network) ---------------------------------

def _dated(a: dict) -> bool:
    """A list/columns answer whose rows carry a date or time."""
    fields = a.get("row") if a.get("kind") == "list" else a.get("columns")
    return any(isinstance(f, dict) and f.get("type") in ("time", "date") for f in fields or [])


def _rows(a: dict) -> bool:
    return a.get("kind") in ("list", "columns")


# which answer shapes serve each question kind a record declares; a kind missing here (lookup, map, image,
# text_brief, compare) promises no particular shape
KIND_SERVED_BY = {
    "current_value": lambda a: a.get("kind") == "value",
    "forecast": lambda a: a.get("window") in ("today", "tonight", "tomorrow") or (_rows(a) and _dated(a)),
    "next_event": lambda a: a.get("type") in ("time", "date") or (_rows(a) and _dated(a)),
    "schedule": lambda a: _rows(a) and _dated(a),
    "result": lambda a: a.get("type") in ("number", "text") or _rows(a),
    "latest_items": _rows,
    "ranking": _rows,
    "trend": lambda a: _rows(a) and _dated(a),
    "alerts": lambda a: a.get("kind") == "list",
    "status": lambda a: a.get("type") == "text" or a.get("kind") == "list",
    "count": lambda a: a.get("type") == "count" or a.get("kind") == "list",
}


def answer_lints(rec: dict) -> list[str]:
    """Problems between a record and its answers: every declared kind is served by some answer (a
    next-game-only source never claims `schedule`), and a count never answers an existence question
    ("any hurricanes?" wants the list, which may be empty — not a bare number)."""
    answers = [a for a in rec.get("answers") or [] if isinstance(a, dict)]
    if not answers:
        return []
    out = [f"{rec['id']}: kind {k} has no answer that serves it" for k in rec.get("kinds") or []
           if k in KIND_SERVED_BY and not any(KIND_SERVED_BY[k](a) for a in answers)]
    for a in answers:
        if a.get("type") == "count":
            out += [f"{rec['id']}: count answer {a.get('name')} claims {w!r} (an existence question takes the list)"
                    for w in a.get("words") or [] if w == "any" or w.startswith("any ")]
    return out


def _time_cells(a: dict):
    """Every time field of one answer, as (path, utc_declared) — None for utc when not set."""
    out: list[tuple[str, bool | None]] = []
    if a.get("kind") == "value" and a.get("type") == "time":
        out.append((str(a.get("path") or ""), a.get("utc") if "utc" in a else None))
    for f in (a.get("row") or []) + (a.get("columns") or []):
        if isinstance(f, dict) and f.get("type") == "time":
            out.append((str(f.get("path") or ""), f.get("utc") if "utc" in f else None))
    return out


def utc_consistency_lints(recs: list[dict]) -> list[str]:
    """F13 (2026-10-04): answers from the SAME provider reading the SAME time path must agree on
    `utc` — the field found that swpc-kp's time cells lacked `utc: true` while sibling
    swpc-kp-forecast declared it, so the engine's window transform treated zoneless UTC times
    as local wall and dropped tonight's rows. The lint catches that whole class."""
    groups: dict[tuple[str, str], dict] = {}
    for r in recs:
        provider = str(((r or {}).get("provider") or {}).get("name") or "")
        for a in (r.get("answers") or []):
            if not isinstance(a, dict):
                continue
            for path, utc in _time_cells(a):
                key = (provider, path)
                groups.setdefault(key, {}).setdefault(utc, []).append(r["id"])
    out: list[str] = []
    for (provider, path), by_flag in groups.items():
        if len(by_flag) < 2 or not provider:  # one declaration (or no provider) — nothing to disagree on
            continue
        if True in by_flag and (False in by_flag or None in by_flag):
            disagree = sorted({sid for ids in by_flag.values() for sid in ids})
            out.append(f"{provider}: time path {path!r} disagrees on utc across {disagree}")
    return out


def _fold(text: str) -> str:
    """Whole lowercase tokens, plurals folded ("thunderstorms" -> "thunderstorm")."""
    toks = re.findall(r"[a-z0-9.+]+", (text or "").lower())
    return " " + " ".join(t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks) + " "


def own_words(rec: dict) -> str:
    """What a source says it is about in ITS OWN words: name, description, examples and declared answers."""
    parts = [rec.get("name", ""), rec.get("description", ""), " ".join(rec.get("examples") or [])]
    for a in rec.get("answers") or []:
        if isinstance(a, dict):
            parts += [str(a.get("label", "")), " ".join(str(w) for w in a.get("words") or [])]
    return _fold(" ".join(parts))


def keyword_lints(recs: list[dict], tax: dict | None = None) -> list[str]:
    """Each subcategory keyword is spoken to by at least one curated source filed there, so a keyword never
    classifies an ask into a subcategory whose sources can't answer it. Subcategories with no curated
    source are a coverage gap (reported by `coverage`), not a lint."""
    tax = tax or taxonomy()
    filed: dict[str, list[str]] = {}
    for r in recs:
        if r.get("tier") == "curated" and not r.get("replaced_by"):
            for c in r.get("categories") or []:
                filed.setdefault(c, []).append(own_words(r))
    out = []
    for c in tax["categories"]:
        for sc in c["subcategories"]:
            cid = f"{c['id']}/{sc['id']}"
            if cid in filed:
                out += [f"{cid}: no curated source filed here speaks to {kw!r}" for kw in sc["keywords"]
                        if not any(_fold(kw) in text for text in filed[cid])]
    return out


# --- the check against a real response -------------------------------------------------------------

_MISSING = object()


def resolve(data, path: str, examples: dict | None = None):
    """Walk `a.b[0].c` through dicts and list indexes, a `{param}` segment standing for its example
    value; _MISSING when any step is absent."""
    cur = data
    for seg in path.split("."):
        m = _SEG.fullmatch(seg)
        if not m:
            return _MISSING
        key = m.group(1) if m.group(1) else (examples or {}).get(m.group(2))
        if key is None or not isinstance(cur, dict) or str(key) not in cur:
            return _MISSING
        cur = cur[str(key)]
        for idx, quoted in re.findall(r'\[(\d+)\]|\["([^"]*)"\]', m.group(3) or ""):  # every subscript, in order
            if quoted:
                if not isinstance(cur, dict) or quoted not in cur:
                    return _MISSING
                cur = cur[quoted]
                continue
            n = int(idx)
            if not isinstance(cur, list) or n >= len(cur):
                return _MISSING
            cur = cur[n]
    return cur


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float)) or (isinstance(v, str) and bool(_NUMBER.fullmatch(v)))


def type_ok(v, typ: str) -> bool:
    """Does value `v` hold what `typ` promises (spec v1.1, "The check")."""
    if typ == "number":
        return _is_number(v)
    if typ == "text":
        return isinstance(v, str) and bool(v.strip())
    if typ == "time":
        if isinstance(v, str) and (_ISO_TIME.match(v) or (_RFC2822.fullmatch(v.strip())
                                                          and parsedate_tz(v) is not None)):
            return True
        return _is_number(v) and float(v) > 1e8
    if typ == "date":  # a calendar date, or a midnight-style timestamp whose DAY is the value (as the app reads it)
        return isinstance(v, str) and bool(_DATE_VALUE.fullmatch(v))
    if typ == "count":
        return isinstance(v, list)
    return False


def _show(v) -> str:
    s = json.dumps(v, ensure_ascii=False) if v is not _MISSING else "missing"
    return s if len(s) <= 60 else s[:57] + "..."


def _check_field(where: str, item, root, f: dict, ex: dict) -> list[str]:
    errs = []
    v = resolve(item, f["path"], ex)
    if not type_ok(v, f["type"]):
        errs.append(f"{where}: {f['path']} is {_show(v)}, not {f['type']}")
    if "unit_path" in f and not isinstance(resolve(root, f["unit_path"], ex), str):
        errs.append(f"{where}: unit_path {f['unit_path']} is {_show(resolve(root, f['unit_path'], ex))}, not text")
    if f.get("type") == "time":
        errs += _tbd_check(where, item, f, ex)
    return errs


_TBD_KEY = re.compile(r"tb[da]", re.I)  # startTimeTBD, timeTBA, isTBD
_TBD_VALUE = re.compile(r"\s*(tbd|tba|to be (determined|announced))\s*", re.I)


def _tbd_flag(node, depth: int = 3) -> str:
    """The first key or value near a time that says the time may be a placeholder, or ""."""
    if isinstance(node, dict):
        for k, v in node.items():
            if _TBD_KEY.search(str(k)):
                return str(k)
            if depth and (found := _tbd_flag(v, depth - 1)):
                return found
    elif isinstance(node, str) and _TBD_VALUE.fullmatch(node):
        return node
    return ""


def _parent(item, path: str, ex: dict):
    """The object that holds the last segment of `path` (where a time's TBD flag lives beside it)."""
    head = path.rsplit(".", 1)[0] if "." in path else ""
    return resolve(item, head, ex) if head else item


def _tbd_check(where: str, item, f: dict, ex: dict) -> list[str]:
    """A declared tbd_if resolves; an undeclared one is a problem when a TBD/TBA flag sits beside the time."""
    if "tbd_if" in f:
        v = resolve(item, f["tbd_if"]["path"], ex)
        return [] if v is not _MISSING and not isinstance(v, (dict, list)) else \
            [f"{where}: tbd_if path {f['tbd_if']['path']} is {_show(v)}, not a flag"]
    flag = _tbd_flag(_parent(item, f["path"], ex))
    return [f"{where}: a TBD/TBA flag ({flag}) sits beside {f['path']}; declare tbd_if"] if flag else []


def _iso(v) -> bool:
    return isinstance(v, str) and bool(_ISO_TIME.match(v) or _DATE_VALUE.fullmatch(v) or _DATE.fullmatch(v))


def _axis_check(w: str, cells: list) -> list[str]:
    """Every row's axis cell is an ISO date or date-time (what a window cut reads), not only the first."""
    bad = next((c for c in cells if not _iso(c)), _MISSING)
    return [] if bad is _MISSING else [f"{w}: axis cell {_show(bad)} is not an ISO date/time"]


_MAX_CHECKED_ROWS = 500  # the engine's series cap: every row a card could show is typed (spec v1.3, 2026-10-05)
# a cell a row lacks: the app's sparse-row rule (ni_flow._MISSING) shows "—" for these, never a wrong value
_SPARSE_TEXT = frozenset({"", "mm", "n/a", "na", "-", "--", "—", "null", "none", "missing"})


def _sparse(v) -> bool:
    return v is _MISSING or v is None or (isinstance(v, str) and v.strip().lower() in _SPARSE_TEXT)


def _check_rows(w: str, items: list, row: list, ex: dict) -> list[str]:
    """Every row after the first (spec v1.3): a cell that is PRESENT must hold its declared type; a cell
    the row lacks is allowed (the card shows "—" for it). The caller checks the first row in full. One
    message per field, naming the first offending row; bounded by ``_MAX_CHECKED_ROWS``."""
    errs: list[str] = []
    for j, f in enumerate(row):
        for i, it in enumerate(items[1:_MAX_CHECKED_ROWS], start=1):  # rows 1..499: the first 500 rows with row 0
            v = resolve(it, f["path"], ex)
            if _sparse(v):
                continue
            if not type_ok(v, f["type"]):
                errs.append(f"{w} row[{j}] at item {i}: {f['path']} is {_show(v)}, not {f['type']}")
                break
    return errs


def _check_column(w: str, col: list, f: dict) -> list[str]:
    """Every element of a column (spec v1.3): a present value holds the declared type; nulls are allowed."""
    for i, v in enumerate(col[:_MAX_CHECKED_ROWS]):
        if _sparse(v):
            continue
        if not type_ok(v, f["type"]):
            return [f"{w} at index {i}: {f['path']} is {_show(v)}, not {f['type']}"]
    return []


def check_sample(answers: list, sample, examples: dict | None = None) -> list[str]:
    """Every path resolves in the sample with the type its answer promises. `examples` maps each of the
    record's params to its `example` (what a `{param}` segment or a filter stands for in the sample).
    unit_path always resolves from the response root (units are response metadata, e.g.
    `daily_units.temperature_2m_max`)."""
    ex = examples or {}
    if isinstance(sample, list):  # the app wraps a top-level list as {"items": [...]} before any path runs
        sample = {"items": sample}
    errs: list[str] = []
    for a in answers:
        w = f"answer {a['name']}"
        if a["kind"] == "value":
            errs += _check_field(w, sample, sample, a, ex)
        elif a["kind"] == "list":
            items = resolve(sample, a["path"], ex)
            if isinstance(items, list) and "filter" in a:
                param = _PARAM.fullmatch(a["filter"]["equals"])
                want = ex.get(param.group(1)) if param else a["filter"]["equals"]
                if want is None:
                    errs.append(f"{w}: filter {a['filter']['equals']} has no example value")
                    continue
                items = [it for it in items if (v := resolve(it, a["filter"]["path"], ex)) is not _MISSING
                         and str(v) == str(want)]
            if not isinstance(items, list):
                errs.append(f"{w}: {a['path']} is {_show(items)}, not a list")
            elif not items:
                if not a.get("may_be_empty"):
                    errs.append(f"{w}: {a['path']} is empty{' after the filter' if 'filter' in a else ''} "
                                "(set may_be_empty when that is a real answer)")
            else:
                for j, f in enumerate(a["row"]):
                    errs += _check_field(f"{w} row[{j}]", items[0], sample, f, ex)
                errs += _check_rows(w, items, a["row"], ex)
                if "axis" in a:
                    errs += _axis_check(w, [resolve(it, a["axis"]["cell"], ex) for it in items])
        else:
            lengths = set()
            for j, f in enumerate(a["columns"]):
                col = resolve(sample, f["path"], ex)
                if not isinstance(col, list):
                    errs.append(f"{w} columns[{j}]: {f['path']} is {_show(col)}, not a list")
                    continue
                lengths.add(len(col))
                errs += _check_column(f"{w} columns[{j}]", col, f)
                if "unit_path" in f and not isinstance(resolve(sample, f["unit_path"], ex), str):
                    errs.append(f"{w} columns[{j}]: unit_path {f['unit_path']} is not text")
            if len(lengths) > 1:
                errs.append(f"{w}: columns have different lengths {sorted(lengths)}")
            elif lengths == {0}:
                errs.append(f"{w}: columns are empty")
            if "axis" in a and isinstance(col := resolve(sample, a["axis"]["cell"], ex), list):
                errs += _axis_check(w, col)
    return errs


# --- authoring files -------------------------------------------------------------------------------

def params_of(rec: dict) -> dict:
    """{param name: example} for a record's access.params."""
    return {q["name"]: q.get("example") for q in rec["access"].get("params", [])}


def load_answers(params_by_id: dict[str, dict], directory: Path = ANSWERS) -> tuple[dict[str, list], list[str]]:
    """Read every answers/<id>.json: ({source_id: answers}, problems). `params_by_id` maps each record id
    to its params (`params_of`). A file whose id has no record, or whose answers fail the schema, is a
    problem (build refuses it)."""
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
        if sid not in params_by_id:
            errs.append(f"{f.name}: no record has id {sid!r}")
            continue
        bad = answer_problems(d["answers"], params_by_id[sid])
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
        status, body, ctype = get(url, cache_hours=cache_hours, headers=probe_headers(kind, headers),
                                  api=kind in _API_KINDS and bool(rec["access"].get("docs_url")))
    except Refused:
        raise Failed("robots.txt disallows") from None
    except Exception as e:  # network failure is a result, not a crash
        raise Failed(f"fetch failed: {type(e).__name__}") from None
    if status != 200:
        raise Failed(f"HTTP {status}")
    text = decode_body(body, ctype)  # as the app decodes it: BOM, then the declared charset, then UTF-8
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


def lint_report(recs: list[dict], tax: dict | None = None) -> list[str]:
    """Every lint over records that carry their answers (no network): each record's promises against its
    answers (answer_lints), each subcategory keyword against the curated sources filed there, and utc
    consistency across answers of the same provider reading the same time path (F13 2026-10-04)."""
    out = [p for r in recs for p in answer_lints(r)]
    out += utc_consistency_lints(recs)
    return out + keyword_lints(recs, tax)


def exempt_key(lint: str) -> str:
    """The key a kind lint is exempted under in lint_exempt.json: ``"<id>: kind <k>"`` — the lint message up to
    its " has no answer" tail. One place for the rule, used by the gate and the test suite alike."""
    assert isinstance(lint, str), "lint message required"
    return lint.split(" has no")[0]


KEYWORD_LINT_BASELINE = ROOT / "tests" / "fixtures" / "keyword_lint_baseline.json"
LINT_EXEMPT = ROOT / "tests" / "fixtures" / "records" / "lint_exempt.json"


def cmd_lint() -> int:
    """`answers-check --lint` (offline). Gating since Phase 0 (2026-10-05): kind lints (minus the reasoned
    exemptions in lint_exempt.json, the same list the test suite applies) and utc lints fail; a keyword lint
    fails only when it is NEW against the committed baseline — a ratchet, so the backlog never grows and
    shrinks as sources are filed under their keywords."""
    recs = _records()
    loaded, errs = load_answers({k: params_of(r) for k, r in recs.items()})
    merged = [{**r, "answers": loaded[k]} if k in loaded else r for k, r in recs.items() if not r.get("replaced_by")]
    exempt = json.loads(LINT_EXEMPT.read_text()) if LINT_EXEMPT.is_file() else {}
    hard = [p for r in merged for p in answer_lints(r) if exempt_key(p) not in exempt]
    hard += utc_consistency_lints(merged)
    keyword = keyword_lints(merged)
    baseline = set(json.loads(KEYWORD_LINT_BASELINE.read_text())) if KEYWORD_LINT_BASELINE.is_file() else set()
    new_kw = [p for p in keyword if p not in baseline]
    gone = sorted(baseline - set(keyword))
    problems = [f"INVALID {e}" for e in errs] + [f"LINT {p}" for p in hard] + [f"LINT NEW {p}" for p in new_kw]
    for p in problems:
        print(p)
    if gone:
        print(f"{len(gone)} baseline keyword lints no longer fire; drop them from {KEYWORD_LINT_BASELINE.name}")
    print(f"{len(merged)} records, {len(loaded)} with answers: {len(problems)} lint problems "
          f"({len(keyword) - len(new_kw)} keyword lints held by the baseline)")
    return 1 if problems else 0


def cmd_check(ids: list[str]) -> int:
    if "--lint" in ids:  # offline: the lints only, no sample is fetched
        return cmd_lint()
    recs = _records()
    loaded, errs = load_answers({k: params_of(r) for k, r in recs.items()})
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
        problems = check_sample(loaded[sid], sample, params_of(recs[sid]))
        if problems:
            bad += 1
            print(f"FAIL {sid}: {'; '.join(problems)}")
        else:
            print(f"PASS {sid}: {len(loaded[sid])} answers")
    print(f"{len(todo)} checked, {bad} failing")
    return 1 if bad else 0


# --- code-generated answers (no model) -------------------------------------------------------------

# What each curated FRED series is called on a card and the unit it is in, from the series' own FRED page
# (fred.stlouisfed.org/series/<ID>, "Units:"), keyed by series id (plus the transformation when the record
# asks FRED for one: pc1 = percent change from a year ago). A plain index level ("Index") carries no unit; a
# series missing here is not generated (a card never reads "Latest | 4.1").
FRED_SERIES = {
    "A191RL1Q225SBEA": ("Real GDP growth (annual rate)", "%"),
    "APU0000702111": ("White bread price (per pound)", "$/lb"),
    "APU0000708111": ("Egg price (a dozen, grade A large)", "$/dozen"),
    "APU0000709112": ("Whole milk price (per gallon)", "$/gal"),
    "APU0000717311": ("Ground coffee price (per pound)", "$/lb"),
    "BOPGSTB": ("Trade balance (goods and services)", "million USD"),
    "CPIAUCSL": ("Consumer Price Index (all items)", "index 1982-84=100"),
    "CPILFESL/pc1": ("Core inflation (year over year)", "%"),
    "CSUSHPINSA": ("Case-Shiller home price index", "index Jan 2000=100"),
    "DCOILWTICO": ("WTI crude oil price", "$/barrel"),
    "DEXUSEU": ("One euro in US dollars", "USD"),
    "DGS10": ("10-year Treasury yield", "%"),
    "DGS2": ("2-year Treasury yield", "%"),
    "DHHNGSP": ("Henry Hub natural gas price", "$/MMBtu"),
    "DJIA": ("Dow Jones Industrial Average (close)", None),
    "FEDFUNDS": ("Federal funds rate", "%"),
    "GASREGW": ("Regular gas price (US average)", "$/gal"),
    "GDP": ("GDP (annual rate)", "billion USD"),
    "GFDEBTN": ("Total federal debt", "million USD"),
    "HOUST": ("Housing starts (annual rate)", "thousand units"),
    "ICSA": ("Initial jobless claims (weekly)", "claims"),
    "MORTGAGE30US": ("30-year mortgage rate", "%"),
    "MSPUS": ("Median price of new houses sold", "USD"),
    "NASDAQCOM": ("Nasdaq Composite (close)", None),
    "PAYEMS": ("Nonfarm payroll jobs", "thousand jobs"),
    "PCEPI/pc1": ("PCE inflation (year over year)", "%"),
    "SP500": ("S&P 500 (close)", None),
    "T10Y2Y": ("10-year minus 2-year Treasury yield", "%"),
    "UMCSENT": ("Consumer sentiment", "index 1966 Q1=100"),
    "UNRATE": ("Unemployment rate", "%"),
    "VIXCLS": ("VIX volatility index (close)", None),
}

# what a feed's items are, by the record's first subcategory (a news feed's items are headlines)
FEED_LABELS = {"news/fact_checks": "Fact checks", "news/press_releases": "Press releases",
               "science/papers": "Latest papers", "tech/repos_releases": "Releases",
               "culture/reference_daily": "Word of the day", "hazards/tropical_storms": "Latest advisories",
               "hazards/tsunami": "Tsunami messages", "markets/filings": "Latest filings",
               "shopping/deals": "Deals", "travel/parks_travel": "Travel advisories"}


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
    label = FEED_LABELS.get(rec["categories"][0], "Headlines")
    return [{"name": "latest", "label": label, "kind": "list", "primary": True, "words": words,
             "path": "items", "row": row, "may_be_empty": False}]


def fred_answers(rec: dict, sample: dict) -> list[dict]:
    date_col, value_col = sample["columns"][0], sample["columns"][1]
    q = parse_qs(urlsplit(rec["access"]["url_template"]).query)
    series = q.get("id", [""])[0] + "".join(f"/{t}" for t in q.get("transformation", []))
    label, unit = FRED_SERIES[series]  # KeyError: name the series in FRED_SERIES first
    unit = {"unit": unit} if unit else {}
    name = re.sub(r"\s*\(FRED\)\s*$", "", rec["name"])
    return [
        {"name": "latest", "label": label, "kind": "value", "primary": True,
         "words": _words([name, label], rec.get("examples", []), ["latest", "current", "now", "today"]),
         "path": f"rows[0].{value_col}", "type": "number", **unit},
        {"name": "as_of", "label": "As of", "kind": "value",
         "words": ["as of", "when", "date", "last updated", "updated"],
         "path": f"rows[0].{date_col}", "type": "date"},
        {"name": "recent", "label": "Recent readings", "kind": "list",
         "words": ["recent", "history", "trend", "past", "over time", "chart", "last few"],
         "path": "rows", "row": [{"path": date_col, "label": "Date", "type": "date"},
                                 {"path": value_col, "label": label, "type": "number", **unit}]},
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
        problems = answer_problems(answers, params_of(rec)) + check_sample(answers, sample, params_of(rec))
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
