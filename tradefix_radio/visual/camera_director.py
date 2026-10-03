"""The Camera Director: which of the seven frozen compositions is live.

Behavioural, not random. A cut happens because something became worth looking at, or
because a long hold expired, or because the market materially changed — never because a
timer hit a round number.

Three ideas carry the design:

**A cut needs a motivation, a satisfied minimum hold, and no veto.** All three, and the
order matters: motivations are scored, the hold is a gate, and the vetoes are checked
last and absolutely. A cut with no motivation is the "predictable rotation" the brief
rules out; a cut that passes motivation but violates a veto is the visible pop.

**Safety is a veto, never a weight.** A weight can always be overcome by enough other
multipliers. "Never cut during an object acquisition" has to mean never, so it is a
filter applied after scoring, and `CameraSoakReport.unsafe_cuts` must be zero.

**`CAM_6` is gated, not weighted.** The acceptance criterion is that the hands shot is
used *only when close interaction is visible*. A weight, however small, eventually fires
on an empty desk — so the camera is simply unavailable unless a desk interaction is in
flight. Same shape as the camera-gaze gate in the behaviour director, for the same
reason.

What this module does not do: invent camera geometry. The seven transforms are frozen V1
(`GEOMETRY_FREEZE.md`) and this only chooses between them.
"""

from __future__ import annotations

import enum
import math
import random
from dataclasses import dataclass, field
from typing import Final

from tradefix_radio.director.selection import Candidate, Constraint, WeightedSelector
from tradefix_radio.visual.camera import (
    CAMERA_METADATA,
    DEFAULT_CAMERA,
    TRANSITION_CROSSFADE,
    TRANSITION_CUT,
    TRANSITION_PUSH_CONTINUE,
    CameraState,
)
from tradefix_radio.visual.catalog import CHAINS, spec
from tradefix_radio.visual.contracts import (
    CharacterActionV1,
    CharacterState,
    IntensityBand,
    TransitionState,
    VisualStateV1,
)

# ============================================================ families
#
# Grouped by what the shot is *for*, so anti-repetition can act on purpose rather than
# on identity. `CAM_1 -> CAM_7 -> CAM_1` is three hero shots in a row even though no
# camera repeated, and that reads as indecision.


class CameraFamily(str, enum.Enum):
    HERO = "hero"
    PROFILE = "profile"
    WORK = "work"
    INTIMATE = "intimate"
    ATMOSPHERE = "atmosphere"


CAMERA_FAMILY: Final[dict[str, CameraFamily]] = {
    "CAM_1": CameraFamily.HERO,
    "CAM_7": CameraFamily.HERO,
    "CAM_2": CameraFamily.PROFILE,
    "CAM_3": CameraFamily.WORK,
    "CAM_6": CameraFamily.WORK,
    "CAM_4": CameraFamily.INTIMATE,
    "CAM_5": CameraFamily.ATMOSPHERE,
}

#: The home shot. Must remain primary — acceptance criterion.
PRIMARY_CAMERA: Final = DEFAULT_CAMERA  # CAM_1, the hero front

#: How long away from the home shot before its pull reaches full strength, seconds.
#:
#: Primacy is expressed as *return to master*, not as a flat multiplier. A constant bonus
#: makes the home shot win almost every contest it enters, which is a different defect
#: from the one being fixed: the stream stops exploring. A bias that grows with time away
#: makes `CAM_1` the shot the stream keeps coming back to, which is what "primary"
#: actually means, and it composes correctly with the family penalty — `CAM_1 -> CAM_7 ->
#: CAM_1` is still discouraged, but a return after several minutes away is not.
HOME_RETURN_SECONDS: Final = 420.0

#: The home bias at full strength. Reached only after `HOME_RETURN_SECONDS` away.
HOME_BIAS_MAX: Final = 2.6

# ============================================================ timing

#: Absolute floor on any hold, seconds. No market condition justifies faster cutting,
#: and this outranks every motivation and every operator slider.
MINIMUM_HOLD_FLOOR: Final = 30.0

#: Two cuts may never fall inside this window, whatever else is true.
#:
#: **Currently subsumed by :data:`MINIMUM_HOLD_FLOOR` and kept deliberately.** Both are
#: measured from the same instant — the last cut sets `since_monotonic` — so an elapsed
#: time past the 30 s floor already exceeds this 15 s window, and the check can never be
#: the one that fires. It stays as a guard: the floor is a pacing decision that a future
#: tuning pass might lower, and this is the hard limit that must survive it.
#: `test_the_floor_subsumes_the_double_cut_window` asserts the relationship rather than
#: pretending the veto is reachable.
DOUBLE_CUT_WINDOW: Final = 15.0

#: No cut for this long after a market reaction begins. A cut on the reaction frame turns
#: the single most meaningful moment in the system into an edit.
REACTION_LOCKOUT: Final = 2.5

#: Hold distribution per band, as (minimum, median, maximum) seconds.
#:
#: Sampled log-normally about the median rather than uniformly, which is what produces
#: mostly-long holds with an occasional short one — a uniform draw over the same range
#: gives far too many mid-length shots and reads as a rotation.
HOLD_DISTRIBUTION: Final[dict[IntensityBand, tuple[float, float, float]]] = {
    IntensityBand.B0_DORMANT: (75.0, 210.0, 420.0),
    IntensityBand.B1_QUIET: (70.0, 180.0, 360.0),
    IntensityBand.B2_STEADY: (55.0, 135.0, 290.0),
    IntensityBand.B3_FOCUSED: (48.0, 110.0, 240.0),
    IntensityBand.B4_ALERT: (40.0, 80.0, 170.0),
    IntensityBand.B5_PEAK: (35.0, 62.0, 130.0),
}

