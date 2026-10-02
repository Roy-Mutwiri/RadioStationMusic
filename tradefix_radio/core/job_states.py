"""Generation job lifecycle (§70, milestone 4.2).

A **job** is a unit of generation work. A **track** is a thing that can air. They have
separate lifecycles on purpose, and conflating them is the mistake this module exists to
prevent: one track can own several jobs over its life (a timeout, a retry, a success), and a
job's failure is not the same event as a track's failure.

``TrackState`` already models the track side (:mod:`tradefix_radio.core.state_machine`).
This is the job side.

The states, and what each one *means operationally*:

``PLANNED``        the blueprint exists; nothing has been asked of a provider yet.
``QUEUED``         eligible for a worker to claim.
``LEASED``         a worker has claimed it and written its identity and an expiry (§70).
``GENERATING``     the provider call is in flight.
``GENERATED``      audio exists on disk. Terminal for the job; the *track* continues.
``RETRY_PENDING``  failed, attempts remain, waiting out its backoff.
``FAILED``         failed and out of attempts. Terminal.
``CANCELLED``      abandoned deliberately — §28's replan, or an operator. Terminal.
``ABANDONED``      its lease expired and nobody reclaimed it in time. Terminal.

``RETRY_PENDING`` is the state that makes exponential backoff expressible. Without it, a job
waiting out a backoff is indistinguishable from one eligible to run, so either every worker
picks it up immediately (no backoff) or the backoff has to live in a worker's memory — where
a restart loses it, and §70's whole point is surviving restarts.

``ABANDONED`` is distinct from ``FAILED`` because the causes are different and the operator
response differs: ``FAILED`` means the provider could not do the work, ``ABANDONED`` means a
worker disappeared. A §57 alert on a rising ``ABANDONED`` count is about worker health; one on
``FAILED`` is about the model.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from tradefix_radio.core.errors import IllegalTransitionError


class JobState(str, enum.Enum):
    """Where a generation job is in its life (§70)."""

    PLANNED = "planned"
    QUEUED = "queued"
    LEASED = "leased"
    GENERATING = "generating"
    GENERATED = "generated"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class JobFailureKind(str, enum.Enum):
    """Structured failure classification (§70).

    A string message is useless for deciding what to do next. These categories answer the
    only question the manager actually has: **is retrying this worth a slot?**
    """

    TIMEOUT = "timeout"
    """Exceeded its deadline. Retryable — the next attempt may be luckier, and §20's ladder
    can reduce the request."""

    PROVIDER_ERROR = "provider_error"
    """The provider raised. Retryable; most model failures are transient."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The provider is unreachable. Retryable, and the backoff matters more than usual
    because hammering a dead endpoint delays recovery."""

    RESOURCE_EXHAUSTED = "resource_exhausted"
    """Out of VRAM or disk (§20, §36). Retryable **only after** something changes, so the
    manager backs off hard rather than immediately."""

    QUALITY_REJECTED = "quality_rejected"
    """Audio was produced and failed §24 QC. Retryable: a different seed may pass."""

    INVALID_REQUEST = "invalid_request"
    """The blueprint itself cannot be realised. **Not** retryable — the same request will
    fail identically, and retrying it burns the attempt budget that a transient failure
    needs."""

    CANCELLED = "cancelled"
    """Not a failure. Recorded so the §101 report can tell a cancelled job from a broken
    one, which a bare count of non-successes cannot."""

    LEASE_EXPIRED = "lease_expired"
    """Nobody finished the work before the lease ran out. Retryable."""

    UNKNOWN = "unknown"
    """An exception the manager did not recognise. Retryable, and logged loudly — an
    unrecognised failure kind means this table is missing a case."""

    @property
    def is_retryable(self) -> bool:
        return self not in {JobFailureKind.INVALID_REQUEST, JobFailureKind.CANCELLED}

    @property
    def needs_long_backoff(self) -> bool:
        """Whether an immediate retry would make things worse rather than better."""
        return self in {
            JobFailureKind.PROVIDER_UNAVAILABLE,
            JobFailureKind.RESOURCE_EXHAUSTED,
        }


#: Job states from which nothing further happens.
TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {
        JobState.GENERATED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ABANDONED,
    }
)

#: States in which a worker is believed to hold the job.
#:
#: Both are checked when reclaiming expired leases: a worker can die between taking the lease
#: and starting the provider call, and a reclaimer that only looked at ``GENERATING`` would
#: leave those jobs stuck in ``LEASED`` forever — which is exactly the failure §70 forbids.
LEASED_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.LEASED, JobState.GENERATING}
)

#: States a worker may claim from.
CLAIMABLE_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.QUEUED, JobState.RETRY_PENDING}
)


_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    # A planned job is queued when the manager admits it, or cancelled if §28 replans the
    # slot before any work started.
    JobState.PLANNED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.QUEUED: frozenset({JobState.LEASED, JobState.CANCELLED}),
    # A leased job may start, be cancelled, lose its lease, or fail outright — a worker can
    # die between the lease and the call.
    JobState.LEASED: frozenset(
        {
            JobState.GENERATING,
            JobState.CANCELLED,
            JobState.ABANDONED,
            JobState.RETRY_PENDING,
            JobState.FAILED,
        }
    ),
    JobState.GENERATING: frozenset(
        {
            JobState.GENERATED,
            JobState.RETRY_PENDING,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.ABANDONED,
        }
    ),
    # Waiting out a backoff. Back to QUEUED when the delay elapses.
    JobState.RETRY_PENDING: frozenset(
        {JobState.QUEUED, JobState.CANCELLED, JobState.FAILED, JobState.ABANDONED}
    ),
    # Terminal.
    JobState.GENERATED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    # An abandoned job is terminal *as a job*. Recovery creates a fresh job for the same
    # track rather than resurrecting this one, so the audit trail keeps both attempts and the
    # §101 report can count the abandonment.
    JobState.ABANDONED: frozenset(),
}


@dataclass(frozen=True)
class JobTransition:
    """One edge, for logging and for the audit trail."""

    from_state: JobState | None
    to_state: JobState
    reason: str


def allowed_job_transitions(state: JobState) -> frozenset[JobState]:
    return _TRANSITIONS[state]


def can_transition_job(from_state: JobState, to_state: JobState) -> bool:
    return to_state in _TRANSITIONS[from_state]


def is_terminal_job(state: JobState) -> bool:
    return state in TERMINAL_JOB_STATES


def assert_job_transition(from_state: JobState, to_state: JobState) -> None:
    """Raise unless the edge exists.

    Checked rather than trusted because the two most dangerous bugs in this subsystem are
    silent: a job that completes twice, and a job that leaves a terminal state. Both are
    impossible if every change goes through here.
    """
    if not can_transition_job(from_state, to_state):
        allowed = sorted(s.value for s in _TRANSITIONS[from_state])
        raise IllegalTransitionError(
            from_state=from_state.value,
            to_state=to_state.value,
            subject="generation_job",
            allowed=allowed or ["none (terminal)"],
        )


__all__ = [
    "CLAIMABLE_JOB_STATES",
    "LEASED_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "JobFailureKind",
    "JobState",
    "JobTransition",
    "allowed_job_transitions",
    "assert_job_transition",
    "can_transition_job",
    "is_terminal_job",
]
