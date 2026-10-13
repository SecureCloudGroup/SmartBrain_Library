"""The TBD/TBA flag beside a time (answers-check / answers-recheck): a key NAMED as a flag, never the
letters inside another word. The 2026-10-12 weekly recheck failed every TheSportsDB answer because
the new ``idAPIfootball`` field holds "tba" inside "football"."""
from __future__ import annotations

from sourcetool.answers import _tbd_flag


def test_a_flag_key_is_found_in_camel_and_snake_case() -> None:
    for key in ("startTimeTBD", "timeTBA", "isTBD", "tbd", "time_tbd", "TBA", "date_tba_flag"):
        assert _tbd_flag({key: True}) == key, key


def test_letters_inside_another_word_are_not_a_flag() -> None:
    for key in ("idAPIfootball", "strTimestamp", "football_id", "tbdx", "outback"):
        assert _tbd_flag({key: "21592"}) == "", key


def test_a_tbd_value_beside_the_time_still_counts() -> None:
    assert _tbd_flag({"strTime": "TBA"}) == "TBA"
    assert _tbd_flag({"strTime": "00:15:00", "idAPIfootball": "21592"}) == ""
