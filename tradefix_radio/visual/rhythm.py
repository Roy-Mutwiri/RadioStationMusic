"""The long-run work rhythm: focus, fatigue, and recovery as a cycle.

Replaces the one-directional fatigue accumulator that the V6 soak found pinning at its
ceiling after roughly sixteen hours. That version was wrong in a specific, instructive
way: it modelled fatigue as a *quantity that only goes up*, with small step decrements
for coffee. Two failures followed from that single choice, and they pulled in opposite
directions so no amount of tuning fixed both.

* With a large step decrement (0.25 per coffee) the measured 2.17 coffees per hour
  produced 13.0 of recovery against 3.43 of accumulation across a day. The phase never
  rose above 0.16 and the whole fatigue family fired twenty-four times in twenty-four
  hours — the rhythm did not exist.
* With a small one (0.035) the phase reached the ceiling at about hour sixteen and
  stayed there. A character permanently on the late shift.

The fix is not a better constant. It is to model the thing that actually happens: a
**cycle**.

```
FOCUS_BUILD  ──▶  DEEP_WORK  ──▶  FATIGUE_RISE  ──▶  RECOVERY  ──┐
     ▲                                                            │
     └────────────────────────────────────────────────────────────┘
```

Four properties this design has and the old one could not:

**Phases set rates, never values.** Nothing here assigns `fatigue_level`; every phase
contributes a per-second derivative, integrated against real elapsed time. That is what
makes transitions smooth by construction rather than by easing applied afterward — there
is no jump to smooth.

**Transition thresholds are sampled.** `DEEP_WORK` ends when fatigue crosses a threshold
drawn from [0.45, 0.65], not a constant. Combined with a workload-dependent accumulation
rate, consecutive cycles differ in length by tens of minutes. A fixed-period oscillation
is as mechanical as a pinned ceiling, and the brief rules out both.

**Recovery influences change the rate, not the level.** Coffee does not subtract from
fatigue. It raises `recovery_rate` for a while, which makes the `RECOVERY` phase steeper
and the accumulation phases shallower. The brief's instruction — *do not instantly reset
fatigue to zero, use gradual decay* — falls out of that rather than being enforced on top.

**The ceiling is unreachable.** `FATIGUE_RISE` exits into `RECOVERY` at a threshold drawn
from [0.74, 0.90], below the 0.95 clamp. The clamp exists as a guard against a pathological
workload, not as a value the system is expected to visit.

This is behavioural pacing, not health simulation. It changes *when* he stretches and how
long he holds a gaze. It does not degrade him: the four-percent share ceiling on the
fatigue family holds at every level, and every fatigue action still chains a refocus.
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass, field
from typing import Final

from tradefix_radio.visual.contracts import IntensityBand

# ============================================================ phases


class WorkPhase(str, enum.Enum):
    """Where in the long cycle he is.

    Named for what he is *doing*, not for how tired he is, because the phase drives
    behaviour selection and "deep work" is a useful thing to condition on in a way that
    "fatigue 0.52" is not.
    """

    FOCUS_BUILD = "focus_build"
    """Settling in. Focus climbing, fatigue barely moving."""

    DEEP_WORK = "deep_work"
    """The productive stretch. Focus high and flat, fatigue accumulating with workload."""

    FATIGUE_RISE = "fatigue_rise"
    """Still working, visibly costing more. Focus declining, fatigue accelerating."""

    RECOVERY = "recovery"
    """Easing off. Fatigue decaying, focus rebuilding. Not a break — he is still at the
    desk, just pacing himself."""


# ============================================================ rates
#
# All per-second, integrated against real elapsed time. Sized so one full cycle runs
# roughly 3-6 hours at typical workload, which over 24 h gives four to seven cycles.

#: Base fatigue accumulation, per second, at full workload.
#:
#: 1.4e-4 means 0.5 of fatigue in one hour at workload 1.0, or about three hours at the
#: workload a steady market actually produces. That is the figure the cycle length
#: follows from.
FATIGUE_BASE_RATE: Final = 1.4e-4

#: Phase multipliers on the accumulation rate.
FATIGUE_PHASE_SCALE: Final[dict[WorkPhase, float]] = {
    WorkPhase.FOCUS_BUILD: 0.40,
    WorkPhase.DEEP_WORK: 1.00,
    WorkPhase.FATIGUE_RISE: 1.55,
    WorkPhase.RECOVERY: 0.0,
}

#: Base recovery, per second, at `recovery_rate` 1.0. Clears 0.65 of fatigue in about an
#: hour at full rate, and three at the rate a busy market allows.
RECOVERY_BASE_RATE: Final = 2.0e-4

#: Focus rise and decay, per second.
FOCUS_RISE_RATE: Final = 1.0 / 1_500.0
FOCUS_DECAY_RATE: Final = 1.0 / 4_200.0

#: Hard bounds. The fatigue ceiling is a guard, not a destination — see the module
#: docstring. `RECOVERY_TRIGGER_RANGE` always fires below it.
FATIGUE_FLOOR: Final = 0.02
FATIGUE_CEILING: Final = 0.95
FOCUS_FLOOR: Final = 0.10
FOCUS_CEILING: Final = 1.00

# ============================================================ thresholds
#
# Sampled per cycle, never constant. This is what stops the rhythm becoming a fixed
# periodic oscillation, which the brief rules out as explicitly as it rules out pinning.

FOCUS_TARGET_RANGE: Final = (0.70, 0.90)
DEEP_WORK_EXIT_RANGE: Final = (0.45, 0.65)
RECOVERY_TRIGGER_RANGE: Final = (0.74, 0.90)
RECOVERY_EXIT_RANGE: Final = (0.12, 0.30)

#: Floors on time in phase, seconds. Prevents a thrash when a threshold sits right at the
#: current level — without them a workload spike can bounce the phase twice in a minute,
#: and the brief asks for no sudden state jumps.
MIN_TIME_IN_PHASE: Final[dict[WorkPhase, float]] = {
    WorkPhase.FOCUS_BUILD: 600.0,
    WorkPhase.DEEP_WORK: 1_200.0,
    WorkPhase.FATIGUE_RISE: 900.0,
    WorkPhase.RECOVERY: 1_200.0,
}

# ============================================================ recovery influences

#: Baseline recovery rate with no influences at all.
RECOVERY_BASELINE: Final = 0.30

#: A quiet or dormant market is itself restorative — less to track, longer holds.
QUIET_BAND_BONUS: Final = 0.22

#: Low action density contributes up to this much.
LOW_WORKLOAD_BONUS: Final = 0.24

#: Transient bonuses, and how long each decays over. These are the brief's recovery
#: influences, and they raise the *rate* rather than subtracting from the level.
@dataclass(frozen=True, slots=True)
class Influence:
    """One transient recovery influence: its strength and how long it lasts."""

    name: str
    strength: float
    decay_seconds: float


INFLUENCES: Final[dict[str, Influence]] = {
    "coffee": Influence("coffee", 0.30, 900.0),
    "posture_reset": Influence("posture_reset", 0.18, 600.0),
    "long_gaze_hold": Influence("long_gaze_hold", 0.10, 240.0),
    "session_transition": Influence("session_transition", 0.22, 1_800.0),
}

#: Workload normalisation. `workload` arrives as a rolling activity level in [0, 1] from
#: the director, already damped, so this is a light smoothing rather than a scale.
WORKLOAD_SMOOTHING: Final = 0.02


# ============================================================ the rhythm


@dataclass
class WorkRhythm:
    """Long-run behavioural pacing. Cyclic, bounded, aperiodic.

    Deterministic under an injected RNG, so a soak finding is reproducible and the cycle
    count is a stable measurement rather than a sample.
    """

    phase: WorkPhase = WorkPhase.FOCUS_BUILD
    fatigue_level: float = 0.08
    focus_level: float = 0.25
    recovery_rate: float = RECOVERY_BASELINE
    workload: float = 0.3
    time_in_phase: float = 0.0

    #: Completed cycles, counted on each return to FOCUS_BUILD.
    cycles_completed: int = 0

    _rng: random.Random = field(default_factory=random.Random)
    _last_monotonic: float | None = None
    _started_monotonic: float = 0.0
    #: Active transient influences: name -> (added_at, strength, decay_seconds).
    _influences: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    #: The current cycle's sampled thresholds.
    _focus_target: float = 0.80
    _deep_work_exit: float = 0.55
    _recovery_trigger: float = 0.82
    _recovery_exit: float = 0.20
    #: Phase durations of the last few cycles, for the report and for the aperiodicity
    #: assertion. Bounded: a 24/7 process cannot grow a list forever.
    cycle_durations: list[float] = field(default_factory=list)
    _cycle_started: float = 0.0

    def __post_init__(self) -> None:
        self._resample_thresholds()

    def seed(self, rng: random.Random) -> None:
        """Inject the RNG. Called by the director so the whole layer shares one stream."""
        self._rng = rng
        self._resample_thresholds()

    # ------------------------------------------------------------ sampling

    def _resample_thresholds(self) -> None:
        self._focus_target = self._rng.uniform(*FOCUS_TARGET_RANGE)
        self._deep_work_exit = self._rng.uniform(*DEEP_WORK_EXIT_RANGE)
        self._recovery_trigger = self._rng.uniform(*RECOVERY_TRIGGER_RANGE)
        self._recovery_exit = self._rng.uniform(*RECOVERY_EXIT_RANGE)

    # ------------------------------------------------------------ integration

    def advance(
        self,
        now_monotonic: float,
        *,
        workload: float | None = None,
        band: IntensityBand | None = None,
    ) -> float:
        """Integrate to ``now`` and return the current fatigue level.

        ``workload`` is the director's rolling activity level, already damped and in
        [0, 1]. ``band`` lets a quiet market be restorative in itself.
        """
        if self._last_monotonic is None:
            self._last_monotonic = now_monotonic
            self._started_monotonic = now_monotonic
            self._cycle_started = now_monotonic
            return self.fatigue_level

        elapsed = max(0.0, now_monotonic - self._last_monotonic)
        self._last_monotonic = now_monotonic
        if elapsed == 0.0:
            return self.fatigue_level

        if workload is not None:
            # Smoothed rather than taken raw: the director's activity measure moves on a
            # one-minute window, and feeding that straight in makes the long cycle
            # jitter against a short signal.
            target = max(0.0, min(1.0, workload))
            blend = min(1.0, WORKLOAD_SMOOTHING * elapsed)
            self.workload += (target - self.workload) * blend

        self.recovery_rate = self._compute_recovery_rate(now_monotonic, band)
        self._integrate(elapsed)
        self.time_in_phase += elapsed
        self._maybe_transition(now_monotonic)
        return self.fatigue_level

    def _integrate(self, elapsed: float) -> None:
        """One step of the differential. Rates only — nothing assigns a level."""
        if self.phase is WorkPhase.RECOVERY:
            decay = RECOVERY_BASE_RATE * self.recovery_rate * elapsed
            self.fatigue_level = max(FATIGUE_FLOOR, self.fatigue_level - decay)
            # Focus rebuilds during recovery, but more slowly than it does while
            # settling in: easing off is not the same as warming up.
            rebuild = FOCUS_RISE_RATE * elapsed * 0.6
            self.focus_level = min(FOCUS_CEILING, self.focus_level + rebuild)
            return

        # Accumulation phases. Recovery influences do not subtract here; they damp the
        # rate, which is the honest version of "a coffee helps but does not undo an hour".
        damping = 1.0 - 0.45 * self.recovery_rate
        gain = (
            FATIGUE_BASE_RATE
            * FATIGUE_PHASE_SCALE[self.phase]
            * (0.25 + 0.75 * self.workload)
            * damping
            * elapsed
        )
        self.fatigue_level = min(FATIGUE_CEILING, self.fatigue_level + gain)

        if self.phase is WorkPhase.FOCUS_BUILD:
            self.focus_level = min(FOCUS_CEILING, self.focus_level + FOCUS_RISE_RATE * elapsed)
        elif self.phase is WorkPhase.DEEP_WORK:
            # Holds, with a slow drift down so DEEP_WORK does not read as indefinite.
            self.focus_level = max(
                FOCUS_FLOOR, self.focus_level - FOCUS_DECAY_RATE * elapsed * 0.35
            )
        else:  # FATIGUE_RISE
            self.focus_level = max(FOCUS_FLOOR, self.focus_level - FOCUS_DECAY_RATE * elapsed)

    def _maybe_transition(self, now_monotonic: float) -> None:
        """Threshold-driven, with a floor on time in phase to prevent thrash."""
        if self.time_in_phase < MIN_TIME_IN_PHASE[self.phase]:
            return

        if self.phase is WorkPhase.FOCUS_BUILD:
            if self.focus_level >= self._focus_target:
                self._enter(WorkPhase.DEEP_WORK)
        elif self.phase is WorkPhase.DEEP_WORK:
            if self.fatigue_level >= self._deep_work_exit:
                self._enter(WorkPhase.FATIGUE_RISE)
        elif self.phase is WorkPhase.FATIGUE_RISE:
            # Two ways out, and the second is what makes the influences matter: a strong
            # sustained recovery signal can start the recovery early rather than only
            # steepening it once it has begun.
            early = self.recovery_rate > 0.72 and self.fatigue_level > 0.55
            if self.fatigue_level >= self._recovery_trigger or early:
                self._enter(WorkPhase.RECOVERY)
        elif self.phase is WorkPhase.RECOVERY and self.fatigue_level <= self._recovery_exit:
            self.cycles_completed += 1
            self.cycle_durations.append(now_monotonic - self._cycle_started)
            if len(self.cycle_durations) > 64:
                del self.cycle_durations[:32]
            self._cycle_started = now_monotonic
            self._resample_thresholds()
            self._enter(WorkPhase.FOCUS_BUILD)

    def _enter(self, phase: WorkPhase) -> None:
        self.phase = phase
        self.time_in_phase = 0.0

    def _compute_recovery_rate(
        self, now_monotonic: float, band: IntensityBand | None
    ) -> float:
        rate = RECOVERY_BASELINE
        if band is not None and band.rank <= IntensityBand.B1_QUIET.rank:
            rate += QUIET_BAND_BONUS
        rate += LOW_WORKLOAD_BONUS * (1.0 - self.workload)

        expired = []
        for name, (added_at, strength, decay) in self._influences.items():
            age = now_monotonic - added_at
            if age >= decay:
                expired.append(name)
                continue
            # Linear decay. Exponential would be more physical and is indistinguishable
            # at this timescale; linear makes the contribution easy to reason about when
            # reading a soak report.
            rate += strength * (1.0 - age / decay)
        for name in expired:
            del self._influences[name]

        return max(0.0, min(1.0, rate))

    # ------------------------------------------------------------ influences

    def note(self, influence: str, now_monotonic: float) -> None:
        """Record a recovery influence. Raises the rate; never touches the level."""
        spec = INFLUENCES.get(influence)
        if spec is None:
            raise KeyError(
                f"unknown recovery influence {influence!r}; "
                f"known: {', '.join(sorted(INFLUENCES))}"
            )
        self._influences[influence] = (now_monotonic, spec.strength, spec.decay_seconds)

    @property
    def active_influences(self) -> tuple[str, ...]:
        return tuple(sorted(self._influences))

    # ------------------------------------------------------------ effects

    def blink_rate_scale(self, _now: float = 0.0) -> float:
        """Blink interval multiplier. Below 1.0 means blinking more often.

        Subtle by design: 1.0 down to about 0.78 across the whole range. The brief allows
        a slight increase during fatigue and asks for it to stay subtle; a visibly
        fluttering eye is what being generous here produces.
        """
        return 1.0 - 0.22 * self.fatigue_level

    def dwell_scale(self, _now: float = 0.0) -> float:
        """Gaze and posture holds lengthen with fatigue and shorten with focus."""
        return 1.0 + 0.24 * self.fatigue_level - 0.08 * self.focus_level

    def fatigue_action_gate(self, _now: float = 0.0) -> float:
        """Weight multiplier for the FATIGUE family. Zero below its availability floor.

        Gated on `fatigue_level` rather than on phase, so a long quiet stretch that keeps
        fatigue low correctly suppresses the family even inside `FATIGUE_RISE`.
        """
        if self.fatigue_level < 0.32:
            return 0.0
        return min(1.5, (self.fatigue_level - 0.32) / 0.33)

    def coffee_weight_scale(self, _now: float = 0.0) -> float:
        """Coffee becomes more attractive as fatigue rises and focus falls."""
        return 1.0 + 0.7 * self.fatigue_level + 0.3 * (1.0 - self.focus_level)

    def work_intensity_scale(self) -> float:
        """Multiplier on WORK-family weight. High focus works harder.

        The behavioural payoff of tracking focus separately: `DEEP_WORK` at high focus
        produces more work actions than `RECOVERY` at the same market band, so the
        character's output varies over hours for reasons the market did not cause.
        """
        return 0.82 + 0.30 * self.focus_level

    # ------------------------------------------------------------ reporting

    @property
    def elapsed_hours(self) -> float:
        if self._last_monotonic is None:
            return 0.0
        return (self._last_monotonic - self._started_monotonic) / 3600.0

    def snapshot(self) -> dict[str, float | str | int]:
        return {
            "phase": self.phase.value,
            "fatigue_level": round(self.fatigue_level, 4),
            "focus_level": round(self.focus_level, 4),
            "recovery_rate": round(self.recovery_rate, 4),
            "workload": round(self.workload, 4),
            "time_in_phase": round(self.time_in_phase, 1),
            "cycles_completed": self.cycles_completed,
        }


@dataclass
class RhythmTrace:
    """Sampled fatigue history, and the statistics the brief asks for.

    Kept out of :class:`WorkRhythm` because the runtime has no use for it: the director
    needs the current level, and only a soak needs the series. A 24/7 process should not
    carry a day of samples it never reads.
    """

    #: Sampling interval, seconds. 60 is ample for a cycle measured in hours.
    interval_seconds: float = 60.0
    samples: list[float] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    _next_sample_at: float = 0.0

    #: Bands for the floor and ceiling occupancy figures.
    floor_below: float = 0.15
    ceiling_above: float = 0.80

    def observe(self, rhythm: WorkRhythm, now_monotonic: float) -> None:
        if now_monotonic < self._next_sample_at:
            return
        self._next_sample_at = now_monotonic + self.interval_seconds
        self.samples.append(rhythm.fatigue_level)
        self.phases.append(rhythm.phase.value)

    # -- statistics --------------------------------------------------------

    @property
    def minimum(self) -> float:
        return min(self.samples) if self.samples else 0.0

    @property
    def maximum(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def time_near_floor(self) -> float:
        """Fraction of samples below :attr:`floor_below`."""
        if not self.samples:
            return 0.0
        return sum(1 for s in self.samples if s < self.floor_below) / len(self.samples)

    @property
    def time_near_ceiling(self) -> float:
        """Fraction of samples above :attr:`ceiling_above`.

        The figure the V6 soak would have failed: the old accumulator spent roughly a
        third of a day above 0.80, and the last eight hours pinned at 1.0.
        """
        if not self.samples:
            return 0.0
        return sum(1 for s in self.samples if s > self.ceiling_above) / len(self.samples)

    @property
    def pinned_at_ceiling(self) -> bool:
        """Whether the series ends in a run long enough to read as pinned."""
        if len(self.samples) < 30:
            return False
        tail = self.samples[-30:]
        return all(s >= FATIGUE_CEILING - 1e-6 for s in tail)

    @property
    def pinned_at_floor(self) -> bool:
        if len(self.samples) < 30:
            return False
        tail = self.samples[-30:]
        return all(s <= FATIGUE_FLOOR + 1e-6 for s in tail)

    def turning_points(self) -> int:
        """Direction changes in the sampled series — a cycle count that needs no state.

        Counted independently of :attr:`WorkRhythm.cycles_completed` on purpose: the
        phase counter can only be right if the phase machine is right, and this is the
        check that the *level* actually rises and falls rather than merely being labelled
        as doing so.
        """
        if len(self.samples) < 3:
            return 0
        turns = 0
        direction = 0
        for previous, current in zip(self.samples, self.samples[1:], strict=False):
            delta = current - previous
            if abs(delta) < 1e-6:
                continue
            sign = 1 if delta > 0 else -1
            if direction and sign != direction:
                turns += 1
            direction = sign
        return turns

    def phase_shares(self) -> dict[str, float]:
        if not self.phases:
            return {}
        total = len(self.phases)
        out: dict[str, float] = {}
        for phase in self.phases:
            out[phase] = out.get(phase, 0.0) + 1.0 / total
        return {k: round(v, 4) for k, v in sorted(out.items())}

    def summary(self) -> str:
        return (
            f"fatigue  min {self.minimum:.3f}  max {self.maximum:.3f}  "
            f"mean {self.mean:.3f}  near-floor {self.time_near_floor * 100:.1f} %  "
            f"near-ceiling {self.time_near_ceiling * 100:.1f} %  "
            f"turns {self.turning_points()}"
        )


__all__ = [
    "DEEP_WORK_EXIT_RANGE",
    "FATIGUE_BASE_RATE",
    "FATIGUE_CEILING",
    "FATIGUE_FLOOR",
    "FOCUS_TARGET_RANGE",
    "INFLUENCES",
    "MIN_TIME_IN_PHASE",
    "RECOVERY_BASELINE",
    "RECOVERY_BASE_RATE",
    "RECOVERY_EXIT_RANGE",
    "RECOVERY_TRIGGER_RANGE",
    "Influence",
    "RhythmTrace",
    "WorkPhase",
    "WorkRhythm",
]
