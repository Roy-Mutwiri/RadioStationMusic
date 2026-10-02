"""Constrained weighted selection (§9).

§9 is blunt: "Do NOT select songs randomly with simple ``random.choice()``. Use
constrained weighted selection."

The distinction matters because `random.choice` over a genre list produces, over a
day, a roughly uniform distribution that ignores the market entirely — which is the
"repeatedly generates random music" the brief opens by ruling out. And weights alone
are not enough either: weighted sampling will happily pick the same genre four times
in a row, so §11's anti-boredom rules have to be expressible as **hard constraints**
rather than as further weights.

This module therefore separates three things that are easy to conflate:

``Candidate``   an option with a weight and a record of *why* it has that weight
``Constraint``  a hard veto with a stated reason — §11's rules live here
``select``      weighted sampling over the survivors, with a documented fallback

The fallback is the part most worth getting right. If every candidate is vetoed, the
station must still choose something: silence is worse than a repeated genre. So
constraints are relaxed in a defined order — soft ones first — and the relaxation is
*reported*, so §48-style explainability holds and the §11 diversity score can record
that a rule had to be broken.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

#: Weight below which a candidate is treated as effectively impossible. Not zero,
#: because floating-point products of several small factors legitimately underflow
#: toward it without being a deliberate exclusion.
NEGLIGIBLE_WEIGHT = 1e-9


@dataclass
class Candidate(Generic[T]):
    """One option, its weight, and the reasoning behind it.

    ``factors`` is not debug decoration: §48 requires the station to be able to
    explain a decision, the §47 Market Lab displays it, and tuning a weighted system
    blind is how programming quietly collapses.
    """

    value: T
    weight: float = 1.0
    factors: dict[str, float] = field(default_factory=dict)
    vetoes: list[str] = field(default_factory=list)

    def multiply(self, name: str, factor: float) -> Candidate[T]:
        """Apply a named multiplicative factor, recording it."""
        if factor < 0:
            raise ValueError(f"factor {name!r} is negative ({factor})")
        self.factors[name] = factor
        self.weight *= factor
        return self

    def veto(self, reason: str) -> Candidate[T]:
        """Hard-exclude this candidate, recording why."""
        self.vetoes.append(reason)
        return self

    @property
    def is_vetoed(self) -> bool:
        return bool(self.vetoes)

    @property
    def is_viable(self) -> bool:
        return not self.is_vetoed and self.weight > NEGLIGIBLE_WEIGHT

    def explain(self) -> str:
        """One-line account of how this candidate's weight was reached."""
        if self.is_vetoed:
            return f"{self.value}: vetoed ({'; '.join(self.vetoes)})"
        parts = ", ".join(f"{name}={value:.3g}" for name, value in sorted(self.factors.items()))
        return f"{self.value}: weight={self.weight:.4g} [{parts}]"


@dataclass(frozen=True)
class Constraint(Generic[T]):
    """A hard rule that vetoes candidates.

    ``severity`` orders relaxation when every candidate has been vetoed. Lower is
    relaxed first, so the rules the brief treats as inviolable — "same blueprint: never
    repeat" — are the last to give way.
    """

    name: str
    predicate: Callable[[T], bool]
    reason: str
    severity: int = 1

    def permits(self, value: T) -> bool:
        return self.predicate(value)


@dataclass
class SelectionResult(Generic[T]):
    """What was chosen, and the full reasoning."""

    chosen: T
    candidates: list[Candidate[T]]
    #: Constraints that had to be relaxed because nothing survived them.
    relaxed: list[str] = field(default_factory=list)
    #: ``True`` only when the relaxation ladder was exhausted and the highest raw weight
    #: had to be taken.
    #:
    #: Rarer than it looks. Relaxing a constraint always rescues the candidates that
    #: constraint blocked, so the ladder normally succeeds after dropping one rule. The
    #: only way to exhaust it is for every candidate's weight to be negligible — which
    #: means the *weighting* has gone wrong, not that the rules conflicted. Treat a
    #: forced selection as a signal to look at the factors, not at the constraints.
    was_forced: bool = False

    @property
    def viable_count(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.is_viable)

    def explain(self, limit: int = 8) -> str:
        """Human-readable account, best candidates first."""
        ranked = sorted(
            self.candidates, key=lambda c: (c.is_vetoed, -c.weight)
        )
        lines = [f"chose {self.chosen!r} from {self.viable_count} viable candidates"]
        if self.relaxed:
            lines.append(f"  relaxed constraints: {', '.join(self.relaxed)}")
        if self.was_forced:
            lines.append("  NOTE: selection was forced; no candidate satisfied every rule")
        lines.extend(f"  {candidate.explain()}" for candidate in ranked[:limit])
        return "\n".join(lines)


