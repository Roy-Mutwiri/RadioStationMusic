"""Gaze and blink: two systems that run on their own clocks.

Both are deliberately outside the action scheduler. Routing every eye movement through
cooldowns and locks would make gaze discrete, and gaze is the one thing that must be
continuous — it is the strongest aliveness cue there is, and the easiest to get wrong.

Realism rules, all from the brief and all cheap to get right once stated:

**Eyes lead, head follows.** Simultaneous eye-and-head onset is the clearest signal of a
cheap rig. Eyes arrive in 40-90 ms; the head takes 25-60 % of the remaining angle over
180-400 ms.

**Participation scales with angle.** Under 10 degrees, eyes only. Over 25, the head
carries most of it. Over 45, shoulders. Over 70, the torso. Each target declares the
tier its geometry requires, and the blockout validator checks the declaration.

**Nothing snaps.** A floor of 180 ms to any new target, including inside a reaction. A
fast scan is fast, not instantaneous.

**A blink crosses a large saccade.** Real eyes blink across big gaze shifts, and omitting
it is one of the clearest tells of synthetic animation. Forced above 12 degrees.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from tradefix_radio.visual.contracts import (
    CharacterState,
    GazeParticipation,
    GazeShiftV1,
    GazeTarget,
    IntensityBand,
    VisualStateV1,
)
from tradefix_radio.visual.geometry import Blockout
from tradefix_radio.visual.modulation import profile_for
from tradefix_radio.visual.rhythm import WorkRhythm

# ============================================================ blink

#: Human blink intervals cluster rather than spreading uniformly, so a uniform draw
#: produces a visibly metronomic eye. Log-normal with these parameters gives a median
#: near 4.1 s and a long right tail, which is what real blinking looks like.
BLINK_MEDIAN_SECONDS: Final = 4.1
BLINK_SIGMA: Final = 0.55
BLINK_MIN_SECONDS: Final = 2.0
BLINK_MAX_SECONDS: Final = 8.0

#: Probability of a double blink on any given draw, and of a slow one.
DOUBLE_BLINK_CHANCE: Final = 0.08
SLOW_BLINK_CHANCE: Final = 0.04

#: Gap between the two halves of a double blink.
DOUBLE_BLINK_GAP_MS: Final = (180, 260)

#: Blink rate multipliers by behavioural context. Below 1.0 shortens the interval.
#:
#: Suppression during concentration is the interesting one: people genuinely hold their
#: eyes open while reading something closely, and a 25 % longer interval during
#: `ANALYZING` reads as focus without any other cue.
BLINK_STATE_SCALE: Final[dict[CharacterState, float]] = {
    CharacterState.ANALYZING: 1.25,
    CharacterState.EXECUTING: 1.15,
    CharacterState.MARKET_REACTION: 1.35,
    CharacterState.WAITING: 0.92,
    CharacterState.CAFFEINE_BREAK: 0.90,
    CharacterState.POSTURE_RESET: 0.88,
}

#: Minimum gap between a forced saccade blink and the next scheduled one, so a busy
#: scan does not produce a flutter.
BLINK_REFRACTORY_SECONDS: Final = 0.9

#: Minimum gap between two *variant* blinks, seconds. Without these the 8 % and 4 %
#: draws are independent on every blink, so two doubles can land two seconds apart —
#: which reads as a twitch rather than as variation. The brief's own figures: double
#: blink 25-120 s, slow blink rather less often.
VARIANT_BLINK_MIN_GAP_SECONDS: Final[dict[str, float]] = {
    "double_blink": 25.0,
    "slow_blink": 45.0,
}


@dataclass(slots=True)
class BlinkDriver:
    """Independent blink generator.

    Separate from the action scheduler because blinking is a reflex with its own
    distribution, and because the brief asks for three specific things the scheduler
    cannot express: no exact timing, no blink while the eyes are already closing for
    another action, and no double blink at a predictable cadence.

    The second of those is why :attr:`_closing_until` exists. A blink fired during
    another blink's close phase produces a visible stutter in the eyelid, and the only
    place that can be known is here.
    """

    next_at: float = 0.0
    last_at: float = -999.0
    _closing_until: float = -999.0
    #: Last time each variant was used, so its minimum gap can be enforced.
    _variant_last: dict[str, float] = field(default_factory=dict)
    #: Counted so the simulation report can assert the distribution is actually varying.
    intervals: list[float] = field(default_factory=list)

    def schedule(
        self,
        now: float,
        rng: random.Random,
        *,
        state: CharacterState,
        fatigue: WorkRhythm,
    ) -> float:
        """Draw the next interval and arm it. Returns the sampled seconds.

        Resamples outside the bounds rather than clamping to them. Clamping put a spike
        at exactly 2.0 s — 88 of 400 draws landed on the floor — and a spike at a single
        value is a quasi-fixed period, which is the one thing blink timing must not have.
        """
        scale = BLINK_STATE_SCALE.get(state, 1.0) * fatigue.blink_rate_scale(now)
        interval = BLINK_MEDIAN_SECONDS * scale
        for _ in range(12):
            candidate = (
                rng.lognormvariate(math.log(BLINK_MEDIAN_SECONDS), BLINK_SIGMA) * scale
            )
            if BLINK_MIN_SECONDS <= candidate <= BLINK_MAX_SECONDS:
                interval = candidate
                break
        else:
            interval = max(BLINK_MIN_SECONDS, min(BLINK_MAX_SECONDS, interval))
        self.next_at = now + interval
        self.intervals.append(interval)
        if len(self.intervals) > 4096:
            # Bounded: a 24/7 process cannot grow a list forever, and the distribution
            # is established long before four thousand samples.
            del self.intervals[:2048]
        return interval

    def due(self, now: float) -> bool:
        return now >= self.next_at and now >= self._closing_until

    def kind(self, rng: random.Random, now: float) -> str:
        """Which blink to perform.

        Sampled so the variants have no cadence, and gap-limited so they cannot cluster.
        Both halves are needed: sampling alone let two double blinks land two seconds
        apart, and a fixed cadence would be worse than either.
        """
        roll = rng.random()
        if roll < DOUBLE_BLINK_CHANCE and self._variant_allowed("double_blink", now):
            self._variant_last["double_blink"] = now
            return "double_blink"
        if (
            roll < DOUBLE_BLINK_CHANCE + SLOW_BLINK_CHANCE
            and self._variant_allowed("slow_blink", now)
        ):
            self._variant_last["slow_blink"] = now
            return "slow_blink"
        return "blink"

    def _variant_allowed(self, variant: str, now: float) -> bool:
        gap = VARIANT_BLINK_MIN_GAP_SECONDS[variant]
        last = self._variant_last.get(variant)
        return last is None or now - last >= gap

    def mark_performed(self, now: float, duration_ms: int) -> None:
        self.last_at = now
        self._closing_until = now + duration_ms / 1000.0

    def force(self, now: float) -> bool:
        """Blink now if the eyelid is free. Used for the saccade-crossing blink."""
        if now < self._closing_until or now - self.last_at < BLINK_REFRACTORY_SECONDS:
            return False
        self.next_at = now
        return True

    def interval_stats(self) -> tuple[float, float, float] | None:
        """Mean, min and max of recorded intervals, for the report."""
        if not self.intervals:
            return None
        return (
            sum(self.intervals) / len(self.intervals),
            min(self.intervals),
            max(self.intervals),
        )


# ============================================================ gaze

#: Eyes arrive first, always. Milliseconds.
SACCADE_MS: Final = (40, 90)

#: Head follows over this window, for the fraction of the angle it carries.
HEAD_FOLLOW_MS: Final = (180, 400)
HEAD_CONTRIBUTION: Final = (0.25, 0.60)

#: Dwell ranges by participation tier, seconds. A target that costs a torso turn is
#: held longer — nobody rotates their trunk for a 300 ms look.
DWELL_SECONDS: Final[dict[GazeParticipation, tuple[float, float]]] = {
    GazeParticipation.EYES: (0.8, 4.5),
    GazeParticipation.HEAD: (1.0, 5.0),
    GazeParticipation.SHOULDERS: (1.2, 4.0),
    GazeParticipation.HEAD_PITCH_STRONG: (0.5, 1.4),
    GazeParticipation.TORSO: (1.8, 6.0),
    GazeParticipation.CHAIR_SWIVEL: (2.5, 7.0),
}

#: Micro-saccade cadence within the current target, seconds. Independent of everything
#: else, which is part of why the combined idle state never recurs.
MICRO_SACCADE_SECONDS: Final = (0.4, 1.6)
MICRO_SACCADE_DEGREES: Final = (0.3, 1.2)

#: Camera glance rationing. A rare, brief, unhurried glance is the moment the viewer
#: feels acknowledged. The same glance four times as often makes him a presenter, and
#: the station is not a presentation.
CAMERA_GLANCE_MIN_GAP_SECONDS: Final = 720.0
CAMERA_GLANCE_DWELL_SECONDS: Final = (0.5, 1.2)
CAMERA_GLANCE_STATES: Final = (CharacterState.IDLE_FOCUS, CharacterState.WAITING)
CAMERA_GLANCE_CAMERAS: Final = ("CAM_4", "CAM_7")


@dataclass(slots=True)
class GazeState:
    """Where the eyes are and when they may move again."""

    target: GazeTarget = GazeTarget.MONITOR_MAIN
    since: float = 0.0
    dwell_until: float = 0.0
    next_micro_saccade: float = 0.0
    last_camera_glance: float = -99999.0
    #: Set while an action is overriding gaze; cleared when it ends.
    forced_by: str | None = None
    sequence: int = 0


class GazeDirector:
    """Chooses where the eyes go, and how the body gets them there.

    Resolves every target through the frozen blockout, so "look at the left monitor"
    means the actual left monitor in all seven cameras. The alternative — per-camera
    tuned angles — is precisely how eyes end up looking *through* monitors, which the
    brief lists as a constraint violation.
    """

    def __init__(self, blockout: Blockout, rng: random.Random) -> None:
        self._geometry = blockout
        self._rng = rng
        self._base_shares = {
            target: resolved.base_share for target, resolved in blockout.gaze.items()
        }

    # -- target choice -----------------------------------------------------

    def _weights(
        self,
        state: VisualStateV1,
        character_state: CharacterState,
        current: GazeTarget,
        now: float,
        gaze_state: GazeState,
        active_camera: str | None,
    ) -> dict[GazeTarget, float]:
        """Per-target weights for the next shift.

        Starts from the blockout's base distribution — 89 % on screens, which is what a
        working trader's eyes actually do and the number that makes the remaining 11 %
        read correctly — then applies context.
        """
        band = state.intensity_band
        weights: dict[GazeTarget, float] = {}
        for target, base in self._base_shares.items():
            if target is current:
                continue  # never "shift" to where the eyes already are
            weight = base

            if target is GazeTarget.CAMERA:
                # Hard-gated rather than weighted: an operator slider or an unlucky
                # sample must not be able to make him stare at the viewer.
                permitted = (
                    character_state in CAMERA_GLANCE_STATES
                    and now - gaze_state.last_camera_glance >= CAMERA_GLANCE_MIN_GAP_SECONDS
                    and (active_camera in CAMERA_GLANCE_CAMERAS if active_camera else False)
                )
                weights[target] = base if permitted else 0.0
                continue

            resolved = self._geometry.gaze_target(target)
            # High-intensity bands favour comparison between screens; quiet favours
            # sustained reading of one, plus the non-screen surfaces.
            if resolved.is_screen and target is not GazeTarget.MONITOR_MAIN:
                weight *= 0.7 + 0.5 * (band.rank / 5.0)
            elif not resolved.is_screen:
                weight *= 1.35 - 0.6 * (band.rank / 5.0)

            # Expensive turns are suppressed when the market is loud. He does not swivel
            # to look out of the window during a breakout.
            if resolved.participation in (
                GazeParticipation.TORSO,
                GazeParticipation.CHAIR_SWIVEL,
            ):
                weight *= max(0.05, 1.3 - 0.9 * (band.rank / 5.0))

            if character_state is CharacterState.ANALYZING and resolved.is_screen:
                weight *= 1.4
            elif character_state is CharacterState.WAITING and not resolved.is_screen:
                weight *= 1.3
            elif character_state is CharacterState.NOTE_TAKING:
                weight *= 2.2 if target is GazeTarget.NOTEBOOK else 0.4

            weights[target] = max(0.0, weight)
        return weights

    def choose(
        self,
        *,
        state: VisualStateV1,
        character_state: CharacterState,
        gaze_state: GazeState,
        now: float,
        active_camera: str | None = None,
    ) -> GazeTarget | None:
        """The next gaze target, or ``None`` to keep holding."""
        if gaze_state.forced_by is not None:
            return None
        if now < gaze_state.dwell_until:
            return None
        weights = self._weights(
            state, character_state, gaze_state.target, now, gaze_state, active_camera
        )
        total = sum(weights.values())
        if total <= 0.0:
            return None
        roll = self._rng.random() * total
        cumulative = 0.0
        for target, weight in weights.items():
            cumulative += weight
            if cumulative >= roll:
                return target
        return next(iter(weights))

    # -- shift construction ------------------------------------------------

    def shift(
        self,
        *,
        to: GazeTarget,
        gaze_state: GazeState,
        now: float,
        at: datetime,
        band: IntensityBand,
        fatigue: WorkRhythm,
    ) -> GazeShiftV1:
        """Build the shift, with transit, dwell, participation and the forced blink."""
        resolved = self._geometry.gaze_target(to)
        angle = (
            self._geometry.angle_between(gaze_state.target, to)
            if not resolved.dynamic
            else 18.0
        )

        # Transit scales with angle but never below the geometry's floor. A 90-degree
        # turn takes real time; a 4-degree one does not take none.
        transit = max(
            self._geometry.min_gaze_transit_ms,
            int(self._rng.uniform(*SACCADE_MS) + angle * self._rng.uniform(4.0, 7.5)),
        )

        if angle < 10.0:
            participation = GazeParticipation.EYES
            head = 0.0
        else:
            participation = resolved.participation
            low, high = HEAD_CONTRIBUTION
            head = self._rng.uniform(low, high)
            if angle > 45.0:
                head = min(1.0, head + 0.2)

        dwell_low, dwell_high = DWELL_SECONDS[resolved.participation]
        dwell_scale = profile_for(band).dwell_scale * fatigue.dwell_scale(now)
        if to is GazeTarget.CAMERA:
            dwell_low, dwell_high = CAMERA_GLANCE_DWELL_SECONDS
            dwell_scale = 1.0
        dwell_ms = int(self._rng.uniform(dwell_low, dwell_high) * dwell_scale * 1000.0)

        gaze_state.sequence += 1
        return GazeShiftV1(
            sequence=gaze_state.sequence,
            target=to,
            from_target=gaze_state.target,
            started_at=at,
            transit_ms=transit,
            dwell_ms=dwell_ms,
            participation=participation,
            angle_degrees=round(angle, 2),
            head_contribution=round(head, 3),
            forces_blink=angle > self._geometry.forced_blink_above_degrees,
        )

    def apply(self, shift: GazeShiftV1, gaze_state: GazeState, now: float) -> None:
        """Commit a shift to the gaze state."""
        gaze_state.target = shift.target
        gaze_state.since = now
        gaze_state.dwell_until = now + (shift.transit_ms + shift.dwell_ms) / 1000.0
        gaze_state.next_micro_saccade = now + self._rng.uniform(*MICRO_SACCADE_SECONDS)
        if shift.target is GazeTarget.CAMERA:
            gaze_state.last_camera_glance = now

    def micro_saccade_due(self, gaze_state: GazeState, now: float) -> bool:
        return now >= gaze_state.next_micro_saccade

    def arm_micro_saccade(self, gaze_state: GazeState, now: float) -> float:
        """Arm the next micro-saccade and return its magnitude in degrees."""
        gaze_state.next_micro_saccade = now + self._rng.uniform(*MICRO_SACCADE_SECONDS)
        return self._rng.uniform(*MICRO_SACCADE_DEGREES)

    def force(
        self, gaze_state: GazeState, target: GazeTarget, action_id: str, now: float
    ) -> None:
        """Pin gaze for an action that overrides it."""
        gaze_state.target = target
        gaze_state.since = now
        gaze_state.forced_by = action_id

    def release(self, gaze_state: GazeState, action_id: str, now: float) -> None:
        """Release a pin, if this action owns it."""
        if gaze_state.forced_by == action_id:
            gaze_state.forced_by = None
            gaze_state.dwell_until = now


__all__ = [
    "BLINK_MAX_SECONDS",
    "BLINK_MEDIAN_SECONDS",
    "BLINK_MIN_SECONDS",
    "BLINK_REFRACTORY_SECONDS",
    "BLINK_SIGMA",
    "BLINK_STATE_SCALE",
    "CAMERA_GLANCE_CAMERAS",
    "CAMERA_GLANCE_MIN_GAP_SECONDS",
    "CAMERA_GLANCE_STATES",
    "DOUBLE_BLINK_CHANCE",
    "DWELL_SECONDS",
    "MICRO_SACCADE_SECONDS",
    "SLOW_BLINK_CHANCE",
    "BlinkDriver",
    "GazeDirector",
    "GazeState",
]
