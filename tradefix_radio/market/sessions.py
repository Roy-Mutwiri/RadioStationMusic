"""Trading session classification (§2.6, §97).

Sessions matter twice over: §97 gives each one a musical personality, and
``session_range_position`` (§4) needs to know when the session window resets.

**Local exchange time, not fixed UTC offsets.** Sessions are defined by the clock
in London and New York, and both observe daylight saving on different dates. A
fixed-UTC table is correct for about ten months of the year and quietly wrong for
the rest — during which the station would announce the London open an hour early.
``zoneinfo`` is used instead, which is why ``tzdata`` is a hard dependency on
Windows.

**The weekend gap is a state, not missing data.** Gold stops trading Friday evening
New York time and resumes Sunday evening. :attr:`TradingSession.CLOSED` is a real
classification, and programming during it must not reference live prices (§32).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradefix_radio.contracts.enums import TradingSession
from tradefix_radio.core.clock import UTC

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
SYDNEY = ZoneInfo("Australia/Sydney")

#: Session boundaries in each centre's **local** time.
_SYDNEY_OPEN = time(7, 0)
#: Sydney's window ends where Tokyo's begins; named separately so the two are not
#: silently coupled.
_SYDNEY_CLOSE = time(9, 0)
_TOKYO_OPEN = time(9, 0)
#: 17:00 rather than the 15:00 Tokyo *equities* close. The Asian session for gold is
#: not Tokyo stock-exchange hours: Singapore and Hong Kong carry liquidity into the
#: late afternoon, and the conventional Asian FX/metals session runs to about 17:00
#: JST. With a 15:00 close there is no Asian/London overlap at all in either GMT or
#: BST — Tokyo would shut an hour before London opened — which made
#: TradingSession.ASIAN_LONDON_OVERLAP unreachable and its §97 musical personality
#: dead configuration.
_TOKYO_CLOSE = time(17, 0)
_LONDON_OPEN = time(8, 0)
_LONDON_CLOSE = time(16, 30)
_NEW_YORK_OPEN = time(8, 0)
_NEW_YORK_CLOSE = time(17, 0)
#: After this New York hour the market is thin even though it is technically open.
_NEW_YORK_LATE = time(15, 0)

#: Gold's weekly window, in New York time: Sunday 18:00 to Friday 17:00.
_WEEK_OPEN_WEEKDAY = 6  # Sunday
_WEEK_OPEN_TIME = time(18, 0)
_WEEK_CLOSE_WEEKDAY = 4  # Friday
_WEEK_CLOSE_TIME = time(17, 0)


def _in_window(moment: time, start: time, end: time) -> bool:
    """Whether ``moment`` falls in ``[start, end)``, handling midnight wrap."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def is_market_open(at: datetime) -> bool:
    """Whether gold is trading.

    Evaluated in New York time because that is how the contract's week is defined.
    Public holidays are **not** modelled: they vary by venue and year, and a wrong
    holiday table would be worse than none — the station would go quiet on a day the
    market was open. A genuinely dead feed is detected by staleness instead (§63-E),
    which is both more reliable and self-correcting.
    """
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    local = at.astimezone(NEW_YORK)
    weekday = local.weekday()
    moment = local.time()

    if weekday == 5:  # Saturday
        return False
    if weekday == _WEEK_OPEN_WEEKDAY:  # Sunday
        return moment >= _WEEK_OPEN_TIME
    if weekday == _WEEK_CLOSE_WEEKDAY:  # Friday
        return moment < _WEEK_CLOSE_TIME
    return True


