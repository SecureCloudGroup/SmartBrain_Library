"""`sourcetool evallookup [file]`: does the FIRST source the lookup offers answer the ask? Top-1 and top-3."""
from __future__ import annotations

import json

from .build import lookup
from .common import ROOT


def run(name: str = "lookup_asks") -> int:
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
