"""RadioEnergyPlanner — station energy that tracks the market without twitching (§98).

§98: "A station should not constantly oscillate between extremes. Create
RadioEnergyPlanner. It considers market energy, recent radio energy, time since last
peak, current track. Smooth transitions unless market conditions justify sharp change."

The planner sits between market energy and musical energy, and the whole point is the
gap between them. Market energy is a measurement that can jump 40 points in two bars;
station energy is a *programming decision* that a listener experiences as a sequence.
Following the market one-for-one would produce whiplash; ignoring it would make the
station's central premise false.

Four inputs, each fixing a different failure:

``market energy``       the target. Without it the station is not market-reactive.
``recent station energy`` the anchor. Limits how far one track may move from the last.
``energy velocity``     §6's rate of change. Lets the station *lead* a developing move
                        instead of lagging it by a track.
``time since last peak`` prevents a station that sits at 90 for an hour because the
                        market stayed loud. Sustained peaks are exhausting, and §98's
                        "do not constantly oscillate between extremes" cuts both ways.

The sharp-change escape hatch matters as much as the smoothing. §29 is explicit that a
genuine breakout must change programming; a planner that only ever smoothed would make
the station unresponsive to the one event it exists to react to.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradefix_radio.market.rolling import clamp

#: Maximum energy points one track may move from the previous, in normal operation.
#: Roughly one BPM band per track, which is a change a listener hears as intentional
#: rather than as a glitch.
DEFAULT_MAX_STEP = 14.0

#: Market/station divergence above which the step limit is lifted. §29's "gold suddenly
#: breaks out" case: at this gap the market has genuinely changed character and holding
#: the station back would make it unresponsive.
SHARP_CHANGE_GAP = 28.0

#: Energy at or above which a track counts as a peak.
PEAK_THRESHOLD = 78.0

#: Tracks at peak energy after which the planner starts pulling down, regardless of the
#: market. A long violent session should not mean an hour of unbroken maximum intensity.
MAX_CONSECUTIVE_PEAKS = 3

#: How much §6 energy velocity contributes, in energy points per point-per-bar.
#: Modest on purpose: velocity is a leading indicator, and overweighting it would make
#: the station lurch on noise.
VELOCITY_GAIN = 2.5

#: Tracks that must pass before a sharp change may fire in the **opposite** direction.
#:
#: Without this the escape hatch defeats the smoothing it exists alongside. A market
#: alternating between 5 and 95 presents a 90-point gap on every single track, so every
#: step qualified as a sharp change and the station swung 67 points per track — precisely
#: the oscillation §98 forbids, caused by the mechanism meant to handle §29.
#:
#: A genuine market shift does not reverse itself every few minutes. If it appears to, the
#: market is oscillating and the station should hold a middle course. Same reasoning as the
#: regime engine's cooldown, applied to energy.
SHARP_CHANGE_COOLDOWN_TRACKS = 3

#: Fraction of the market/station gap a sharp change closes in one track.
#:
#: Not 1.0: even a genuine breakout sounds better as a decisive step than as a jump cut,
#: and the following track closes the remainder.
SHARP_CHANGE_FRACTION = 0.75

#: Ceiling on a sharp change, as a multiple of ``max_step``.
#:
#: The cooldown above limits how *often* the escape hatch fires; this limits how *far* it
#: moves. Both are needed. With only the cooldown, a 90-point gap still produced a single
#: 67-point swing — audibly a different radio station mid-sequence, which is what §98
#: exists to prevent. Twice the normal step is unmistakably decisive to a listener while
#: remaining a step rather than a cut, and successive tracks keep closing the gap: a
#: sustained 75-point move is fully tracked within four or five tracks.
SHARP_CHANGE_STEP_MULTIPLIER = 2.0


@dataclass(frozen=True)
class EnergyPlan:
    """The energy the next track should target, and the reasoning."""

    target_energy: float
    #: What the market alone would have asked for.
    market_energy: float
    #: Station energy of the previous track, or ``None`` at startup.
    previous_energy: float | None
    #: ``True`` when the step limit was lifted because the market changed character.
    sharp_change: bool
    #: ``True`` when a sharp change was *wanted* but suppressed because it would have
    #: reversed a recent one — the market is oscillating rather than shifting.
    sharp_change_suppressed: bool
    #: ``True`` when the planner pulled down to break a run of peaks.
    peak_relief: bool
    #: Contribution from §6 energy velocity, in points.
    velocity_contribution: float
    reason: str

    @property
    def step(self) -> float:
        """Signed change from the previous track."""
        if self.previous_energy is None:
            return 0.0
        return self.target_energy - self.previous_energy


class RadioEnergyPlanner:
    """Plans station energy from market energy and recent programming."""

    def __init__(
        self,
        *,
        max_step: float = DEFAULT_MAX_STEP,
        sharp_change_gap: float = SHARP_CHANGE_GAP,
        peak_threshold: float = PEAK_THRESHOLD,
        max_consecutive_peaks: int = MAX_CONSECUTIVE_PEAKS,
        velocity_gain: float = VELOCITY_GAIN,
        sharp_change_cooldown_tracks: int = SHARP_CHANGE_COOLDOWN_TRACKS,
        sharp_change_step_multiplier: float = SHARP_CHANGE_STEP_MULTIPLIER,
    ) -> None:
        if max_step <= 0:
            raise ValueError("max_step must be positive")
        if sharp_change_step_multiplier < 1.0:
            raise ValueError(
                "sharp_change_step_multiplier must be at least 1.0, otherwise a sharp "
                "change would move less than an ordinary step"
            )
        if sharp_change_gap <= max_step:
            raise ValueError(
                "sharp_change_gap must exceed max_step, otherwise every step would "
                "qualify as a sharp change and the limit would never apply"
            )
        if max_consecutive_peaks < 1:
            raise ValueError("max_consecutive_peaks must be at least 1")
        self._max_step = max_step
        self._sharp_change_gap = sharp_change_gap
        self._peak_threshold = peak_threshold
        self._max_consecutive_peaks = max_consecutive_peaks
        self._velocity_gain = velocity_gain
        self._sharp_cooldown = max(0, sharp_change_cooldown_tracks)
        self._sharp_step_cap = max_step * sharp_change_step_multiplier
        # The previous track's TARGET, not a smoothed version of it.
        #
        # An earlier version kept an ExponentialSmoother here as well as the step limit,
        # which double-smoothed: the station lagged far more than ``max_step`` implied, and
        # ``plan.step`` did not equal the actual change between consecutive tracks. The step
        # limit *is* the smoothing mechanism — adding a second one only made the planner's
        # own reported numbers wrong.
        self._current_energy: float | None = None
        self._consecutive_peaks = 0
        self._last_sharp_direction = 0
        self._tracks_since_sharp = 10_000

    # -- state -------------------------------------------------------------

    @property
    def current_energy(self) -> float | None:
        """The previous track's target energy, or ``None`` before the first track."""
        return self._current_energy

    @property
    def consecutive_peaks(self) -> int:
        return self._consecutive_peaks

    def reset(self) -> None:
        self._current_energy = None
        self._consecutive_peaks = 0
        self._last_sharp_direction = 0
        self._tracks_since_sharp = 10_000

    def restore(self, energy: float | None, consecutive_peaks: int = 0) -> None:
        """Reload state after a restart (§96).

        §96: restarting must not reset creative memory. Without this the station would
        open every restart at whatever the market happened to be, discarding the
        smoothing that makes consecutive tracks feel like a sequence.
        """
        self._current_energy = None if energy is None else clamp(energy)
        self._consecutive_peaks = max(0, consecutive_peaks)

    # -- planning ----------------------------------------------------------

    def plan(
        self,
        *,
        market_energy: float,
        energy_velocity: float = 0.0,
        divergence_strength: float = 0.0,
    ) -> EnergyPlan:
        """Decide the next track's target energy.

        ``divergence_strength`` comes from §11's diversity pressure. When programming has
        gone stale, the planner is permitted to move further than the normal step limit
        — because an unusually large energy move is itself a form of variety, and a
        station stuck at one energy level is exactly what §11 is trying to break.
        """
        market = clamp(market_energy)
        previous = self._current_energy

        # Velocity lets the station lead a developing move rather than lag it (§6).
        velocity_contribution = clamp(energy_velocity * self._velocity_gain, -12.0, 12.0)
        desired = clamp(market + velocity_contribution)

        if previous is None:
            # First track: adopt the market directly. Starting from a neutral 50 and
            # converging would mean the station opens at the wrong energy for several
            # tracks, which is audible and pointless.
            target = desired
            self._current_energy = target
            self._tracks_since_sharp += 1
            self._register_peak(target)
            return EnergyPlan(
                target_energy=target,
                market_energy=market,
                previous_energy=None,
                sharp_change=False,
                sharp_change_suppressed=False,
                peak_relief=False,
                velocity_contribution=velocity_contribution,
                reason="first track: adopting market energy directly",
            )

        gap = desired - previous
        direction = 1 if gap > 0 else -1
        wants_sharp = abs(gap) >= self._sharp_change_gap

        # Read the elapsed count *before* incrementing, so "tracks since the last sharp
        # change" means what it says. Incrementing first made the cooldown expire a track
        # early and reported the wrong number in the reason string.
        elapsed = self._tracks_since_sharp
        self._tracks_since_sharp += 1

        # A sharp change may not immediately reverse a recent one. See
        # SHARP_CHANGE_COOLDOWN_TRACKS: without this, an alternating market triggers the
        # escape hatch on every track and the station oscillates harder than if the hatch
        # did not exist. Note this gates *reversals* only — a market that keeps moving the
        # same way is genuinely shifting, and the station should keep up with it.
        reversing = (
            self._last_sharp_direction != 0
            and direction != self._last_sharp_direction
            and elapsed < self._sharp_cooldown
        )
        sharp_change = wants_sharp and not reversing
        sharp_change_suppressed = wants_sharp and reversing

        # Divergence pressure widens the step limit, so staleness can be broken with a
        # bigger-than-usual move.
        allowance = self._max_step * (1.0 + divergence_strength)

        if sharp_change:
            # §29: the market has changed character. Move decisively, but bounded — see
            # SHARP_CHANGE_STEP_MULTIPLIER. The remaining gap is closed by the following
            # tracks, so a sustained move is tracked quickly while a one-track spike
            # cannot swing the station across the dial.
            magnitude = min(abs(gap) * SHARP_CHANGE_FRACTION, self._sharp_step_cap)
            target = previous + magnitude * direction
            reason = (
                f"market diverged {gap:+.0f} points, beyond the "
                f"{self._sharp_change_gap:.0f}-point sharp-change threshold; "
                f"moving {magnitude:.0f}"
            )
            self._last_sharp_direction = direction
            self._tracks_since_sharp = 0
        elif sharp_change_suppressed:
            target = previous + min(abs(gap), allowance) * direction
            reason = (
                f"market diverged {gap:+.0f} points but that would reverse a sharp change "
                f"{elapsed} track(s) ago; holding the step limit rather than oscillating"
            )
            # Record the *attempted* direction and restart the cooldown. A suppressed
            # reversal is evidence the market is thrashing, and while it thrashes the
            # escape hatch should stay shut in both directions.
            #
            # Without this the planner was directionally biased: whichever way the first
            # sharp change happened to go, subsequent moves that way kept matching
            # ``_last_sharp_direction`` and firing at the sharp magnitude while moves the
            # other way were held to the ordinary step. A 5/95 alternating market therefore
            # ratcheted down to 13 — tracking one extreme while never reaching the other.
            # Flipping the recorded direction makes the next attempt either way a reversal,
            # so a thrashing market produces a steady ±max_step hold instead of a drift.
            #
            # It is self-limiting: once the market stops alternating, two consecutive
            # attempts share a direction, the second is not a reversal, and the hatch
            # reopens after one track of delay.
            self._last_sharp_direction = direction
            self._tracks_since_sharp = 0
        elif abs(gap) <= allowance:
            target = desired
            reason = "within the normal step allowance"
        else:
            target = previous + allowance * (1.0 if gap > 0 else -1.0)
            reason = (
                f"step limited to {allowance:.0f} points"
                + (f" (widened by divergence pressure {divergence_strength:.2f})"
                   if divergence_strength > 0 else "")
            )

        peak_relief = False
        if (
            self._consecutive_peaks >= self._max_consecutive_peaks
            and target >= self._peak_threshold
        ):
            # Pull below the peak threshold regardless of the market. A sustained
            # maximum is exhausting, and §98 forbids sitting at an extreme.
            target = min(target, self._peak_threshold - 6.0)
            peak_relief = True
            reason = (
                f"{self._consecutive_peaks} consecutive peak tracks; easing off "
                "regardless of market energy"
            )

        target = clamp(target)
        self._current_energy = target
        self._register_peak(target)

        return EnergyPlan(
            target_energy=target,
            market_energy=market,
            previous_energy=previous,
            sharp_change=sharp_change,
            sharp_change_suppressed=sharp_change_suppressed,
            peak_relief=peak_relief,
            velocity_contribution=velocity_contribution,
            reason=reason,
        )

    def _register_peak(self, energy: float) -> None:
        if energy >= self._peak_threshold:
            self._consecutive_peaks += 1
        else:
            self._consecutive_peaks = 0


__all__ = [
    "DEFAULT_MAX_STEP",
    "MAX_CONSECUTIVE_PEAKS",
    "PEAK_THRESHOLD",
    "SHARP_CHANGE_COOLDOWN_TRACKS",
    "SHARP_CHANGE_FRACTION",
    "SHARP_CHANGE_GAP",
    "SHARP_CHANGE_STEP_MULTIPLIER",
    "VELOCITY_GAIN",
    "EnergyPlan",
    "RadioEnergyPlanner",
]