#: Spread of the log-normal hold sample. 0.45 gives a long right tail without the
#: occasional absurd ten-minute hold a wider sigma produces.
HOLD_SIGMA: Final = 0.45

#: Exponent applied to behaviour affinity, so it competes with the recency, family and
#: share penalties rather than being diluted by them.
#:
#: Measured at matched durations and seed, state alignment:
#:
#:     power    2 h      8 h
#:     1.0     26.5 %   28.1 %
#:     1.6     26.5 %   30.4 %
#:     2.2     29.4 %   30.7 %
#:
#: A real but small effect, and the smallness is structural rather than a tuning
#: shortfall: a 130-second hold spans three or four character states at a mean dwell near
#: 35 seconds, so no amount of optimising at the instant of the cut can keep the camera
#: aligned for the whole hold. Around 30 % is close to what cut-time choice can reach.
#:
#: What steers framing where it matters is not this multiplier. It is the CAM_6 gate (the
#: hands shot exists only while a desk interaction does) and CAM_4's reaction affinity.
#: Those cover the moments a viewer would notice; the rest of the time several cameras
#: are equally correct, which is why the figure looks low and the result does not.
BEHAVIOUR_AFFINITY_POWER: Final = 2.2

# ============================================================ motivations


class CutMotivation(str, enum.Enum):
    """Why a cut is being considered. A cut with none of these does not happen."""

    HOLD_EXPIRED = "hold_expired"
    """The sampled hold ran out. The ordinary case."""

    INTERACTION_VISIBLE = "interaction_visible"
    """An object chain or desk interaction started and a closer shot would show it."""

    MARKET_SHIFT = "market_shift"
    """The intensity band moved materially."""

    TRACK_TRANSITION = "track_transition"
    """A song transition — the one place music may open a cut."""

    DIVERSITY = "diversity"
    """A camera has gone unused long enough that the stream is narrowing."""

    REACTION_RESOLVED = "reaction_resolved"
    """A market reaction finished; its close-up has served its purpose."""

    OPERATOR = "operator"
    """A manual request from the console."""


#: Weight each motivation contributes, and the earliest fraction of the sampled hold at
#: which it may fire. `HOLD_EXPIRED` is 1.0 by definition; the others can cut early.
MOTIVATION_WEIGHT: Final[dict[CutMotivation, float]] = {
    CutMotivation.HOLD_EXPIRED: 1.0,
    CutMotivation.INTERACTION_VISIBLE: 1.6,
    CutMotivation.MARKET_SHIFT: 1.3,
    CutMotivation.TRACK_TRANSITION: 0.45,
    CutMotivation.DIVERSITY: 0.8,
    CutMotivation.REACTION_RESOLVED: 0.9,
    CutMotivation.OPERATOR: 10.0,
}

MOTIVATION_EARLIEST: Final[dict[CutMotivation, float]] = {
    CutMotivation.HOLD_EXPIRED: 1.0,
    CutMotivation.INTERACTION_VISIBLE: 0.60,
    CutMotivation.MARKET_SHIFT: 0.50,
    CutMotivation.TRACK_TRANSITION: 0.65,
    CutMotivation.DIVERSITY: 0.85,
    CutMotivation.REACTION_RESOLVED: 0.55,
    CutMotivation.OPERATOR: 0.0,
}

#: Probability a licensed (non-expiry) motivation actually produces a cut.
#:
#: Licences rather than triggers, and the music one is lowest on purpose. A guaranteed cut
#: on every song transition would teach viewers to read a cut as a track change, and a
#: camera that cuts on every drop turns the stream into a music video.
#: Measured at the first draft's values: 36 cuts/hour, 37 % of them from
#: INTERACTION_VISIBLE. The brief asks for mostly-long shots and says not to force a cut
#: merely because an affinity exists, so the early motivations are rationed hard. These
#: are *per consideration*, and the director considers on every tick, so a 0.02 here is
#: still a cut within a minute of becoming eligible.
MOTIVATION_PROBABILITY: Final[dict[CutMotivation, float]] = {
    CutMotivation.HOLD_EXPIRED: 1.0,
    CutMotivation.INTERACTION_VISIBLE: 0.004,
    CutMotivation.MARKET_SHIFT: 0.010,
    CutMotivation.TRACK_TRANSITION: 0.002,
    CutMotivation.DIVERSITY: 0.006,
    CutMotivation.REACTION_RESOLVED: 0.004,
    CutMotivation.OPERATOR: 1.0,
}

#: How long a camera must go unused before `DIVERSITY` starts arguing for it.
DIVERSITY_STARVATION_SECONDS: Final = 1_800.0

#: How long an event motivation stays live after its event, seconds.
#:
#: Needed because the probabilities are per *consideration* and deliberately tiny. A
#: band change evaluated on the single tick it occurred had one 1 % chance and so never
#: fired: measured zero early cuts across twenty-four runs. Over a 90-second window at
#: 20 Hz it fires reliably without ever being a guaranteed cut, which is the intended
#: shape — a licence, not a trigger.
EVENT_WINDOW_SECONDS: Final = 90.0

