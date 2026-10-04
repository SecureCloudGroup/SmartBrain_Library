"""taxonomy.json: its closed shape (taxonomy_problems), the placements the root-cause fix needs, and the
classify recall gate on the blind labeled sets (tests/classify_*.json, written by an agent that never saw
the code or the keywords)."""
from __future__ import annotations

import copy

import pytest

from sourcetool.common import taxonomy
from sourcetool.evallookup import classify_report
from sourcetool.schema import classify, taxonomy_problems


def _sub(t: dict, cid: str) -> dict:
    cat, _, sub = cid.partition("/")
    return next(s for c in t["categories"] if c["id"] == cat for s in c["subcategories"] if s["id"] == sub)


# --- the closed shape ---------------------------------------------------------------------------------

def test_the_repo_taxonomy_is_well_formed():
    assert taxonomy_problems(taxonomy()) == []


def test_taxonomy_problems_name_each_defect():
    t = taxonomy()

    def broken(edit):
        t2 = copy.deepcopy(t)
        edit(t2)
        return " | ".join(taxonomy_problems(t2))

    assert "unknown keys" in broken(lambda x: _sub(x, "weather/forecast").update(extra=1))
    assert "expects" in broken(lambda x: _sub(x, "water/surf_waves").update(expects=["wave height"]))
    assert "expects" in broken(lambda x: _sub(x, "water/surf_waves").update(expects=[]))
    assert "policy.measure" in broken(lambda x: _sub(x, "water/surf_waves")["policy"].update(measure={"zip": "WVHT"}))
    assert "policy.measure" in broken(lambda x: _sub(x, "water/surf_waves")["policy"].update(measure={"buoy": "wave"}))
    assert "policy keys" in broken(lambda x: _sub(x, "water/surf_waves")["policy"].update(nope=1))
    assert "kinds" in broken(lambda x: _sub(x, "weather/forecast").update(kinds=["forecast", "weather"]))
    # classify strips everything but [a-z0-9.&+ ] from the ask, so "tip-off" could never match
    assert "never match" in broken(lambda x: _sub(x, "sports/schedules")["keywords"].append("tip-off"))
    assert "duplicate" in broken(lambda x: x["categories"][0]["subcategories"].append(
        copy.deepcopy(x["categories"][0]["subcategories"][0])))


# --- what the fix needs from the data -----------------------------------------------------------------

def test_kinds_vocabulary_has_result():
    assert "result" in taxonomy()["question_kinds"]


def test_space_weather_keeps_only_multi_word_storms_and_forecast_drops_daily():
    t = taxonomy()
    assert not {"storm", "storms"} & set(_sub(t, "hazards/space_weather")["keywords"])
    assert {"geomagnetic storm", "solar storm"} <= set(_sub(t, "hazards/space_weather")["keywords"])
    assert "daily" not in _sub(t, "weather/forecast")["keywords"]


def test_geysers_is_a_next_event_subcategory():
    g = _sub(taxonomy(), "science/geysers")
    assert "next_event" in g["kinds"] and {"geyser", "old faithful"} <= set(g["keywords"])


def test_policies_the_fix_names():
    t = taxonomy()
    trop = _sub(t, "hazards/tropical_storms")["policy"]
    assert trop["match"] == "geo" and trop["resolvers"][:2] == ["place", "zip"]
    assert _sub(t, "water/water_temperature")["policy"]["measure"] == {"buoy": "WTMP"}
    assert _sub(t, "water/surf_waves")["policy"]["measure"] == {"buoy": "WVHT"}
    for cid in ("water/surf_waves", "water/water_temperature", "weather/forecast", "weather/air_quality",
                "sports/scores", "sports/schedules", "sports/standings"):
        assert _sub(t, cid).get("expects"), cid
    assert {"waves", "wave_period", "wave_direction", "wind"} <= set(_sub(t, "water/surf_waves")["expects"])


