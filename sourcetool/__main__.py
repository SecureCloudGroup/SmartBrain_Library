"""sourcetool — create, validate, harvest and build the SmartBrain Library.

  python -m sourcetool validate [--only curated|harvested] [--file NAME] [--id ID,ID..] [--samples N]
                                                                        probe as the app does, record results
                                                                        (+ N random resolver readings; default 2)
  python -m sourcetool check                                            schema-check every record (CI)
  python -m sourcetool harvest NAME|all                                 pull candidates from an open catalog
  python -m sourcetool build                                            compile build/library.duckdb (+ term index)
  python -m sourcetool lookup "words"                                   try a lookup against the build
  python -m sourcetool coverage                                         categories x sources report
  python -m sourcetool resolvers [--refine] [NAME ...]                  harvest resolver tables (all by default;
                                                                        --refine: today's rules over the table)
  python -m sourcetool policies                                         write the source policy onto every subcategory
  python -m sourcetool fills                                            declare how every source parameter is filled
  python -m sourcetool evalres [resolution_asks|resolution_holdout|resolution_sealed]  resolver accuracy
  python -m sourcetool evallookup [lookup_asks|...]                     is the first source offered the right one
  python -m sourcetool ingest [--dry-run] [--no-probe] [--commit]      pull the Library API's votes + suggestions
  python -m sourcetool answers-check [ID ...]                           fetch samples, check answers/ paths (live)
  python -m sourcetool answers-check --lint                             record + keyword lints (offline, no fetch)
  python -m sourcetool answers-generate                                 write answers for curated feeds + FRED series
  python -m sourcetool asks-overlap --against FILE [FILE ...]           report asks that overlap any labeled set
                                                                        (exact + jaccard>=0.75); exit nonzero if any

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
    """The taxonomy is well-formed (schema.taxonomy_problems), every subcategory has a policy, and every
    resolver a policy names has a table (a missing one is a noted gap)."""
    from .resolvers import RES
    from .schema import taxonomy_problems
    t = taxonomy()
    problems = taxonomy_problems(t)  # the closed shape: keys, kinds, params, keywords, expects, policy.measure
    for p in problems:
        print(f"taxonomy: {p}")
    bad = len(problems)
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
    from .schema import load_asks, resolver_host_value_problems, validate_record
    bad = _check_taxonomy_and_resolvers()
    seen: dict[str, dict] = {}
    records: list[dict] = []
    for f in _files(None):
        for r in read_jsonl(f):
            records.append(r)
            errs = validate_record(r)
            # F3 2026-10-04: resolver values filling a host position must be safe hosts.
            errs += [f"resolver host value: {m}" for m in resolver_host_value_problems(r)]
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
    _, _, errs = load_asks(records, taxonomy())  # locate v2's example asks: well-formed, and every source covered
    for e in errs:
        print(f"asks/{e}")
    bad += len(errs)
    print(f"{len(seen)} records, {len(answers)} with answers, {bad} invalid")
    return 1 if bad else 0


def cmd_validate(args) -> int:
    from .validate import validate_all
    only = args[args.index("--only") + 1] if "--only" in args else None
    file = args[args.index("--file") + 1] if "--file" in args else None
    want = set(args[args.index("--id") + 1].split(",")) if "--id" in args else None
    samples = int(args[args.index("--samples") + 1]) if "--samples" in args else 2
    for f in _files(only):
        if file and f.stem != file:
            continue
        rows = read_jsonl(f)
        todo = [r for r in rows if not want or r["id"] in want]
        if not todo:
            continue  # untouched files are not rewritten
        validate_all(todo, samples=samples)
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
    names = [a for a in args if a != "--refine"]
    for k, v in resolvers.harvest(names or None, refine="--refine" in args).items():
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


def cmd_asks_overlap(args) -> int:
    """`asks-overlap --against FILE [FILE ...]`: report asks that overlap labeled eval sets outside
    the repo. Prints a per-file breakdown (exact / near) and returns 1 if any overlap is found."""
    from pathlib import Path

    from .overlap import eval_asks_from, overlaps, train_asks_from_library
    from .schema import ASKS
    assert "--against" in args, "usage: asks-overlap --against FILE [FILE ...]"
    paths = [Path(p) for p in args[args.index("--against") + 1:]]
    assert paths, "give at least one --against file"
    train = train_asks_from_library(ASKS)
    total = 0
    for p in paths:
        hits = overlaps(train, eval_asks_from(p))
        exact = sum(1 for _, _, j in hits if j == 1.0)
        near = len(hits) - exact
        print(f"{p}: exact={exact} near={near}")
        for t, e, j in hits:
            print(f"  j={j:.2f}  train={t!r}  eval={e!r}")
        total += len(hits)
    return 1 if total else 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"check", "validate", "harvest", "build", "lookup", "coverage", "resolvers",
                                   "policies", "fills", "evalres", "evallookup", "ingest", "answers-check",
                                   "answers-generate", "asks-overlap"}:
        print(__doc__)
        return 2
    return globals()["cmd_" + argv[0].replace("-", "_")](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
