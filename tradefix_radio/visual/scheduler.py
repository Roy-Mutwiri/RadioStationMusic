"""Locks, cooldowns and anti-repetition: the mechanics the director scores against.

Three independent systems, kept separate because they answer different questions:

:class:`LockTable`        *can* this action run right now — is the hand free?
:class:`CooldownTable`    *may* it run — has enough time passed?
:class:`ActionHistory`    *should* it run — would it be repetitive?

The first two are hard gates. The third is graded, and that distinction is the brief's:
*"Create a repetition penalty instead of only hard exclusions. This allows natural
actions to recur eventually."* A veto-only system starves — after twenty minutes
everything plausible has been used and the scheduler is left choosing between things it
has forbidden. A graded penalty lets a coffee sip become likely again after half an
hour without ever being guaranteed.

Reuse note
----------
Candidate scoring reuses :mod:`tradefix_radio.director.selection` — the same
``WeightedSelector`` / ``Candidate`` / ``Constraint`` machinery the music director uses
for genre choice, and the same ``recency_penalty`` curve. It is pure arithmetic with no
station state, so importing it does not compromise the visual layer's read-only
isolation, and the alternative is a second implementation of a solved problem that would
drift from the first.
"""

from __future__ import annotations

import random
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from tradefix_radio.visual.catalog import (
    CATEGORY_HOUR_SHARE_CEILING,
    group_cooldown_range,
    spec,
)
from tradefix_radio.visual.contracts import (
    ActionCategory,
    ActionSpecV1,
    InteractionLock,
    Interruptibility,
)

#: History depths the brief asks for. Three windows, three different jobs: the short one
#: stops back-to-back repeats, the medium one stops a three-action cycle, the long one
#: stops a family quietly dominating.
WINDOW_SHORT: Final = 3
WINDOW_MEDIUM: Final = 10
WINDOW_LONG: Final = 30

#: Rolling window for share ceilings, seconds.
SHARE_WINDOW_SECONDS: Final = 3600.0

#: How many ordered triples to remember. The brief's `A → B → C → A → B → C` test.
#:
#: Worth being explicit about why this matters most: rules against back-to-back repeats
#: are satisfied by almost any scheduler and are *not* what makes a stream feel looped.
#: What viewers actually notice is the n-gram — the same three actions in the same order,
#: twenty minutes apart. Tracking triples is cheap and is the difference between passing
#: a two-minute review and passing a two-hour soak.
TRIGRAM_MEMORY: Final = 48

#: Penalty applied to a candidate that would complete a remembered triple.
TRIGRAM_PENALTY: Final = 0.12

#: Rolling entries required before a share ceiling is applied.
#:
#: Without this a one-entry window reports every category at 100 % and the ceiling
#: zeroes the penalty outright — which turns the graded rule the brief asked for back
#: into the veto it was meant to replace. Twenty is enough for a share to mean anything.
SHARE_MIN_SAMPLE: Final = 20

#: Recency penalty strengths per window. Graded, never zero.
RECENCY_SHORT: Final = 0.15
RECENCY_MEDIUM: Final = 0.45
RECENCY_LONG: Final = 0.82


# ============================================================ locks


class LockConflict(Exception):
    """Raised when a claim is attempted against a held lock.

    An exception rather than a boolean return, because every caller inside the director
    checks availability first — so reaching this means a scheduling bug, and a silent
    `False` would let it through as a dropped action instead of a loud failure.
    """


@dataclass(slots=True)
class _Held:
    action_id: str
    until_monotonic: float
    interruptibility: Interruptibility
    chain_id: str | None


