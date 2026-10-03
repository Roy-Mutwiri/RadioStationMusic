"""The visual state bridge: the station's live frame, turned into `VisualStateV1`.

```
TRADE FIX RADIO (station + api)
        |  existing /ws -- LiveStateV1 @ 2 Hz, read-only
        v
   VisualStateBridge        <- this module
        |  VisualStateV1
        v
   BehaviorDirector
```

**The station does not change.** Everything the visual layer needs is already published
by `api/live.py`'s `LiveHub`, so the bridge is a WebSocket *client* of an endpoint that
already exists. That is the whole reason the brief's rule — *the radio should not know
how character animation works* — costs nothing: the radio already does not know, and
adding nothing to it is how it stays that way.

Three properties this module is responsible for
-----------------------------------------------
**It is the only place market vocabulary exists.** `MarketRegime` is imported here and
nowhere else under `visual/`. Below the bridge there is only
:class:`~tradefix_radio.visual.contracts.IntensityBand`. A fifteenth regime is one line
in :data:`_BAND_OF_REGIME`.

**Nullable, never zero-filled.** `music_bpm` of `None` means nothing is playing; a zero
would mean playing at 0 BPM. The brief is explicit that unavailable values must not be
replaced with fake zeros, and the behavioural consequence is real — a zero energy reads
as a calm market rather than as an absent one.

**Read-only.** No database session, no event publication, no station mutation. Asserted
structurally by `tests/unit/test_visual_isolation.py` rather than left to discipline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final

import structlog

from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
    VocalStyle,
)
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.visual.contracts import (
    NO_ACTIVE_MARKET,
    FeedTrust,
    IntensityBand,
    StationMode,
    TransitionState,
    VisualStateV1,
    band_from_index,
)
from tradefix_radio.visual.modulation import behavior_energy

_log = structlog.get_logger(__name__)

# ============================================================ the band mapping
#
# THE ONLY PLACE A MARKET REGIME IS READ. Everything downstream sees a band.

_BAND_OF_REGIME: Final[dict[MarketRegime, IntensityBand]] = {
    MarketRegime.UNKNOWN: IntensityBand.B0_DORMANT,
    MarketRegime.QUIET: IntensityBand.B1_QUIET,
    MarketRegime.LOW_VOLATILITY_RANGE: IntensityBand.B1_QUIET,
    MarketRegime.NORMAL_RANGE: IntensityBand.B2_STEADY,
    MarketRegime.COMPRESSION: IntensityBand.B2_STEADY,
    MarketRegime.POST_EVENT_NORMALIZATION: IntensityBand.B2_STEADY,
    MarketRegime.BULLISH_TREND: IntensityBand.B3_FOCUSED,
    MarketRegime.BEARISH_TREND: IntensityBand.B3_FOCUSED,
    MarketRegime.BREAKOUT_BUILDUP: IntensityBand.B3_FOCUSED,
    MarketRegime.BULLISH_BREAKOUT: IntensityBand.B4_ALERT,
    MarketRegime.BEARISH_BREAKOUT: IntensityBand.B4_ALERT,
    MarketRegime.HIGH_VOLATILITY_RANGE: IntensityBand.B4_ALERT,
    MarketRegime.REVERSAL: IntensityBand.B4_ALERT,
    MarketRegime.EXTREME_VOLATILITY: IntensityBand.B5_PEAK,
}

#: A band may move at most one step per this interval. Regime classification is already
#: minimum-duration-constrained upstream, but the extra damping is nearly free and stops
#: a flapping classification producing a character who oscillates between calm and
#: alert — a far more visible artefact than the classification itself.
BAND_STEP_INTERVAL_SECONDS: Final = 20.0
#: A *downward* move additionally requires the new band to hold this long. Calming down
#: slowly and waking up quickly is both more realistic and more forgiving of noise.
BAND_DOWNWARD_HOLD_SECONDS: Final = 45.0

# ============================================================ degradation ladder
#
# The brief: if Trade Fix Radio disappears, do not freeze mid-action. These are the
# windows. Below five seconds nothing changes at all — that is shorter than three frame
# intervals, and a socket blip must not be visible in the performance.

DEGRADE_HOLD_SECONDS: Final = 5.0
DEGRADE_DECAY_SECONDS: Final = 20.0
DEGRADE_DORMANT_SECONDS: Final = 60.0

#: Reconnect backoff, seconds. Indefinite: a 24/7 visual layer must keep trying.
RECONNECT_BACKOFF: Final = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

# ============================================================ salience

#: Window over which the energy delta is measured, seconds.
SALIENCE_WINDOW_SECONDS: Final = 30.0
#: Energy points over the window that count as a full-magnitude move.
SALIENCE_ENERGY_SCALE: Final = 22.0
#: Velocity, points per minute, that counts as fully fast.
SALIENCE_VELOCITY_SCALE: Final = 8.0

_W_ENERGY_DELTA: Final = 0.40
_W_REGIME_CHANGE: Final = 0.30
_W_VELOCITY: Final = 0.20
_W_CONFIDENCE: Final = 0.10

#: How long a symbol change stays visible to the director as a switch.
SWITCH_VISIBILITY_SECONDS: Final = 30.0


def _as_dict(frame: Any) -> dict[str, Any]:
    """Accept a `LiveStateV1`, a dict, or a JSON string.

    Three forms because three callers: the WebSocket delivers JSON text, tests build
    dicts, and an in-process wiring could hand over the DTO itself. Normalising here
    keeps the derivation logic single-form.
    """
    if isinstance(frame, str):
        parsed = json.loads(frame)
        if not isinstance(parsed, dict):
            raise TypeError("a live frame must decode to an object")
        return parsed
    if isinstance(frame, dict):
        return dict(frame)
    dump = getattr(frame, "model_dump", None)
    if dump is not None:
        return dict(dump(mode="json"))
    raise TypeError(f"cannot read a live frame from {type(frame).__name__}")


def _enum(value: Any, enum_type: Any) -> Any | None:
    """Parse an enum member from its serialised value, tolerating absence."""
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        _log.warning("visual.bridge.unknown_enum", enum=enum_type.__name__, value=value)
        return None


@dataclass
class _BandGate:
    """Rate-limits band movement. See :data:`BAND_STEP_INTERVAL_SECONDS`."""

    current: IntensityBand = IntensityBand.B2_STEADY
    last_change: float = 0.0
    pending: IntensityBand | None = None
    pending_since: float = 0.0

    def apply(self, requested: IntensityBand, now: float) -> IntensityBand:
        if requested is self.current:
            self.pending = None
            return self.current
        if self.pending is not requested:
            self.pending = requested
            self.pending_since = now
        if now - self.last_change < BAND_STEP_INTERVAL_SECONDS:
            return self.current
        downward = requested.rank < self.current.rank
        if downward and now - self.pending_since < BAND_DOWNWARD_HOLD_SECONDS:
            return self.current
        # One step at a time, so a two-band jump takes two intervals.
        step = 1 if requested.rank > self.current.rank else -1
        self.current = band_from_index(self.current.rank + step)
        self.last_change = now
        if self.current is requested:
            self.pending = None
        return self.current

    def force(self, band: IntensityBand, now: float) -> IntensityBand:
        """Set a band immediately, bypassing the gate. Used for the stale override."""
        self.current = band
        self.last_change = now
        self.pending = None
        return band


@dataclass
class BridgeStats:
    """What the control page shows about the bridge itself."""

    frames_received: int = 0
    frames_rejected: int = 0
    reconnects: int = 0
    last_frame_monotonic: float | None = None
    last_error: str | None = None
    connected: bool = False


class VisualStateBridge:
    """Turns station frames into `VisualStateV1`, and degrades honestly without them.

    Stateful by necessity — band damping, salience and the symbol-switch window are all
    differences across frames — but the state is small and entirely derived. Nothing here
    persists.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._band = _BandGate()
        self._energy: deque[tuple[float, float]] = deque()
        self._prev_band: IntensityBand | None = None
        self._prev_confidence: float | None = None
        self._symbol: str | None = None
        self._symbol_changed_at: float | None = None
        self._last_frame_monotonic: float | None = None
        self._last_state: VisualStateV1 | None = None
        self.stats = BridgeStats()

    # ================================================== derivation

    def derive(self, frame: Any) -> VisualStateV1:
        """Build a `VisualStateV1` from one station frame."""
        now = self._clock.monotonic()
        at = self._clock.now()
        raw = _as_dict(frame)

        market = raw.get("market") or {}
        routing = raw.get("routing") or {}
        playing = raw.get("now_playing") or {}
        status = raw.get("status") or {}
        buffer_ = raw.get("buffer") or {}
        emergency = raw.get("emergency") or {}

        self._last_frame_monotonic = now
        self.stats.frames_received += 1
        self.stats.last_frame_monotonic = now

        # -- symbol and the switch window
        symbol = routing.get("active_symbol") or market.get("symbol") or NO_ACTIVE_MARKET
        if routing.get("has_active_market") is False:
            symbol = NO_ACTIVE_MARKET
        if self._symbol is not None and symbol != self._symbol:
            self._symbol_changed_at = now
            _log.info("visual.bridge.symbol_changed", was=self._symbol, now=symbol)
        self._symbol = symbol
        switched_ago = (
            None if self._symbol_changed_at is None else now - self._symbol_changed_at
        )

        # -- feed trust, which gates everything market-shaped
        feed_status = _enum(market.get("feed_status"), FeedStatus)
        session = _enum(market.get("session"), TradingSession)
        trust = self._trust_from(feed_status, float(market.get("data_age_seconds", 0.0) or 0.0))

        regime = _enum(market.get("regime"), MarketRegime)
        requested = (
            _BAND_OF_REGIME.get(regime, IntensityBand.B0_DORMANT)
            if regime
            else IntensityBand.B0_DORMANT
        )

        # Override 1: anything we cannot interpret forces dormant, immediately, bypassing
        # the damping gate.
        #
        # Three cases, one rule. A stale feed means the regime may be hours old. No active
        # market means there is no regime. An absent or unparseable regime means the
        # station reported something this version does not understand — and letting the
        # gate hold the previous band through that would have the character behaving as
        # though the market were steady while we have no idea what it is doing, which is
        # the same class of error as presenting a stale price as current.
        if trust is FeedTrust.STALE or symbol == NO_ACTIVE_MARKET or regime is None:
            band = self._band.force(IntensityBand.B0_DORMANT, now)
        else:
            # Override 2: a closed session caps at quiet. Gold is not continuously
            # traded; he may be at the desk during the weekend gap, but he is not
            # reacting to a market that is shut.
            if session is TradingSession.CLOSED and requested.rank > 1:
                requested = IntensityBand.B1_QUIET
            band = self._band.apply(requested, now)

        energy = _float_or_none(market.get("energy"))
        velocity = _float_or_none(market.get("energy_velocity"))
        confidence = _float_or_none(market.get("regime_confidence"))

        salience, components = self._salience(
            now, band=band, energy=energy, velocity=velocity, confidence=confidence
        )
        if trust is not FeedTrust.LIVE or band is IntensityBand.B0_DORMANT:
            # Not merely unlikely — zero. A reaction with no market event behind it is
            # the visual equivalent of a fabricated price.
            salience, components = 0.0, {}

        drive = behavior_energy(
            band=band,
            market_energy=energy,
            energy_velocity=velocity,
            music_energy=_float_or_none(playing.get("planned_energy")),
        )

        state = VisualStateV1(
            at=at,
            source_age_seconds=max(0.0, float(market.get("data_age_seconds", 0.0) or 0.0)),
            feed_trust=trust,
            degraded_reason=None if trust is FeedTrust.LIVE else f"feed_status={feed_status}",
            active_symbol=symbol,
            symbol_changed_seconds_ago=switched_ago,
            market_regime=(regime.value if regime and symbol != NO_ACTIVE_MARKET else None),
            intensity_band=band,
            market_energy=energy if trust is not FeedTrust.STALE else None,
            market_energy_velocity=velocity if trust is not FeedTrust.STALE else None,
            market_direction=_enum(market.get("direction"), MarketDirection),
            market_confidence=confidence,
            market_health=(feed_status.value if feed_status else None),
            session=(session.value if session else None),
            music_bpm=_int_or_none(playing.get("bpm")),
            music_energy=_float_or_none(playing.get("planned_energy")),
            music_genre=playing.get("genre"),
            vocal_style=_enum(playing.get("vocal_style"), VocalStyle),
            track_progress=_float_or_none(playing.get("progress")),
            transition_state=_transition_state(playing),
            station_mode=self._station_mode(
                status=status, buffer=buffer_, emergency=emergency, switched_ago=switched_ago
            ),
            emergency_tier=emergency.get("tier"),
            broadcasting=bool(status.get("is_broadcasting", False)),
            behavior_energy=drive,
            reaction_salience=salience,
            salience_components=components,
        )
        self._last_state = state
        return state

    # ================================================== degradation

    def degraded_state(self) -> VisualStateV1:
        """What to hand the director when no frame has arrived recently.

        The ladder, and the reasoning for each rung:

        0-5 s      hold the last state. Shorter than three frame intervals; a blip must
                   not be visible in the performance.
        5-20 s     mark degraded and decay the band toward steady. Not straight to
                   dormant — a brief gap should not visibly change what he is doing, and
                   steady is the honest "working, no strong claim" posture.
        20-60 s    force dormant. Charts stop advancing; reactions are already disabled.
        60 s+      neutral idle. He keeps breathing, blinking, reading and drinking
                   coffee. He simply has nothing to react to.
        """
        now = self._clock.monotonic()
        at = self._clock.now()
        last = self._last_frame_monotonic
        gap = float("inf") if last is None else now - last

        if self._last_state is not None and gap <= DEGRADE_HOLD_SECONDS:
            return self._last_state

        if gap >= DEGRADE_DORMANT_SECONDS or self._last_state is None:
            return VisualStateV1.neutral(
                at=at, reason=f"no station frame for {gap:.0f}s" if last else "never connected"
            )

        if gap >= DEGRADE_DECAY_SECONDS:
            band = self._band.force(IntensityBand.B0_DORMANT, now)
            return self._last_state.model_copy(
                update={
                    "at": at,
                    "feed_trust": FeedTrust.STALE,
                    "degraded_reason": f"no station frame for {gap:.0f}s",
                    "intensity_band": band,
                    "market_energy": None,
                    "market_energy_velocity": None,
                    "reaction_salience": 0.0,
                    "salience_components": {},
                    "source_age_seconds": gap,
                }
            )

        # 5-20 s: decay toward steady, keep working.
        decayed = _decay_toward(self._last_state.intensity_band, IntensityBand.B2_STEADY)
        self._band.force(decayed, now)
        return self._last_state.model_copy(
            update={
                "at": at,
                "feed_trust": FeedTrust.DEGRADED,
                "degraded_reason": f"no station frame for {gap:.0f}s",
                "intensity_band": decayed,
                "reaction_salience": 0.0,
                "salience_components": {},
                "source_age_seconds": gap,
            }
        )

    def state_or_degraded(self) -> VisualStateV1:
        """The current best state. What a caller polls when it does not drive frames."""
        return self.degraded_state()

    # ================================================== internals

    def _trust_from(self, feed_status: FeedStatus | None, data_age: float) -> FeedTrust:
        if feed_status is None:
            return FeedTrust.STALE
        if feed_status in (FeedStatus.DISCONNECTED, FeedStatus.STALE):
            return FeedTrust.STALE
        # SIMULATED is trustworthy *for behaviour* — it is real, coherent, synthetic
        # data, and the station's own rule is that it must be visibly badged rather than
        # hidden. The badge is the renderer's job; the behaviour may use it.
        if data_age > DEGRADE_DECAY_SECONDS:
            return FeedTrust.DEGRADED
        return FeedTrust.LIVE

    def _station_mode(
        self,
        *,
        status: dict[str, Any],
        buffer: dict[str, Any],
        emergency: dict[str, Any],
        switched_ago: float | None,
    ) -> StationMode:
        """First match wins, in the order the brief lists."""
        if not status.get("is_broadcasting", False):
            return StationMode.OFFLINE
        if switched_ago is not None and switched_ago < SWITCH_VISIBILITY_SECONDS:
            return StationMode.SWITCHING_MARKET
        tier = emergency.get("tier")
        if tier == "procedural":
            return StationMode.PROCEDURAL
        if tier == "emergency_reserve":
            return StationMode.RESERVE
        level = buffer.get("level")
        if level in ("critical", "low"):
            return StationMode.BUFFER_LOW
        return StationMode.NORMAL

    def _salience(
        self,
        now: float,
        *,
        band: IntensityBand,
        energy: float | None,
        velocity: float | None,
        confidence: float | None,
    ) -> tuple[float, dict[str, float]]:
        """How much this frame deserves a reaction.

        Computed here rather than in the director because it is a *market* judgement,
        and putting it downstream would be exactly the market logic leaking into
        animation that the architecture forbids. The director consumes one number and a
        threshold.
        """
        if energy is not None:
            self._energy.append((now, energy))
        cutoff = now - SALIENCE_WINDOW_SECONDS
        while self._energy and self._energy[0][0] < cutoff:
            self._energy.popleft()

        components: dict[str, float] = {}
        if len(self._energy) >= 2:
            delta = abs(self._energy[-1][1] - self._energy[0][1])
            components["energy_delta"] = min(1.0, delta / SALIENCE_ENERGY_SCALE)
        if self._prev_band is not None:
            components["regime_change"] = min(1.0, band.distance_to(self._prev_band) / 2.0)
        if velocity is not None:
            components["velocity"] = min(
                1.0, abs(velocity) / SALIENCE_VELOCITY_SCALE
            )
        if confidence is not None and self._prev_confidence is not None:
            components["confidence_delta"] = min(
                1.0, abs(confidence - self._prev_confidence) * 2.0
            )

        self._prev_band = band
        self._prev_confidence = confidence

        score = (
            _W_ENERGY_DELTA * components.get("energy_delta", 0.0)
            + _W_REGIME_CHANGE * components.get("regime_change", 0.0)
            + _W_VELOCITY * components.get("velocity", 0.0)
            + _W_CONFIDENCE * components.get("confidence_delta", 0.0)
        )
        return min(1.0, score), {k: round(v, 4) for k, v in components.items()}


