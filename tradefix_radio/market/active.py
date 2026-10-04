"""The station's market input: several feeds, one active answer.

Owns a `MarketDataService` per configured symbol and a `MarketRouter` over them, and
presents the interface the rest of the station already expected — `current_state`,
`feed_status`, `data_age_seconds` — so the scheduler, the director and the API consume an
*active market* without knowing that routing exists.

Why one service per symbol rather than one service that switches feeds
----------------------------------------------------------------------
Each `MarketDataService` carries a `FeatureEngine`, an `EnergyCalculator` and a
`RegimeEngine`, and all three hold rolling state built from the symbol's own history. A
single service pointed at a different feed would carry gold's ATR into Bitcoin's regime
classification and produce a confident reading of a market it had never seen. Separate
services make the "no regime-state contamination between symbols" requirement structural
rather than a rule to remember — and they mean returning to gold returns to gold's own
warmed history, not to a cold start.

The inactive symbol keeps polling. That is the point: availability cannot be assessed for a
market nobody is watching, and the whole reopen path depends on noticing gold come back
while Bitcoin is on air.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.contracts.enums import FeedStatus
from tradefix_radio.contracts.events import (
    ActiveSymbolChanged,
    MarketAvailabilityChanged,
)
from tradefix_radio.core.clock import SystemClock
from tradefix_radio.market.availability import (
    AvailabilityAssessment,
    MarketAvailability,
    assess_availability,
)
from tradefix_radio.market.router import (
    ActiveMarket,
    MarketRouter,
    RoutingDecision,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from tradefix_radio.config.schema import AppSettings
    from tradefix_radio.contracts.market import MarketStateV1
    from tradefix_radio.core.clock import Clock
    from tradefix_radio.market.feeds.base import MarketFeed
    from tradefix_radio.market.service import MarketDataService
    from tradefix_radio.runtime.coordinator import RuntimeCoordinator

_log = structlog.get_logger(__name__)

__all__ = ["ActiveMarketService"]


class ActiveMarketService:
    """Several market services, one routed answer."""

    def __init__(
        self,
        settings: AppSettings,
        services: Mapping[str, MarketDataService],
        *,
        coordinator: RuntimeCoordinator | None = None,
        clock: Clock | None = None,
        router: MarketRouter | None = None,
    ) -> None:
        if not services:
            raise ValueError("ActiveMarketService needs at least one market service")
        self._settings = settings
        self._services = dict(services)
        self._coordinator = coordinator
        self._clock: Clock = clock or SystemClock()
        self._router = router or MarketRouter(settings.markets, clock=self._clock)
        self._previous_states: dict[str, MarketAvailability] = {}
        #: Operator/simulator override, per symbol: ``True`` forced open, ``False`` forced
        #: closed, absent means "believe the feed and the calendar".
        #:
        #: Both directions are needed to exercise routing on a real station. A closure can
        #: be simulated on a trading day by forcing closed, but the reverse — testing the
        #: *reopen* path at a weekend, when the calendar says gold is shut — is impossible
        #: without being able to say "treat this as trading". A one-directional override
        #: can only be rehearsed two days a week.
        self._overrides: dict[str, bool] = {}
        #: Symbols whose feed is simulated as faulted. They are not polled at all.
        self._faulted: set[str] = set()

    # ------------------------------------------------------------ inspection

    @property
    def router(self) -> MarketRouter:
        return self._router

    @property
    def services(self) -> Mapping[str, MarketDataService]:
        return dict(self._services)

    @property
    def active_symbol(self) -> str:
        return self._router.active_symbol

    @property
    def active(self) -> ActiveMarket | None:
        return self._router.active

    @property
    def active_service(self) -> MarketDataService | None:
        return self._services.get(self._router.active_symbol)

    @property
    def current_state(self) -> MarketStateV1 | None:
        """The active market's state — what the director plans against.

        ``None`` when no market is active, which the caller must treat as "do not plan new
        programming", never as "energy zero". The radio keeps playing from its buffer.
        """
        service = self.active_service
        return None if service is None else service.current_state

    @property
    def feed_status(self) -> FeedStatus:
        service = self.active_service
        return FeedStatus.DISCONNECTED if service is None else service.feed_status

    @property
    def data_age_seconds(self) -> float:
        service = self.active_service
        return float("inf") if service is None else service.data_age_seconds()

    @property
    def feed(self) -> MarketFeed | None:
        """The active symbol's feed.

        Here so the §72 scenario control keeps working unchanged: "run a volatility spike"
        means in the market the listener is hearing, not in the one sitting in reserve.
        """
        service = self.active_service
        return None if service is None else service.feed

    @property
    def bars_processed(self) -> int:
        """The active symbol's bar count.

        Part of the informal interface the API's health panel already read off a
        `MarketDataService`; delegating keeps that panel working unchanged when the runner
        hands it an `ActiveMarketService` instead.
        """
        service = self.active_service
        return 0 if service is None else service.bars_processed

    def state_for(self, symbol: str) -> MarketStateV1 | None:
        service = self._services.get(symbol)
        return None if service is None else service.current_state

    def assessments(self) -> Mapping[str, AvailabilityAssessment]:
        return self._router.assessments

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start every feed, then make an initial routing decision."""
        for symbol, service in self._services.items():
            try:
                await service.start()
            except Exception as error:  # noqa: BLE001 - one dead feed must not stop the rest
                _log.warning(
                    "market.feed_start_failed",
                    symbol=symbol,
                    error_type=type(error).__name__,
                    error=str(error),
                    detail="this symbol will be assessed as unavailable",
                )
        await self.evaluate()

    async def stop(self) -> None:
        for symbol, service in self._services.items():
            with contextlib.suppress(Exception):
                await service.stop()
                _log.debug("market.feed_stopped", symbol=symbol)

    async def poll_once(self) -> MarketStateV1 | None:
        """Poll every feed once, re-route, and return the active state.

        Every feed, not only the active one: a market nobody polls cannot be assessed, and
        the reopen path depends on seeing gold return while Bitcoin is on air.
        """
        await asyncio.gather(
            *(self._poll_safely(symbol) for symbol in self._services),
            return_exceptions=True,
        )
        await self.evaluate()
        return self.current_state

    async def _poll_safely(self, symbol: str) -> None:
        if symbol.upper() in self._faulted:
            # A faulted feed is simply not polled. Stopping the service is not enough on
            # its own: the station drives polling from its own loop and calls straight
            # through to every service, so a service whose internal task is cancelled goes
            # right on producing bars. Skipping here is what "no data is arriving" means.
            return
        service = self._services[symbol]
        try:
            await service.poll_once()
        except Exception as error:  # noqa: BLE001 - a feed fault is data, not a crash
            _log.debug(
                "market.poll_failed",
                symbol=symbol,
                error_type=type(error).__name__,
                error=str(error),
            )

    # -------------------------------------------------------------- routing

    def force_closed(self, symbol: str, *, closed: bool = True) -> None:
        """Simulator/operator override: treat a symbol as closed, or clear the override.

        Exists so the market-switch path can be exercised on a Wednesday afternoon without
        waiting for Friday. It overrides the *calendar*, never the feed: a symbol forced
        closed is reported CLOSED with a reason naming the override, so nobody reading the
        Market page mistakes a test for a real closure — and, critically, it never sets
        `feed_degraded`, so the closed-versus-broken distinction survives simulation.

        ``closed=False`` clears the override and returns the symbol to whatever the feed
        and the calendar actually say. See :meth:`force_open` to assert the opposite.
        """
        if closed:
            self._overrides[symbol.upper()] = False
        else:
            self._overrides.pop(symbol.upper(), None)
        _log.info(
            "market.forced_closed" if closed else "market.override_cleared",
            symbol=symbol,
            detail="operator/simulator override",
        )

    def force_open(self, symbol: str, *, open_: bool = True) -> None:
        """Simulator/operator override: treat a symbol as trading.

        The mirror of :meth:`force_closed`, and it exists for one reason: the reopen path
        cannot otherwise be tested at a weekend. Gold is shut from Friday evening to Sunday
        evening, so for most of any testing window "wait for it to reopen" is not a thing
        that can be made to happen.

        It overrides the calendar only. A symbol forced open whose feed has stopped still
        goes STALE and then UNAVAILABLE, still reports `feed_degraded`, and still does not
        authorise a fallback — which is exactly the scenario this override exists to let
        someone stage on a Saturday.
        """
        if open_:
            self._overrides[symbol.upper()] = True
        else:
            self._overrides.pop(symbol.upper(), None)
        _log.info(
            "market.forced_open" if open_ else "market.override_cleared",
            symbol=symbol,
            detail="operator/simulator override",
        )

    async def set_feed_enabled(self, symbol: str, *, enabled: bool) -> bool:
        """Stop or restart one symbol's feed, simulating a broker outage.

        The control that makes the most important routing property testable on a running
        station. "The market is closed" can be staged by overriding the calendar; "the feed
        has died while the market is trading" cannot be staged any other way, and it is the
        case the whole subsystem exists to handle correctly — a silent feed must never be
        read as a closure.

        Stopping the service rather than muting the feed is deliberate: it is what a broker
        disconnect actually looks like from here. The service stops polling, its data ages,
        and the availability assessment reaches STALE and then UNAVAILABLE on its own,
        through exactly the code path a real outage would take.
        """
        service = self._services.get(symbol.upper())
        if service is None:
            return False
        if enabled:
            self._faulted.discard(symbol.upper())
            await service.start()
        else:
            # Both: stop the service's own polling task *and* mark it faulted so the
            # station's loop stops calling into it. Either alone leaves data flowing.
            self._faulted.add(symbol.upper())
            await service.stop()
        _log.warning(
            "market.feed_restarted" if enabled else "market.feed_stopped_by_operator",
            symbol=symbol,
            detail="operator/simulator override; this is a feed fault, not a closure",
        )
        return True

    @property
    def faulted_feeds(self) -> frozenset[str]:
        """Symbols whose feed is simulated as broken."""
        return frozenset(self._faulted)

    @property
    def overrides(self) -> dict[str, bool]:
        """Per-symbol overrides: True forced open, False forced closed."""
        return dict(self._overrides)

    @property
    def forced_closed(self) -> frozenset[str]:
        return frozenset(s for s, is_open in self._overrides.items() if not is_open)

    async def evaluate(self) -> RoutingDecision:
        """Assess every symbol, route, and publish whatever changed."""
        now = self._clock.now()
        assessments: dict[str, AvailabilityAssessment] = {}

        for symbol, service in self._services.items():
            override = self._overrides.get(symbol.upper())
            assessment = assess_availability(
                symbol=symbol,
                now=now,
                settings=self._settings.markets.for_symbol(symbol),
                data_age_seconds=(
                    None if service.bars_processed == 0 else service.data_age_seconds()
                ),
                feed_status=service.feed_status,
                bars_processed=service.bars_processed,
                # Forced *closed* is expressed as the provider reporting a closure, which
                # is the highest-priority branch. Forced *open* overrides only the calendar
                # verdict, so every staleness rule below it still runs — which is the whole
                # point of the override: it is what lets "open market, dead feed" be staged.
                feed_reported_closed=True if override is False else None,
                calendar_open_override=True if override is True else None,
            )
            if override is False:
                assessment = AvailabilityAssessment(
                    symbol=assessment.symbol,
                    state=MarketAvailability.CLOSED,
                    reason="forced closed by operator/simulator override",
                    data_age_seconds=assessment.data_age_seconds,
                    feed_status=assessment.feed_status,
                    calendar_open=assessment.calendar_open,
                    assessed_at=now,
                    feed_degraded=False,
                    extras={"forced": "closed"},
                )
            elif override is True:
                # The assessment already ran with the calendar forced open, so its state is
                # whatever the feed's health actually warrants — OPEN, STALE or UNAVAILABLE.
                # Only the reason is annotated, so the Market page says why the calendar was
                # ignored without overwriting what the data says.
                assessment = AvailabilityAssessment(
                    symbol=assessment.symbol,
                    state=assessment.state,
                    reason=f"{assessment.reason} (calendar forced open by operator)",
                    data_age_seconds=assessment.data_age_seconds,
                    feed_status=assessment.feed_status,
                    calendar_open=True,
                    assessed_at=now,
                    feed_degraded=assessment.feed_degraded,
                    extras={**assessment.extras, "forced": "open"},
                )
            assessments[symbol] = assessment

        await self._publish_availability_changes(assessments)
        decision = self._router.evaluate(assessments, now=now)
        if decision.changed:
            await self._publish_switch(decision)
        return decision

    async def _publish_availability_changes(
        self, assessments: Mapping[str, AvailabilityAssessment]
    ) -> None:
        for symbol, assessment in assessments.items():
            previous = self._previous_states.get(symbol)
            if previous is assessment.state:
                continue
            self._previous_states[symbol] = assessment.state
            if previous is None:
                # First observation of this symbol. Recorded, not announced: every symbol
                # transitions from "nothing known" at startup and a burst of events there
                # would train an operator to ignore the topic.
                continue
            _log.info(
                "market.availability_changed",
                symbol=symbol,
                previous=previous.value,
                new=assessment.state.value,
                reason=assessment.reason,
                feed_degraded=assessment.feed_degraded,
            )
            if self._coordinator is not None:
                await self._coordinator.publish(
                    MarketAvailabilityChanged(
                        at=self._clock.now(),
                        symbol=symbol,
                        previous_state=previous.value,
                        new_state=assessment.state.value,
                        reason=assessment.reason[:240],
                        feed_degraded=assessment.feed_degraded,
                    )
                )

    async def _publish_switch(self, decision: RoutingDecision) -> None:
        if self._coordinator is None:
            return
        await self._coordinator.publish(
            ActiveSymbolChanged(
                at=decision.active.since,
                previous_symbol=decision.previous_symbol,
                new_symbol=decision.active.symbol,
                reason=decision.active.reason.value,
                previous_state=(
                    None if decision.previous_state is None else decision.previous_state.value
                ),
                new_state=decision.active.availability.value,
            )
        )

    # ------------------------------------------------------------- reporting
    #
    # There is deliberately no `as_payload()` here. The Control Center's rendering lives in
    # `api/snapshot.routing_to_dto`, which builds a typed DTO from the attributes below; a
    # second dict-shaped rendering in this module would be a parallel contract that drifts
    # the first time a field is added to one and not the other.

    @property
    def router_state(self) -> MarketRouter:
        """The routing decision state, for the API boundary to render."""
        return self._router

    def last_price(self, symbol: str) -> float | None:
        service = self._services.get(symbol)
        state = None if service is None else service.current_state
        return None if state is None else state.price

    def bars_for(self, symbol: str) -> int:
        service = self._services.get(symbol)
        return 0 if service is None else service.bars_processed
