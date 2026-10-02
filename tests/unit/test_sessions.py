"""Trading session classification (§2.6, §97, milestone 2.6).

Milestone 2.6's exit criterion is DST correctness: "boundary timestamps across DST
transitions map to correct sessions". This matters because London and New York change
clocks on *different dates*, so a fixed-UTC session table is correct for roughly ten
months a year and quietly wrong for the rest — during which the station would announce
the London open an hour early and apply the wrong §97 musical personality.

The weekend gap gets equal attention. Gold is not continuously traded, and
``CLOSED`` is a real state rather than missing data: programming during it must not
reference live prices (§32).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tradefix_radio.contracts.enums import TradingSession
from tradefix_radio.market.sessions import (
    LONDON,
    NEW_YORK,
    SessionTracker,
    classify_session,
    is_market_open,
    next_session_change,
)

UTC = timezone.utc


def london(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A moment expressed in London local time."""
    return datetime(year, month, day, hour, minute, tzinfo=LONDON)


def new_york(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK)


# ---------------------------------------------------------------- market hours


def test_saturday_is_closed() -> None:
    # 2026-10-03 is a Saturday.
    assert not is_market_open(new_york(2026, 10, 3, 12))


def test_sunday_opens_at_six_pm_new_york() -> None:
    assert not is_market_open(new_york(2026, 10, 4, 17, 59))
    assert is_market_open(new_york(2026, 10, 4, 18, 0))
    assert is_market_open(new_york(2026, 10, 4, 23, 0))


def test_friday_closes_at_five_pm_new_york() -> None:
    assert is_market_open(new_york(2026, 10, 2, 16, 59))
    assert not is_market_open(new_york(2026, 10, 2, 17, 0))
    assert not is_market_open(new_york(2026, 10, 2, 22, 0))


@pytest.mark.parametrize("day", [5, 6, 7, 8])  # Mon-Thu
def test_midweek_is_open_around_the_clock(day: int) -> None:
    for hour in (0, 6, 12, 18, 23):
        assert is_market_open(new_york(2026, 10, day, hour))


def test_closed_market_classifies_as_closed() -> None:
    """A real state, not missing data: no live price may be shown (§32)."""
    assert classify_session(new_york(2026, 10, 3, 12)) is TradingSession.CLOSED


def test_naive_datetimes_are_rejected() -> None:
    for function in (is_market_open, classify_session):
        with pytest.raises(ValueError, match="timezone-aware"):
            function(datetime(2026, 10, 2, 12, 0))


# ---------------------------------------------------------------- sessions


def test_london_morning_before_new_york_is_london() -> None:
    assert classify_session(london(2026, 10, 2, 9, 0)) is TradingSession.LONDON


def test_the_overlap_is_recognised() -> None:
    """The highest-volume window of the day deserves its own §97 personality."""
    # 14:00 London = 09:00 New York; both open.
    assert (
        classify_session(london(2026, 10, 2, 14, 0))
        is TradingSession.LONDON_NEW_YORK_OVERLAP
    )


def test_new_york_afternoon_after_london_closes() -> None:
    # 12:00 New York = 17:00 London, which is past the London close.
    assert classify_session(new_york(2026, 10, 2, 12, 0)) is TradingSession.NEW_YORK


def test_late_new_york_is_distinguished() -> None:
    """Thin but technically open; §97 programmes it differently."""
    assert classify_session(new_york(2026, 10, 2, 15, 30)) is TradingSession.NEW_YORK_LATE


def test_tokyo_hours_classify_as_asian() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    assert classify_session(datetime(2026, 10, 2, 10, 0, tzinfo=tokyo)) is TradingSession.ASIAN


def test_the_asian_london_overlap_is_recognised() -> None:
    # 08:30 London = 16:30 Tokyo, within both windows.
    assert (
        classify_session(london(2026, 10, 2, 8, 30))
        is TradingSession.ASIAN_LONDON_OVERLAP
    )


def test_every_session_value_is_reachable_across_a_week() -> None:
    """A session no timestamp maps to would be dead configuration in §97."""
    reached = set()
    moment = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
    for _ in range(7 * 24 * 4):  # a week at 15-minute resolution
        reached.add(classify_session(moment))
        moment += timedelta(minutes=15)
    missing = set(TradingSession) - reached
    assert not missing, f"unreachable sessions: {sorted(s.value for s in missing)}"


def test_the_whole_week_is_classified_without_gaps() -> None:
    """Every open minute must belong to exactly one session."""
    moment = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
    for _ in range(7 * 24 * 12):  # 5-minute resolution
        session = classify_session(moment)
        assert isinstance(session, TradingSession)
        if session is TradingSession.CLOSED:
            assert not is_market_open(moment)
        else:
            assert is_market_open(moment)
        moment += timedelta(minutes=5)


# ---------------------------------------------------------------- DST


def test_london_open_tracks_british_summer_time() -> None:
    """The milestone 2.6 exit test.

    08:00 London is the open in both winter and summer, but that is 08:00 UTC in
    winter and 07:00 UTC in summer. A fixed-UTC table gets one of them wrong.
    """
    # Winter (GMT): 08:00 London == 08:00 UTC.
    winter = london(2026, 1, 15, 8, 0)
    assert winter.utcoffset() == timedelta(0)
    assert classify_session(winter) in {
        TradingSession.LONDON,
        TradingSession.ASIAN_LONDON_OVERLAP,
    }
    assert classify_session(winter - timedelta(minutes=30)) is TradingSession.ASIAN

    # Summer (BST): 08:00 London == 07:00 UTC.
    summer = london(2026, 7, 15, 8, 0)
    assert summer.utcoffset() == timedelta(hours=1)
    assert classify_session(summer) in {
        TradingSession.LONDON,
        TradingSession.ASIAN_LONDON_OVERLAP,
    }
    assert classify_session(summer - timedelta(minutes=30)) is TradingSession.ASIAN


