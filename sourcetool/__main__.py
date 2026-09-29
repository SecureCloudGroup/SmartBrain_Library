"""sourcetool — create, validate, harvest and build the SmartBrain Library.

  python -m sourcetool validate [--only curated|harvested] [--file NAME] [--id ID,ID..]   probe, record results
  python -m sourcetool check                                            schema-check every record (CI)
  python -m sourcetool harvest NAME|all                                 pull candidates from an open catalog
  python -m sourcetool build                                            compile build/library.duckdb (+ term index)
  python -m sourcetool lookup "words"                                   try a lookup against the build
  python -m sourcetool coverage                                         categories x sources report
  python -m sourcetool resolvers [NAME ...]                             harvest resolver tables (all by default)
  python -m sourcetool policies                                         write the source policy onto every subcategory
  python -m sourcetool fills                                            declare how every source parameter is filled
  python -m sourcetool evalres [resolution_asks|resolution_holdout|resolution_sealed]  resolver accuracy
  python -m sourcetool evallookup [lookup_asks|...]                     is the first source offered the right one
  python -m sourcetool ingest [--dry-run] [--no-probe] [--commit]      pull the Library API's votes + suggestions
  python -m sourcetool answers-check [ID ...]                           fetch samples, check answers/ paths (live)
  python -m sourcetool answers-generate                                 write answers for curated feeds + FRED series

Writes only to this checkout. Publishing is a PR the operator merges.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

from .common import SOURCES, read_jsonl, taxonomy, write_jsonl


def _files(only: str | None) -> list[Path]:
    dirs = [SOURCES / only] if only else [SOURCES / "curated", SOURCES / "harvested", SOURCES / "suggested"]
    return sorted(p for d in dirs for p in d.glob("*.jsonl"))


def _check_taxonomy_and_resolvers() -> int:
    """Every subcategory has a policy; every resolver a policy or a fill names has a table."""
    from .resolvers import RES
    bad = 0
    t = taxonomy()
    have = {p.stem for p in RES.glob("*.jsonl")}
    for c in t["categories"]:
        for sc in c["subcategories"]:
            pol = sc.get("policy")
            if not pol:
                print(f"taxonomy: {c['id']}/{sc['id']} has no source policy")
                bad += 1
                continue
            missing = [r for r in pol["resolvers"] if r not in have]
            if missing:  # a named gap (fills mark the params as gaps), not an error: reported, not fatal
                print(f"note: {c['id']}/{sc['id']} policy names resolver tables not harvested yet: {missing}")
    return bad


def cmd_check(_args) -> int:
    from .answers import load_answers, params_of
    from .schema import validate_record
    bad = _check_taxonomy_and_resolvers()
    seen: dict[str, dict] = {}
    for f in _files(None):
        for r in read_jsonl(f):
            errs = validate_record(r)
            if r["tier"] == "curated":
                errs += [f"param {p['name']} has no fill" for p in r["access"].get("params", []) if "fill" not in p]
            if r["id"] in seen:
                errs.append("duplicate id")
            seen[r["id"]] = params_of(r)
            if errs:
                bad += 1
                print(f"{f.name}:{r['id']}: {'; '.join(errs)}")
    for f in _files(None):  # a retired source names a live replacement
        for r in read_jsonl(f):
            to = r.get("replaced_by")
            if to is not None and (to not in seen or to == r["id"]):
                bad += 1
                print(f"{f.name}:{r['id']}: replaced_by {to!r} is not another record")
    answers, errs = load_answers(seen)  # schema only; `answers-check` verifies them against live samples
    for e in errs:
        print(f"answers/{e}")
    bad += len(errs)
    print(f"{len(seen)} records, {len(answers)} with answers, {bad} invalid")
    return 1 if bad else 0


def cmd_validate(args) -> int:
    from .validate import validate_all
    only = args[args.index("--only") + 1] if "--only" in args else None
    file = args[args.index("--file") + 1] if "--file" in args else None
    want = set(args[args.index("--id") + 1].split(",")) if "--id" in args else None
    for f in _files(only):
        if file and f.stem != file:
            continue
        rows = read_jsonl(f)
        todo = [r for r in rows if not want or r["id"] in want]
        if not todo:
            continue  # untouched files are not rewritten
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


def cmd_resolvers(args) -> int:
    from . import resolvers
    for k, v in resolvers.harvest(args or None).items():
        print(f"{k}: {v}")
    return 0


def cmd_policies(_args) -> int:
    from . import policies
    print(f"policies written to {policies.apply()} subcategories")
    return 0


def cmd_fills(_args) -> int:
    import json

    from . import fills
    print(json.dumps(fills.apply(), indent=1))
    return 0


def cmd_evalres(args) -> int:
    from . import evalres
    rc = 0
    for name in args or ["resolution_asks", "resolution_holdout"]:
        print(f"== {name}")
        rc |= evalres.run(name)
    return rc


def cmd_evallookup(args) -> int:
    from . import evallookup
    rc = 0
    for name in args or ["lookup_asks"]:
        rc |= evallookup.run(name)
    return rc


def cmd_ingest(args) -> int:
    from . import ingest
    return ingest.main(args)


def cmd_answers_check(args) -> int:
    from . import answers
    return answers.cmd_check(args)


def cmd_answers_generate(args) -> int:
    from . import answers
    return answers.cmd_generate(args)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"check", "validate", "harvest", "build", "lookup", "coverage", "resolvers",
                                   "policies", "fills", "evalres", "evallookup", "ingest", "answers-check",
                                   "answers-generate"}:
        print(__doc__)
        return 2
    return globals()["cmd_" + argv[0].replace("-", "_")](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