def classify_session(at: datetime) -> TradingSession:
    """Map an instant to its trading session.

    Overlaps take precedence over single sessions, because they are where gold
    actually moves: the London/New-York overlap is the highest-volume window of the
    day and deserves its own musical treatment (§97).
    """
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if not is_market_open(at):
        return TradingSession.CLOSED

    london = at.astimezone(LONDON).time()
    new_york = at.astimezone(NEW_YORK).time()
    tokyo = at.astimezone(TOKYO).time()
    sydney = at.astimezone(SYDNEY).time()

    london_open = _in_window(london, _LONDON_OPEN, _LONDON_CLOSE)
    new_york_open = _in_window(new_york, _NEW_YORK_OPEN, _NEW_YORK_CLOSE)
    tokyo_open = _in_window(tokyo, _TOKYO_OPEN, _TOKYO_CLOSE)
    sydney_open = _in_window(sydney, _SYDNEY_OPEN, _SYDNEY_CLOSE)

    if london_open and new_york_open:
        return TradingSession.LONDON_NEW_YORK_OVERLAP
    if tokyo_open and london_open:
        return TradingSession.ASIAN_LONDON_OVERLAP
    if new_york_open:
        if new_york >= _NEW_YORK_LATE:
            return TradingSession.NEW_YORK_LATE
        return TradingSession.NEW_YORK
    if london_open:
        return TradingSession.LONDON
    if tokyo_open:
        return TradingSession.ASIAN
    if sydney_open:
        return TradingSession.SYDNEY
    # Open but between centres — the thin hours after the Sydney open and before
    # Tokyo, or after the New York close. Musically this is Asian-session
    # territory, which is also where §97 puts the quietest programming.
    return TradingSession.ASIAN


@dataclass
class SessionTracker:
    """Tracks the current session and its price extremes.

    Exists because ``session_range_position`` (§4) is meaningless without knowing
    when to reset. Resetting on a *session change* rather than on a fixed daily
    boundary is what makes the figure answer the question a trader actually asks:
    "where are we within today's London range?"
    """

    session: TradingSession = TradingSession.CLOSED
    session_high: float | None = None
    session_low: float | None = None
    session_started_at: datetime | None = None
    #: Number of bars observed in the current session.
    bars_in_session: int = 0

    def observe(self, at: datetime, high: float, low: float) -> bool:
        """Fold in a bar. Returns ``True`` if the session changed on this bar."""
        session = classify_session(at)
        changed = session is not self.session
        if changed:
            self.session = session
            self.session_high = high
            self.session_low = low
            self.session_started_at = at
            self.bars_in_session = 1
            return True

        self.session_high = high if self.session_high is None else max(self.session_high, high)
        self.session_low = low if self.session_low is None else min(self.session_low, low)
        self.bars_in_session += 1
        return False

    def range_position(self, price: float) -> float:
        """Where ``price`` sits in the session range, 0 (low) to 1 (high).

        Returns 0.5 when the range is degenerate — one bar into a session, or a
        completely flat market. Neutral rather than 0 or 1, because an arbitrary
        extreme would feed a false breakout signal into the music director.
        """
        high, low = self.session_high, self.session_low
        if high is None or low is None or high <= low:
            return 0.5
        return min(1.0, max(0.0, (price - low) / (high - low)))

    def age_seconds(self, now: datetime) -> float:
        """Seconds since the session began, or 0 when no session has started.

        Takes ``now`` rather than reading a clock, so the tracker stays a pure value
        object and the accelerated endurance clock (§64) drives it correctly.
        """
        if self.session_started_at is None:
            return 0.0
        return max(0.0, (now - self.session_started_at).total_seconds())


def next_session_change(at: datetime, *, limit_hours: int = 24) -> datetime | None:
    """The next instant at which the session classification changes.

    Found by scanning forward in 5-minute steps rather than by solving the boundary
    algebraically. The naive approach is correct here and the clever one is not:
    boundaries interact with two independent DST schedules and the weekly close, and
    a closed-form version would need to re-derive all of that. At one call per
    session change, the cost is irrelevant.

    Used by the AI DJ (§32) to announce an upcoming session transition, and by the
    scheduler to pre-position programming.
    """
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    current = classify_session(at)
    step = timedelta(minutes=5)
    moment = at.astimezone(UTC)
    deadline = moment + timedelta(hours=limit_hours)
    while moment < deadline:
        moment += step
        if classify_session(moment) is not current:
            return moment
    return None


__all__ = [
    "LONDON",
    "NEW_YORK",
    "SYDNEY",
    "TOKYO",
    "SessionTracker",
    "classify_session",
    "is_market_open",
    "next_session_change",
]