def test_the_overlap_survives_the_weeks_when_offsets_disagree() -> None:
    """London and New York change clocks on different dates.

    In late October 2026 the UK has returned to GMT but the US has not yet left EDT,
    so the UTC offset between them is four hours rather than the usual five. The
    overlap must still be found.
    """
    # 2026: UK clocks go back 25 October; US clocks go back 1 November.
    between = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    assert between.astimezone(LONDON).hour == 13
    assert between.astimezone(NEW_YORK).hour == 9
    assert classify_session(between) is TradingSession.LONDON_NEW_YORK_OVERLAP


def test_session_classification_is_stable_across_a_dst_transition_hour() -> None:
    """The ambiguous/skipped hour must not raise or produce a nonsense session."""
    # UK spring-forward 2026: 29 March, 01:00 GMT -> 02:00 BST.
    moment = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    for _ in range(24 * 4):
        assert isinstance(classify_session(moment), TradingSession)
        moment += timedelta(minutes=15)


def test_us_dst_transition_is_handled() -> None:
    # US spring-forward 2026: 8 March.
    moment = datetime(2026, 3, 8, 0, 0, tzinfo=UTC)
    for _ in range(24 * 4):
        assert isinstance(classify_session(moment), TradingSession)
        moment += timedelta(minutes=15)


# ---------------------------------------------------------------- next change


def test_next_session_change_finds_a_boundary() -> None:
    moment = london(2026, 10, 2, 7, 0)  # Asian, before the London open
    change = next_session_change(moment)
    assert change is not None
    assert classify_session(change) is not classify_session(moment)
    assert change > moment


def test_next_session_change_respects_the_search_limit() -> None:
    """Saturday morning: the next change is more than an hour away."""
    assert next_session_change(new_york(2026, 10, 3, 6), limit_hours=1) is None


def test_next_session_change_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_session_change(datetime(2026, 10, 2, 12))


# ---------------------------------------------------------------- tracker


def test_tracker_reports_a_session_change() -> None:
    tracker = SessionTracker()
    assert tracker.observe(london(2026, 10, 2, 9, 0), 4010.0, 4000.0) is True
    assert tracker.session is TradingSession.LONDON
    assert tracker.observe(london(2026, 10, 2, 9, 1), 4012.0, 4002.0) is False


def test_tracker_accumulates_session_extremes() -> None:
    tracker = SessionTracker()
    base = london(2026, 10, 2, 9, 0)
    tracker.observe(base, 4010.0, 4000.0)
    tracker.observe(base + timedelta(minutes=1), 4020.0, 4005.0)
    tracker.observe(base + timedelta(minutes=2), 4015.0, 3995.0)
    assert tracker.session_high == pytest.approx(4020.0)
    assert tracker.session_low == pytest.approx(3995.0)
    assert tracker.bars_in_session == 3


def test_tracker_resets_extremes_on_a_session_change() -> None:
    """``session_range_position`` is meaningless without this (§4)."""
    tracker = SessionTracker()
    tracker.observe(london(2026, 10, 2, 9, 0), 4100.0, 3900.0)
    assert tracker.session_high == pytest.approx(4100.0)
    # Crossing into the overlap resets the window.
    changed = tracker.observe(london(2026, 10, 2, 14, 0), 4010.0, 4005.0)
    assert changed is True
    assert tracker.session_high == pytest.approx(4010.0)
    assert tracker.session_low == pytest.approx(4005.0)
    assert tracker.bars_in_session == 1


def test_range_position_spans_the_session() -> None:
    tracker = SessionTracker()
    base = london(2026, 10, 2, 9, 0)
    tracker.observe(base, 4100.0, 4000.0)
    assert tracker.range_position(4000.0) == pytest.approx(0.0)
    assert tracker.range_position(4100.0) == pytest.approx(1.0)
    assert tracker.range_position(4050.0) == pytest.approx(0.5)


def test_range_position_is_neutral_for_a_degenerate_range() -> None:
    """One bar in, or a dead-flat market: an arbitrary extreme would be a false signal."""
    tracker = SessionTracker()
    tracker.observe(london(2026, 10, 2, 9, 0), 4000.0, 4000.0)
    assert tracker.range_position(4000.0) == 0.5
    assert SessionTracker().range_position(4000.0) == 0.5


def test_range_position_clamps_outside_the_session_range() -> None:
    tracker = SessionTracker()
    tracker.observe(london(2026, 10, 2, 9, 0), 4100.0, 4000.0)
    assert tracker.range_position(3000.0) == 0.0
    assert tracker.range_position(5000.0) == 1.0


def test_tracker_age_requires_an_explicit_now() -> None:
    """The tracker is a value object; the accelerated clock (§64) drives it."""
    tracker = SessionTracker()
    base = london(2026, 10, 2, 9, 0)
    assert tracker.age_seconds(base) == 0.0
    tracker.observe(base, 4010.0, 4000.0)
    assert tracker.age_seconds(base + timedelta(minutes=30)) == pytest.approx(1800.0)


def test_tracker_age_never_goes_negative() -> None:
    tracker = SessionTracker()
    base = london(2026, 10, 2, 9, 0)
    tracker.observe(base, 4010.0, 4000.0)
    assert tracker.age_seconds(base - timedelta(hours=1)) == 0.0