class WeightedSelector:
    """Constrained weighted selection with a defined relaxation order.

    The RNG is injected, so a seeded selector is fully reproducible. §3.12's
    statistical report depends on that, and so does reproducing an endurance finding.
    The global RNG is never touched.

    Deliberately **not** generic at the class level. One selector chooses genres, BPMs,
    keys, personas and topics, so binding the type variable to the instance would make
    every call ``WeightedSelector[Any]`` and silently erase the element type from every
    return value. The type variable belongs to the individual methods.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()  # noqa: S311 - creative choice, not security

    @property
    def rng(self) -> random.Random:
        return self._rng

    def select(
        self,
        candidates: Sequence[Candidate[T]],
        constraints: Iterable[Constraint[T]] = (),
        *,
        temperature: float = 1.0,
    ) -> SelectionResult[T]:
        """Choose one candidate.

        Parameters
        ----------
        candidates:
            Options with weights already applied. Must not be empty.
        constraints:
            Hard rules. Applied in order; each veto is recorded on the candidate.
        temperature:
            §95 creative temperature. 1.0 samples the weights as given. Below 1.0
            sharpens toward the strongest candidate (conservative); above 1.0 flattens
            toward uniform (adventurous). Implemented as ``weight ** (1/temperature)``,
            which is the standard softmax-temperature shape and keeps the ordering
            of weights intact while changing how decisively the best one wins.

        Raises
        ------
        ValueError
            If ``candidates`` is empty. A caller with nothing to choose from has a bug
            upstream, and silently inventing an option would hide it.
        """
        if not candidates:
            raise ValueError("cannot select from an empty candidate list")
        if temperature <= 0:
            raise ValueError("temperature must be positive")

        ordered_constraints = sorted(constraints, key=lambda c: c.severity)
        for constraint in ordered_constraints:
            for candidate in candidates:
                if not constraint.permits(candidate.value):
                    candidate.veto(constraint.reason)

        viable = [candidate for candidate in candidates if candidate.is_viable]
        relaxed: list[str] = []

        # Relaxation ladder. Each pass drops the least severe remaining constraint and
        # re-tests, rather than abandoning all rules at once — breaking one rule is
        # much better than broadcasting with none.
        if not viable:
            for constraint in ordered_constraints:
                relaxed.append(constraint.name)
                survivors = [
                    candidate
                    for candidate in candidates
                    if candidate.weight > NEGLIGIBLE_WEIGHT
                    and all(
                        other.permits(candidate.value)
                        for other in ordered_constraints
                        if other.name not in relaxed
                    )
                ]
                if survivors:
                    viable = survivors
                    break

        was_forced = False
        if not viable:
            # Every constraint relaxed and still nothing, or every weight negligible.
            # Fall back to the highest raw weight: silence is worse than a repeat.
            was_forced = True
            relaxed = [constraint.name for constraint in ordered_constraints]
            viable = [max(candidates, key=lambda c: c.weight)]

        chosen = self._sample(viable, temperature)
        return SelectionResult(
            chosen=chosen,
            candidates=list(candidates),
            relaxed=relaxed,
            was_forced=was_forced,
        )

    def _sample(self, viable: Sequence[Candidate[T]], temperature: float) -> T:
        """Weighted sample with temperature applied."""
        if len(viable) == 1:
            return viable[0].value
        exponent = 1.0 / temperature
        weights = [max(NEGLIGIBLE_WEIGHT, candidate.weight) ** exponent for candidate in viable]
        total = math.fsum(weights)
        if total <= 0 or not math.isfinite(total):
            # Degenerate after exponentiation (every weight underflowed). Uniform is
            # the honest answer; biasing toward index 0 would quietly skew programming.
            return self._rng.choice([candidate.value for candidate in viable])
        target = self._rng.random() * total
        cumulative = 0.0
        for candidate, weight in zip(viable, weights, strict=True):
            cumulative += weight
            if cumulative >= target:
                return candidate.value
        return viable[-1].value

    def choose_from(
        self,
        values: Sequence[T],
        *,
        weight_of: Callable[[T], float] | None = None,
        temperature: float = 1.0,
    ) -> T:
        """Convenience path for a simple weighted pick with no constraints."""
        if not values:
            raise ValueError("cannot choose from an empty sequence")
        candidates = [
            Candidate(value=value, weight=1.0 if weight_of is None else weight_of(value))
            for value in values
        ]
        return self.select(candidates, temperature=temperature).chosen

    def shuffled(self, values: Sequence[T]) -> list[T]:
        """A shuffled copy, using the injected RNG."""
        out = list(values)
        self._rng.shuffle(out)
        return out


def band_fit(
    value: float,
    low: float,
    high: float,
    *,
    falloff: float = 25.0,
    floor: float = 0.02,
) -> float:
    """How well ``value`` fits the band ``[low, high]``, as a 0–1 multiplier.

    **This is the function through which §1's energy mapping emerges.** Each genre
    declares the energy band it belongs to; the director scores every genre by how
    well that band fits the current market energy. Lo-fi wins in a dead market because
    its band is 0–38, not because any rule says "if quiet then lofi".

    Full weight inside the band, then a smooth decay outside over ``falloff`` units.
    Decay rather than a hard cut-off for two reasons: a genre just outside the band is
    a *less good* choice, not an impossible one, and a hard edge would make programming
    jump discontinuously as energy crossed a boundary — audible as an abrupt genre
    switch for a one-point energy move.

    ``floor`` keeps a far-away genre at a small non-zero weight so it remains available
    when constraints have vetoed everything closer. It must never reach 0, or the
    relaxation ladder in :meth:`WeightedSelector.select` would have nothing to fall
    back to.
    """
    if falloff <= 0:
        raise ValueError("falloff must be positive")
    if low > high:
        raise ValueError(f"band low ({low}) exceeds high ({high})")
    if low <= value <= high:
        return 1.0
    distance = low - value if value < low else value - high
    # Gaussian-shaped decay: smooth, and reaches the floor at roughly 2x falloff.
    decayed = math.exp(-0.5 * (distance / falloff) ** 2)
    return max(floor, decayed)


def recency_penalty(
    positions_ago: int | None, *, horizon: int, strength: float = 0.85
) -> float:
    """Multiplier that discourages something used recently (§11, §12).

    ``positions_ago`` is how many tracks back the item last appeared; ``None`` means
    never. Returns 1.0 for anything outside ``horizon``, and a penalty rising toward
    ``strength`` the more recent the use.

    A graded penalty rather than a veto, because the §11 rules that *are* absolute
    ("same genre maximum 2 consecutive") are expressed as
    :class:`Constraint` objects. This handles the softer "prefer something we have not
    played lately" preference, which is what keeps a 100-track stretch from feeling
    repetitive even where no hard rule is broken.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if not 0.0 <= strength < 1.0:
        raise ValueError("strength must be within [0, 1)")
    if positions_ago is None or positions_ago >= horizon:
        return 1.0
    closeness = (horizon - positions_ago) / horizon
    return max(1.0 - strength, 1.0 - strength * closeness)


__all__ = [
    "NEGLIGIBLE_WEIGHT",
    "Candidate",
    "Constraint",
    "SelectionResult",
    "WeightedSelector",
    "band_fit",
    "recency_penalty",
]
