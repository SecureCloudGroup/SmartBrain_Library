"""`sourcetool evallookup [file]`: does the FIRST source the lookup offers answer the ask? Top-1 and top-3.

A `classify_*` file is the classify recall gate instead. An ask is recalled when its labeled subcategory
(`sub`, or one the labeler also accepted, `also`) is among the subcategories classify returns AND the first
one is in the labeled category (the app gates candidates on the first category and passes sources filed in
any returned subcategory). Reported per subcategory, with the strict first-choice rate beside it; the gate
is >= 95% recall in every subcategory."""
from __future__ import annotations

import collections
import json

from .common import ROOT

GATE = 0.95


def classify_report(name: str = "classify_asks") -> dict:
    """{per_sub: {sub: (recalled, n)}, hits, n, first, misses: [(ask, label, got)]} for tests/<name>.json."""
    from .schema import classify
    spec = json.loads((ROOT / "tests" / f"{name}.json").read_text())
    per: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    first, misses = 0, []
    for c in spec["cases"]:
        ok = {c["sub"], *c.get("also", [])}
        got = classify(c["ask"])
        hit = bool(ok & set(got)) and got[0].split("/")[0] in {s.split("/")[0] for s in ok}
        first += bool(got) and got[0] in ok
        per[c["sub"]][0] += hit
        per[c["sub"]][1] += 1
        if not hit:
            misses.append((c["ask"], c["sub"], got))
    return {"per_sub": {k: tuple(v) for k, v in sorted(per.items())}, "hits": sum(v[0] for v in per.values()),
            "n": len(spec["cases"]), "first": first, "misses": misses}


def _run_classify(name: str) -> int:
    rep = classify_report(name)
    n = rep["n"]
    low = [(s, h, k) for s, (h, k) in rep["per_sub"].items() if h < GATE * k]
    print(f"{name}: recall {rep['hits']}/{n} ({100 * rep['hits'] // n}%), first choice {rep['first']}/{n}; "
          f"{len(rep['per_sub']) - len(low)}/{len(rep['per_sub'])} subcategories at >= {GATE:.0%}")
    for s, h, k in low:
        print(f"  LOW {s}: {h}/{k}")
    for ask, sub, got in rep["misses"]:
        print(f"  MISS {ask!r} [{sub}]: got {got}")
    return 1 if low else 0


def run(name: str = "lookup_asks") -> int:
    if name.startswith("classify_"):
        return _run_classify(name)
    from .build import lookup
    spec = json.loads((ROOT / "tests" / f"{name}.json").read_text())
    top1 = top3 = 0
    fails = []
    for c in spec["cases"]:
        ids = [r["id"] for r in lookup(c["ask"], limit=3)]
        ok1 = bool(ids) and ids[0] in c["ok"]
        ok3 = any(i in c["ok"] for i in ids)
        top1 += ok1
        top3 += ok3
        if not ok1:
            fails.append(f"  {'top3' if ok3 else 'MISS'} {c['ask']!r}: got {ids}")
    n = len(spec["cases"])
    print(f"{name}: top-1 {top1}/{n} ({100 * top1 // n}%), top-3 {top3}/{n} ({100 * top3 // n}%)")
    print("\n".join(fails))
    return 0 if top1 == n else 1
