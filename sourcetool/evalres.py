"""`sourcetool evalres`: run tests/resolution_asks.json through the one matcher; report by resolver."""
from __future__ import annotations

import collections
import json

from . import resolve
from .common import ROOT


def run(name: str = "resolution_asks") -> int:
    spec = json.loads((ROOT / "tests" / f"{name}.json").read_text())
    per = collections.defaultdict(lambda: [0, 0])
    fails = []
    for c in spec["cases"]:
        r = resolve.by_name(c["resolver"], c["ask"], many=c.get("many", False))
        b = r["best"] or {}
        ok = r["status"] == c["expect"]
        if ok and "keys" in c:
            ok = [x["key"] for x in r["candidates"]] == c["keys"]
        if ok and c["expect"] == "resolved":
            ok = all(str(b.get(k) if k in ("key", "state") else b.get("attrs", {}).get(k)) == str(c[k])
                     for k in ("key", "state", "league") if k in c)
        per[c["resolver"]][0] += ok
        per[c["resolver"]][1] += 1
        if not ok:
            fails.append(f"  FAIL {c['ask']!r} [{c['resolver']}] expected {c['expect']} "
                         f"{ {k: c[k] for k in ('key', 'state', 'league') if k in c} } got {r['status']} "
                         f"{(b.get('name'), b.get('state'), b.get('key')) if b else r['reason'][:120]}")
    for c in spec["near"]:
        place = resolve.by_name(c["from"], c["ask"])["best"]
        r = resolve.near(c["resolver"], place["lat"], place["lon"], c["max_km"], differ_on=tuple(c["differ_on"])) \
            if place else {"status": "none", "reason": "place unresolved"}
        ok = r["status"] in c["expect_any"]
        per["near:" + c["resolver"]][0] += ok
        per["near:" + c["resolver"]][1] += 1
        print(f"  near {c['ask']!r}: {r['status']} - {r['reason'][:150]}")
        if not ok:
            fails.append(f"  FAIL near {c['ask']!r} got {r['status']}")
    tot = [sum(v[0] for v in per.values()), sum(v[1] for v in per.values())]
    for k, (a, n) in sorted(per.items()):
        print(f"{a:>3}/{n:<3} {k}")
    print(f"TOTAL {tot[0]}/{tot[1]}")
    print("\n".join(fails))
    return 0 if tot[0] == tot[1] else 1