def test_indices_own_the_market_words_and_stocks_do_not():
    t = taxonomy()
    idx, stocks = set(_sub(t, "markets/indices")["keywords"]), set(_sub(t, "markets/stocks")["keywords"])
    assert {"nasdaq", "dow", "s&p 500", "stock market"} <= idx
    assert not {"nasdaq", "dow", "stock market", "the market"} & stocks


# --- classify: the seeds from the root-cause analysis, then the blind sets ----------------------------

SEEDS = [  # (ask, acceptable first subcategory)
    ("will it thunderstorm in Houston today", {"weather/forecast"}),
    ("any hail in Denver this afternoon", {"weather/forecast"}),
    ("is a tornado coming to Oklahoma City", {"weather/alerts"}),
    ("foggy in San Francisco tomorrow morning?", {"weather/forecast"}),
    ("frost tonight in Boise", {"weather/forecast"}),
    ("blizzard warning Buffalo", {"weather/alerts"}),
    ("lightning near Miami right now", {"weather/forecast", "weather/current", "weather/alerts"}),
    ("is it gonna storm in Tulsa tonight", {"weather/forecast"}),
    ("geomagnetic storm tonight", {"hazards/space_weather"}),
    ("solar storm this week", {"hazards/space_weather"}),
    ("when does Old Faithful erupt", {"science/geysers"}),
    ("Old Faithful next eruption", {"science/geysers"}),
    ("Grand Geyser prediction", {"science/geysers"}),
    ("what time is high tide in Savannah", {"water/tides"}),
    ("when do the Cubs play", {"sports/schedules"}),
    ("Lakers game time", {"sports/schedules"}),
    ("latest episode of Radiolab", {"news/podcasts"}),
    ("latest episode of The Daily podcast", {"news/podcasts"}),
    ("ice hockey scores tonight", {"sports/scores"}),
    ("Tampa Bay Lightning score", {"sports/scores"}),
    ("how's the Nasdaq doing today", {"markets/indices"}),
    ("how is the stock market doing", {"markets/indices"}),
    ("Nasdaq Inc stock", {"markets/stocks"}),
    ("how's the surf at Virginia Beach", {"water/surf_waves"}),
    ("Lake Michigan water temp Milwaukee", {"water/water_temperature"}),
    ("any hurricanes headed for Tampa?", {"hazards/tropical_storms"}),
]


@pytest.mark.parametrize("ask,ok", SEEDS)
def test_classify_seeds(ask, ok):
    got = classify(ask)
    assert got and got[0] in ok, (ask, got)


def test_classify_keeps_weather_out_of_other_domains():
    for ask in ("latest episode of The Daily podcast", "ice hockey scores tonight", "Tampa Bay Lightning score",
                "geomagnetic storm tonight"):
        assert not any(c.startswith("weather/") for c in classify(ask)[:1]), ask


# The target is >= 95% recall in EVERY subcategory (`sourcetool evallookup classify_asks` exits 1 below it).
# Keywords alone can't recall an ask that names only an entity ("how's nvidia doing", "is dfw on time",
# "richmond va news"): the app's locate step adds the intent's subject/wants and the entity resolvers for
# those. So this suite holds the floor measured when the keywords were frozen (2026-09-29; before the fix:
# 50% / 54% / 53%), and any keyword change that lowers it fails. Keywords were drafted from classify_asks
# and class-level misses in classify_holdout; classify_sealed was never read, only scored.
FLOOR = {"classify_asks": 0.95, "classify_holdout": 0.93, "classify_sealed": 0.80}


@pytest.mark.parametrize("name", sorted(FLOOR))
def test_classify_recall_floor(name):
    """An ask is recalled when its labeled subcategory (or one the labeler also accepted) is among those
    classify returns and the first returned is in the labeled category."""
    rep = classify_report(name)
    assert rep["hits"] >= FLOOR[name] * rep["n"] - 1e-9, (rep["hits"], rep["n"], rep["misses"][:10])


def test_classify_gate_counts_per_subcategory():
    rep = classify_report("classify_asks")
    assert len(rep["per_sub"]) == 46 and sum(n for _, n in rep["per_sub"].values()) == rep["n"]
