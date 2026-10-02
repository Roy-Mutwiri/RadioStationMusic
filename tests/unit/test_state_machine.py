"""Generation state machine (§92, milestone 1.7).

§92 requires that every transition be tested. "Every" is taken literally: the
suite iterates the **full Cartesian product** of states and asserts that exactly
the declared set is accepted and the entire complement is rejected. That is the
only form of this test that cannot rot — adding a state or an edge immediately
shows up as a diff in a count, not as a silently untested path.
"""

from __future__ import annotations

import itertools

import pytest

from tradefix_radio.core.errors import IllegalTransitionError
from tradefix_radio.core.state_machine import (
    ACTIVE_WORK_STATES,
    PLAYABLE_STATES,
    TERMINAL_STATES,
    TrackLifecycle,
    TrackState,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_terminal,
)
from tests.conftest import FIXED_NOW

ALL_STATES = list(TrackState)

#: Every legal edge, derived from the module's own table so the test and the
#: implementation cannot drift.
LEGAL_EDGES = {
    (source, target) for source in ALL_STATES for target in allowed_transitions(source)
}


def test_every_state_has_a_transition_entry() -> None:
    """A state missing from the table would raise KeyError at runtime."""
    for state in ALL_STATES:
        assert isinstance(allowed_transitions(state), frozenset)


@pytest.mark.parametrize(("source", "target"), sorted(LEGAL_EDGES, key=lambda e: (e[0].value, e[1].value)))
def test_legal_transitions_are_accepted(source: TrackState, target: TrackState) -> None:
    assert can_transition(source, target)
    assert_transition(source, target)  # must not raise


@pytest.mark.parametrize(
    ("source", "target"),
    sorted(
        {pair for pair in itertools.product(ALL_STATES, ALL_STATES) if pair not in LEGAL_EDGES},
        key=lambda e: (e[0].value, e[1].value),
    ),
)
def test_illegal_transitions_are_rejected(source: TrackState, target: TrackState) -> None:
    """The complement of the legal set must raise a controlled error (§92)."""
    assert not can_transition(source, target)
    with pytest.raises(IllegalTransitionError) as caught:
        assert_transition(source, target)
    error = caught.value
    assert error.from_state == source.value
    assert error.to_state == target.value
    # The error must say what *was* allowed; an error that only says "no" forces
    # the reader into the source.
    assert "allowed" in error.context


def test_no_state_transitions_to_itself() -> None:
    """Self-transitions would make "state changed" meaningless in the audit trail."""
    for state in ALL_STATES:
        assert state not in allowed_transitions(state), f"{state.value} -> itself is legal"


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset()
        assert is_terminal(state)


def test_failed_is_not_terminal_but_is_recoverable() -> None:
    """FAILED must be retryable; everything else that stops is terminal.

    This asymmetry is deliberate and easy to break: if FAILED became terminal, a
    transient provider error would permanently burn a queue slot.
    """
    assert not is_terminal(TrackState.FAILED)
    assert TrackState.GENERATING in allowed_transitions(TrackState.FAILED)


def test_playing_cannot_reach_played_without_completing() -> None:
    """§75: an interrupted airing must not be recorded as played.

    PLAYING -> FAILED must exist, and FAILED must not then reach PLAYED, or an
    interrupted track could be laundered into a successful play.
    """
    assert TrackState.FAILED in allowed_transitions(TrackState.PLAYING)
    assert TrackState.PLAYED not in allowed_transitions(TrackState.FAILED)


def test_rejected_can_never_reach_a_playable_state() -> None:
    """§22: "Never place a rejected track into radio queue."

    Verified as a reachability property over the whole graph rather than a single
    edge check, so no future edge can open a back door.
    """
    reachable: set[TrackState] = set()
    frontier = [TrackState.REJECTED]
    while frontier:
        current = frontier.pop()
        for nxt in allowed_transitions(current):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert not (reachable & PLAYABLE_STATES), (
        f"rejected tracks can reach playable states via {reachable & PLAYABLE_STATES}"
    )


def test_quarantined_can_never_reach_a_playable_state() -> None:
    reachable: set[TrackState] = set()
    frontier = [TrackState.QUARANTINED]
    while frontier:
        current = frontier.pop()
        for nxt in allowed_transitions(current):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert not (reachable & PLAYABLE_STATES)


def test_every_non_terminal_state_can_reach_a_terminal_state() -> None:
    """No state may be a dead end that is neither terminal nor progressing.

    A state with outgoing edges that can never terminate would leak queue slots
    forever — exactly the kind of fault that only shows up on day six of a run.
    """
    for start in ALL_STATES:
        if is_terminal(start):
            continue
        seen = {start}
        frontier = [start]
        found_terminal = False
        while frontier and not found_terminal:
            current = frontier.pop()
            for nxt in allowed_transitions(current):
                if is_terminal(nxt):
                    found_terminal = True
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert found_terminal, f"{start.value} cannot reach any terminal state"


def test_happy_path_is_the_sequence_the_brief_specifies() -> None:
    """The §92 diagram, walked end to end."""
    sequence = [
        TrackState.GENERATING,
        TrackState.GENERATED,
        TrackState.ANALYZING,
        TrackState.APPROVED,
        TrackState.MASTERING,
        TrackState.READY,
        TrackState.QUEUED,
        TrackState.PLAYING,
        TrackState.PLAYED,
    ]
    lifecycle = TrackLifecycle("TF-20261002-00001")
    assert lifecycle.state is TrackState.PLANNED
    for target in sequence:
        lifecycle.transition(target, at=FIXED_NOW, reason="test")
    assert lifecycle.state is TrackState.PLAYED
    assert lifecycle.is_terminal
    assert len(lifecycle.history) == len(sequence)


def test_lifecycle_records_reason_and_detail() -> None:
    """§48 needs a stored reason for every rejection; mandatory ``reason`` provides it."""
    lifecycle = TrackLifecycle("TF-20261002-00002")
    lifecycle.transition(TrackState.GENERATING, at=FIXED_NOW, reason="scheduler_claim")
    record = lifecycle.transition(
        TrackState.FAILED, at=FIXED_NOW, reason="provider_timeout", elapsed=301.5
    )
    assert record.from_state is TrackState.GENERATING
    assert record.to_state is TrackState.FAILED
    assert record.reason == "provider_timeout"
    assert record.detail == {"elapsed": 301.5}


def test_lifecycle_rejects_illegal_transition_and_preserves_state() -> None:
    """A rejected transition must leave the object untouched.

    If a failed transition mutated state, a caught IllegalTransitionError would
    leave the track in an inconsistent place — worse than the original mistake.
    """
    lifecycle = TrackLifecycle("TF-20261002-00003", state=TrackState.READY)
    with pytest.raises(IllegalTransitionError):
        lifecycle.transition(TrackState.PLAYED, at=FIXED_NOW, reason="bogus")
    assert lifecycle.state is TrackState.READY
    assert lifecycle.history == ()


def test_playable_and_active_sets_are_disjoint() -> None:
    """A track cannot simultaneously be airable and occupying a worker."""
    assert not (PLAYABLE_STATES & ACTIVE_WORK_STATES)


def test_terminal_states_are_not_playable() -> None:
    assert not (TERMINAL_STATES & PLAYABLE_STATES)


def test_queued_can_return_to_ready_for_replanning() -> None:
    """§28 replanning must be able to release a slot without destroying the track."""
    assert TrackState.READY in allowed_transitions(TrackState.QUEUED)