# ============================================================ safety


class CutVeto(str, enum.Enum):
    """Why a cut was refused. Reported, so a soak can show what the vetoes caught."""

    BELOW_FLOOR = "below_minimum_hold_floor"
    BELOW_SAMPLED_HOLD = "before_the_sampled_hold"
    DOUBLE_CUT = "second_cut_inside_15s"
    MID_BLEND = "action_inside_a_blend_window"
    OBJECT_ACQUISITION = "object_being_acquired_or_released"
    COMMITTED_CHAIN_INVISIBLE = "target_cannot_show_a_committed_chain"
    REACTION_LOCKOUT = "within_2.5s_of_a_reaction"
    SAME_CAMERA = "already_live"
    CAM6_NO_INTERACTION = "cam_6_without_a_visible_interaction"
    NO_MOTIVATION = "no_motivation"

    @property
    def is_safety(self) -> bool:
        """Whether this veto protects the picture rather than the pacing.

        The distinction matters for the soak: a pacing veto firing often is healthy, and a
        safety veto firing at all means something tried to do the wrong thing.
        """
        return self in {
            CutVeto.MID_BLEND,
            CutVeto.OBJECT_ACQUISITION,
            CutVeto.COMMITTED_CHAIN_INVISIBLE,
            CutVeto.REACTION_LOCKOUT,
        }


#: Chain steps during which the hands are taking or releasing an object. Cutting here
#: shows a hand arriving at nothing, or a mug changing hands across an edit.
ACQUISITION_STEPS: Final[frozenset[str]] = frozenset({
    "pick_cup", "place_cup", "acquire_pen", "return_pen", "reach_cup", "reach_pen",
})

#: Anchors that mean "a desk interaction is visible", which is what gates `CAM_6`.
DESK_ANCHORS: Final[frozenset[str]] = frozenset({
    "ANCHOR_MOUSE", "ANCHOR_KEYBOARD_HOME_L", "ANCHOR_KEYBOARD_HOME_R",
    "ANCHOR_MUG_BODY", "ANCHOR_MUG_RING", "ANCHOR_MUG_LIP",
    "ANCHOR_NOTEBOOK", "ANCHOR_PEN", "ANCHOR_STREAM_PAD",
})

# ============================================================ transitions

#: Transition shares. Hard cut dominates because it is what a real multi-camera
#: production uses and the only transition that is invisible when it is right.
TRANSITION_WEIGHTS: Final[dict[str, float]] = {
    TRANSITION_CUT: 0.80,
    TRANSITION_CROSSFADE: 0.15,
    TRANSITION_PUSH_CONTINUE: 0.05,
}

TRANSITION_MS: Final[dict[str, int]] = {
    TRANSITION_CUT: 0,
    TRANSITION_CROSSFADE: 420,
    TRANSITION_PUSH_CONTINUE: 1_200,
}

#: The one piece of overt camera language: the hero front tightening into the close-up as
#: a single continuous move. Capped because its entire value is being rare.
PUSH_CONTINUE_PAIR: Final = ("CAM_1", "CAM_4")
PUSH_CONTINUE_MAX_PER_HOUR: Final = 2


# ============================================================ decision


@dataclass(frozen=True, slots=True)
class ActionView:
    """A running action with its monotonic timing attached.

    The camera director needs to know whether an action is inside a blend window, and
    `CharacterActionV1` cannot answer that: it carries a wall-clock `started_at` for the
    renderer, not a monotonic start. The first version of the blend veto tried to infer
    it from `blend_in_ms` alone and so vetoed every cut while any action with a non-zero
    blend was running — which is nearly always, and would have frozen the camera.
    """

    action: CharacterActionV1
    started_monotonic: float
    ends_monotonic: float

    def in_blend(self, now: float) -> bool:
        """Whether a cut now would land inside a blend-in or blend-out window."""
        elapsed = now - self.started_monotonic
        if elapsed < self.action.blend_in_ms / 1000.0:
            return True
        remaining = self.ends_monotonic - now
        return 0.0 <= remaining < self.action.blend_out_ms / 1000.0

    @property
    def anchor(self) -> str | None:
        return self.action.anchor

    @property
    def action_id(self) -> str:
        return self.action.action_id


@dataclass(frozen=True, slots=True)
class CameraDecision:
    """What the camera director decided, and why.

    Carries the rejected candidates and the vetoes as well as the choice, because tuning
    a camera system blind is how a stream quietly acquires a rotation.
    """

    camera_id: str
    changed: bool
    transition: str
    transition_ms: int
    motivation: CutMotivation | None
    hold_target_seconds: float
    reason: str
    factors: dict[str, float] = field(default_factory=dict)
    vetoes: dict[str, str] = field(default_factory=dict)

    @property
    def is_cut(self) -> bool:
        return self.changed


