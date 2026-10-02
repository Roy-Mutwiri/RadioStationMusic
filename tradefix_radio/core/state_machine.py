"""Explicit track/generation state machine (§92, §27).

§92 demands that illegal transitions "raise controlled errors" and that every
transition be tested. That is only checkable if the legal set is *data*, not
scattered ``if`` statements — so the whole machine is one table, and the test
suite iterates the full state product to assert the complement is rejected.

Why one machine for both §27 (queue states) and §92 (generation states): they are
the same lifecycle viewed from two angles. §27 lists ``MASTERING`` and
``QUEUED``; §92 lists ``GENERATED`` and ``APPROVED``. Modelling them separately
would mean two sources of truth about where a track is, and inevitable drift.
The union is used, with §27's names where they overlap.

Terminal states deliberately differ in meaning:

* ``PLAYED``      — aired successfully; audio bytes may later be reclaimed (§36).
* ``FAILED``      — generation/mastering broke; retryable by *replanning*, not by
                    resurrecting this record.
* ``REJECTED``    — failed QC or duplication policy; must never reach the queue
                    (§22). Kept forever as evidence for the §48 page.
* ``QUARANTINED`` — output is suspect in a way that needs human eyes (e.g.
                    repeated unexplained corruption). Distinct from REJECTED
                    because it signals a *system* problem, not a bad roll.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tradefix_radio.core.errors import IllegalTransitionError


class TrackState(str, enum.Enum):
    """Lifecycle of a single track, from intent to air."""

    PLANNED = "planned"
    GENERATING = "generating"
    GENERATED = "generated"
    ANALYZING = "analyzing"
    APPROVED = "approved"
    MASTERING = "mastering"
    READY = "ready"
    QUEUED = "queued"
    PLAYING = "playing"
    PLAYED = "played"

    FAILED = "failed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    CANCELLED = "cancelled"


#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[TrackState] = frozenset(
    {
        TrackState.PLAYED,
        TrackState.REJECTED,
        TrackState.QUARANTINED,
        TrackState.CANCELLED,
    }
)

#: States in which a track occupies GPU/worker capacity and holds a job lease.
ACTIVE_WORK_STATES: frozenset[TrackState] = frozenset(
    {
        TrackState.GENERATING,
        TrackState.ANALYZING,
        TrackState.MASTERING,
    }
)

#: States in which a track is safe to broadcast.
PLAYABLE_STATES: frozenset[TrackState] = frozenset(
    {TrackState.READY, TrackState.QUEUED, TrackState.PLAYING}
)

# ---------------------------------------------------------------------------
# The single source of truth. Everything else in this module derives from it.
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[TrackState, frozenset[TrackState]] = {
    # Planned work can start, be abandoned during replanning (§28), or be
    # cancelled outright by an operator.
    TrackState.PLANNED: frozenset(
        {TrackState.GENERATING, TrackState.CANCELLED, TrackState.FAILED}
    ),
    # Generation may succeed, break, time out, or be cancelled mid-flight (§19).
    TrackState.GENERATING: frozenset(
        {TrackState.GENERATED, TrackState.FAILED, TrackState.CANCELLED}
    ),
    # Raw output exists; QC and fingerprinting come next (§24, §21).
    TrackState.GENERATED: frozenset(
        {TrackState.ANALYZING, TrackState.FAILED, TrackState.CANCELLED}
    ),
    # Analysis decides: approved, rejected (QC or duplication), or the file is so
    # broken it is a system signal rather than a bad roll.
    TrackState.ANALYZING: frozenset(
        {
            TrackState.APPROVED,
            TrackState.REJECTED,
            TrackState.QUARANTINED,
            TrackState.FAILED,
            TrackState.CANCELLED,
        }
    ),
    TrackState.APPROVED: frozenset(
        {TrackState.MASTERING, TrackState.FAILED, TrackState.CANCELLED}
    ),
    # Mastering can reveal fatal problems the raw analysis missed (e.g. the file
    # will not decode), hence REJECTED is reachable here too.
    TrackState.MASTERING: frozenset(
        {
            TrackState.READY,
            TrackState.FAILED,
            TrackState.REJECTED,
            TrackState.CANCELLED,
        }
    ),
    # READY tracks live in the vault. They may be queued, or cancelled during
    # replanning. FAILED is reachable because the master file can vanish from
    # disk (retention bug, external deletion) before it is ever queued.
    TrackState.READY: frozenset(
        {TrackState.QUEUED, TrackState.CANCELLED, TrackState.FAILED}
    ),
    # A queued track can be pulled back to READY when the scheduler replans an
    # unlocked slot — this is what keeps §28 from having to destroy tracks.
    TrackState.QUEUED: frozenset(
        {
            TrackState.PLAYING,
            TrackState.READY,
            TrackState.CANCELLED,
            TrackState.FAILED,
        }
    ),
    # A playing track normally completes. FAILED covers a decode error or sink
    # death mid-air. It must NOT be able to reach PLAYED in that case — §75
    # forbids marking an incomplete track as successfully played.
    TrackState.PLAYING: frozenset({TrackState.PLAYED, TrackState.FAILED}),
    # Terminal.
    TrackState.PLAYED: frozenset(),
    TrackState.REJECTED: frozenset(),
    TrackState.QUARANTINED: frozenset(),
    TrackState.CANCELLED: frozenset(),
    # FAILED is the one non-terminal failure state: a failed *planned* track may
    # be retried, which re-enters GENERATING. Retry budget is enforced by the
    # generation manager (§19 retry policy), not by the state machine, because
    # the machine must stay a pure predicate.
    TrackState.FAILED: frozenset({TrackState.GENERATING, TrackState.CANCELLED}),
}


@dataclass(frozen=True)
class StateTransition:
    """A recorded transition. Persisted so §27's audit trail is complete."""

    from_state: TrackState | None
    to_state: TrackState
    at: datetime
    reason: str
    detail: dict[str, Any]


