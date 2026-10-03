"""Which market the station is currently programming against.

```
XAUUSD feed ──> MarketDataService(XAUUSD) ──┐
BTCUSD feed ──> MarketDataService(BTCUSD) ──┤
                                            v
                                      MarketRouter
                                            v
                                     ActiveMarket
                                            v
                              features / regimes / MusicDirector
```

One service per symbol, each with its own feature, energy and regime engines. That is not
an implementation convenience — it is the "no regime-state contamination between symbols"
requirement made structural. Gold's rolling windows and Bitcoin's cannot mix because they
are different objects, and a switch does not reset either, so returning to gold returns to
gold's own history rather than to a cold start.

The router decides **which state is the active one**. It does not compute market state, it
does not touch the scheduler, and there is exactly one of it.

Two rules do all the work
-------------------------
**Only a confirmed closure moves the station.** `MarketAvailability.authorises_fallback`
is true for `CLOSED` and nothing else. A dead feed during trading hours is a feed problem
and is reported as one; it must not look like a closure, because reacting to it would swap
the station's entire musical character every time a broker connection hiccuped.

**Nothing moves without surviving a timer.** Market boundaries are noisy — ticks straggle
in after the close and arrive before the open — so every transition has to hold its
condition for a configured window, and a market that just became active cannot be replaced
for a minimum dwell. Without both, the station oscillates at exactly the moments it is most
visible.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.market.availability import (
    AvailabilityAssessment,
    MarketAvailability,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from tradefix_radio.config.schema import MarketRoutingSettings
    from tradefix_radio.core.clock import Clock

_log = structlog.get_logger(__name__)

__all__ = [
    "NO_ACTIVE_MARKET",
    "ActiveMarket",
    "MarketRouter",
    "RoutingDecision",
    "SwitchReason",
]

#: What `active_symbol` reads when no configured market can be programmed against.
#:
#: A sentinel rather than ``None`` so it survives serialisation to the Control Center and
#: reads unambiguously in a log line. The radio keeps broadcasting from its buffer and its
#: emergency tiers; this says only that nothing new should be planned against live data.
NO_ACTIVE_MARKET = "NO_ACTIVE_MARKET"


class SwitchReason(str, enum.Enum):
    """Why the active market changed. Carried on the event and shown in the UI."""

    INITIAL_SELECTION = "initial_selection"
    PRIMARY_MARKET_CLOSED = "primary_market_closed"
    PRIMARY_MARKET_REOPENED = "primary_market_reopened"
    FALLBACK_MARKET_CLOSED = "fallback_market_closed"
    NO_MARKET_AVAILABLE = "no_market_available"
    MARKET_RESTORED = "market_restored"


@dataclass(frozen=True)
class ActiveMarket:
    """The station's current market context, for everything downstream.

    Downstream code takes *this* rather than a hard-coded ``"XAUUSD"``. The symbol is here
    so the lyrics director can write about the right asset, and `is_primary` is here so the
    UI can say "running on the fallback" without knowing what the configured primary is.
    """

    symbol: str
    availability: MarketAvailability
    is_primary: bool
    since: datetime
    reason: SwitchReason

    @property
    def is_active(self) -> bool:
        return self.symbol != NO_ACTIVE_MARKET


@dataclass
class RoutingDecision:
    """What one evaluation concluded."""

    active: ActiveMarket
    changed: bool
    previous_symbol: str | None = None
    previous_state: MarketAvailability | None = None
    assessments: dict[str, AvailabilityAssessment] = field(default_factory=dict)

    @property
    def event_payload(self) -> dict[str, object]:
        """The `market.active_symbol_changed` body."""
        return {
            "previous_symbol": self.previous_symbol,
            "new_symbol": self.active.symbol,
            "reason": self.active.reason.value,
            "timestamp": self.active.since.isoformat(),
            "previous_state": (
                None if self.previous_state is None else self.previous_state.value
            ),
            "new_state": self.active.availability.value,
        }


@dataclass
class _Pending:
    """A candidate transition that is waiting out its confirmation window."""

    symbol: str
    reason: SwitchReason
    since_monotonic: float


class MarketRouter:
    """Picks the active symbol from per-symbol availability, with hysteresis."""

    def __init__(
        self,
        settings: MarketRoutingSettings,
        *,
        clock: Clock,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._active: ActiveMarket | None = None
        self._active_since_monotonic: float | None = None
        self._pending: _Pending | None = None
        self._assessments: dict[str, AvailabilityAssessment] = {}
        self._switch_count = 0

    # ------------------------------------------------------------ inspection

    @property
    def symbols(self) -> tuple[str, ...]:
        """Every configured symbol, primary first."""
        return (self._settings.primary, *self._settings.fallback)

    @property
    def primary(self) -> str:
        return self._settings.primary

    @property
    def active(self) -> ActiveMarket | None:
        return self._active

    @property
    def active_symbol(self) -> str:
        return NO_ACTIVE_MARKET if self._active is None else self._active.symbol

    @property
    def switch_count(self) -> int:
        return self._switch_count

    @property
    def assessments(self) -> Mapping[str, AvailabilityAssessment]:
        """The latest per-symbol assessment, for the Market page."""
        return dict(self._assessments)

    @property
    def pending_symbol(self) -> str | None:
        """A transition being confirmed, if any. Shown so a wait is visible, not mysterious."""
        return None if self._pending is None else self._pending.symbol

    def pending_seconds_remaining(self) -> float | None:
        """How much longer the pending transition must hold."""
        if self._pending is None:
            return None
        window = self._window_for(self._pending.reason)
        elapsed = self._clock.monotonic() - self._pending.since_monotonic
        return max(0.0, window - elapsed)

    # -------------------------------------------------------------- evaluate

    def evaluate(
        self, assessments: Mapping[str, AvailabilityAssessment], *, now: datetime
    ) -> RoutingDecision:
        """Decide the active market from the current per-symbol assessments.

        Pure with respect to the clock it was given: called twice with the same inputs and
        no time passing, it returns the same answer and does not switch twice.
        """
        self._assessments = dict(assessments)
        # Normalised to the sentinel immediately. `_preferred_symbol` returns None for "no
        # market", and comparing that against a pending record — which stores the sentinel
        # — never matched, so a transition *to* no-market restarted its confirmation window
        # on every evaluation and could never complete.
        preferred = self._preferred_symbol(assessments) or NO_ACTIVE_MARKET

        if self._active is None:
            # First evaluation. No hysteresis — there is nothing to flap away from, and
            # making the station wait 90 seconds before its first blueprint would be a
            # cost with no benefit.
            reason = (
                SwitchReason.INITIAL_SELECTION
                if preferred != NO_ACTIVE_MARKET
                else SwitchReason.NO_MARKET_AVAILABLE
            )
            return self._commit(preferred, reason, now=now, previous=None)

        current = self._active
        if preferred == current.symbol:
            # Still the right answer. Cancel any transition that was being confirmed —
            # this is the flap that hysteresis exists to absorb.
            if self._pending is not None:
                _log.info(
                    "market.switch_cancelled",
                    candidate=self._pending.symbol,
                    active=current.symbol,
                    detail="the condition that prompted the switch no longer holds",
                )
                self._pending = None
            refreshed = self._refresh_availability(current, assessments)
            return RoutingDecision(
                active=refreshed, changed=False, assessments=self._assessments
            )

        reason = self._reason_for(current.symbol, preferred, assessments)

        # Acquiring a market when there is none is immediate, and is not a "switch".
        #
        # Hysteresis exists to stop the station oscillating between two markets, and to stop
        # it abandoning a working one on a blip. Neither applies here: there is nothing to
        # oscillate with and nothing to abandon, and every second spent waiting is a second
        # the director plans against no live data at all.
        #
        # This is the ordinary startup path, not an edge case. Every process begins with no
        # assessments, and the feeds take a moment to produce their first bar — so without
        # this the station would sit in NO_ACTIVE_MARKET for the full confirmation window
        # (two minutes at the defaults) at *every* launch, broadcasting on emergency tiers,
        # and would then log a market switch that nobody made.
        if not current.is_active and preferred != NO_ACTIVE_MARKET:
            return self._commit(
                preferred, reason, now=now, previous=current, counts_as_switch=False
            )

        # A market that has only just become active cannot be replaced immediately.
        dwell = self._clock.monotonic() - (self._active_since_monotonic or 0.0)
        if (
            current.is_active
            and dwell < self._settings.minimum_active_market_seconds
            and preferred != NO_ACTIVE_MARKET
        ):
            _log.debug(
                "market.switch_held_by_dwell",
                active=current.symbol,
                candidate=preferred,
                dwell_seconds=round(dwell, 1),
                minimum=self._settings.minimum_active_market_seconds,
            )
            return RoutingDecision(
                active=current, changed=False, assessments=self._assessments
            )

        # Start or continue confirming.
        if self._pending is None or self._pending.symbol != preferred:
            self._pending = _Pending(
                symbol=preferred,
                reason=reason,
                since_monotonic=self._clock.monotonic(),
            )
            _log.info(
                "market.switch_pending",
                active=current.symbol,
                candidate=self._pending.symbol,
                reason=reason.value,
                confirm_seconds=self._window_for(reason),
            )
            return RoutingDecision(
                active=current, changed=False, assessments=self._assessments
            )

        elapsed = self._clock.monotonic() - self._pending.since_monotonic
        if elapsed < self._window_for(self._pending.reason):
            return RoutingDecision(
                active=current, changed=False, assessments=self._assessments
            )

        return self._commit(preferred, self._pending.reason, now=now, previous=current)

    # --------------------------------------------------------------- helpers

    def _preferred_symbol(
        self, assessments: Mapping[str, AvailabilityAssessment]
    ) -> str | None:
        """The symbol the station *should* be on, ignoring hysteresis.

        Priority order is the configured order, and a symbol is eligible when it is usable.
        The primary is only skipped when it is **confirmed closed** — a degraded primary
        feed keeps the primary, which is the rule this whole subsystem is built around.
        """
        for symbol in self.symbols:
            assessment = assessments.get(symbol)
            if assessment is None:
                continue
            if assessment.state.is_usable:
                return symbol
            # Not usable, but not a closure either: a feed fault. Hold this symbol rather
            # than falling past it to the next one — a broken gold feed is not a reason to
            # put Bitcoin on the air.
            #
            # UNKNOWN is the exception: a feed that has never produced data is not
            # something to wait on indefinitely at startup, so it falls through.
            if (
                not assessment.state.authorises_fallback
                and assessment.state is not MarketAvailability.UNKNOWN
            ):
                return symbol
        return None

    def _reason_for(
        self,
        current_symbol: str,
        preferred: str,
        assessments: Mapping[str, AvailabilityAssessment],
    ) -> SwitchReason:
        if preferred == NO_ACTIVE_MARKET:
            return SwitchReason.NO_MARKET_AVAILABLE
        if current_symbol == NO_ACTIVE_MARKET:
            return SwitchReason.MARKET_RESTORED
        if preferred == self._settings.primary:
            return SwitchReason.PRIMARY_MARKET_REOPENED
        current = assessments.get(current_symbol)
        if current_symbol == self._settings.primary:
            return SwitchReason.PRIMARY_MARKET_CLOSED
        if current is not None and current.state.authorises_fallback:
            return SwitchReason.FALLBACK_MARKET_CLOSED
        return SwitchReason.PRIMARY_MARKET_CLOSED

    def _window_for(self, reason: SwitchReason) -> float:
        """Confirmation window for this kind of transition.

        Reopening is held longer than closing, deliberately and asymmetrically. Leaving a
        closed market promptly costs nothing — the data has genuinely stopped. Returning
        early, on a straggling pre-open tick, means switching back and forth across the
        boundary, which a listener hears as the station changing its mind.
        """
        if reason is SwitchReason.PRIMARY_MARKET_REOPENED:
            return self._settings.reopen_confirmation_seconds
        return self._settings.switch_confirmation_seconds

    def _refresh_availability(
        self, current: ActiveMarket, assessments: Mapping[str, AvailabilityAssessment]
    ) -> ActiveMarket:
        """Keep the active record's availability current without counting it as a switch."""
        assessment = assessments.get(current.symbol)
        if assessment is None or assessment.state is current.availability:
            return current
        refreshed = ActiveMarket(
            symbol=current.symbol,
            availability=assessment.state,
            is_primary=current.is_primary,
            since=current.since,
            reason=current.reason,
        )
        self._active = refreshed
        return refreshed

    def _commit(
        self,
        symbol: str,
        reason: SwitchReason,
        *,
        now: datetime,
        previous: ActiveMarket | None,
        counts_as_switch: bool = True,
    ) -> RoutingDecision:
        """Make ``symbol`` active.

        ``counts_as_switch`` separates "the station changed which market it programmes
        against" from "the station acquired a market it did not have". Only the first is a
        switch an operator cares about; counting the second would mean every dashboard
        opened reporting a switch that never happened.
        """
        resolved = symbol
        assessment = self._assessments.get(resolved)
        active = ActiveMarket(
            symbol=resolved,
            availability=(
                assessment.state if assessment is not None else MarketAvailability.UNKNOWN
            ),
            is_primary=resolved == self._settings.primary,
            since=now,
            reason=reason,
        )
        self._active = active
        self._active_since_monotonic = self._clock.monotonic()
        self._pending = None
        if previous is not None and counts_as_switch:
            self._switch_count += 1

        _log.info(
            "market.active_symbol_changed",
            previous=None if previous is None else previous.symbol,
            active=active.symbol,
            reason=reason.value,
            availability=active.availability.value,
        )
        return RoutingDecision(
            active=active,
            changed=True,
            previous_symbol=None if previous is None else previous.symbol,
            previous_state=None if previous is None else previous.availability,
            assessments=self._assessments,
        )
