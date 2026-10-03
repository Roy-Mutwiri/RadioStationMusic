"""The Behaviour Director: what the character does next.

Runs at 20 Hz. Fast enough that a market reaction fires within 50 ms, slow enough to be
free. One tick does nine things, in this order, and the order matters:

1. advance clocks — fatigue phase, rolling activity, history trim
2. retire finished actions and release their locks
3. advance any running chain to its next step
4. resolve deferred reactions whose locks have cleared
5. check reaction triggers
6. evaluate the state machine
7. score and select, if the action clock has elapsed
8. drive gaze and blink, independently
9. record to history

Why selection is last among the decisions
-----------------------------------------
Everything before step 7 can *remove* freedom: a running chain forbids new work, a
reaction pre-empts, a state change narrows the admissible set. Scoring against a set
that has already been narrowed is both cheaper and correct. The reverse order — select,
then discover the hand is busy — is how a scheduler ends up silently dropping choices,
which shows up as a character who does nothing for ten seconds at a time.

The do-nothing candidate
------------------------
Not an implementation detail. A scheduler that always selects something produces a
fidgeting man, and the brief's character is disciplined and still. So stillness is an
explicit option whose weight *rises* with recent activity, which is what makes work
arrive in bouts rather than at a constant drip.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

import structlog

from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.director.selection import Candidate, Constraint, WeightedSelector
from tradefix_radio.visual.camera import CameraState
from tradefix_radio.visual.catalog import (
    AMPLITUDE_JITTER,
    CATALOG,
    FATIGUE_TAIL,
    POSTURE_MAINTENANCE_INTERVAL_SECONDS,
    POSTURE_MINOR_INTERVAL_SECONDS,
    REACTION_ACTIONS,
    REACTION_CLOSERS,
    REACTION_OPENERS,
    SCHEDULABLE,
    ActionChain,
    chain_for,
    is_body_maintenance,
    is_major_posture,
    spec,
)
from tradefix_radio.visual.contracts import (
    ActionCategory,
    ActionSpecV1,
    CharacterActionV1,
    CharacterState,
    GazeShiftV1,
    GazeTarget,
    IntensityBand,
    InteractionLock,
    Interruptibility,
    MarketDirection,
    VisualStateV1,
)
from tradefix_radio.visual.gaze import BlinkDriver, GazeDirector, GazeState
from tradefix_radio.visual.geometry import Blockout, default_blockout
from tradefix_radio.visual.modulation import (
    behavior_energy,
    interval_scale,
    music_weight_factor,
    profile_for,
)
from tradefix_radio.visual.rhythm import RhythmTrace, WorkRhythm
from tradefix_radio.visual.scheduler import (
    ActionHistory,
    CooldownTable,
    HistoryEntry,
    LockTable,
)

_log = structlog.get_logger(__name__)

#: Director tick rate. 50 ms.
TICK_HZ: Final = 20.0
TICK_SECONDS: Final = 1.0 / TICK_HZ

#: Base gap between action *considerations*, seconds. Scaled by band and energy, then
#: sampled. Never a constant — see :mod:`tradefix_radio.visual.scheduler`.
BASE_INTERVAL_SECONDS: Final = (1.4, 4.2)

#: Rolling window over which `energy_cost` accumulates into the activity damper.
ACTIVITY_WINDOW_SECONDS: Final = 60.0
#: Cost sum that counts as fully busy.
ACTIVITY_SATURATION: Final = 6.0

#: Reaction gates.
REACTION_REFRACTORY_SECONDS: Final = 25.0
REACTION_HOUR_CAP: Final = 10
#: A reaction deferred longer than this is dropped. A reaction three seconds after its
#: cause reads as a reaction to nothing.
REACTION_DEFER_LIMIT_SECONDS: Final = 1.5
#: Confidence below which the market's own classification is not believed enough to
#: react to.
REACTION_MIN_CONFIDENCE: Final = 0.35

#: Market-switch attention shift. A brief look at the main chart, then normal work.
#: Explicitly **not** a behaviour reset — the brief forbids that.
SWITCH_ATTENTION_WINDOW_SECONDS: Final = 30.0

#: Do-nothing weight: floor, and how much recent activity adds.
IDLE_WEIGHT_BASE: Final = 0.35
IDLE_WEIGHT_ACTIVITY_GAIN: Final = 2.6

#: Dwell ranges per character state, seconds, before the state is reconsidered.
STATE_DWELL_SECONDS: Final[dict[CharacterState, tuple[float, float]]] = {
    CharacterState.IDLE_FOCUS: (18.0, 75.0),
    CharacterState.ANALYZING: (15.0, 70.0),
    CharacterState.EXECUTING: (4.0, 20.0),
    CharacterState.WAITING: (16.0, 55.0),
    CharacterState.NOTE_TAKING: (10.0, 40.0),
    CharacterState.CAFFEINE_BREAK: (6.0, 35.0),
    CharacterState.MARKET_REACTION: (2.0, 6.0),
    CharacterState.POSTURE_RESET: (3.0, 10.0),
}

#: The transition graph.
#:
#: **`EXECUTING` is reachable only from `ANALYZING` or `MARKET_REACTION`.** Nobody types
#: an order out of idle, and that one constraint does more for believability than any
#: amount of motion polish: it means visible action always has visible motivation in
#: front of it. The reaction edge belongs for exactly that reason — he saw something,
#: looked at it, and acted — and an earlier draft of the V1 motion library stated the
#: rule as "only from ANALYZING" while also drawing the reaction edge. Both cannot hold;
#: the purpose of the rule is what settles it.
#:
#: :data:`EXECUTING_PREDECESSORS` is the machine-readable form, asserted by the
#: simulator and by `tests/unit/test_visual_director.py`.
STATE_TRANSITIONS: Final[dict[CharacterState, tuple[tuple[CharacterState, float], ...]]] = {
    CharacterState.IDLE_FOCUS: (
        (CharacterState.ANALYZING, 3.0),
        (CharacterState.WAITING, 1.6),
        (CharacterState.NOTE_TAKING, 0.6),
        (CharacterState.CAFFEINE_BREAK, 0.5),
        (CharacterState.POSTURE_RESET, 0.7),
        (CharacterState.IDLE_FOCUS, 1.2),
    ),
    CharacterState.ANALYZING: (
        (CharacterState.EXECUTING, 1.4),
        (CharacterState.IDLE_FOCUS, 2.2),
        (CharacterState.NOTE_TAKING, 0.9),
        (CharacterState.WAITING, 1.0),
        (CharacterState.ANALYZING, 0.8),
    ),
    CharacterState.EXECUTING: (
        (CharacterState.ANALYZING, 3.0),
        (CharacterState.IDLE_FOCUS, 1.0),
        (CharacterState.POSTURE_RESET, 0.5),
    ),
    CharacterState.WAITING: (
        (CharacterState.IDLE_FOCUS, 2.4),
        (CharacterState.NOTE_TAKING, 0.8),
        (CharacterState.CAFFEINE_BREAK, 0.9),
        (CharacterState.ANALYZING, 1.2),
    ),
    CharacterState.NOTE_TAKING: (
        (CharacterState.IDLE_FOCUS, 2.0),
        (CharacterState.ANALYZING, 1.6),
        (CharacterState.WAITING, 0.8),
    ),
    CharacterState.CAFFEINE_BREAK: (
        (CharacterState.IDLE_FOCUS, 3.0),
        (CharacterState.ANALYZING, 1.0),
    ),
    # A reaction resolving straight back to idle implies he saw something and dismissed
    # it. Mostly he looks into it.
    CharacterState.MARKET_REACTION: (
        (CharacterState.ANALYZING, 6.0),
        (CharacterState.IDLE_FOCUS, 3.0),
        (CharacterState.EXECUTING, 1.0),
    ),
    CharacterState.POSTURE_RESET: (
        (CharacterState.IDLE_FOCUS, 4.0),
        (CharacterState.ANALYZING, 1.0),
    ),
}


#: The only states from which deliberate execution may begin.
EXECUTING_PREDECESSORS: Final[frozenset[CharacterState]] = frozenset(
    {CharacterState.ANALYZING, CharacterState.MARKET_REACTION}
)


@dataclass(slots=True)
class _RunningAction:
    """An action currently in flight."""

    action: CharacterActionV1
    ends_monotonic: float
    chain: ActionChain | None = None
    chain_step: int = 0


@dataclass(slots=True)
class _RunningChain:
    """A chain in progress."""

    chain: ActionChain
    step: int
    started_monotonic: float

    @property
    def committed(self) -> bool:
        return self.chain.is_committed_at(self.step)


@dataclass
class DirectorOutput:
    """Everything one tick produced. Empty is the normal case."""

    actions: list[CharacterActionV1] = field(default_factory=list)
    gaze_shifts: list[GazeShiftV1] = field(default_factory=list)
    state_changed: CharacterState | None = None
    #: Human-readable notes for the debug timeline.
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.actions or self.gaze_shifts or self.state_changed)


@dataclass
class DirectorSnapshot:
    """What the control page and the debug overlay read."""

    character_state: CharacterState
    behavior_energy: float
    intensity_band: IntensityBand
    fatigue_phase: float
    focus_level: float
    work_phase: str
    recovery_rate: float
    rhythm_cycles: int
    current_action: str | None
    current_chain: str | None
    current_gaze: GazeTarget
    held_locks: tuple[str, ...]
    next_action_in_seconds: float
    actions_performed: int
    reactions_this_hour: int
    recent_actions: tuple[str, ...]
    camera_id: str
    activity: float


class BehaviorDirector:
    """Decides the character's behaviour. Pure logic; no artwork, no rendering.

    Deterministic under an injected clock and RNG, which is the whole argument for it
    living in Python: a 24-hour behaviour soak runs in seconds against
    ``VirtualClock.advance_sync`` and is reproducible from a seed.
    """

    def __init__(
        self,
        *,
        blockout: Blockout | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        market_reactivity: float = 1.0,
        music_reactivity: float = 1.0,
        visual_energy: float = 1.0,
    ) -> None:
        self._geometry = blockout or default_blockout()
        self._clock: Clock = clock or SystemClock()
        self._rng = rng or random.Random()  # noqa: S311 - behaviour variety, not security
        self._selector = WeightedSelector(self._rng)

        self.market_reactivity = market_reactivity
        self.music_reactivity = music_reactivity
        self.visual_energy = visual_energy
        self.force_idle = False

        self.locks = LockTable()
        self.cooldowns = CooldownTable()
        self.history = ActionHistory()
        #: Long-run work rhythm. Cyclic, replacing the V6 accumulator that pinned at
        #: its ceiling after about sixteen hours.
        self.rhythm = WorkRhythm()
        self.rhythm.seed(self._rng)
        self.trace = RhythmTrace()
        self.gaze_state = GazeState()
        self.gaze = GazeDirector(self._geometry, self._rng)
        self.blink = BlinkDriver()
        self.camera = CameraState()

        self.character_state = CharacterState.IDLE_FOCUS
        self._state_until = 0.0
        self._next_consider_at = 0.0
        self._running: list[_RunningAction] = []
        self._chain: _RunningChain | None = None
        self._sequence = 0

        self._activity: list[tuple[float, float]] = []
        self._reaction_times: list[float] = []
        self._deferred_reaction: tuple[float, MarketDirection | None] | None = None
        self._last_band = IntensityBand.B2_STEADY
        #: When body maintenance last happened, and when it is next admissible. Sampled,
        #: so the family has no visible period.
        self._last_posture_major: float | None = None
        self._last_posture_minor: float | None = None
        self._posture_major_due = 0.0
        self._posture_minor_due = 0.0
        self._last_symbol: str | None = None
        self._switch_seen_at: float | None = None
        self._behavior_energy = 0.5

        #: Unbounded trigram log for the soak report. The runtime penalty uses the
        #: bounded deque in ActionHistory; analysis needs every triple.
        self.trigram_log: list[tuple[str, str, str]] = []

    # ================================================== public surface

    @property
    def behavior_energy(self) -> float:
        return self._behavior_energy

    @property
    def running_actions(self) -> tuple[str, ...]:
        return tuple(entry.action.action_id for entry in self._running)

    def snapshot(self) -> DirectorSnapshot:
        now = self._clock.monotonic()
        return DirectorSnapshot(
            character_state=self.character_state,
            behavior_energy=round(self._behavior_energy, 4),
            intensity_band=self._last_band,
            fatigue_phase=round(self.rhythm.fatigue_level, 4),
            focus_level=round(self.rhythm.focus_level, 4),
            work_phase=self.rhythm.phase.value,
            recovery_rate=round(self.rhythm.recovery_rate, 4),
            rhythm_cycles=self.rhythm.cycles_completed,
            current_action=self._running[-1].action.action_id if self._running else None,
            current_chain=self._chain.chain.chain_id if self._chain else None,
            current_gaze=self.gaze_state.target,
            held_locks=tuple(sorted(lock.value for lock in self.locks.held)),
            next_action_in_seconds=round(max(0.0, self._next_consider_at - now), 3),
            actions_performed=self.history.total_recorded,
            reactions_this_hour=len(self._reaction_times),
            recent_actions=tuple(
                entry.action_id for entry in self.history.window(10)
            ),
            camera_id=self.camera.camera_id,
            activity=round(self._activity_level(now), 4),
        )

    def tick(self, state: VisualStateV1) -> DirectorOutput:
        """One decision cycle. The only entry point."""
        now = self._clock.monotonic()
        at = self._clock.now()
        out = DirectorOutput()

        self.rhythm.advance(
            now, workload=self._activity_level(now), band=state.intensity_band
        )
        self.trace.observe(self.rhythm, now)
        self.history.trim(now)
        self._trim_activity(now)
        self._trim_reactions(now)
        self._observe_symbol(state, now)

        self._behavior_energy = behavior_energy(
            state, recent_activity=self._activity_level(now)
        ) * max(0.3, min(1.6, self.visual_energy))
        self._behavior_energy = max(0.05, min(0.98, self._behavior_energy))
        self._last_band = state.intensity_band

        self._retire(now, out)
        self._advance_chain(now, at, state, out)
        self._resolve_deferred(now, at, out)
        self._maybe_react(now, at, state, out)
        self._maybe_change_state(now, state, out)

        if self._chain is None and now >= self._next_consider_at:
            self._consider(now, at, state, out)

        self._drive_gaze(now, at, state, out)
        self._drive_blink(now, at, out)
        return out

    def advance_to(self, state: VisualStateV1, seconds: float) -> list[DirectorOutput]:
        """Run ticks across ``seconds`` of clock time. For the simulation harness.

        Requires a clock that can be advanced synchronously, which is why the harness
        uses :class:`~tradefix_radio.core.clock.VirtualClock` — a behaviour soak is pure
        computation with nothing awaiting, so ``advance_sync`` is both correct and the
        fastest path.
        """
        outputs: list[DirectorOutput] = []
        remaining = seconds
        while remaining > 0:
            step = min(TICK_SECONDS, remaining)
            result = self.tick(state)
            if not result.is_empty:
                outputs.append(result)
            if hasattr(self._clock, "advance_sync"):
                self._clock.advance_sync(step)
            remaining -= step
        return outputs

    def trigger(self, action_id: str) -> CharacterActionV1 | None:
        """Operator test-trigger: run one action now, bypassing cooldowns.

        Locks are **still respected**. Bypassing them would let a console click produce
        the floating mug every structural guarantee in this package exists to prevent.
        """
        action = spec(action_id)
        if self._object_conflict(action):
            _log.info("visual.trigger_blocked", action=action_id, reason="object_in_hand")
            return None
        if not self.locks.permits(action):
            held = self.locks.blocked_by(action.effective_locks)
            blocked = ", ".join(sorted(lock.value for lock in held))
            _log.info("visual.trigger_blocked", action=action_id, locks=blocked)
            return None
        self.cooldowns.force_ready(action_id)
        now, at = self._clock.monotonic(), self._clock.now()
        out = DirectorOutput()
        return self._start(action, now, at, out, factors={"operator_trigger": 1.0})

    # ================================================== lifecycle internals

    def _retire(self, now: float, out: DirectorOutput) -> None:
        still_running: list[_RunningAction] = []
        for entry in self._running:
            if entry.ends_monotonic > now:
                still_running.append(entry)
                continue
            action_id = entry.action.action_id
            if entry.chain is None:
                self.locks.release(action_id)
            self.gaze.release(self.gaze_state, action_id, now)
            out.notes.append(f"end {action_id}")
        self._running = still_running

    def _advance_chain(
        self, now: float, at: datetime, state: VisualStateV1, out: DirectorOutput
    ) -> None:
        """Step a running chain forward. Chains are transactional once committed."""
        if self._chain is None:
            return
        if any(entry.chain is not None for entry in self._running):
            return  # current step still in flight

        running = self._chain
        running.step += 1
        if running.step >= running.chain.length:
            self.locks.release_chain(running.chain.chain_id)
            out.notes.append(f"chain complete {running.chain.chain_id}")
            if running.chain.chain_id == "COFFEE_DRINK":
                # Raises the recovery RATE for a while. It does not subtract from the
                # level: a coffee helps, it does not undo an hour, and the step-decrement
                # version suppressed the whole cycle.
                self.rhythm.note("coffee", now)
            self._chain = None
            self._arm_next_consider(now, state)
            return

        step_action = spec(running.chain.steps[running.step])
        self._start(
            step_action,
            now,
            at,
            out,
            chain=running.chain,
            chain_step=running.step,
            factors={"chain_step": float(running.step)},
        )

    def _start(
        self,
        action: ActionSpecV1,
        now: float,
        at: datetime,
        out: DirectorOutput,
        *,
        chain: ActionChain | None = None,
        chain_step: int = 0,
        factors: dict[str, float] | None = None,
    ) -> CharacterActionV1:
        """Emit one action: sample it, claim its locks, record it."""
        low, high = action.duration_ms
        duration = low if high <= low else self._rng.randint(low, high)
        blend_scale = profile_for(self._last_band).blend_scale
        self._sequence += 1

        emitted = CharacterActionV1(
            sequence=self._sequence,
            action_id=action.action_id,
            category=action.category,
            started_at=at,
            duration_ms=duration,
            blend_in_ms=int(action.blend_in_ms * blend_scale),
            blend_out_ms=int(action.blend_out_ms * blend_scale),
            interruptibility=action.interruptibility,
            locks=tuple(sorted(action.effective_locks, key=lambda lock: lock.value)),
            amplitude=round(self._rng.uniform(*AMPLITUDE_JITTER), 4),
            anchor=action.required_anchor,
            gaze_target=action.gaze_target,
            character_state=self.character_state,
            chain_id=chain.chain_id if chain else None,
            chain_step=chain_step if chain else None,
            factors=factors or {},
        )

        ends = emitted.ends_at_monotonic(now)
        if chain is None:
            self.locks.claim(action, until_monotonic=ends)
        elif chain_step == 0:
            # The chain claims the union of EVERY step's locks, for the whole run. Per
            # step would leave gaps between steps for another action to grab the hand,
            # and claiming only step 0's locks let the chain start without the resources
            # a later step needs — which failed loudly inside two simulated hours.
            self.locks.claim_locks(
                chain.required_locks(),
                action_id=chain.chain_id,
                until_monotonic=ends,
                chain_id=chain.chain_id,
            )

        self._running.append(
            _RunningAction(action=emitted, ends_monotonic=ends, chain=chain, chain_step=chain_step)
        )
        self.cooldowns.arm(action.action_id, now, self._rng)

        entry = HistoryEntry(
            action_id=action.action_id,
            category=action.category,
            anti_repeat_key=action.anti_repeat_key,
            started_monotonic=now,
            duration_seconds=emitted.total_ms / 1000.0,
            chain_id=chain.chain_id if chain else None,
        )
        if len(self.history.window(2)) == 2:
            window = self.history.window(2)
            self.trigram_log.append(
                (window[0].action_id, window[1].action_id, action.action_id)
            )
        self.history.record(entry)
        self._activity.append((now, action.energy_cost))

        if action.gaze_target is not None:
            self.gaze.force(self.gaze_state, action.gaze_target, action.action_id, now)
        if is_body_maintenance(action.action_id):
            self._note_posture(action.action_id, now)

        out.actions.append(emitted)
        return emitted

    # ================================================== reactions

    def _maybe_react(
        self, now: float, at: datetime, state: VisualStateV1, out: DirectorOutput
    ) -> None:
        if not self._reaction_permitted(now, state):
            return
        threshold = profile_for(state.intensity_band).reaction_threshold
        if state.reaction_salience * self.market_reactivity < threshold:
            return

        # A committed chain defers rather than cancels. The converse of the brief's
        # "coffee cannot interrupt an emergency reaction" is equally required: a
        # reaction that cancelled a grip would leave a mug in mid-air with no hand on it.
        if self._chain is not None and self._chain.committed:
            self._deferred_reaction = (now, state.market_direction)
            out.notes.append("reaction deferred behind a committed chain")
            return
        self._fire_reaction(now, at, out, state.market_direction)

    def _reaction_permitted(self, now: float, state: VisualStateV1) -> bool:
        if self.force_idle or not state.market_reactive:
            return False
        if state.market_confidence is not None and (
            state.market_confidence < REACTION_MIN_CONFIDENCE
        ):
            return False
        if self._reaction_times and now - self._reaction_times[-1] < REACTION_REFRACTORY_SECONDS:
            return False
        return len(self._reaction_times) < REACTION_HOUR_CAP

    def _resolve_deferred(
        self, now: float, at: datetime, out: DirectorOutput
    ) -> None:
        if self._deferred_reaction is None:
            return
        queued_at, direction = self._deferred_reaction
        if now - queued_at > REACTION_DEFER_LIMIT_SECONDS:
            self._deferred_reaction = None
            out.notes.append("deferred reaction dropped: too late to read as a reaction")
            return
        if self._chain is not None and self._chain.committed:
            return
        self._deferred_reaction = None
        self._fire_reaction(now, at, out, direction)

    def _fire_reaction(
        self,
        now: float,
        at: datetime,
        out: DirectorOutput,
        direction: MarketDirection | None,
    ) -> None:
        """Compose a reaction from an opener, an optional action, an optional close."""
        self.character_state = CharacterState.MARKET_REACTION
        low, high = STATE_DWELL_SECONDS[CharacterState.MARKET_REACTION]
        self._state_until = now + self._rng.uniform(low, high)
        self._reaction_times.append(now)
        out.state_changed = CharacterState.MARKET_REACTION

        sequence = [self._pick_reaction(REACTION_OPENERS, direction)]
        if self._rng.random() < 0.55:
            sequence.append(self._pick_reaction(REACTION_ACTIONS, direction))
        if self._rng.random() < 0.6:
            sequence.append(self._pick_reaction(REACTION_CLOSERS, direction))

        for action_id in sequence:
            if action_id is None:
                continue
            action = spec(action_id)
            if not self.locks.permits(action):
                continue
            self._start(action, now, at, out, factors={"reaction": 1.0})
            now += action.duration_ms[0] / 1000.0
        out.notes.append("reaction fired")

    def _pick_reaction(
        self, pool: tuple[str, ...], direction: MarketDirection | None
    ) -> str | None:
        """Weighted pick from a reaction role, with the directional filter applied.

        `subtle_smirk` is removed on an adverse move as a **filter, not a weight**. A
        weight can always be overcome by enough other multipliers, and the character
        smirking at a loss would be the single most character-breaking frame the system
        could produce.
        """
        favourable = direction in (MarketDirection.BULLISH, None)
        candidates: list[Candidate[str]] = []
        for action_id in pool:
            action = spec(action_id)
            if "favourable_only" in action.tags and not favourable:
                continue
            if self._object_conflict(action):
                continue
            if action.max_hour_share is not None and (
                self.history.action_share(action_id) >= action.max_hour_share
            ):
                continue
            penalty, _ = self.history.penalty(action_id)
            candidate = Candidate(value=action_id, weight=action.weight)
            candidate.multiply("anti_repeat", max(0.02, penalty))
            candidates.append(candidate)
        if not candidates:
            return None
        return self._selector.select(candidates).chosen

    # ================================================== state machine

    def _maybe_change_state(
        self, now: float, state: VisualStateV1, out: DirectorOutput
    ) -> None:
        if self.force_idle:
            if self.character_state is not CharacterState.IDLE_FOCUS:
                self.character_state = CharacterState.IDLE_FOCUS
                out.state_changed = CharacterState.IDLE_FOCUS
            self._state_until = now + 30.0
            return
        if self._chain is not None or now < self._state_until:
            return

        profile = profile_for(state.intensity_band)
        options = STATE_TRANSITIONS[self.character_state]
        candidates: list[Candidate[CharacterState]] = []
        for target, weight in options:
            candidate = Candidate(value=target, weight=weight)
            if target is CharacterState.IDLE_FOCUS:
                # Idle share is the band's business: dormant sits still, peak does not.
                candidate.multiply("idle_share", 0.4 + 1.6 * profile.idle_share)
            if target is CharacterState.CAFFEINE_BREAK:
                candidate.multiply("fatigue", self.rhythm.coffee_weight_scale())
                if self.locks.holds_object():
                    candidate.veto("an object is already held")
            if target is CharacterState.EXECUTING:
                candidate.multiply("energy", 0.5 + 1.2 * self._behavior_energy)
            if target is CharacterState.POSTURE_RESET:
                since = self.history.seconds_since_key("posture", now)
                gap = 1.0 if since is None else min(2.5, since / 600.0)
                candidate.multiply("posture_gap", gap)
            candidates.append(candidate)

        chosen = self._selector.select(candidates).chosen
        low, high = STATE_DWELL_SECONDS[chosen]
        # Low energy lengthens dwell, but gently. An earlier version divided by
        # (energy + 0.5), which at the 0.08 energy floor stretched every dwell by 1.7x
        # and let WAITING take 36 % of a quiet half-hour against a 10 % target.
        self._state_until = now + self._rng.uniform(low, high) * (
            1.25 - 0.35 * self._behavior_energy
        )
        if chosen is not self.character_state:
            self.character_state = chosen
            out.state_changed = chosen
            # Consider an action promptly on entering a new state. Without this the
            # old interval runs on, and a short-dwell purposeful state exits before it
            # ever acts — POSTURE_RESET dwells 3-10 s against a consideration interval
            # of up to 4 s, and in a 30-minute soak it produced its action zero times.
            self._next_consider_at = min(
                self._next_consider_at, now + self._rng.uniform(0.2, 0.8)
            )

    # ================================================== selection

    def _consider(
        self, now: float, at: datetime, state: VisualStateV1, out: DirectorOutput
    ) -> None:
        """Score every admissible action and select one, or deliberately nothing."""
        self._arm_next_consider(now, state)
        if self.force_idle:
            return

        band = state.intensity_band
        fatigue_gate = self.rhythm.fatigue_action_gate()
        fatigue_phase = self.rhythm.fatigue_level
        posture_gate = self._posture_gate(now)
        candidates: list[Candidate[str | None]] = []

        for action_id in SCHEDULABLE:
            action = CATALOG[action_id]
            if not action.permits_state(self.character_state):
                continue
            # A chain entry must be checked against everything the WHOLE chain will
            # need, not just its first step's locks.
            chain = chain_for(action_id)
            required = chain.required_locks() if chain else action.effective_locks
            if not self.locks.permits_locks(required):
                continue
            if self._object_conflict(action, chain):
                continue
            if not self.cooldowns.ready(action_id, now):
                continue
            if action.min_fatigue_phase > 0.0 and fatigue_phase < action.min_fatigue_phase:
                continue
            bias = action.market_bias.for_band(band)
            if bias is None:
                continue  # unavailable in this band, not merely unlikely

            candidate: Candidate[str | None] = Candidate(value=action_id, weight=action.weight)
            candidate.multiply("band_bias", bias)
            candidate.multiply(
                "music",
                music_weight_factor(
                    action.music_bias, state, reactivity=self.music_reactivity
                ),
            )
            penalty, _ = self.history.penalty(action_id)
            candidate.multiply("anti_repeat", penalty)
            # Keyed on the anti-repeat family rather than the category: `posture_reset`
            # is catalogued under FATIGUE but is ordinary postural behaviour, and gating
            # it on the category suppressed it entirely below phase 0.35.
            if action.anti_repeat_key == "fatigue":
                candidate.multiply("fatigue_gate", fatigue_gate)
            if action.anti_repeat_key == "coffee":
                candidate.multiply("fatigue_coffee", self.rhythm.coffee_weight_scale())
            if action.category is ActionCategory.WORK:
                candidate.multiply("energy", 0.55 + 0.9 * self._behavior_energy)
                # Focus is why his output varies over hours for reasons the market did
                # not cause: DEEP_WORK at high focus works harder than RECOVERY does.
                candidate.multiply("focus", self.rhythm.work_intensity_scale())
            if action.category is ActionCategory.POSTURE:
                tier = "major" if is_major_posture(action_id) else "minor"
                candidate.multiply("posture_gate", posture_gate[tier])
            # The market-switch attention shift: a brief look at the main chart, then
            # normal work. Explicitly not a reset — the brief forbids that.
            switching = (
                self._switch_seen_at is not None
                and now - self._switch_seen_at < SWITCH_ATTENTION_WINDOW_SECONDS
            )
            if switching and action.gaze_target is GazeTarget.MONITOR_MAIN:
                candidate.multiply("symbol_switch", 1.8)
            candidates.append(candidate)

        # Stillness. Weight rises with recent activity, which is what makes work arrive
        # in bouts rather than at a constant drip.
        activity = self._activity_level(now)
        idle: Candidate[str | None] = Candidate(value=None, weight=IDLE_WEIGHT_BASE)
        idle.multiply("activity", 1.0 + IDLE_WEIGHT_ACTIVITY_GAIN * activity)
        idle.multiply("idle_share", 0.5 + 1.5 * profile_for(band).idle_share)
        candidates.append(idle)

        constraints: tuple[Constraint[str | None], ...] = (
            Constraint(
                name="no_immediate_repeat",
                predicate=self._not_immediate_repeat,
                reason="the same action twice in a row",
                severity=9,
            ),
        )
        result = self._selector.select(candidates, constraints)
        chosen = result.chosen
        if chosen is None:
            out.notes.append("chose stillness")
            return

        action = spec(chosen)
        chosen_chain = chain_for(chosen)
        factors = {
            name: round(value, 4)
            for name, value in next(
                c.factors for c in result.candidates if c.value == chosen
            ).items()
        }
        if chosen_chain is not None:
            self._chain = _RunningChain(chain=chosen_chain, step=0, started_monotonic=now)
            entry = spec(chosen_chain.steps[0])
            self._start(
                entry, now, at, out, chain=chosen_chain, chain_step=0, factors=factors
            )
            # The intent's own cooldown is armed too, so `coffee_drink` cannot be
            # re-selected the moment the chain completes.
            self.cooldowns.arm(chosen, now, self._rng)
            out.notes.append(f"chain start {chosen_chain.chain_id}")
            return

        self._start(action, now, at, out, factors=factors)
        if action.anti_repeat_key == "fatigue" and action.action_id != FATIGUE_TAIL:
            tail = spec(FATIGUE_TAIL)
            if self.locks.permits(tail):
                self._start(tail, now, at, out, factors={"fatigue_tail": 1.0})

    def _object_conflict(self, action: ActionSpecV1, chain: ActionChain | None = None) -> bool:
        """Whether starting this would put two object interactions in flight at once.

        **One object interaction at a time**, and this must be checked wherever an action
        can start — not only in the scheduler. The lock union does not express it: a
        coffee chain holds {coffee, left_hand} and a pen action holds {pen, right_hand},
        so the lock table sees no conflict and both proceed.

        The first version of this rule lived only in `_consider`, and the soak found the
        hole within eight hours: a market reaction composed `quick_note`, which grabbed
        the pen while the mug was already in his other hand. Two hands could do it; a man
        mid-sip writing a note is not a work moment anyone would recognise.
        """
        wanted = chain.required_locks() if chain is not None else action.effective_locks
        objects = {InteractionLock.COFFEE, InteractionLock.PEN}
        if not (wanted & objects):
            return False
        return bool(self.locks.held & objects)

    def _posture_gate(self, now: float) -> dict[str, float]:
        """Whether body maintenance is admissible, per tier.

        The structural half of the posture fix. The V6 version tied `posture_reset` to a
        single short-dwell state and a fatigue ramp, and it fired zero times in two
        simulated hours. Raising its weight would not have helped: it was almost never
        *eligible*.

        So eligibility is a gate with the brief's three conditions, and nothing else:

        1. **Time since the last major posture change exceeds a sampled threshold.** Not
           a fixed one — a fixed interval is a visible period, and over eight hours a
           viewer would learn when he is about to shift in his chair.
        2. **No object-interaction chain is active.** Checked through the lock table as
           well as the chain slot, because a chain's locks outlive its last step by a
           tick and a reset that began in that window would fight the release.
        3. **No high-priority market reaction is active.** He does not stretch while
           something is moving.

        Returns a multiplier per tier rather than a boolean, so the family fades in as
        the threshold approaches instead of becoming available at full strength the
        instant it passes — which would cluster resets immediately after each gate.
        """
        blocked = {"major": 0.0, "minor": 0.0}

        if self._chain is not None or self.locks.holds_object():
            return blocked
        if self.character_state is CharacterState.MARKET_REACTION:
            return blocked
        if self._deferred_reaction is not None:
            return blocked
        if any(
            entry.action.category is ActionCategory.REACTION for entry in self._running
        ):
            return blocked

        if self._posture_major_due == 0.0:
            self._arm_posture_gates(now)

        def ramp(last: float | None, due: float, window: float) -> float:
            if last is None:
                return 1.0  # never done; admissible from the first opportunity
            remaining = due - now
            if remaining <= 0.0:
                return 1.0
            if remaining > window:
                return 0.0
            # Linear fade over the last third of the interval.
            return 1.0 - remaining / window

        major_window = POSTURE_MAINTENANCE_INTERVAL_SECONDS[0] / 3.0
        minor_window = POSTURE_MINOR_INTERVAL_SECONDS[0] / 3.0
        return {
            "major": ramp(self._last_posture_major, self._posture_major_due, major_window),
            "minor": ramp(self._last_posture_minor, self._posture_minor_due, minor_window),
        }

    def _arm_posture_gates(self, now: float) -> None:
        """Sample the next maintenance intervals. Never constant."""
        self._posture_major_due = now + self._rng.uniform(
            *POSTURE_MAINTENANCE_INTERVAL_SECONDS
        )
        self._posture_minor_due = now + self._rng.uniform(*POSTURE_MINOR_INTERVAL_SECONDS)

    def _note_posture(self, action_id: str, now: float) -> None:
        """Record a body-maintenance action and re-arm its gate."""
        if is_major_posture(action_id):
            self._last_posture_major = now
            self._posture_major_due = now + self._rng.uniform(
                *POSTURE_MAINTENANCE_INTERVAL_SECONDS
            )
            # A major reset is one of the brief's recovery influences.
            self.rhythm.note("posture_reset", now)
        else:
            self._last_posture_minor = now
            self._posture_minor_due = now + self._rng.uniform(
                *POSTURE_MINOR_INTERVAL_SECONDS
            )

    def _not_immediate_repeat(self, action_id: str | None) -> bool:
        if action_id is None:
            return True
        last = self.history.last
        return last is None or last.action_id != action_id

    def _arm_next_consider(self, now: float, state: VisualStateV1) -> None:
        low, high = BASE_INTERVAL_SECONDS
        scale = interval_scale(state.intensity_band, self._behavior_energy)
        self._next_consider_at = now + self._rng.uniform(low, high) * scale

    # ================================================== gaze and blink

    def _drive_gaze(
        self, now: float, at: datetime, state: VisualStateV1, out: DirectorOutput
    ) -> None:
        if self.gaze_state.forced_by is not None:
            holder = self.gaze_state.forced_by
            if holder not in self.running_actions:
                self.gaze.release(self.gaze_state, holder, now)
            return
        target = self.gaze.choose(
            state=state,
            character_state=self.character_state,
            gaze_state=self.gaze_state,
            now=now,
            active_camera=self.camera.camera_id,
        )
        if target is None:
            if self.gaze.micro_saccade_due(self.gaze_state, now):
                self.gaze.arm_micro_saccade(self.gaze_state, now)
            return
        shift = self.gaze.shift(
            to=target,
            gaze_state=self.gaze_state,
            now=now,
            at=at,
            band=state.intensity_band,
            fatigue=self.rhythm,
        )
        self.gaze.apply(shift, self.gaze_state, now)
        out.gaze_shifts.append(shift)
        if shift.forces_blink and self.blink.force(now):
            out.notes.append("blink forced across saccade")

    def _drive_blink(self, now: float, at: datetime, out: DirectorOutput) -> None:
        if self.blink.next_at == 0.0:
            self.blink.schedule(
                now, self._rng, state=self.character_state, fatigue=self.rhythm
            )
            return
        if not self.blink.due(now):
            return
        kind = self.blink.kind(self._rng, now)
        action = spec(kind)
        self._sequence += 1
        low, high = action.duration_ms
        duration = self._rng.randint(low, high)
        emitted = CharacterActionV1(
            sequence=self._sequence,
            action_id=kind,
            category=ActionCategory.MICRO,
            started_at=at,
            duration_ms=duration,
            blend_in_ms=0,
            blend_out_ms=0,
            interruptibility=Interruptibility.ALWAYS,
            character_state=self.character_state,
            factors={"reflex": 1.0},
        )
        out.actions.append(emitted)
        self.blink.mark_performed(now, duration)
        self.blink.schedule(
                now, self._rng, state=self.character_state, fatigue=self.rhythm
            )
        # Blinks are reflexes and are deliberately kept out of the action history: they
        # would otherwise dominate every window and swamp the anti-repetition signal
        # that exists to police deliberate behaviour.

    # ================================================== bookkeeping

    def _observe_symbol(self, state: VisualStateV1, now: float) -> None:
        """Notice a market switch. **Does not reset anything.**

        The brief is explicit: *"When symbol changes, do NOT reset character."* So this
        records a timestamp that biases gaze toward the main chart for thirty seconds,
        and nothing else. No state change, no lock release, no history clear, no chain
        abort.
        """
        if self._last_symbol is None:
            self._last_symbol = state.active_symbol
            return
        if state.active_symbol != self._last_symbol:
            self._last_symbol = state.active_symbol
            self._switch_seen_at = now

    def _trim_activity(self, now: float) -> None:
        cutoff = now - ACTIVITY_WINDOW_SECONDS
        self._activity = [(t, cost) for t, cost in self._activity if t >= cutoff]

    def _activity_level(self, now: float) -> float:
        self._trim_activity(now)
        total = sum(cost for _, cost in self._activity)
        return min(1.0, total / ACTIVITY_SATURATION)

    def _trim_reactions(self, now: float) -> None:
        self._reaction_times = [t for t in self._reaction_times if now - t <= 3600.0]


__all__ = [
    "ACTIVITY_SATURATION",
    "ACTIVITY_WINDOW_SECONDS",
    "BASE_INTERVAL_SECONDS",
    "EXECUTING_PREDECESSORS",
    "REACTION_DEFER_LIMIT_SECONDS",
    "REACTION_HOUR_CAP",
    "REACTION_MIN_CONFIDENCE",
    "REACTION_REFRACTORY_SECONDS",
    "STATE_DWELL_SECONDS",
    "STATE_TRANSITIONS",
    "SWITCH_ATTENTION_WINDOW_SECONDS",
    "TICK_HZ",
    "TICK_SECONDS",
    "BehaviorDirector",
    "DirectorOutput",
    "DirectorSnapshot",
]