def _decay_toward(current: IntensityBand, target: IntensityBand) -> IntensityBand:
    if current is target:
        return current
    step = 1 if target.rank > current.rank else -1
    return band_from_index(current.rank + step)


def _transition_state(playing: dict[str, Any]) -> TransitionState:
    if playing.get("is_station_id"):
        return TransitionState.STATION_ID
    remaining = _float_or_none(playing.get("remaining_seconds"))
    if remaining is not None and remaining < 12.0:
        return TransitionState.OUTBOUND
    progress = _float_or_none(playing.get("progress"))
    if progress is not None and progress < 0.06:
        return TransitionState.INBOUND
    return TransitionState.NONE


def _float_or_none(value: Any) -> float | None:
    """Parse a float, keeping absence absent.

    The brief forbids replacing unavailable values with fake zeros, so this returns
    ``None`` for ``None`` and for anything non-finite — a NaN that reached the director
    would propagate silently through every multiplier.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================ the socket client


@dataclass
class StationLink:
    """Read-only WebSocket consumer of the station's existing `/ws`.

    Deliberately thin. `LiveHub` already solves the hard parts — bounded per-client
    queues that drop the oldest message rather than buffering without limit, and a hub
    that cannot be back-pressured by a slow client. A visual process that stalls is
    already handled by code that is already in production, so this class only has to
    connect, read, and hand frames to the bridge.

    **Nothing here writes.** The only message ever sent is a keepalive `ping`, which the
    station's socket already accepts and which exists so a proxy that kills idle
    connections does not kill a working one.
    """

    url: str
    bridge: VisualStateBridge
    clock: Clock = field(default_factory=SystemClock)
    ping_interval_seconds: float = 20.0
    _task: asyncio.Task[None] | None = None
    _running: bool = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="visual-station-link")
        _log.info("visual.link.started", url=self.url)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.bridge.stats.connected = False
        _log.info("visual.link.stopped")

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a dead socket must not kill the loop
                self.bridge.stats.connected = False
                self.bridge.stats.last_error = f"{type(error).__name__}: {error}"
                self.bridge.stats.reconnects += 1
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                _log.warning(
                    "visual.link.reconnecting",
                    error_type=type(error).__name__,
                    error=str(error),
                    delay_seconds=delay,
                )
                await self.clock.sleep(delay)

    async def _session(self) -> None:
        # Imported lazily so the visual package imports without a websocket client
        # present — the simulation harness and every unit test run without one.
        import websockets  # noqa: PLC0415

        async with websockets.connect(self.url, open_timeout=10) as socket:
            self.bridge.stats.connected = True
            self.bridge.stats.last_error = None
            _log.info("visual.link.connected", url=self.url)
            while self._running:
                message = await socket.recv()
                self._consume(message if isinstance(message, str) else message.decode())

    def _consume(self, message: str) -> None:
        try:
            envelope = json.loads(message)
        except json.JSONDecodeError:
            self.bridge.stats.frames_rejected += 1
            return
        if envelope.get("type") != "state":
            # `position` frames arrive four times as often and carry only audio
            # progress. Track progress is re-read from the next state frame instead,
            # which is accurate enough for a rhythm policy re-anchored every 2 s.
            return
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            self.bridge.stats.frames_rejected += 1
            return
        try:
            self.bridge.derive(payload)
        except Exception as error:  # noqa: BLE001 - one bad frame must not stop the link
            self.bridge.stats.frames_rejected += 1
            self.bridge.stats.last_error = f"{type(error).__name__}: {error}"
            _log.warning(
                "visual.bridge.frame_rejected",
                error_type=type(error).__name__,
                error=str(error),
            )


def default_station_url(host: str = "127.0.0.1", port: int = 8080) -> str:
    return f"ws://{host}:{port}/ws"


__all__ = [
    "BAND_DOWNWARD_HOLD_SECONDS",
    "BAND_STEP_INTERVAL_SECONDS",
    "DEGRADE_DECAY_SECONDS",
    "DEGRADE_DORMANT_SECONDS",
    "DEGRADE_HOLD_SECONDS",
    "RECONNECT_BACKOFF",
    "SALIENCE_WINDOW_SECONDS",
    "SWITCH_VISIBILITY_SECONDS",
    "BridgeStats",
    "StationLink",
    "VisualStateBridge",
    "default_station_url",
]