def allowed_transitions(state: TrackState) -> frozenset[TrackState]:
    """States reachable from ``state`` in one step."""
    return _TRANSITIONS[state]


def can_transition(from_state: TrackState, to_state: TrackState) -> bool:
    """Whether ``from_state -> to_state`` is legal."""
    return to_state in _TRANSITIONS[from_state]


def is_terminal(state: TrackState) -> bool:
    return state in TERMINAL_STATES


def assert_transition(from_state: TrackState, to_state: TrackState) -> None:
    """Raise :class:`IllegalTransitionError` unless the transition is legal."""
    if not can_transition(from_state, to_state):
        raise IllegalTransitionError(
            from_state.value,
            to_state.value,
            allowed=sorted(s.value for s in _TRANSITIONS[from_state]),
        )


class TrackLifecycle:
    """Stateful guard around a single track's progression.

    Holds the transition history so the §27 requirement to "persist all state
    transitions" has a concrete in-memory source; the repository layer flushes
    :attr:`history` to the database.
    """

    __slots__ = ("_history", "_state", "_track_id")

    def __init__(
        self,
        track_id: str,
        state: TrackState = TrackState.PLANNED,
        history: list[StateTransition] | None = None,
    ) -> None:
        self._track_id = track_id
        self._state = state
        self._history: list[StateTransition] = list(history or [])

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def state(self) -> TrackState:
        return self._state

    @property
    def history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self._state)

    @property
    def is_playable(self) -> bool:
        return self._state in PLAYABLE_STATES

    def transition(
        self,
        to_state: TrackState,
        *,
        at: datetime,
        reason: str,
        **detail: Any,
    ) -> StateTransition:
        """Move to ``to_state``, recording why.

        ``reason`` is mandatory and free-text-but-short (e.g.
        ``"qc_failed"``, ``"operator_skip"``, ``"replan"``). Requiring it means
        the §48 "show why a candidate was rejected" page always has an answer,
        and post-mortems on a week-long run are possible at all.
        """
        assert_transition(self._state, to_state)
        record = StateTransition(
            from_state=self._state,
            to_state=to_state,
            at=at,
            reason=reason,
            detail=dict(detail),
        )
        self._state = to_state
        self._history.append(record)
        return record

    def __repr__(self) -> str:
        return f"TrackLifecycle(track_id={self._track_id!r}, state={self._state.value!r})"


__all__ = [
    "ACTIVE_WORK_STATES",
    "PLAYABLE_STATES",
    "TERMINAL_STATES",
    "StateTransition",
    "TrackLifecycle",
    "TrackState",
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "is_terminal",
]