@dataclass
class CameraSoakReport:
    """What a camera soak measured."""

    duration_seconds: float = 0.0
    cuts: int = 0
    holds: list[float] = field(default_factory=list)
    camera_counts: dict[str, int] = field(default_factory=dict)
    camera_time: dict[str, float] = field(default_factory=dict)
    family_sequence: list[str] = field(default_factory=list)
    camera_sequence: list[str] = field(default_factory=list)
    transition_counts: dict[str, int] = field(default_factory=dict)
    motivation_counts: dict[str, int] = field(default_factory=dict)
    veto_counts: dict[str, int] = field(default_factory=dict)

    #: Must be zero. A safety veto that was bypassed rather than merely not needed.
    unsafe_cuts: list[str] = field(default_factory=list)
    #: Cuts to CAM_6 with no desk interaction visible. Must be zero.
    cam6_without_interaction: list[str] = field(default_factory=list)
    #: Notable actions whose camera affinity excluded the live camera while they ran.
    invisible_action_seconds: float = 0.0
    visible_action_seconds: float = 0.0
    #: Time the live camera had an above-neutral behaviour affinity for the character
    #: state. The metric the design can actually answer — see `state_alignment`.
    aligned_state_seconds: float = 0.0
    misaligned_state_seconds: float = 0.0

    @property
    def hours(self) -> float:
        return self.duration_seconds / 3600.0

    @property
    def cuts_per_hour(self) -> float:
        return self.cuts / max(1e-9, self.hours)

    @property
    def mean_hold(self) -> float:
        return sum(self.holds) / len(self.holds) if self.holds else 0.0

    @property
    def median_hold(self) -> float:
        if not self.holds:
            return 0.0
        ordered = sorted(self.holds)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    @property
    def min_hold(self) -> float:
        return min(self.holds) if self.holds else 0.0

    @property
    def max_hold(self) -> float:
        return max(self.holds) if self.holds else 0.0

    @property
    def camera_shares(self) -> dict[str, float]:
        total = sum(self.camera_time.values())
        if not total:
            return {}
        return {
            camera: round(seconds / total, 4)
            for camera, seconds in sorted(
                self.camera_time.items(), key=lambda item: -item[1]
            )
        }

    @property
    def action_visibility(self) -> float:
        """Fraction of notable affinity-carrying action time the live camera could show.

        **Expected to be low, and that is not a defect.** A 128-second hold cannot
        follow actions lasting two to eleven seconds, and the only way to raise this
        materially is to cut far more often — which is the music-video cutting the brief
        rules out. Reported because it is asked for; judged against
        :attr:`state_alignment`, which is the question the design can answer.
        """
        total = self.visible_action_seconds + self.invisible_action_seconds
        return self.visible_action_seconds / total if total else 1.0

    @property
    def state_alignment(self) -> float:
        """Fraction of time the live camera favoured the current character state.

        This is the meaningful visibility measure. Character states last 10-70 seconds,
        which a hold *can* track, so `behavior_affinity` is the mechanism that actually
        steers framing — action affinity is a tiebreaker at the moment of the cut.
        """
        total = self.aligned_state_seconds + self.misaligned_state_seconds
        return self.aligned_state_seconds / total if total else 1.0

    def repeated_sequences(self, length: int = 3) -> dict[tuple[str, ...], int]:
        """Camera n-grams and their counts. The ABAB / 123123 detector."""
        counts: dict[tuple[str, ...], int] = {}
        for index in range(len(self.camera_sequence) - length + 1):
            window = tuple(self.camera_sequence[index : index + length])
            counts[window] = counts.get(window, 0) + 1
        return counts

    @property
    def top_sequence_share(self) -> float:
        counts = self.repeated_sequences()
        if not counts:
            return 0.0
        return max(counts.values()) / sum(counts.values())

    @property
    def alternations(self) -> int:
        """Count of A→B→A patterns. A handful is natural; a plateau is a rotation."""
        hits = 0
        for index in range(len(self.camera_sequence) - 2):
            first, _, third = self.camera_sequence[index : index + 3]
            if first == third:
                hits += 1
        return hits

    @property
    def is_safe(self) -> bool:
        return not (self.unsafe_cuts or self.cam6_without_interaction)

    def summary(self) -> str:
        lines = [
            f"duration           {self.hours:.2f} h",
            f"cuts               {self.cuts}  ({self.cuts_per_hour:.1f} / h)",
            f"hold               mean {self.mean_hold:.0f} s  median {self.median_hold:.0f} s  "
            f"min {self.min_hold:.0f} s  max {self.max_hold:.0f} s",
            f"state alignment    {self.state_alignment * 100:.1f} %   "
            f"(action visibility {self.action_visibility * 100:.1f} % - see the docstring)",
            "",
            "-- camera distribution (by time on air)",
        ]
        for camera, share in self.camera_shares.items():
            bar = "#" * max(1, round(40 * share))
            count = self.camera_counts.get(camera, 0)
            lines.append(f"  {camera}  {share * 100:5.1f} %  {count:>4} cuts  {bar}")
        lines += ["", "-- motivations"]
        total_motivations = max(1, sum(self.motivation_counts.values()))
        for name, count in sorted(self.motivation_counts.items(), key=lambda i: -i[1]):
            lines.append(f"  {name:<22} {count:>5}  {count / total_motivations * 100:5.1f} %")
        lines += ["", "-- transitions"]
        for name, count in sorted(self.transition_counts.items(), key=lambda i: -i[1]):
            lines.append(f"  {name:<22} {count:>5}")
        lines += ["", "-- vetoes (pacing vetoes firing often is healthy)"]
        for name, count in sorted(self.veto_counts.items(), key=lambda i: -i[1])[:8]:
            lines.append(f"  {name:<36} {count:>7}")
        top = self.repeated_sequences()
        worst = max(top.items(), key=lambda i: i[1]) if top else None
        lines += [
            "",
            "-- repetition",
            f"distinct 3-sequences  {len(top)}",
            f"top 3-sequence        {' -> '.join(worst[0]) if worst else '-'}"
            f"  x{worst[1] if worst else 0}  ({self.top_sequence_share * 100:.1f} %)",
            f"A-B-A alternations    {self.alternations} of {max(0, len(self.camera_sequence) - 2)}",
            "",
            f"unsafe cuts           {len(self.unsafe_cuts)}",
            f"CAM_6 without work    {len(self.cam6_without_interaction)}",
            "",
            "RESULT                " + ("SAFE" if self.is_safe else "UNSAFE - INVESTIGATE"),
        ]
        return "\n".join(lines)


