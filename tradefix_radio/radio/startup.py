"""Fresh start programming (§FSP).

TRADE FIX RADIO MUST SOUND FRESH FROM THE FIRST AUDIBLE TRACK.

This module implements the startup state machine and priming logic that ensures
the station never begins with repetitive emergency fallback music. The key insight
is that "process running" is not the same as "radio ready for listener".

Two startup modes exist:

**CONTROLLED_START** — No audience yet. Prime fresh tracks BEFORE audible playout.
The listener waits while fresh music generates, then hears never-before-played tracks.

**LIVE_RECOVERY** — Audience is already live. Emergency audio prevents dead air
while fresh generation rebuilds the buffer. Used when a 24/7 stream crashes.

The state machine:

    BOOTING
        → MARKET_ACQUIRE
        → GENERATOR_WARMING
        → PRIMING
        → READY_TO_AIR
        → ON_AIR

Only at READY_TO_AIR does listener-facing playout begin in controlled start mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from tradefix_radio.contracts.enums import StartupMode, StartupState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import RadioSettings

_log = structlog.get_logger(__name__)


@dataclass
class StartupProgress:
    """Current startup progress for display in Control Center."""

    state: StartupState = StartupState.BOOTING
    mode: StartupMode = StartupMode.CONTROLLED_START
    active_symbol: str | None = None
    fresh_tracks_ready: int = 0
    fresh_tracks_required: int = 2
    fresh_minutes_ready: float = 0.0
    fresh_minutes_target: float = 8.0
    generator_ready: bool = False
    market_ready: bool = False
    audio_held: bool = True
    started_at: float = field(default_factory=time.monotonic)
    ready_at: float | None = None

    @property
    def is_priming_complete(self) -> bool:
        """Whether startup priming requirements are met."""
        tracks_ok = self.fresh_tracks_ready >= self.fresh_tracks_required
        minutes_ok = self.fresh_minutes_ready >= self.fresh_minutes_target
        return tracks_ok or minutes_ok

    @property
    def priming_progress_fraction(self) -> float:
        """0.0 to 1.0 progress toward priming completion."""
        track_progress = min(1.0, self.fresh_tracks_ready / max(1, self.fresh_tracks_required))
        minute_progress = min(1.0, self.fresh_minutes_ready / max(0.1, self.fresh_minutes_target))
        return max(track_progress, minute_progress)

    @property
    def time_to_ready(self) -> float | None:
        """Seconds from start to READY_TO_AIR, or None if not ready yet."""
        if self.ready_at is None:
            return None
        return self.ready_at - self.started_at


@dataclass
class StartupRequirements:
    """What the startup priming phase requires before ON_AIR."""

    minimum_fresh_tracks: int
    target_fresh_minutes: float
    require_unplayed: bool
    prime_enabled: bool

    @classmethod
    def from_settings(cls, settings: RadioSettings) -> StartupRequirements:
        return cls(
            minimum_fresh_tracks=settings.minimum_fresh_tracks,
            target_fresh_minutes=settings.target_fresh_minutes,
            require_unplayed=settings.require_unplayed,
            prime_enabled=settings.prime_enabled,
        )


class StartupProgrammingPlanner:
    """Plans the opening tracks with diversity constraints.

    The opening tracks must deliberately differ to avoid a recognizable
    startup pattern. Track 1 and Track 2 may not share:
    - genre
    - creative identity
    - vocal mode (where avoidable)
    - topic pair
    - persona

    This planner delegates to the real MusicDirector with additional
    constraints rather than implementing a parallel music generation path.
    """

    def __init__(
        self,
        requirements: StartupRequirements,
        *,
        mode: StartupMode = StartupMode.CONTROLLED_START,
    ) -> None:
        self._requirements = requirements
        self._mode = mode
        self._progress = StartupProgress(
            mode=mode,
            fresh_tracks_required=requirements.minimum_fresh_tracks,
            fresh_minutes_target=requirements.target_fresh_minutes,
        )
        self._planned_opening_genres: list[str] = []
        self._planned_opening_personas: list[str | None] = []
        self._state = StartupState.BOOTING

    @property
    def state(self) -> StartupState:
        return self._state

    @property
    def progress(self) -> StartupProgress:
        return self._progress

    @property
    def is_controlled_start(self) -> bool:
        return self._mode is StartupMode.CONTROLLED_START

    @property
    def should_hold_audio(self) -> bool:
        """Whether listener-facing audio should be held."""
        if self._mode is StartupMode.LIVE_RECOVERY:
            # Live recovery never holds - dead air is worse than fallback
            return False
        # Controlled start holds until ready
        return self._state not in (StartupState.READY_TO_AIR, StartupState.ON_AIR)

    def transition_to(self, state: StartupState) -> None:
        """Move to a new startup state."""
        previous = self._state
        if previous is state:
            return

        self._state = state
        self._progress.state = state

        if state is StartupState.READY_TO_AIR:
            self._progress.ready_at = time.monotonic()
            self._progress.audio_held = False

        if state is StartupState.ON_AIR:
            self._progress.audio_held = False

        _log.info(
            "startup.state_changed",
            previous=previous.value,
            state=state.value,
            mode=self._mode.value,
            fresh_tracks=self._progress.fresh_tracks_ready,
            fresh_minutes=round(self._progress.fresh_minutes_ready, 1),
        )

    def on_market_acquired(self, symbol: str) -> None:
        """Called when MarketRouter establishes the active symbol."""
        self._progress.active_symbol = symbol
        self._progress.market_ready = True
        _log.info("startup.market_acquired", symbol=symbol)
        if self._state is StartupState.MARKET_ACQUIRE:
            self.transition_to(StartupState.GENERATOR_WARMING)

    def on_generator_ready(self) -> None:
        """Called when the generator (ACE-Step) is warmed and ready."""
        self._progress.generator_ready = True
        _log.info("startup.generator_ready")
        if self._state is StartupState.GENERATOR_WARMING:
            if self._requirements.prime_enabled:
                self.transition_to(StartupState.PRIMING)
                # Fresh tracks credited before this point (a restored queue of never-played
                # audio) may already satisfy priming; nothing else re-checks, so do it here.
                if self._progress.is_priming_complete:
                    self.transition_to(StartupState.READY_TO_AIR)
            else:
                # Skip priming if disabled
                self.transition_to(StartupState.READY_TO_AIR)

    def on_fresh_track_ready(
        self,
        track_id: str,
        duration_seconds: float,
        genre: str,
        persona_id: str | None,
    ) -> None:
        """Called when a fresh (never-played) track becomes ready."""
        self._progress.fresh_tracks_ready += 1
        self._progress.fresh_minutes_ready += duration_seconds / 60.0
        self._planned_opening_genres.append(genre)
        self._planned_opening_personas.append(persona_id)

        _log.info(
            "startup.fresh_track_ready",
            track_id=track_id,
            fresh_tracks=self._progress.fresh_tracks_ready,
            fresh_minutes=round(self._progress.fresh_minutes_ready, 1),
            genre=genre,
        )

        if self._state is StartupState.PRIMING and self._progress.is_priming_complete:
            self.transition_to(StartupState.READY_TO_AIR)

    def opening_constraints(self) -> dict[str, object]:
        """Return constraints for the next opening track.

        Used to ensure opening tracks differ from each other.
        """
        return {
            "avoid_genres": list(self._planned_opening_genres),
            "avoid_personas": [p for p in self._planned_opening_personas if p],
            "opening_track_index": len(self._planned_opening_genres),
        }

    def check_fresh_eligibility(
        self,
        play_count: int,
        market_symbol: str | None,
        track_market_symbol: str | None,
    ) -> bool:
        """Check if a track qualifies as fresh for startup.

        A startup track qualifies as FRESH only if:
        - it has never been played (play_count=0) when require_unplayed is True
        - its market context matches the current active market
        """
        if self._requirements.require_unplayed and play_count > 0:
            return False

        # Market context check: track must match current symbol or be neutral
        return not (market_symbol and track_market_symbol and track_market_symbol != market_symbol)


def derive_procedural_session_seed() -> int:
    """Derive a unique seed for procedural audio that never repeats across sessions.

    The procedural audio must not sound identical across startups. This function
    derives a seed from monotonic sources that are guaranteed unique per session:
    - Current time in nanoseconds
    - Process-level monotonic counter

    The seed is NOT persisted - each session gets a fresh seed, ensuring
    procedural audio never starts from the same point twice.
    """
    # Combine multiple entropy sources
    now_ns = time.time_ns()
    monotonic_ns = int(time.monotonic_ns())

    # Mix the bits to ensure good distribution
    return (now_ns ^ (monotonic_ns << 20)) & 0x7FFFFFFF  # Keep positive 31-bit


__all__ = [
    "StartupProgrammingPlanner",
    "StartupProgress",
    "StartupRequirements",
    "derive_procedural_session_seed",
]