class LockTable:
    """Which body parts and objects are occupied, and until when.

    The brief's examples become arithmetic here. "While the coffee mug is held, typing
    must not start" is not a rule the scheduler remembers; it is the observation that
    ``typing_short`` claims ``LEFT_HAND`` and ``coffee_drink`` already holds it.

    ``BOTH_HANDS`` is expanded by :attr:`ActionSpecV1.effective_locks` before it gets
    here, which is what makes a one-handed action correctly conflict with a two-handed
    one — the naive version treats ``BOTH_HANDS`` as a distinct resource and lets a
    mouse move start during a two-handed typing burst.
    """

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held: dict[InteractionLock, _Held] = {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if not self._held:
            return "LockTable(free)"
        ordered = sorted(self._held.items(), key=lambda item: item[0].value)
        parts = ", ".join(f"{lock.value}<-{held.action_id}" for lock, held in ordered)
        return f"LockTable({parts})"

    @property
    def held(self) -> frozenset[InteractionLock]:
        return frozenset(self._held)

    @property
    def is_free(self) -> bool:
        return not self._held

    def holder(self, lock: InteractionLock) -> str | None:
        entry = self._held.get(lock)
        return entry.action_id if entry else None

    def blocked_by(self, locks: Iterable[InteractionLock]) -> frozenset[InteractionLock]:
        """Which of ``locks`` are unavailable."""
        return frozenset(lock for lock in locks if lock in self._held)

    def permits(self, action: ActionSpecV1) -> bool:
        return not self.blocked_by(action.effective_locks)

    def claim(
        self,
        action: ActionSpecV1,
        *,
        until_monotonic: float,
        chain_id: str | None = None,
    ) -> None:
        locks = action.effective_locks
        conflict = self.blocked_by(locks)
        if conflict:
            names = ", ".join(sorted(lock.value for lock in conflict))
            holders = ", ".join(sorted(f"{self.holder(lock)}" for lock in conflict))
            raise LockConflict(
                f"{action.action_id} cannot claim {names}: held by {holders}"
            )
        for lock in locks:
            self._held[lock] = _Held(
                action_id=action.action_id,
                until_monotonic=until_monotonic,
                interruptibility=action.interruptibility,
                chain_id=chain_id,
            )

    def claim_locks(
        self,
        locks: Iterable[InteractionLock],
        *,
        action_id: str,
        until_monotonic: float,
        interruptibility: Interruptibility = Interruptibility.NEVER,
        chain_id: str | None = None,
    ) -> None:
        """Claim an explicit lock set. Used for a chain's full union at commit."""
        wanted = frozenset(lock for lock in locks if lock is not InteractionLock.NONE)
        conflict = self.blocked_by(wanted)
        if conflict:
            names = ", ".join(sorted(lock.value for lock in conflict))
            holders = ", ".join(sorted(str(self.holder(lock)) for lock in conflict))
            raise LockConflict(f"{action_id} cannot claim {names}: held by {holders}")
        for lock in wanted:
            self._held[lock] = _Held(
                action_id=action_id,
                until_monotonic=until_monotonic,
                interruptibility=interruptibility,
                chain_id=chain_id,
            )

    def permits_locks(self, locks: Iterable[InteractionLock]) -> bool:
        return not self.blocked_by(locks)

    def release(self, action_id: str) -> None:
        for lock in [lock for lock, entry in self._held.items() if entry.action_id == action_id]:
            del self._held[lock]

    def release_chain(self, chain_id: str) -> None:
        for lock in [lock for lock, entry in self._held.items() if entry.chain_id == chain_id]:
            del self._held[lock]

    def expire(self, now_monotonic: float) -> tuple[str, ...]:
        """Release everything whose action has finished. Returns the released ids."""
        done = sorted(
            {
                entry.action_id
                for entry in self._held.values()
                if entry.until_monotonic <= now_monotonic
            }
        )
        for action_id in done:
            self.release(action_id)
        return tuple(done)

    def interruptible_holders(self, now_monotonic: float) -> tuple[str, ...]:
        """Actions currently holding a lock that could be pre-empted.

        ``AFTER_BLEND_IN`` holders are reported only once their blend-in has elapsed.
        A cut into a blend is the neck snap the brief forbids.
        """
        out: set[str] = set()
        for entry in self._held.values():
            if entry.interruptibility is Interruptibility.ALWAYS:
                out.add(entry.action_id)
            elif entry.interruptibility is Interruptibility.AFTER_BLEND_IN:
                blend = spec(entry.action_id).blend_in_ms / 1000.0
                if entry.until_monotonic - now_monotonic < 0 or blend <= 0:
                    out.add(entry.action_id)
        return tuple(sorted(out))

    def holds_object(self) -> bool:
        """Whether a physical object is currently gripped.

        The single most important query in the system. While it is true, no chain may
        start and no conflicting hand action may begin — which is how the brief's
        "start coffee chain while already in another object chain" invariant holds.
        """
        return bool(self.held & {InteractionLock.COFFEE, InteractionLock.PEN})


# ============================================================ cooldowns


class CooldownTable:
    """Per-action and per-group readiness, with **sampled** intervals.

    The brief: *"Cooldowns must be sampled, not constant."* Enforced structurally — this
    class has no method that accepts a fixed interval. Every arm draws from the spec's
    range, so no two gaps between the same action are ever equal, and the action cannot
    acquire a visible period even if someone later tunes its range badly.

    Group cooldowns are the second half. Four headphone actions with separate 240-second
    cooldowns can be satisfied in sequence, producing a man who spends a minute adjusting
    his headphones while breaking no rule. The shared `headphones` key forbids it.
    """

    __slots__ = ("_action_ready", "_group_ready", "_last_sampled")

    def __init__(self) -> None:
        self._action_ready: dict[str, float] = {}
        self._group_ready: dict[str, float] = {}
        #: Kept for the simulation report, which checks that sampling is actually varying.
        self._last_sampled: dict[str, float] = {}

    def ready(self, action_id: str, now_monotonic: float) -> bool:
        if self._action_ready.get(action_id, 0.0) > now_monotonic:
            return False
        key = spec(action_id).anti_repeat_key
        return self._group_ready.get(key, 0.0) <= now_monotonic

    def remaining(self, action_id: str, now_monotonic: float) -> float:
        """Seconds until ready. Zero when ready. Feeds the next-action timer in the UI."""
        own = self._action_ready.get(action_id, 0.0) - now_monotonic
        key = spec(action_id).anti_repeat_key
        group = self._group_ready.get(key, 0.0) - now_monotonic
        return max(0.0, own, group)

    def arm(self, action_id: str, now_monotonic: float, rng: random.Random) -> float:
        """Sample this action's cooldown and start it. Returns the sampled seconds."""
        action = spec(action_id)
        low, high = action.cooldown_range_seconds
        sampled = low if high <= low else rng.uniform(low, high)
        self._action_ready[action_id] = now_monotonic + sampled
        self._last_sampled[action_id] = sampled

        group = group_cooldown_range(action_id)
        if group is not None:
            # Sampled too: a fixed group cooldown reintroduces exactly the periodicity
            # the per-action sampling removed, one level up.
            low_g, high_g = group
            jittered = low_g if high_g <= low_g else rng.uniform(low_g, high_g)
            key = action.anti_repeat_key
            self._group_ready[key] = max(
                self._group_ready.get(key, 0.0), now_monotonic + jittered
            )
        return sampled

    def last_sampled(self, action_id: str) -> float | None:
        return self._last_sampled.get(action_id)

    def force_ready(self, action_id: str) -> None:
        """Clear this action's cooldown. Used only by the operator test-trigger."""
        self._action_ready.pop(action_id, None)
        self._group_ready.pop(spec(action_id).anti_repeat_key, None)


# ============================================================ history


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One performed action, as the anti-repetition rules see it."""

    action_id: str
    category: ActionCategory
    anti_repeat_key: str
    started_monotonic: float
    duration_seconds: float
    chain_id: str | None = None


@dataclass
class RepetitionReport:
    """What the anti-repetition analysis found. Produced by the simulation harness."""

    total_actions: int
    distinct_actions: int
    category_shares: dict[str, float] = field(default_factory=dict)
    action_shares: dict[str, float] = field(default_factory=dict)
    #: The most frequent ordered triple, and how often it occurred.
    top_trigram: tuple[tuple[str, str, str], int] | None = None
    #: Share of all triples taken by the single most common one. The headline number:
    #: a deterministic loop drives this toward 1.0.
    top_trigram_share: float = 0.0
    #: Longest run of a single action id back to back. Must be 1.
    longest_identical_run: int = 1
    #: Distinct triples divided by total triples. Higher is more varied.
    trigram_diversity: float = 0.0
    back_to_back_repeats: int = 0

    @property
    def has_visible_loop(self) -> bool:
        """Whether a reviewer would plausibly notice a cycle.

        Thresholds chosen from the soak rather than from theory: with 58 actions and a
        working penalty the top triple lands near 1 %, so 4 % is comfortably abnormal
        without being so loose that a real regression slips through.
        """
        return (
            self.top_trigram_share > 0.04
            or self.longest_identical_run > 1
            or self.back_to_back_repeats > 0
            or self.trigram_diversity < 0.25
        )


class ActionHistory:
    """Multi-window history, graded penalties, and n-gram memory.

    Bounded in every dimension. A 24/7 process cannot grow a list forever, and the
    windows the rules actually consult are small — the long tail is kept only as
    aggregate counts, not as entries.
    """

    __slots__ = (
        "_counts",
        "_entries",
        "_last_use",
        "_recent",
        "_rolling",
        "_trigrams",
    )

    def __init__(self) -> None:
        #: The most recent entries, deepest window the rules read.
        self._recent: deque[HistoryEntry] = deque(maxlen=WINDOW_LONG)
        #: Entries inside the share window, trimmed by time rather than by count.
        self._rolling: deque[HistoryEntry] = deque()
        #: Lifetime counts, for the simulation report. Aggregate only.
        self._counts: Counter[str] = Counter()
        self._entries = 0
        #: Last use time per action and per anti-repeat key.
        self._last_use: dict[str, float] = {}
        self._trigrams: deque[tuple[str, str, str]] = deque(maxlen=TRIGRAM_MEMORY)

    # -- recording ---------------------------------------------------------

    def record(self, entry: HistoryEntry) -> None:
        if len(self._recent) >= 2:
            self._trigrams.append(
                (self._recent[-2].action_id, self._recent[-1].action_id, entry.action_id)
            )
        self._recent.append(entry)
        self._rolling.append(entry)
        self._counts[entry.action_id] += 1
        self._entries += 1
        self._last_use[entry.action_id] = entry.started_monotonic
        self._last_use[f"key:{entry.anti_repeat_key}"] = entry.started_monotonic
        self._last_use[f"cat:{entry.category.value}"] = entry.started_monotonic

    def trim(self, now_monotonic: float) -> None:
        """Drop rolling entries older than the share window."""
        cutoff = now_monotonic - SHARE_WINDOW_SECONDS
        while self._rolling and self._rolling[0].started_monotonic < cutoff:
            self._rolling.popleft()

    # -- queries -----------------------------------------------------------

    @property
    def total_recorded(self) -> int:
        return self._entries

    @property
    def last(self) -> HistoryEntry | None:
        return self._recent[-1] if self._recent else None

    def window(self, size: int) -> tuple[HistoryEntry, ...]:
        if size <= 0:
            return ()
        return tuple(self._recent)[-size:]

    def positions_ago(self, action_id: str) -> int | None:
        """How many actions back this id last appeared, 1-based. ``None`` if not in window."""
        for offset, entry in enumerate(reversed(self._recent), start=1):
            if entry.action_id == action_id:
                return offset
        return None

    def key_positions_ago(self, key: str) -> int | None:
        for offset, entry in enumerate(reversed(self._recent), start=1):
            if entry.anti_repeat_key == key:
                return offset
        return None

    def seconds_since(self, action_id: str, now_monotonic: float) -> float | None:
        last = self._last_use.get(action_id)
        return None if last is None else now_monotonic - last

    def seconds_since_category(
        self, category: ActionCategory, now_monotonic: float
    ) -> float | None:
        last = self._last_use.get(f"cat:{category.value}")
        return None if last is None else now_monotonic - last

    def seconds_since_key(self, key: str, now_monotonic: float) -> float | None:
        last = self._last_use.get(f"key:{key}")
        return None if last is None else now_monotonic - last

    def category_share(self, category: ActionCategory) -> float:
        """This category's share of the rolling window, by count."""
        if not self._rolling:
            return 0.0
        hits = sum(1 for entry in self._rolling if entry.category is category)
        return hits / len(self._rolling)

    def action_share(self, action_id: str) -> float:
        if not self._rolling:
            return 0.0
        hits = sum(1 for entry in self._rolling if entry.action_id == action_id)
        return hits / len(self._rolling)

    def would_repeat_trigram(self, action_id: str) -> bool:
        """Whether choosing this would complete a triple already seen recently."""
        if len(self._recent) < 2:
            return False
        candidate = (self._recent[-2].action_id, self._recent[-1].action_id, action_id)
        return candidate in self._trigrams

    # -- the penalty -------------------------------------------------------

    def penalty(self, action_id: str) -> tuple[float, dict[str, float]]:
        """Graded repetition penalty, with its components.

        Returns a multiplier in (0, 1] and the factors that produced it, so the debug
        overlay and the simulation report can explain a choice rather than assert it.

        **Not a veto.** The one hard exclusion — the same action twice in a row — is a
        :class:`~tradefix_radio.director.selection.Constraint` in the director, where it
        belongs. Everything here is graded, which is what lets a coffee sip become
        likely again after half an hour.
        """
        action = spec(action_id)
        factors: dict[str, float] = {}

        own = self.positions_ago(action_id)
        if own is not None:
            if own <= WINDOW_SHORT:
                factors["recency_short"] = RECENCY_SHORT
            elif own <= WINDOW_MEDIUM:
                factors["recency_medium"] = RECENCY_MEDIUM
            else:
                factors["recency_long"] = RECENCY_LONG

        key_ago = self.key_positions_ago(action.anti_repeat_key)
        if key_ago is not None and key_ago <= WINDOW_SHORT:
            # A different action from the same family, very recently. Suppressed harder
            # than plain recency because families are what read as repetition.
            factors["key_recency"] = 0.22

        if self.would_repeat_trigram(action_id):
            factors["trigram"] = TRIGRAM_PENALTY

        saturated = len(self._rolling) >= SHARE_MIN_SAMPLE
        ceiling = CATEGORY_HOUR_SHARE_CEILING.get(action.category) if saturated else None
        if ceiling:
            share = self.category_share(action.category)
            if share >= ceiling:
                factors["category_ceiling"] = 0.0
            elif share > ceiling * 0.7:
                # Taper into the ceiling rather than hitting a wall, so behaviour
                # thins out smoothly instead of a family vanishing mid-hour.
                headroom = (ceiling - share) / (ceiling * 0.3)
                factors["category_taper"] = max(0.05, headroom)

        if saturated and action.max_hour_share is not None:
            share = self.action_share(action_id)
            if share >= action.max_hour_share:
                factors["action_ceiling"] = 0.0
            elif share > action.max_hour_share * 0.7:
                headroom = (action.max_hour_share - share) / (action.max_hour_share * 0.3)
                factors["action_taper"] = max(0.05, headroom)

        multiplier = 1.0
        for value in factors.values():
            multiplier *= value
        return multiplier, factors

    # -- analysis ----------------------------------------------------------

    def analyse(self, trigrams: Sequence[tuple[str, str, str]] | None = None) -> RepetitionReport:
        """Summarise repetition. Fed the full trigram log by the simulation harness.

        The in-memory deque is bounded to :data:`TRIGRAM_MEMORY` because that is all the
        *penalty* needs. A 24-hour report needs every triple, so the harness keeps its
        own unbounded log and passes it here — keeping the runtime bounded and the
        analysis complete, instead of compromising one for the other.
        """
        source = list(trigrams) if trigrams is not None else list(self._trigrams)
        counts = Counter(source)
        top = counts.most_common(1)[0] if counts else None
        total = len(source)
        return RepetitionReport(
            total_actions=self._entries,
            distinct_actions=len(self._counts),
            action_shares={
                action: count / max(1, self._entries)
                for action, count in self._counts.most_common()
            },
            top_trigram=top,
            top_trigram_share=(top[1] / total) if top and total else 0.0,
            trigram_diversity=(len(counts) / total) if total else 0.0,
        )


__all__ = [
    "RECENCY_LONG",
    "RECENCY_MEDIUM",
    "RECENCY_SHORT",
    "SHARE_MIN_SAMPLE",
    "SHARE_WINDOW_SECONDS",
    "TRIGRAM_MEMORY",
    "TRIGRAM_PENALTY",
    "WINDOW_LONG",
    "WINDOW_MEDIUM",
    "WINDOW_SHORT",
    "ActionHistory",
    "CooldownTable",
    "HistoryEntry",
    "LockConflict",
    "LockTable",
    "RepetitionReport",
]