# ============================================================ the director


class CameraDirector:
    """Chooses between the seven frozen compositions.

    Deterministic under an injected RNG, like the behaviour director, so a camera soak is
    reproducible and a finding can be bisected.
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        state: CameraState | None = None,
        auto: bool = True,
        energy: float = 1.0,
    ) -> None:
        self._rng = rng or random.Random()  # noqa: S311 - framing variety, not security
        self._selector = WeightedSelector(self._rng)
        self.state = state or CameraState(camera_id=PRIMARY_CAMERA, auto=auto)
        self.state.auto = auto
        #: Operator multiplier on hold times. Scales, never below the absolute floor.
        self.energy = energy

        self._hold_target = 0.0
        self._last_cut = -9_999.0
        #: Last time each camera was live. Seeded for ALL cameras on the first tick:
        #: left empty, every unused camera reads as starved and DIVERSITY fires on a
        #: fifth of the cuts in the first two hours, which is a startup artefact rather
        #: than a narrowing stream.
        self._last_used: dict[str, float] = {}
        self._last_band: IntensityBand | None = None
        self._band_changed_at: float | None = None
        self._last_transition_state = TransitionState.NONE
        self._transition_seen_at: float | None = None
        self._reaction_started: float | None = None
        self._push_times: list[float] = []
        self._pending_request: str | None = None
        self._hour_time: dict[str, float] = {}
        self._hour_window: list[tuple[float, str, float]] = []

    # ------------------------------------------------------------ public

    def tick(
        self,
        *,
        now: float,
        state: VisualStateV1,
        character_state: CharacterState,
        running: tuple[ActionView, ...],
        chain_id: str | None,
        chain_step: int | None,
        chain_committed: bool,
    ) -> CameraDecision:
        """One camera decision. Usually "keep holding"."""
        if self._hold_target == 0.0:
            self._hold_target = self._sample_hold(state.intensity_band)
            self.state.since_monotonic = now
            for camera_id in CAMERA_METADATA:
                self._last_used.setdefault(camera_id, now)

        self._track_time(now)
        if character_state is CharacterState.MARKET_REACTION and self._reaction_started is None:
            self._reaction_started = now
        elif character_state is not CharacterState.MARKET_REACTION:
            self._reaction_started = None

        motivation, vetoes = self._motivation(now, character_state, chain_id)
        if motivation is None:
            self._remember_band(state, now)
            return self._hold(now, vetoes)

        safety = self._safety_vetoes(now, running, chain_id, chain_step, chain_committed)
        if safety:
            self._remember_band(state, now)
            return self._hold(now, {**vetoes, **safety})

        chosen, factors, candidate_vetoes = self._choose(
            now, state, character_state, running, chain_id, chain_committed
        )
        if chosen is None or chosen == self.state.camera_id:
            self._remember_band(state, now)
            return self._hold(now, {**vetoes, **candidate_vetoes})

        return self._cut(now, chosen, motivation, state, factors, candidate_vetoes)

    def request(self, camera_id: str) -> None:
        """Operator request. Queues behind the vetoes rather than bypassing them.

        An override that bypassed them would let one click produce exactly the artefact
        the vetoes exist to prevent, live, with no undo. Queuing costs a few seconds and
        cannot produce a broken frame.
        """
        if camera_id not in CAMERA_METADATA:
            raise KeyError(
                f"unknown camera {camera_id!r}; the seven frozen V1 cameras are "
                f"{', '.join(sorted(CAMERA_METADATA))}"
            )
        self._pending_request = camera_id

    @property
    def hold_seconds(self) -> float:
        return self._hold_target

    # ------------------------------------------------------------ timing

    def _sample_hold(self, band: IntensityBand, camera_id: str | None = None) -> float:
        """Log-normal about the band's median, clamped to the band and the camera.

        Log-normal rather than uniform because a uniform draw over the same range gives
        far too many mid-length shots, which is what a rotation looks like. This produces
        mostly-long holds with an occasional short one.

        **Clamped to the live camera's own declared minimum**, not only the band's. The
        first version used the band alone, so `CAM_5` — which declares a 70 s minimum —
        could be handed a 62 s hold drawn from the B5_PEAK range and would then cut below
        its own floor on ordinary expiry, with no veto to catch it because expiry is not
        an early cut.
        """
        low, median, high = HOLD_DISTRIBUTION[band]
        meta = CAMERA_METADATA[camera_id or self.state.camera_id]
        floor = max(MINIMUM_HOLD_FLOOR, low, meta.minimum_hold_seconds)
        ceiling = max(floor, min(high, meta.maximum_hold_seconds))
        sample = self._rng.lognormvariate(math.log(median), HOLD_SIGMA)
        scaled = sample * max(0.5, min(1.6, self.energy))
        return max(floor, min(ceiling, scaled))

    def _elapsed(self, now: float) -> float:
        return max(0.0, now - self.state.since_monotonic)

    def _hold(self, now: float, vetoes: dict[str, str]) -> CameraDecision:
        return CameraDecision(
            camera_id=self.state.camera_id,
            changed=False,
            transition=self.state.transition,
            transition_ms=0,
            motivation=None,
            hold_target_seconds=self._hold_target,
            reason=f"holding ({self._elapsed(now):.0f}s of {self._hold_target:.0f}s)",
            vetoes=vetoes,
        )

    # ------------------------------------------------------------ motivation

    def _motivation(
        self,
        now: float,
        character_state: CharacterState,
        chain_id: str | None,
    ) -> tuple[CutMotivation | None, dict[str, str]]:
        """Which motivation, if any, argues for a cut right now."""
        vetoes: dict[str, str] = {}
        elapsed = self._elapsed(now)

        if elapsed < MINIMUM_HOLD_FLOOR:
            vetoes["floor"] = CutVeto.BELOW_FLOOR.value
            return None, vetoes
        if now - self._last_cut < DOUBLE_CUT_WINDOW:
            vetoes["double_cut"] = CutVeto.DOUBLE_CUT.value
            return None, vetoes

        fraction = elapsed / max(1e-9, self._hold_target)
        candidates: list[Candidate[CutMotivation]] = []

        if self._pending_request is not None:
            candidates.append(Candidate(value=CutMotivation.OPERATOR, weight=10.0))

        if fraction >= 1.0:
            candidates.append(
                Candidate(value=CutMotivation.HOLD_EXPIRED, weight=MOTIVATION_WEIGHT[
                    CutMotivation.HOLD_EXPIRED
                ])
            )

        def offer(motivation: CutMotivation, *, active: bool) -> None:
            if not active or fraction < MOTIVATION_EARLIEST[motivation]:
                return
            if self._rng.random() > MOTIVATION_PROBABILITY[motivation]:
                return
            candidates.append(
                Candidate(value=motivation, weight=MOTIVATION_WEIGHT[motivation])
            )

        offer(
            CutMotivation.INTERACTION_VISIBLE,
            active=chain_id is not None or character_state in (
                CharacterState.NOTE_TAKING, CharacterState.CAFFEINE_BREAK,
                CharacterState.EXECUTING,
            ),
        )
        offer(
            CutMotivation.MARKET_SHIFT,
            active=(
                self._band_changed_at is not None
                and now - self._band_changed_at <= EVENT_WINDOW_SECONDS
            ),
        )
        offer(
            CutMotivation.TRACK_TRANSITION,
            active=(
                self._transition_seen_at is not None
                and now - self._transition_seen_at <= EVENT_WINDOW_SECONDS
            ),
        )
        offer(CutMotivation.DIVERSITY, active=self._starved_cameras(now) != ())
        offer(
            CutMotivation.REACTION_RESOLVED,
            active=(
                self.state.camera_id == "CAM_4"
                and character_state is not CharacterState.MARKET_REACTION
            ),
        )

        if not candidates:
            vetoes["motivation"] = CutVeto.NO_MOTIVATION.value
            return None, vetoes

        # Before the hold expires only a licensed motivation may cut, and never before
        # the camera's own declared minimum.
        early = fraction < 1.0
        if early and elapsed < self.state.metadata().minimum_hold_seconds:
            vetoes["camera_minimum"] = CutVeto.BELOW_SAMPLED_HOLD.value
            return None, vetoes

        return self._selector.select(candidates).chosen, vetoes

    def _starved_cameras(self, now: float) -> tuple[str, ...]:
        out = []
        for camera_id in CAMERA_METADATA:
            if camera_id == self.state.camera_id:
                continue
            last = self._last_used.get(camera_id)
            if last is not None and now - last > DIVERSITY_STARVATION_SECONDS:
                out.append(camera_id)
        return tuple(out)

    def _remember_band(self, state: VisualStateV1, now: float) -> None:
        """Record the event timestamps the windowed motivations read."""
        if (
            self._last_band is not None
            and state.intensity_band.distance_to(self._last_band) >= 1
        ):
            self._band_changed_at = now
        self._last_band = state.intensity_band

        if (
            state.transition_state is not TransitionState.NONE
            and state.transition_state is not self._last_transition_state
        ):
            self._transition_seen_at = now
        self._last_transition_state = state.transition_state

    # ------------------------------------------------------------ safety

    def _safety_vetoes(
        self,
        now: float,
        running: tuple[ActionView, ...],
        chain_id: str | None,
        chain_step: int | None,
        chain_committed: bool,
    ) -> dict[str, str]:
        """The vetoes that protect the picture. Absolute, checked before any choice."""
        vetoes: dict[str, str] = {}

        if self._reaction_started is not None and now - self._reaction_started < REACTION_LOCKOUT:
            vetoes["reaction"] = CutVeto.REACTION_LOCKOUT.value

        # A cut inside a blend window means two blends in flight on one joint, which is
        # the visible pop the brief forbids.
        if any(view.in_blend(now) for view in running):
            vetoes["blend"] = CutVeto.MID_BLEND.value

        if chain_id is not None and chain_step is not None:
            chain = CHAINS.get(chain_id)
            if chain is not None and chain_step < chain.length:
                step_id = chain.steps[chain_step]
                if step_id in ACQUISITION_STEPS:
                    vetoes["acquisition"] = CutVeto.OBJECT_ACQUISITION.value
            if chain_committed:
                vetoes.setdefault("committed", "committed_chain_limits_targets")
        return vetoes

    # ------------------------------------------------------------ choice

    def _choose(
        self,
        now: float,
        state: VisualStateV1,
        character_state: CharacterState,
        running: tuple[ActionView, ...],
        chain_id: str | None,
        chain_committed: bool,
    ) -> tuple[str | None, dict[str, float], dict[str, str]]:
        """Score the six alternatives and pick one."""
        if self._pending_request is not None:
            wanted, self._pending_request = self._pending_request, None
            if wanted != self.state.camera_id:
                return wanted, {"operator": 1.0}, {}

        interaction_visible = self._interaction_visible(running, chain_id)
        affinity = self._affinity(running, character_state)
        band = state.intensity_band
        vetoes: dict[str, str] = {}
        candidates: list[Candidate[str]] = []

        for camera_id, meta in CAMERA_METADATA.items():
            if camera_id == self.state.camera_id:
                continue

            # CAM_6 is GATED, not weighted. A weight, however small, eventually fires on
            # an empty desk — and the acceptance criterion is that the hands shot appears
            # only when close interaction is visible.
            if camera_id == "CAM_6" and not interaction_visible:
                vetoes["CAM_6"] = CutVeto.CAM6_NO_INTERACTION.value
                continue

            # A committed chain must stay visible across the cut.
            if chain_committed and camera_id not in affinity and affinity:
                vetoes[camera_id] = CutVeto.COMMITTED_CHAIN_INVISIBLE.value
                continue

            candidate: Candidate[str] = Candidate(value=camera_id, weight=1.0)
            candidate.multiply("market", meta.market_affinity.get(band, 1.0))
            affinity_for_state = meta.behavior_affinity.get(character_state, 1.0)
            candidate.multiply(
                "behaviour", affinity_for_state**BEHAVIOUR_AFFINITY_POWER
            )
            if affinity:
                candidate.multiply("action_affinity", 2.2 if camera_id in affinity else 0.55)
            candidate.multiply("recency", self._recency_penalty(camera_id, now))
            candidate.multiply("family", self._family_penalty(camera_id))
            candidate.multiply("hour_share", self._share_penalty(camera_id, now))
            candidate.multiply("music", self._music_factor(camera_id, state))
            if camera_id == PRIMARY_CAMERA:
                candidate.multiply("home_return", self._home_bias(now))
            candidates.append(candidate)

        if not candidates:
            return None, {}, vetoes

        constraints: tuple[Constraint[str], ...] = (
            Constraint(
                name="no_immediate_return",
                predicate=lambda camera: camera != self._previous_camera(),
                reason="returning to the camera just left",
                severity=5,
            ),
        )
        result = self._selector.select(candidates, constraints)
        factors = next(
            (c.factors for c in result.candidates if c.value == result.chosen), {}
        )
        return result.chosen, {k: round(v, 4) for k, v in factors.items()}, vetoes

    def _previous_camera(self) -> str | None:
        return self.state.recent[-1] if self.state.recent else None

    def _interaction_visible(
        self, running: tuple[ActionView, ...], chain_id: str | None
    ) -> bool:
        """Whether a desk interaction is in flight. The CAM_6 gate."""
        if chain_id is not None:
            return True
        return any(
            view.anchor in DESK_ANCHORS for view in running if view.anchor is not None
        )

    def _affinity(
        self, running: tuple[ActionView, ...], character_state: CharacterState
    ) -> frozenset[str]:
        """Cameras that can show what is happening, from the running actions.

        Taken from the catalogue's `camera_affinity`, which is why that field was
        declared in V6 — this is its consumer.
        """
        cameras: set[str] = set()
        for view in running:
            cameras.update(spec(view.action_id).camera_affinity)
        if not cameras and character_state is CharacterState.MARKET_REACTION:
            cameras.update({"CAM_4", "CAM_7"})
        return frozenset(cameras)

    def _home_bias(self, now: float) -> float:
        """The home shot's pull, growing with time spent away from it.

        Zero extra pull while it is live or just left — the stream should be free to go
        and look at something — rising to :data:`HOME_BIAS_MAX` once it has been away for
        :data:`HOME_RETURN_SECONDS`. This is what concentrates airtime on `CAM_1` without
        starving the other six, and it is why `CAM_1`'s share lands near its cap rather
        than pinned at it.
        """
        last = self._last_used.get(PRIMARY_CAMERA)
        if last is None:
            return HOME_BIAS_MAX
        if self.state.camera_id == PRIMARY_CAMERA:
            return 1.0
        away = max(0.0, now - last)
        reach = min(1.0, away / HOME_RETURN_SECONDS)
        return 1.0 + (HOME_BIAS_MAX - 1.0) * reach

    def _recency_penalty(self, camera_id: str, now: float) -> float:
        last = self._last_used.get(camera_id)
        if last is None:
            return 1.4  # never used; the stream should reach it
        gap = now - last
        if gap < 120.0:
            return 0.12
        if gap < 420.0:
            return 0.5
        if gap > DIVERSITY_STARVATION_SECONDS:
            return 1.6
        return 1.0

    def _family_penalty(self, camera_id: str) -> float:
        """Penalise the family just used, not only the camera.

        `CAM_1 -> CAM_7 -> CAM_1` is three hero shots running even though no camera
        repeated, and that reads as indecision rather than as variety.
        """
        family = CAMERA_FAMILY[camera_id]
        recent_families = [
            CAMERA_FAMILY[camera] for camera in self.state.recent[-3:]
        ]
        recent_families.append(CAMERA_FAMILY[self.state.camera_id])
        hits = sum(1 for seen in recent_families if seen is family)
        return (1.0, 0.45, 0.18, 0.08)[min(hits, 3)]

    def _share_penalty(self, camera_id: str, now: float) -> float:
        """Taper into the camera's rolling-hour cap."""
        self._track_time(now)
        total = sum(self._hour_time.values())
        if total < 600.0:
            return 1.0
        share = self._hour_time.get(camera_id, 0.0) / total
        cap = CAMERA_METADATA[camera_id].hour_share_cap
        if share >= cap:
            return 0.0
        if share > cap * 0.7:
            return max(0.05, (cap - share) / (cap * 0.3))
        return 1.0

    def _music_factor(self, camera_id: str, state: VisualStateV1) -> float:
        """Weaker than the market, and reaching only two families.

        High-energy music biases slightly toward the hero and work shots; low-energy
        music toward atmosphere. That is the whole of music's influence on framing.
        """
        if state.music_energy is None or not state.music_present:
            return 1.0
        family = CAMERA_FAMILY[camera_id]
        energy = state.music_energy
        if family is CameraFamily.ATMOSPHERE:
            return 1.0 + 0.25 * (0.5 - energy)
        if family in (CameraFamily.HERO, CameraFamily.WORK):
            return 1.0 + 0.18 * (energy - 0.5)
        return 1.0

    # ------------------------------------------------------------ the cut

    def _cut(
        self,
        now: float,
        camera_id: str,
        motivation: CutMotivation,
        state: VisualStateV1,
        factors: dict[str, float],
        vetoes: dict[str, str],
    ) -> CameraDecision:
        transition = self._transition(camera_id, now)
        held = self._elapsed(now)
        self.state.cut_to(camera_id, now, transition=transition)
        self._last_cut = now
        self._last_used[camera_id] = now
        self._hold_target = self._sample_hold(state.intensity_band, camera_id)
        self._remember_band(state, now)
        if transition == TRANSITION_PUSH_CONTINUE:
            self._push_times.append(now)
            self._push_times = [t for t in self._push_times if now - t <= 3600.0]

        return CameraDecision(
            camera_id=camera_id,
            changed=True,
            transition=transition,
            transition_ms=TRANSITION_MS[transition],
            motivation=motivation,
            hold_target_seconds=self._hold_target,
            reason=f"{motivation.value} after {held:.0f}s",
            factors=factors,
            vetoes=vetoes,
        )

    def _transition(self, camera_id: str, now: float) -> str:
        """Hard cut by default; crossfade into and out of the wide; push rarely."""
        from_camera = self.state.camera_id
        if (from_camera, camera_id) == PUSH_CONTINUE_PAIR:
            recent = [t for t in self._push_times if now - t <= 3600.0]
            if len(recent) < PUSH_CONTINUE_MAX_PER_HOUR and self._rng.random() < 0.35:
                return TRANSITION_PUSH_CONTINUE
        # A crossfade into or out of the wide shot: the one place a soft transition
        # reads as intentional rather than as a streamer effect.
        atmosphere = CameraFamily.ATMOSPHERE
        touches_wide = atmosphere in (
            CAMERA_FAMILY[from_camera], CAMERA_FAMILY[camera_id]
        )
        if touches_wide and self._rng.random() < 0.55:
            return TRANSITION_CROSSFADE
        return TRANSITION_CUT

    # ------------------------------------------------------------ bookkeeping

    def _track_time(self, now: float) -> None:
        """Accumulate per-camera time inside a rolling hour."""
        if self._hour_window and self._hour_window[-1][1] == self.state.camera_id:
            start, camera, _ = self._hour_window[-1]
            self._hour_window[-1] = (start, camera, now)
        else:
            self._hour_window.append((now, self.state.camera_id, now))
        cutoff = now - 3600.0
        self._hour_window = [
            entry for entry in self._hour_window if entry[2] >= cutoff
        ]
        self._hour_time = {}
        for start, camera, end in self._hour_window:
            self._hour_time[camera] = self._hour_time.get(camera, 0.0) + max(
                0.0, end - max(start, cutoff)
            )


__all__ = [
    "ACQUISITION_STEPS",
    "BEHAVIOUR_AFFINITY_POWER",
    "CAMERA_FAMILY",
    "DESK_ANCHORS",
    "DIVERSITY_STARVATION_SECONDS",
    "DOUBLE_CUT_WINDOW",
    "EVENT_WINDOW_SECONDS",
    "HOLD_DISTRIBUTION",
    "MINIMUM_HOLD_FLOOR",
    "MOTIVATION_PROBABILITY",
    "PRIMARY_CAMERA",
    "PUSH_CONTINUE_MAX_PER_HOUR",
    "REACTION_LOCKOUT",
    "TRANSITION_MS",
    "ActionView",
    "CameraDecision",
    "CameraDirector",
    "CameraFamily",
    "CameraSoakReport",
    "CutMotivation",
    "CutVeto",
]
