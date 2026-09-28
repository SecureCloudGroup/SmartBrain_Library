"""The one resolver matcher: ask words -> a parameter value, for every resolver (docs/RESOLVERS.md).

Two operations, both general:
  * ``by_name(resolver, ask)`` — find entries whose name/alias appears as a phrase in the ask; score by
    specificity (longer phrase), agreement with a state named in the ask, and popularity; ambiguous when
    the best candidates tie but mean different things (Portland OR vs Portland ME; the NFL vs MLB Giants).
  * ``near(resolver, lat, lon, max_km)`` — nearest entries within the policy's distance; ambiguous when the
    runner-up is almost as close but differs in what it measures (a lagoon gauge vs an ocean gauge: the
    ``attrs`` that differ are named in the question to the user); nothing within range is an honest miss.

Every result is {status: resolved|ambiguous|none, best, candidates, reason} — the finder asks the user one
typed question when ambiguous, and never guesses silently.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache

from .common import read_jsonl
from .resolvers import _STATE_BY_NAME, RES, US_STATES, norm

MAX_NGRAM = 6
TIE = 0.75  # candidates scoring within this of the best are "the same strength"

_ENGLISH = set("""a an the of in on at for to and or is are was be by with from as it its this that what whats how
when where who which my me i show get give tell today now current latest near about into per vs next last this
week weekend month year tonight tomorrow yesterday price prices stock stocks score scores game games team schedule
weather forecast status down up is are open closed news report delays delay rate rates level levels air quality
index time times tide tides high low sunset sunrise map chart live new top best any all list value city town county
state station buoy airport near around local home work traffic bus train subway flight flights
day days daily hour hours hourly minute minutes week weekly month monthly year yearly date info information
data update updates
""".split())


# how much popularity counts, per resolver: strong where one reading dominates (a pro team over a college
# namesake, bitcoin over a token with the same symbol, a hub airport over an airstrip); weak for places, so
# "Portland" or "Charleston" without a state is a question to the user, not a silent guess
RANK_WEIGHT = {"team_espn": 1.0, "team_mlb": 1.0, "team_nhl": 1.0, "crypto": 0.004, "airport": 1.0,
               "ticker": 0.8, "tide_station": 0.1, "place": 0.0, "county": 0.0, "zip": 0.0}
# a same-named candidate this many times bigger is what people mean (Denver CO over Denver IA); below it,
# ask (Portland OR vs ME is ~10x, Springfield MO/MA/IL are close)
DOMINANCE = 20
ATTR_CONTEXT = ("sport", "league", "exchange")  # words in the ask that pick among namesakes


def _common_words() -> frozenset[str]:
    return frozenset(_ENGLISH)


@lru_cache(maxsize=None)
def load(resolver: str) -> tuple[list[dict], dict[str, list[int]], dict[str, list[int]]]:
    """Rows, the exact-alias index, and a leading-words index ("hartsfield jackson" for a longer name)."""
    rows = read_jsonl(RES / f"{resolver}.jsonl")
    index: dict[str, list[int]] = {}
    partial: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        for a in r["aliases"]:
            words = a.split()
            if len(words) <= MAX_NGRAM:
                index.setdefault(a, []).append(i)
            for k in range(2, min(len(words), MAX_NGRAM + 1)):
                partial.setdefault(" ".join(words[:k]), []).append(i)
    return rows, index, partial


def states_in(ask: str) -> set[str]:
    """US states named in the ask: a two-letter code written in capitals ("FL"), or a full name."""
    found = {m for m in re.findall(r"\b([A-Z]{2})\b", ask or "") if m in US_STATES}
    low = f" {norm(ask)} "
    # a state name that is part of a place's own name is not a state mention ("Kansas City", "Indiana Dunes")
    found |= {code for name, code in _STATE_BY_NAME.items()
              if f" {name} " in low and not re.search(rf" {re.escape(name)} (city|beach|dunes|harbor)\b", low)}
    return found


def _ngrams(tokens: list[str]) -> list[tuple[int, int, str]]:
    return [(i, j, " ".join(tokens[i:j])) for i in range(len(tokens))
            for j in range(i + 1, min(len(tokens), i + MAX_NGRAM) + 1)]


def by_name(resolver: str, ask: str, *, raw_case_codes: bool = True, many: bool = False) -> dict:
    """``many=True`` returns every distinct thing named, in the order the ask names them (currency pairs,
    "compare X and Y") instead of treating two names as an ambiguity."""
    rows, index, partial = load(resolver)
    tokens = norm(ask).split()
    upper = set(re.findall(r"\b[A-Z0-9$]{1,6}\b", ask or ""))  # tokens the user typed in capitals
    states = states_in(ask)
    common = _common_words()
    scored: dict[int, float] = {}
    first_at: dict[int, int] = {}
    text = f" {' '.join(tokens)} "
    for _i, _j, gram in _ngrams(tokens):
        n = len(gram.split())
        # specificity counts the distinctive words only: "midway airport" is as specific as "midway"
        spec = sum(1 for w in gram.split() if w not in common) or 1
        if n == 1 and gram in common:
            continue  # "weather", "down", "price" are never a place or a ticker
        hits = [(idx, 0.0) for idx in index.get(gram, [])] + [(idx, -0.5) for idx in partial.get(gram, [])]
        for idx, partial_penalty in hits:
            r = rows[idx]
            if n == 1 and gram == norm(r["key"]) and len(gram) <= 5 and raw_case_codes \
                    and gram not in norm(r["name"]).split():  # "meta" is META's code AND its name
                # a short code that is also an English word (A, ON, IT, ALL) counts only when typed like a code;
                # crypto symbols are usually typed lowercase, so a known (ranked) coin's symbol is accepted
                if r["kind"] == "ticker" and gram.upper() not in upper:
                    continue
                if r["kind"] == "crypto_asset" and not r.get("rank") and gram.upper() not in upper:
                    continue
            if n == 1 and r["kind"] == "crypto_asset" and gram in r["aliases"] and gram != norm(r["name"]) \
                    and not r.get("rank") and gram.upper() not in upper:
                continue  # an unranked token's lowercase symbol is noise ("eth" is Ethereum, not a clone)
            score = (2.0 * spec + partial_penalty + (0.5 if gram == norm(r["name"]) else 0.0)
                     + RANK_WEIGHT.get(resolver, 0.05) * float(r.get("rank") or 0))
            if states:
                score += 3.0 if r.get("state") in states else (-3.0 if r.get("state") else 0.0)
            for k in ATTR_CONTEXT:  # "Alabama football", "NBA", "on the NYSE"
                v = norm(str(r.get("attrs", {}).get(k) or ""))
                if v and any(f" {w} " in text for w in {v, v.replace("college ", "")} if w):
                    score += 1.5
            scored[idx] = max(scored.get(idx, -1e9), score)
            first_at[idx] = min(first_at.get(idx, 99), _i)
    if not scored:
        return {"status": "none", "best": None, "candidates": [], "reason": f"nothing in {resolver} matches"}
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    if many:
        seen, picks = set(), []
        for i, sc in sorted(ranked, key=lambda kv: first_at[kv[0]]):
            name = norm(rows[i]["name"])
            if name not in seen and sc >= ranked[0][1] - 2.0:
                seen.add(name)
                picks.append(rows[i])
        return {"status": "resolved", "best": picks[0], "candidates": picks, "reason": ""}
    best_score = ranked[0][1]
    all_ties = [rows[i] for i, s in ranked if best_score - s < TIE]
    ties = all_ties[:6]  # shown to the user; dominance looks at every tie
    top = rows[ranked[0][0]]
    pops = sorted(((t.get("attrs", {}).get("pop") or 0, t) for t in all_ties), key=lambda x: -x[0])
    if len(pops) > 1 and pops[0][0] and pops[0][0] >= DOMINANCE * max(pops[1][0], 1):
        return {"status": "resolved", "best": pops[0][1], "candidates": ties,
                "reason": f"{pops[0][1]['name']} is by far the largest of {len(ties)} places with that name"}
    distinct = {(t["name"], t.get("state"), json.dumps(t.get("attrs", {}).get("league"))) for t in ties}
    if len(ties) > 1 and len(distinct) > 1 and not _same_thing(ties):
        # offer the likeliest readings first: the largest places, the most popular teams/assets
        ties = sorted(all_ties, key=lambda t: (-(t.get("attrs", {}).get("pop") or 0), -float(t.get("rank") or 0)))[:6]
        return {"status": "ambiguous", "best": None, "candidates": ties,
                "reason": "several match equally: " + "; ".join(_label(t) for t in ties)}
    return {"status": "resolved", "best": top, "candidates": [rows[i] for i, _ in ranked[:5]], "reason": ""}


def _same_thing(ties: list[dict]) -> bool:
    """Ties that are one real thing (a crypto listed twice, a team under two nicknames) are not ambiguous."""
    names = {norm(t["name"]) for t in ties}
    return len(names) == 1 and len({t.get("state") for t in ties}) == 1


def _label(r: dict) -> str:
    extra = r.get("state") or r.get("attrs", {}).get("league") or r.get("attrs", {}).get("exchange") or ""
    return f"{r['name']}" + (f" ({extra})" if extra else "")


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def near(resolver: str, lat: float, lon: float, max_km: float, *, differ_on: tuple[str, ...] = ()) -> dict:
    rows, _, _ = load(resolver)
    dist = sorted(((km(lat, lon, r["lat"], r["lon"]), r) for r in rows if r.get("lat") is not None),
                  key=lambda x: x[0])[:8]
    within = [(d, r) for d, r in dist if d <= max_km]
    if not within:
        d, r = dist[0] if dist else (None, None)
        return {"status": "none", "best": None, "candidates": [r] if r else [],
                "reason": f"no {resolver} within {max_km:g} km" + (f" (nearest {d:.0f} km: {r['name']})" if r else "")}
    d1, best = within[0]
    rivals = [(d, r) for d, r in within[1:] if d <= max(1.5 * d1, d1 + 5)]
    for d, r in rivals:
        diff = [k for k in differ_on if (r.get("attrs", {}).get(k) or "") != (best.get("attrs", {}).get(k) or "")]
        if diff:
            cands = [best] + [x for _, x in rivals]
            return {"status": "ambiguous", "best": None, "candidates": cands,
                    "reason": f"close {resolver}s differ in {', '.join(diff)}: "
                              + "; ".join(f"{c['name']} ({km(lat, lon, c['lat'], c['lon']):.0f} km)" for c in cands)}
    return {"status": "resolved", "best": best, "candidates": [r for _, r in within[:5]],
            "reason": f"{best['name']} is {d1:.0f} km away"}
