"""sourcetool — create, validate, harvest and build the SmartBrain Library.

  python -m sourcetool validate [--only curated|harvested] [--file NAME] [--id ID]   probe, record results
  python -m sourcetool check                                            schema-check every record (CI)
  python -m sourcetool harvest NAME|all                                 pull candidates from an open catalog
  python -m sourcetool build                                            compile build/library.duckdb (+ term index)
  python -m sourcetool lookup "words"                                   try a lookup against the build
  python -m sourcetool coverage                                         categories x sources report

Writes only to this checkout. Publishing is a PR the operator merges.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

from .common import SOURCES, read_jsonl, taxonomy, write_jsonl


def _files(only: str | None) -> list[Path]:
    dirs = [SOURCES / only] if only else [SOURCES / "curated", SOURCES / "harvested"]
    return sorted(p for d in dirs for p in d.glob("*.jsonl"))


def cmd_check(_args) -> int:
    from .schema import validate_record
    bad = 0
    seen: set[str] = set()
    for f in _files(None):
        for r in read_jsonl(f):
            errs = validate_record(r)
            if r["id"] in seen:
                errs.append("duplicate id")
            seen.add(r["id"])
            if errs:
                bad += 1
                print(f"{f.name}:{r['id']}: {'; '.join(errs)}")
    print(f"{len(seen)} records, {bad} invalid")
    return 1 if bad else 0


def cmd_validate(args) -> int:
    from .validate import validate_all
    only = args[args.index("--only") + 1] if "--only" in args else None
    file = args[args.index("--file") + 1] if "--file" in args else None
    want = args[args.index("--id") + 1] if "--id" in args else None
    for f in _files(only):
        if file and f.stem != file:
            continue
        rows = read_jsonl(f)
        todo = [r for r in rows if not want or r["id"] == want]
        validate_all(todo)
        write_jsonl(f, rows)
        c = collections.Counter(r["validation"]["status"] for r in todo)
        print(f"{f.relative_to(SOURCES)}: {dict(c)}")
    return 0


def cmd_harvest(args) -> int:
    from . import harvest
    names = harvest.HARVESTERS if args[0] == "all" else [args[0]]
    for n in names:
        rows = harvest.run(n)
        print(f"harvest {n}: {len(rows)} sources")
    return 0


def cmd_build(_args) -> int:
    from .build import build
    print(build())
    return 0


def cmd_lookup(args) -> int:
    from .build import lookup
    for r in lookup(" ".join(args)):
        print(f"{r['score']:.2f}  {r['id']:<40} {r['name']}  [{r['categories']}]  {r['tier']}/{r['validation']}")
    return 0


def cmd_coverage(_args) -> int:
    t = taxonomy()
    rows = [r for f in _files(None) for r in read_jsonl(f)]
    per = collections.Counter(c for r in rows if r.get("validation", {}).get("status") != "failed" for c in r["categories"])
    empty = []
    for c in t["categories"]:
        for s in c["subcategories"]:
            cid = f"{c['id']}/{s['id']}"
            print(f"{per.get(cid, 0):>6}  {cid}")
            if not per.get(cid):
                empty.append(cid)
    print(f"\n{len(empty)} empty subcategories: {', '.join(empty)}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"check", "validate", "harvest", "build", "lookup", "coverage"}:
        print(__doc__)
        return 2
    return globals()["cmd_" + argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
