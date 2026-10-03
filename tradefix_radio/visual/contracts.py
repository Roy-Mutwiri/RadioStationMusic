"""Versioned contracts for the visual layer (§90).

Three families live here, and the split is the architecture:

``VisualStateV1``      what the visual layer believes about the station. Produced by the
                       bridge, consumed by the director. Carries *interpretation* —
                       ``intensity_band``, not a regime the director would have to reason
                       about.
``ActionSpecV1``       a catalogue entry: the definition of a behaviour, with ranges.
                       Immutable data, loaded once.
``CharacterActionV1``  one *instance* of a behaviour, with its ranges already sampled.
                       This is what the director emits and the renderer performs.

The spec/instance split is worth being explicit about. The brief asks for one
``CharacterAction`` model carrying ``duration``, ``cooldown_range``, ``weight`` and
``market_bias`` together, but those belong to two different lifetimes: a cooldown range
is a property of *the kind of thing* a coffee sip is, while a duration of 5 240 ms is a
property of *this* sip. Collapsing them would mean either re-sending static tuning data
on every one of the 20 commands a second the director can emit, or mutating a contract
that §90 requires to be frozen. So the catalogue holds the ranges and the emitted action
holds the samples, with ``spec_id`` joining them.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated

from pydantic import Field, computed_field, field_validator, model_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import MarketDirection, VocalStyle
from tradefix_radio.contracts.market import Score100, Unit

#: Milliseconds. A distinct alias from plain int so signatures read unambiguously —
#: the catalogue mixes milliseconds (durations, blends) and seconds (cooldowns), and
#: conflating them silently produces a character who sips coffee for eight minutes.
Millis = Annotated[int, Field(ge=0, le=600_000)]

#: Seconds, for cooldowns and dwell times.
Seconds = Annotated[float, Field(ge=0.0, le=86_400.0)]


# ============================================================ closed vocabularies


class CharacterState(str, enum.Enum):
    """High-level behavioural states.

    **Not animation labels.** A state does not play; it changes what is likely to play,
    which actions are admissible at all, and how long the director dwells before
    reconsidering. ``EXECUTING`` is reachable only from ``ANALYZING`` — nobody types an
    order out of idle, and that single constraint does more for believability than any
    amount of motion polish, because it means visible action always has visible
    deliberation in front of it.
    """

    IDLE_FOCUS = "idle_focus"
    ANALYZING = "analyzing"
    EXECUTING = "executing"
    WAITING = "waiting"
    NOTE_TAKING = "note_taking"
    CAFFEINE_BREAK = "caffeine_break"
    MARKET_REACTION = "market_reaction"
    POSTURE_RESET = "posture_reset"


class ActionCategory(str, enum.Enum):
    """The eight behaviour families.

    Used for category-level anti-repetition: preventing any single family from
    dominating is a different problem from preventing a single action from repeating,
    and the brief asks for both.

    ``POSTURE`` is body maintenance and is deliberately **not** ``FATIGUE``. It happens
    because he has held one position for a while, which is true whether or not he is
    tired — and conflating the two is what starved posture resets completely in the V6
    soak: they were gated behind a fatigue ramp that a quiet market never reached.
    """

    MICRO = "micro"
    WORK = "work"
    POSTURE = "posture"
    HEADPHONES = "headphones"
    CAFFEINE = "caffeine"
    FATIGUE = "fatigue"
    REACTION = "reaction"
    MUSIC = "music"


class InteractionLock(str, enum.Enum):
    """Body and object resources an action occupies for its duration.

    Modelled structurally rather than as a rule to remember. The brief's examples —
    typing must not start while the mug is held; a mouse action must not start with the
    hand that is writing — become arithmetic on a set of held locks, so they cannot be
    violated by a scheduler that forgot about them.

    ``BOTH_HANDS`` is not a separate resource: it *expands* to ``LEFT_HAND`` and
    ``RIGHT_HAND`` when claimed, which is what makes a one-handed action correctly
    conflict with a two-handed one. See :mod:`tradefix_radio.visual.scheduler`.
    """

    NONE = "none"
    LEFT_HAND = "left_hand"
    RIGHT_HAND = "right_hand"
    BOTH_HANDS = "both_hands"
    HEAD = "head"
    UPPER_BODY = "upper_body"
    COFFEE = "coffee"
    PEN = "pen"
    MOUSE = "mouse"
    KEYBOARD = "keyboard"


class Interruptibility(str, enum.Enum):
    """When an action may be pre-empted."""

    ALWAYS = "always"
    """Any time. Micro movements and gaze."""

    AFTER_BLEND_IN = "after_blend_in"
    """Once it has fully blended in. Pre-empting mid-blend produces the neck snap the
    brief forbids, because two blends would be in flight on one joint."""

    NEVER = "never"
    """Runs to completion. Chain steps holding an object lock are always NEVER: a
    reaction that cancelled a grip would leave a mug in mid-air with no hand on it."""


class GazeTarget(str, enum.Enum):
    """Where the eyes can go.

    A *semantic* vocabulary, resolved to blockout coordinates by
    :mod:`tradefix_radio.visual.geometry`. The indirection matters: "look at the left
    monitor" must resolve to the actual left monitor in every camera, and hand-tuned
    per-camera angles are precisely how eyes end up looking *through* monitors.

    ``MONITOR_LEFT`` and ``MONITOR_RIGHT`` are **the character's** left and right, not
    the viewer's. He faces −Y, so his left is east (+X) and resolves to ``MON_3``.
    """

    MONITOR_MAIN = "monitor_main"
    MONITOR_LEFT = "monitor_left"
    MONITOR_RIGHT = "monitor_right"
    MONITOR_LOWER = "monitor_lower"
    MONITOR_FAR_RIGHT = "monitor_far_right"
    PANEL_UPPER_LEFT = "panel_upper_left"
    PANEL_UPPER_RIGHT = "panel_upper_right"
    NOTEBOOK = "notebook"
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    COFFEE = "coffee"
    WINDOW = "window"
    MIDDLE_DISTANCE = "middle_distance"
    CAMERA = "camera"


class GazeParticipation(str, enum.Enum):
    """Which body parts a gaze shift recruits.

    This exists because a flat angular limit is the wrong model. The first draft of the
    geometry rejected the notebook, the keyboard and the window as "outside head range"
    — three things a real trader plainly looks at. The angle was never the problem; an
    angle reached with the wrong body parts is. So a target declares what it recruits,
    and the geometry validator checks the declaration against the measured angle.
    """

    EYES = "eyes"
    HEAD = "head"
    SHOULDERS = "shoulders"
    HEAD_PITCH_STRONG = "head_pitch_strong"
    TORSO = "torso"
    CHAIR_SWIVEL = "chair_swivel"


class IntensityBand(str, enum.Enum):
    """The only market vocabulary that reaches behaviour.

    Fourteen ``MarketRegime`` values collapse to six bands in the bridge, and nothing
    below the bridge sees anything finer. A new regime is a one-line change there.
    """

    B0_DORMANT = "b0_dormant"
    B1_QUIET = "b1_quiet"
    B2_STEADY = "b2_steady"
    B3_FOCUSED = "b3_focused"
    B4_ALERT = "b4_alert"
    B5_PEAK = "b5_peak"

    @property
    def rank(self) -> int:
        """Ordinal position, 0–5.

        Named ``rank`` rather than ``index`` because ``IntensityBand`` subclasses
        ``str``: a property called ``index`` silently overrides ``str.index`` with an
        incompatible signature, so a caller expecting a substring position would get a
        band ordinal instead.
        """
        return _BAND_ORDER.index(self)

    def distance_to(self, other: IntensityBand) -> int:
        """Band steps between two bands. Feeds reaction salience."""
        return abs(self.rank - other.rank)


_BAND_ORDER: tuple[IntensityBand, ...] = (
    IntensityBand.B0_DORMANT,
    IntensityBand.B1_QUIET,
    IntensityBand.B2_STEADY,
    IntensityBand.B3_FOCUSED,
    IntensityBand.B4_ALERT,
    IntensityBand.B5_PEAK,
)


class StationMode(str, enum.Enum):
    """Derived station condition, in the order the bridge tests it.

    Almost none of this is visible on the character, deliberately. A low buffer is the
    station's problem, not the trader's — he is watching a market, not a generation
    queue. It appears on the station monitor, which is what that surface is for.
    """

    OFFLINE = "offline"
    SWITCHING_MARKET = "switching_market"
    PROCEDURAL = "procedural"
    RESERVE = "reserve"
    BUFFER_LOW = "buffer_low"
    NORMAL = "normal"


class TransitionState(str, enum.Enum):
    NONE = "none"
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    STATION_ID = "station_id"


class FeedTrust(str, enum.Enum):
    """How far the market half of the state may be believed.

    Three values rather than a boolean because the degradation ladder needs the middle
    one: a five-second socket gap must not visibly change the performance, while a
    sixty-second gap must stop market-driven behaviour entirely.
    """

    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"

    @property
    def permits_reactions(self) -> bool:
        """A reaction with no market event behind it is a fabricated market event."""
        return self is FeedTrust.LIVE


#: The sentinel for "the station is between markets". A real value, not an absence:
#: gold closes, and the router genuinely has nothing active for a while.
NO_ACTIVE_MARKET = "NO_ACTIVE_MARKET"


# ============================================================ visual state


class BandBiasV1(Contract):
    """Per-band weight multipliers for one action.

    Six floats rather than a dict so a missing band is impossible and mypy can see the
    shape. ``None`` means *unavailable in that band* — which is different from a weight
    of zero, because an unavailable action is not a candidate at all and so cannot be
    rescued by a relaxation pass.
    """

    b0: float | None = Field(default=1.0, ge=0.0, le=4.0)
    b1: float | None = Field(default=1.0, ge=0.0, le=4.0)
    b2: float | None = Field(default=1.0, ge=0.0, le=4.0)
    b3: float | None = Field(default=1.0, ge=0.0, le=4.0)
    b4: float | None = Field(default=1.0, ge=0.0, le=4.0)
    b5: float | None = Field(default=1.0, ge=0.0, le=4.0)

    def for_band(self, band: IntensityBand) -> float | None:
        return (self.b0, self.b1, self.b2, self.b3, self.b4, self.b5)[band.rank]

    @classmethod
    def flat(cls, value: float = 1.0) -> BandBiasV1:
        return cls(b0=value, b1=value, b2=value, b3=value, b4=value, b5=value)

    @classmethod
    def ramp(cls, low: float, high: float) -> BandBiasV1:
        """Linear ramp B0→B5. The common shape: quiet suppresses, alert amplifies."""
        step = (high - low) / 5.0
        values = [round(low + step * i, 3) for i in range(6)]
        return cls(
            b0=values[0], b1=values[1], b2=values[2],
            b3=values[3], b4=values[4], b5=values[5],
        )


class VisualStateV1(Contract):
    """What the visual layer believes about the station.

    The only object the behaviour director consumes. Two properties are load-bearing:

    **Nullable rather than zero-filled.** ``music_bpm`` of ``None`` means "nothing is
    playing"; a zero would mean "playing at 0 BPM", and the brief is explicit that
    unavailable values must never be replaced with fake zeros. Every consumer therefore
    has to handle absence, which is the point — the alternative is a character reacting
    rhythmically to silence.

    **Interpretation, not observation.** ``intensity_band`` and ``behavior_energy`` are
    here instead of a regime name so the director never reasons about markets. ``regime``
    is carried for display and debug only, and :mod:`tradefix_radio.visual.director`
    does not read it.
    """

    at: datetime
    #: Age of the station frame this was derived from. Drives the degradation ladder.
    source_age_seconds: float = Field(ge=0.0)
    feed_trust: FeedTrust
    degraded_reason: str | None = Field(default=None, max_length=200)

    # -- market
    active_symbol: str = Field(min_length=1, max_length=32)
    #: Seconds since the active symbol last changed, or ``None`` if it never has
    #: this process's lifetime. Licenses the market-switch attention shift.
    symbol_changed_seconds_ago: float | None = Field(default=None, ge=0.0)
    #: Verbatim regime, **for display and debug only**. Behaviour must use ``band``.
    market_regime: str | None = Field(default=None, max_length=64)
    intensity_band: IntensityBand
    market_energy: Score100 | None = None
    market_energy_velocity: float | None = None
    market_direction: MarketDirection | None = None
    market_confidence: Unit | None = None
    market_health: str | None = Field(default=None, max_length=32)
    session: str | None = Field(default=None, max_length=48)

    # -- music
    music_bpm: int | None = Field(default=None, ge=20, le=300)
    music_energy: Unit | None = None
    music_genre: str | None = Field(default=None, max_length=64)
    vocal_style: VocalStyle | None = None
    track_progress: Unit | None = None
    transition_state: TransitionState = TransitionState.NONE

    # -- station
    station_mode: StationMode = StationMode.NORMAL
    emergency_tier: str | None = Field(default=None, max_length=32)
    broadcasting: bool = False

    # -- derived behavioural drive, computed by the bridge
    behavior_energy: Unit
    reaction_salience: Unit = 0.0
    salience_components: dict[str, float] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _enforce_honesty(self) -> VisualStateV1:
        """Make the dishonest states unrepresentable rather than merely discouraged.

        The station's own contracts do this for prices: a non-live feed cannot carry
        one. The visual equivalent is that a stale feed cannot carry a band above
        dormant or a reaction, because behaviour driven by a frozen regime that may be
        hours old performs market activity that is not happening.
        """
        if self.feed_trust is FeedTrust.STALE:
            if self.intensity_band is not IntensityBand.B0_DORMANT:
                raise ValueError(
                    f"feed_trust is stale but intensity_band is {self.intensity_band.value}; "
                    "a stale feed cannot drive market-reactive behaviour"
                )
            if self.reaction_salience > 0.0:
                raise ValueError("feed_trust is stale but reaction_salience is non-zero")
        if self.active_symbol == NO_ACTIVE_MARKET and self.market_regime is not None:
            raise ValueError("no active market cannot carry a regime")
        if self.station_mode is StationMode.OFFLINE and self.broadcasting:
            raise ValueError("station_mode is offline but broadcasting is True")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_reactive(self) -> bool:
        """Whether market-driven behaviour is permitted at all."""
        return (
            self.feed_trust.permits_reactions
            and self.intensity_band is not IntensityBand.B0_DORMANT
            and self.active_symbol != NO_ACTIVE_MARKET
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def music_present(self) -> bool:
        return self.music_bpm is not None and self.broadcasting

    @classmethod
    def neutral(cls, *, at: datetime, reason: str) -> VisualStateV1:
        """A safe state carrying no market claim.

        Used when the station is unreachable. He keeps breathing, blinking, reading and
        drinking coffee — he simply has nothing to react to, which is the honest
        picture. Mirrors ``MarketStateV1.neutralised()``.
        """
        return cls(
            at=at,
            source_age_seconds=0.0,
            feed_trust=FeedTrust.STALE,
            degraded_reason=reason,
            active_symbol=NO_ACTIVE_MARKET,
            intensity_band=IntensityBand.B0_DORMANT,
            station_mode=StationMode.OFFLINE,
            broadcasting=False,
            behavior_energy=0.25,
        )


# ============================================================ actions


class ActionSpecV1(Contract):
    """A catalogue entry: the definition of one behaviour.

    Static tuning data, loaded once and never mutated. The director reads these to
    score candidates; it emits :class:`CharacterActionV1` instances.
    """

    action_id: str = Field(min_length=1, max_length=48)
    category: ActionCategory

    duration_ms: tuple[Millis, Millis]
    blend_in_ms: Millis
    blend_out_ms: Millis

    interruptibility: Interruptibility = Interruptibility.ALWAYS
    #: Locks claimed for the duration. Empty means the action occupies nothing.
    interaction_locks: tuple[InteractionLock, ...] = ()

    #: Sampled per execution, never fixed. A constant cooldown is a visible period.
    cooldown_range_seconds: tuple[Seconds, Seconds]
    weight: float = Field(default=1.0, ge=0.0, le=10.0)

    market_bias: BandBiasV1 = Field(default_factory=BandBiasV1)
    #: How strongly music energy scales this action's weight. 0.0 = not at all.
    #: Only the three MUSIC actions carry a meaningful value; see the ceilings in
    #: :mod:`tradefix_radio.visual.modulation`.
    music_bias: float = Field(default=0.0, ge=0.0, le=2.0)

    #: Empty means "admissible in every state".
    allowed_character_states: tuple[CharacterState, ...] = ()
    required_anchor: str | None = Field(default=None, max_length=48)
    gaze_target: GazeTarget | None = None
    #: Cameras this action reads well from, for the later camera director's narrative
    #: bonus. Advisory; an empty tuple means no preference.
    camera_affinity: tuple[str, ...] = ()

    #: Contribution to the rolling activity measure that damps behaviour energy. A
    #: fidget costs almost nothing; a long typing burst costs real attention.
    energy_cost: float = Field(default=0.1, ge=0.0, le=5.0)
    tags: tuple[str, ...] = ()

    #: Group key for anti-repetition. Actions sharing a key share a group cooldown, so
    #: the scheduler cannot satisfy four separate headphone cooldowns in sequence and
    #: produce a man fiddling with his headphones for a minute.
    anti_repeat_key: str = Field(min_length=1, max_length=48)

    #: True when this action exists only as a step inside a chain and must never be
    #: scheduled on its own.
    chain_only: bool = False
    #: Minimum fatigue phase at which this becomes available. Gates the FATIGUE family.
    min_fatigue_phase: Unit = 0.0
    #: Hard ceiling on this action's share of any rolling hour, or ``None``.
    max_hour_share: Unit | None = None

    @model_validator(mode="after")
    def _coherent(self) -> ActionSpecV1:
        low, high = self.duration_ms
        if low > high:
            raise ValueError(f"{self.action_id}: duration_ms low {low} exceeds high {high}")
        cool_low, cool_high = self.cooldown_range_seconds
        if cool_low > cool_high:
            raise ValueError(
                f"{self.action_id}: cooldown low {cool_low} exceeds high {cool_high}"
            )
        if InteractionLock.NONE in self.interaction_locks and len(self.interaction_locks) > 1:
            raise ValueError(f"{self.action_id}: NONE cannot be combined with other locks")
        # An action that holds an object lock but can be pre-empted is the floating-mug
        # bug waiting to happen, so it is rejected at the boundary.
        objects = {InteractionLock.COFFEE, InteractionLock.PEN}
        if objects & set(self.interaction_locks) and self.interruptibility is not (
            Interruptibility.NEVER
        ):
            raise ValueError(
                f"{self.action_id}: holds an object lock but is {self.interruptibility.value}; "
                "an interruptible object grip can leave the object unheld in mid-air"
            )
        return self

    @property
    def effective_locks(self) -> frozenset[InteractionLock]:
        """Locks actually occupied, with ``BOTH_HANDS`` expanded.

        Expansion here rather than at claim time means every consumer — the scheduler,
        the tests, the debug timeline — sees the same answer.
        """
        out: set[InteractionLock] = set()
        for lock in self.interaction_locks:
            if lock is InteractionLock.NONE:
                continue
            if lock is InteractionLock.BOTH_HANDS:
                out.update({InteractionLock.LEFT_HAND, InteractionLock.RIGHT_HAND})
            else:
                out.add(lock)
        return frozenset(out)

    def permits_state(self, state: CharacterState) -> bool:
        return not self.allowed_character_states or state in self.allowed_character_states


class CharacterActionV1(Contract):
    """One instance of a behaviour, ranges already sampled.

    What the director emits and the renderer performs. ``sequence`` is monotonic per
    director so the renderer can drop a late or duplicated command without reasoning
    about time.
    """

    sequence: int = Field(ge=0)
    action_id: str = Field(min_length=1, max_length=48)
    category: ActionCategory
    started_at: datetime
    duration_ms: Millis
    blend_in_ms: Millis
    blend_out_ms: Millis
    interruptibility: Interruptibility
    locks: tuple[InteractionLock, ...] = ()
    #: 0.88–1.12 jitter, pre-applied, so the same action is never the same size twice.
    amplitude: float = Field(default=1.0, ge=0.5, le=1.5)
    anchor: str | None = Field(default=None, max_length=48)
    gaze_target: GazeTarget | None = None
    character_state: CharacterState
    #: Set when this action is a step inside a chain, so the renderer and the debug
    #: timeline can show the chain rather than six unexplained steps.
    chain_id: str | None = Field(default=None, max_length=48)
    chain_step: int | None = Field(default=None, ge=0)
    #: Why this action was chosen, for the debug overlay and the simulation report.
    factors: dict[str, float] = Field(default_factory=dict)

    @field_validator("started_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("started_at must be timezone-aware (UTC)")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_ms(self) -> int:
        """Wall time the action occupies, blends included."""
        return self.blend_in_ms + self.duration_ms + self.blend_out_ms

    def ends_at_monotonic(self, started_monotonic: float) -> float:
        return started_monotonic + self.total_ms / 1000.0


class GazeShiftV1(Contract):
    """One gaze movement.

    Separate from :class:`CharacterActionV1` because gaze runs on its own clock and its
    own layer. Collapsing them would force every eye movement through the action
    scheduler's cooldowns, and gaze is the one system that must be continuous.
    """

    sequence: int = Field(ge=0)
    target: GazeTarget
    from_target: GazeTarget | None = None
    started_at: datetime
    #: Time to reach the new target. Never below the geometry's floor — gaze that
    #: snaps is the clearest tell of a synthetic rig.
    transit_ms: Millis
    #: How long the eyes rest there before the next shift is considered.
    dwell_ms: Millis
    participation: GazeParticipation
    #: Angle travelled, degrees. Drives head contribution and the forced blink.
    angle_degrees: float = Field(ge=0.0, le=360.0)
    #: Fraction of the angle the head carries, 0–1. Eyes always lead.
    head_contribution: Unit = 0.0
    #: True when this shift is large enough to force a blink across it.
    forces_blink: bool = False


class RendererTelemetryV1(Contract):
    """What the renderer reports back.

    ``fps_p05`` rather than a mean is deliberate: a 24/7 stream's problem is never
    average frame rate, it is the periodic hitch a mean makes invisible.
    """

    at: datetime
    frames_rendered: int = Field(ge=0)
    fps_mean: float = Field(ge=0.0)
    fps_p05: float = Field(ge=0.0)
    frame_time_p95_ms: float = Field(ge=0.0)
    dropped_frames: int = Field(ge=0)
    gl_memory_mb: float | None = Field(default=None, ge=0.0)
    state_latency_ms: float | None = Field(default=None, ge=0.0)
    resident_camera_stacks: tuple[str, ...] = ()
    active_actions: tuple[str, ...] = ()
    queued_actions: int = Field(default=0, ge=0)
    current_gaze: GazeTarget | None = None
    current_camera: str | None = Field(default=None, max_length=32)
    beat_phase: Unit | None = None
    quality_profile: str = Field(default="balanced", max_length=16)
    last_command_sequence: int = Field(default=0, ge=0)

    @field_validator("at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at must be timezone-aware (UTC)")
        return value


class RhythmPolicyV1(Contract):
    """Rhythmic policy, sent to the renderer rather than resolved in Python.

    ADR-11's accepted trade-off made concrete. Per-beat round trips to Python are not
    possible at 174 BPM, so Python sets policy and the renderer keeps phase: it runs the
    local beat clock from ``downbeat_phase`` and decides which beats actually land,
    honouring the ceilings below. The renderer *enforces* them rather than merely
    receiving them, because a dropped message must not be able to make a dancing trader
    possible.
    """

    bpm: float | None = Field(default=None, ge=20.0, le=300.0)
    downbeat_phase: Unit | None = None
    #: 1 = every beat, 2 = every second beat. Above 160 BPM a per-beat nod is
    #: physically wrong for a seated man and reads as a glitch.
    beat_subdivision: int = Field(default=1, ge=1, le=4)
    nod_probability: Unit = 0.0
    #: Hard ceiling, degrees at the neck. Not a target — a limit the renderer clamps to.
    max_nod_degrees: float = Field(default=1.1, ge=0.0, le=3.0)
    #: Consecutive beats a nod may run before a mandatory gap.
    max_consecutive_beats: int = Field(default=8, ge=1, le=16)
    mandatory_gap_seconds: Seconds = 25.0

    @model_validator(mode="after")
    def _coherent(self) -> RhythmPolicyV1:
        if self.bpm is None and self.nod_probability > 0.0:
            raise ValueError("nod_probability requires a bpm; nodding to silence is not a thing")
        return self


def band_from_index(index: int) -> IntensityBand:
    """Clamp an integer to a band. Used by the energy ramp, which can overshoot."""
    return _BAND_ORDER[max(0, min(len(_BAND_ORDER) - 1, index))]


def all_bands() -> tuple[IntensityBand, ...]:
    return _BAND_ORDER


__all__ = [
    "NO_ACTIVE_MARKET",
    "ActionCategory",
    "ActionSpecV1",
    "BandBiasV1",
    "CharacterActionV1",
    "CharacterState",
    "FeedTrust",
    "GazeParticipation",
    "GazeShiftV1",
    "GazeTarget",
    "IntensityBand",
    "InteractionLock",
    "Interruptibility",
    "MarketDirection",
    "Millis",
    "RendererTelemetryV1",
    "RhythmPolicyV1",
    "Seconds",
    "StationMode",
    "TransitionState",
    "VisualStateV1",
    "VocalStyle",
    "all_bands",
    "band_from_index",
]
