"""Market data service — feed to MarketState (§4, §63-E).

Owns the pipeline:

    feed -> bar aggregation -> features -> energy -> regime -> MarketState

and the honesty rules around it. Three behaviours matter more than the plumbing:

**Staleness is a first-class state.** Data age is measured against the injected
clock, and the resulting :class:`FeedStatus` gates everything downstream. §32 and
§86 forbid presenting non-live data as a current price, and
:class:`~tradefix_radio.contracts.market.MarketStateV1` makes it unrepresentable —
so the service simply omits the price once the feed is no longer LIVE.

**A dead feed does not stop the station.** §63-E requires last-good-state retention
plus a stale indicator plus "safe neutral programming". When the feed disconnects the
service keeps publishing state, but neutralised: no regime claim, no direction, mid
energy. The radio keeps broadcasting; it just stops asserting things it cannot know.

**Events are published sparingly.** A regime change is an event; a tick is not. Energy
updates are throttled to a configurable interval, because an event per tick would make
the bus the busiest thing in the process and bury the events that matter.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

import structlog

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import FeedStatus, MarketDirection, MarketRegime
from tradefix_radio.contracts.events import (
    MarketEnergyChanged,
    MarketFeedStatusChanged,
    MarketStateChanged,
)
from tradefix_radio.contracts.market import (
    MarketEnergyV1,
    MarketFeaturesV1,
    MarketSnapshotV1,
    MarketStateV1,
)
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import MarketDataError
from tradefix_radio.core.events import EventBus
from tradefix_radio.market.energy import EnergyCalculator
from tradefix_radio.market.features import FeatureEngine
from tradefix_radio.market.feeds.base import MarketFeed
from tradefix_radio.market.regimes import RegimeDecision, RegimeEngine
from tradefix_radio.market.rolling import clamp
from tradefix_radio.market.sessions import classify_session

_log = structlog.get_logger(__name__)

#: Energy must move at least this many points to warrant an event.
ENERGY_EVENT_DELTA = 3.0

#: Heartbeat: publish anyway after this many bars, even if energy barely moved, so a
#: consumer that missed an event still refreshes.
#:
#: Counted in **bars, not seconds**. An earlier version used a 10-second wall-clock
#: gate, which was dead logic: ``_publish`` is only reached when a bar closes, and bars
#: are 60 seconds apart by default, so the gate was always already satisfied and every
#: single bar published an event. Throttling on a time axis finer than the publisher's
#: own cadence cannot throttle anything.
ENERGY_EVENT_HEARTBEAT_BARS = 10


class MarketDataService:
    """Drives a feed and publishes :class:`MarketStateV1`."""

    def __init__(
        self,
        settings: AppSettings,
        feed: MarketFeed,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._feed = feed
        self._bus = bus
        self._clock: Clock = clock or SystemClock()

        self._features = FeatureEngine(settings.market)
        self._energy = EnergyCalculator(settings.energy)
        self._regimes = RegimeEngine(settings.regime)

        self._last_snapshot: MarketSnapshotV1 | None = None
        self._last_snapshot_monotonic: float | None = None
        self._last_features: MarketFeaturesV1 | None = None
        self._last_energy: MarketEnergyV1 | None = None
        self._state: MarketStateV1 | None = None
        self._feed_status = FeedStatus.DISCONNECTED
        self._last_energy_event_value: float | None = None
        self._bars_since_energy_event = 0

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._bars_processed = 0
        self._poll_errors = 0

    # -- introspection -----------------------------------------------------

    @property
    def feed(self) -> MarketFeed:
        return self._feed

    @property
    def current_state(self) -> MarketStateV1 | None:
        return self._state

    @property
    def current_features(self) -> MarketFeaturesV1 | None:
        return self._last_features

    @property
    def current_energy(self) -> MarketEnergyV1 | None:
        return self._last_energy

    @property
    def feed_status(self) -> FeedStatus:
        return self._feed_status

    @property
    def bars_processed(self) -> int:
        return self._bars_processed

    @property
    def poll_errors(self) -> int:
        return self._poll_errors

    @property
    def regime_transitions(self) -> int:
        """Adopted regime changes. The §43 timeline and §56 metrics use this."""
        return self._regimes.transition_count

    def data_age_seconds(self) -> float:
        """Seconds since the last snapshot, measured monotonically.

        Monotonic rather than wall-clock: NTP corrections and DST shifts move
        ``now()`` and would make a healthy feed momentarily look hours stale, tripping
        §63-E for no reason.
        """
        if self._last_snapshot_monotonic is None:
            return float("inf")
        return max(0.0, self._clock.monotonic() - self._last_snapshot_monotonic)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the feed and begin polling in a background task."""
        if self._task is not None:
            return
        await self._feed.open()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="market-data-service")
        _log.info(
            "market.service_started",
            feed=self._feed.name,
            symbol=self._feed.resolved_symbol,
            simulated=self._feed.is_simulated,
        )

    async def stop(self) -> None:
        """Stop polling and close the feed (§74)."""
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._feed.close()
        _log.info(
            "market.service_stopped",
            bars_processed=self._bars_processed,
            regime_transitions=self.regime_transitions,
        )

    async def _run(self) -> None:
        interval = self._settings.market.poll_interval_seconds
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except MarketDataError as exc:
                # A broken feed is an expected operational condition, not a bug. The
                # station stays up and reports DISCONNECTED (§63-E).
                self._poll_errors += 1
                _log.warning(
                    "market.poll_failed",
                    feed=self._feed.name,
                    error=str(exc),
                    poll_errors=self._poll_errors,
                )
            await self._clock.sleep(interval)

    # -- the pipeline ------------------------------------------------------

    async def poll_once(self) -> MarketStateV1 | None:
        """Poll the feed once and refresh state.

        Returns the new state when a bar closed, otherwise ``None``. State is still
        *updated* on every call — staleness must advance even when no data arrives,
        which is the whole point of §63-E.
        """
        snapshot = await self._feed.poll()
        if snapshot is not None:
            self._accept(snapshot)
        await self._refresh_status()
        if snapshot is None:
            return None

        features = self._features.push_snapshot(snapshot)
        if features is None:
            # Mid-bar. Refresh the state's age and price but not the classification.
            self._state = self._rebuild_state()
            return None

        self._bars_processed += 1
        self._last_features = features
        energy = self._energy.compute(features)
        self._last_energy = energy
        decision = self._regimes.classify(features, now=self._clock.now())

        previous = self._state
        self._state = self._assemble(features, energy, decision)
        await self._publish(previous, decision, energy)
        return self._state

    def _accept(self, snapshot: MarketSnapshotV1) -> None:
        self._last_snapshot = snapshot
        self._last_snapshot_monotonic = self._clock.monotonic()

    async def _refresh_status(self) -> None:
        """Recompute :class:`FeedStatus` from data age and publish on change."""
        age = self.data_age_seconds()
        market = self._settings.market
        if self._feed.is_simulated and self._last_snapshot is not None:
            status = FeedStatus.SIMULATED
        elif self._last_snapshot is None or age >= market.disconnected_after_seconds:
            status = FeedStatus.DISCONNECTED
        elif age >= market.stale_after_seconds:
            status = FeedStatus.STALE
        else:
            status = FeedStatus.LIVE

        if status is self._feed_status:
            return
        previous, self._feed_status = self._feed_status, status
        _log.info(
            "market.feed_status_changed",
            feed=self._feed.name,
            previous=previous.value,
            status=status.value,
            data_age_seconds=round(age, 2) if age != float("inf") else None,
        )
        # The state must reflect the new status immediately, because a transition out
        # of LIVE has to drop the price in the same breath.
        if self._state is not None:
            self._state = self._rebuild_state()
        if self._bus is not None:
            await self._bus.publish(
                MarketFeedStatusChanged(
                    at=self._clock.now(),
                    symbol=self._feed.resolved_symbol,
                    status=status.value,
                    previous_status=previous.value,
                    data_age_seconds=0.0 if age == float("inf") else age,
                )
            )

    def _assemble(
        self,
        features: MarketFeaturesV1,
        energy: MarketEnergyV1,
        decision: RegimeDecision,
    ) -> MarketStateV1:
        """Build the state object the music director consumes."""
        now = self._clock.now()
        age = self.data_age_seconds()
        state = MarketStateV1(
            symbol=self._feed.resolved_symbol,
            timestamp=now,
            regime=decision.regime,
            direction=decision.direction,
            session=classify_session(now),
            feed_status=self._feed_status,
            energy=energy.smoothed_energy,
            energy_velocity=energy.energy_velocity,
            volatility=features.atr_percentile,
            trend_strength=features.trend_strength,
            momentum=_momentum_score(features.momentum),
            compression=features.compression_score,
            confidence=decision.confidence,
            regime_age_seconds=decision.age_seconds,
            data_age_seconds=0.0 if age == float("inf") else age,
            price=self._displayable_price(),
        )
        # A disconnected feed yields neutral programming rather than programming
        # driven by a frozen snapshot that may be hours old (§63-E).
        if self._feed_status is FeedStatus.DISCONNECTED:
            return state.neutralised()
        return state

    def _rebuild_state(self) -> MarketStateV1 | None:
        """Refresh the volatile parts of state without reclassifying.

        Used between bars and on a status change. Rebuilding rather than mutating,
        because contracts are frozen — and that immutability is what guarantees three
        subsystems holding the same state see identical values.
        """
        if self._state is None:
            return None
        age = self.data_age_seconds()
        refreshed = self._state.model_copy(
            update={
                "timestamp": self._clock.now(),
                "feed_status": self._feed_status,
                "data_age_seconds": 0.0 if age == float("inf") else age,
                "price": self._displayable_price(),
                "session": classify_session(self._clock.now()),
            }
        )
        if self._feed_status is FeedStatus.DISCONNECTED:
            return refreshed.neutralised()
        return refreshed

    def _displayable_price(self) -> float | None:
        """The price, but only when the feed status permits showing one (§32).

        Returning ``None`` is a meaningful answer — "we do not know the price right
        now" — and the contract rejects any attempt to carry a price alongside a
        non-LIVE status, so this cannot be bypassed.
        """
        if not self._feed_status.is_trustworthy_for_price_display:
            return None
        if self._last_snapshot is None:
            return None
        return self._last_snapshot.mid

    # -- events ------------------------------------------------------------

    async def _publish(
        self,
        previous: MarketStateV1 | None,
        decision: RegimeDecision,
        energy: MarketEnergyV1,
    ) -> None:
        if self._bus is None or self._state is None:
            return

        session_changed = previous is not None and previous.session is not self._state.session
        if decision.changed or previous is None or session_changed:
            await self._bus.publish(
                MarketStateChanged(
                    at=self._clock.now(),
                    state=self._state,
                    previous_regime=(
                        decision.previous_regime.value
                        if decision.previous_regime is not None
                        else None
                    ),
                )
            )
            if decision.changed:
                _log.info(
                    "market.regime_changed",
                    previous=decision.previous_regime.value
                    if decision.previous_regime
                    else None,
                    regime=decision.regime.value,
                    confidence=round(decision.confidence, 3),
                    energy=round(energy.smoothed_energy, 1),
                    direction=decision.direction.value,
                )

        self._bars_since_energy_event += 1
        if self._should_publish_energy(energy.smoothed_energy):
            self._bars_since_energy_event = 0
            self._last_energy_event_value = energy.smoothed_energy
            await self._bus.publish(
                MarketEnergyChanged(
                    at=self._clock.now(),
                    symbol=self._feed.resolved_symbol,
                    energy=energy.raw_energy,
                    energy_velocity=energy.energy_velocity,
                    smoothed_energy=energy.smoothed_energy,
                )
            )

    def _should_publish_energy(self, value: float) -> bool:
        """Publish on a material move, or as a periodic heartbeat.

        Magnitude is the primary gate: a quiet session should be quiet on the bus too.
        The bar-counted heartbeat exists so a consumer that missed an event — a
        reconnecting overlay, a restarted worker — still refreshes within a known
        bound instead of waiting for the market to move.
        """
        if self._last_energy_event_value is None:
            return True
        if abs(value - self._last_energy_event_value) >= ENERGY_EVENT_DELTA:
            return True
        return self._bars_since_energy_event >= ENERGY_EVENT_HEARTBEAT_BARS

    # -- control -----------------------------------------------------------

    def reset(self) -> None:
        """Clear derived state, keeping the feed. Used when switching scenarios."""
        self._features.reset()
        self._energy.reset()
        self._regimes.reset()
        self._last_features = None
        self._last_energy = None
        self._state = None
        self._bars_processed = 0
        self._last_energy_event_value = None
        self._bars_since_energy_event = 0

    def neutral_state(self, now: datetime | None = None) -> MarketStateV1:
        """A safe, claim-free state for use before any data has arrived (§63-E).

        The music director needs *something* to work from at startup, and this is the
        honest version of "nothing": mid energy, no regime, no direction, no price.
        """
        moment = now or self._clock.now()
        return MarketStateV1(
            symbol=self._feed.resolved_symbol,
            timestamp=moment,
            regime=MarketRegime.UNKNOWN,
            direction=MarketDirection.NEUTRAL,
            session=classify_session(moment),
            feed_status=self._feed_status,
            energy=50.0,
            energy_velocity=0.0,
            volatility=50.0,
            trend_strength=0.0,
            momentum=50.0,
            compression=50.0,
            confidence=0.0,
            regime_age_seconds=0.0,
            data_age_seconds=0.0,
            price=None,
        )


def _momentum_score(fractional_momentum: float) -> float:
    """Map a signed fractional momentum to a 0–100 score centred on 50.

    The features layer keeps momentum signed because direction matters there; the
    state layer needs an unsigned intensity for the §43 panel and the music director,
    with 50 meaning "no momentum". A 0.6 % five-bar move saturates the scale.
    """
    full_scale = 0.006
    scaled = 50.0 + 50.0 * max(-1.0, min(1.0, fractional_momentum / full_scale))
    return clamp(scaled)


__all__ = [
    "ENERGY_EVENT_DELTA",
    "ENERGY_EVENT_HEARTBEAT_BARS",
    "MarketDataService",
]
