"""Market regime classification with hysteresis (§5).

§5's central requirement is negative: the engine "should not switch RANGE →
BREAKOUT → RANGE → BREAKOUT every few seconds because one candle moved." That is a
product requirement, not a nicety — regime flicker would make the station change
genre every thirty seconds and sound broken.

The design separates two concerns that are easy to tangle:

:class:`RegimeScorer`
    Stateless. Turns a feature vector into a score per regime. Pure, so the §47
    Market Lab can show every score and an operator can see *why* a classification
    was close.

:class:`RegimeStabiliser`
    Stateful. Decides whether a winning score is allowed to *become* the current
    regime. This is where all four §5 mechanisms live, and they fix four different
    failure modes:

    ``min_confidence``      a weak winner is not adopted at all
    ``confirmation_bars``   a challenger must win repeatedly, killing single-bar noise
    ``hysteresis_margin``   a challenger must beat the incumbent by a margin, killing
                            dithering at a threshold boundary
    ``min_duration``        a newly-adopted regime is held for a minimum time, killing
                            rapid churn
    ``cooldown``            a regime just left cannot be re-entered immediately,
                            killing A→B→A oscillation specifically

Why all five rather than one "stability" knob: they are independent. Confirmation
alone still allows A→B→A if the challenger genuinely wins three bars each way.
Cooldown alone still allows a single-bar spike to flip the regime. Only together do
they produce the behaviour §5 asks for, and the test suite drives adversarial
oscillating input to prove it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradefix_radio.config.schema import RegimeSettings
from tradefix_radio.contracts.enums import MarketDirection, MarketRegime
from tradefix_radio.contracts.market import MarketFeaturesV1
from tradefix_radio.market.rolling import ExponentialSmoother, clamp


class Direction(int, enum.Enum):
    """Internal signed direction, convertible to the public enum."""

    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1

    @property
    def public(self) -> MarketDirection:
        if self is Direction.BULLISH:
            return MarketDirection.BULLISH
        if self is Direction.BEARISH:
            return MarketDirection.BEARISH
        return MarketDirection.NEUTRAL


#: Directional return magnitude (5-bar fraction) above which direction is claimed.
#: Below this the move is indistinguishable from noise and §14 forbids implying one.
DIRECTION_THRESHOLD = 0.0004

#: Minimum absolute score for the *first* classification out of UNKNOWN. Not a
#: confidence margin — see :meth:`RegimeStabiliser.update` for why the two must not
#: be conflated.
FIRST_CLASSIFICATION_MIN_SCORE = 25.0


@dataclass(frozen=True)
class RegimeScores:
    """Scores for every regime, plus the derived winner."""

    scores: dict[MarketRegime, float]
    direction: Direction

    @property
    def winner(self) -> MarketRegime:
        return max(self.scores, key=lambda regime: self.scores[regime])

    @property
    def winning_score(self) -> float:
        return self.scores[self.winner]

    @property
    def runner_up(self) -> tuple[MarketRegime, float] | None:
        """Second place, for the §47 'how close was it?' display."""
        ordered = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)
        return ordered[1] if len(ordered) > 1 else None

    def score_of(self, regime: MarketRegime) -> float:
        return self.scores.get(regime, 0.0)

    def confidence(self) -> float:
        """Separation between first and second place, as 0–1.

        Confidence is a *margin*, not the winner's absolute score. A regime scoring
        90 while the runner-up scores 88 is a coin flip, and treating that as high
        confidence is precisely what produces flicker. Normalised by 40 points,
        above which the winner is unambiguous.
        """
        runner_up = self.runner_up
        if runner_up is None:
            return 1.0
        margin = self.winning_score - runner_up[1]
        return min(1.0, max(0.0, margin / 40.0))


def fast_direction_of(features: MarketFeaturesV1) -> Direction:
    """Direction from the medium-horizon return alone, without slope confirmation.

    Deliberately faster and less certain than
    :meth:`RegimeScorer.direction_of`. Used only by the reversal detector, whose job
    is to notice the moment the fast and slow signals disagree — see
    :meth:`RegimeScorer._reversal_score`.

    Not used for anything the station *asserts* about the market: §14 forbids
    implying direction on a signal this provisional.
    """
    move = features.returns_5m
    if abs(move) < DIRECTION_THRESHOLD:
        return Direction.NEUTRAL
    return Direction.BULLISH if move > 0 else Direction.BEARISH


def _ramp(value: float, low: float, high: float) -> float:
    """Map ``value`` onto 0–100 as it crosses from ``low`` to ``high``.

    The basic shape of every regime score: a soft threshold rather than a hard one.
    Hard thresholds are what make a classifier dither at the boundary, which is the
    failure §5 exists to prevent — hysteresis is the second line of defence, not the
    first.
    """
    if high == low:
        return 100.0 if value >= high else 0.0
    if high > low:
        return clamp(100.0 * (value - low) / (high - low))
    return clamp(100.0 * (low - value) / (low - high))


def _band(value: float, low: float, high: float, softness: float = 15.0) -> float:
    """Score 100 inside ``[low, high]``, falling off outside over ``softness``."""
    if low <= value <= high:
        return 100.0
    if value < low:
        return clamp(100.0 * (1.0 - (low - value) / softness))
    return clamp(100.0 * (1.0 - (value - high) / softness))


class RegimeScorer:
    """Scores all 14 §5 regimes from a feature vector. Stateless and pure.

    A short history is kept for the two regimes that are inherently about *change*
    rather than state — ``REVERSAL`` and ``POST_EVENT_NORMALIZATION`` cannot be
    recognised from a single snapshot, because what makes them distinctive is what
    the market was doing a few bars ago.
    """

    def __init__(self, history: int = 12) -> None:
        if history < 4:
            raise ValueError("history must be at least 4 bars")
        self._history = history
        self._directions: list[int] = []
        self._fast_directions: list[int] = []
        self._volatilities: list[float] = []
        self._trends: list[float] = []

    def reset(self) -> None:
        self._directions.clear()
        self._fast_directions.clear()
        self._volatilities.clear()
        self._trends.clear()

    def observe(self, features: MarketFeaturesV1, direction: Direction) -> None:
        """Record the state needed by the change-based regimes."""
        for store, value in (
            (self._directions, direction.value),
            (self._fast_directions, fast_direction_of(features).value),
            (self._volatilities, features.atr_percentile),
            (self._trends, features.trend_strength),
        ):
            store.append(value)  # type: ignore[arg-type]
            if len(store) > self._history:
                del store[0]

    @staticmethod
    def direction_of(features: MarketFeaturesV1) -> Direction:
        """Infer direction from the medium-horizon return and ADX agreement.

        Both must agree in sign. A positive 5-bar return with a bearish DI reading is
        a pullback inside a downtrend, not a bullish market, and calling it bullish
        would have the station play triumphant programming into a decline.
        """
        move = features.returns_5m
        slope = features.moving_average_slope
        if abs(move) < DIRECTION_THRESHOLD:
            return Direction.NEUTRAL
        if move > 0 and slope >= 0:
            return Direction.BULLISH
        if move < 0 and slope <= 0:
            return Direction.BEARISH
        return Direction.NEUTRAL

    def score(self, features: MarketFeaturesV1) -> RegimeScores:
        """Score every regime. Higher is a better match."""
        direction = self.direction_of(features)

        vol = features.atr_percentile
        rng = features.range_percentile
        trend = features.trend_strength
        compression = features.compression_score
        expansion = features.expansion_score
        breakout = features.breakout_strength
        volume = features.volume_percentile

        # How flat the market is, used by several of the quiet regimes. The ramp
        # saturates at the 12th percentile so a genuinely dead market reaches a full
        # score rather than asymptotically approaching one.
        quietness = (_ramp(vol, 30.0, 12.0) + _ramp(rng, 30.0, 12.0)) / 2.0
        trendlessness = _ramp(trend, 35.0, 8.0)

        scores: dict[MarketRegime, float] = {
            # --- quiet family. The bands are deliberately *nested and disjoint at
            # their centres* rather than merely overlapping: an earlier version gave
            # LOW_VOLATILITY_RANGE a band starting at the very floor, where it scored
            # a perfect 100 and QUIET — capped below 100 by its weighted blend —
            # could never win. QUIET became unreachable, and the station's quietest
            # programming never aired. Each band now owns a distinct centre.
            MarketRegime.QUIET: 0.6 * quietness + 0.4 * trendlessness,
            MarketRegime.LOW_VOLATILITY_RANGE: (
                0.5 * _band(vol, 14.0, 34.0) + 0.3 * trendlessness + 0.2 * _band(rng, 14.0, 38.0)
            ),
            MarketRegime.NORMAL_RANGE: (
                0.55 * _band(vol, 36.0, 62.0) + 0.45 * trendlessness
            ),
            MarketRegime.HIGH_VOLATILITY_RANGE: (
                0.55 * _ramp(vol, 62.0, 88.0) + 0.45 * trendlessness
            ),
            # --- compression family
            MarketRegime.COMPRESSION: (
                0.65 * compression + 0.2 * trendlessness + 0.15 * _ramp(volume, 55.0, 25.0)
            ),
            # Buildup is compression that is starting to attract participation:
            # volume rising while range is still tight. That combination is the
            # genuine tell, and it is why volume is weighted heavily here.
            MarketRegime.BREAKOUT_BUILDUP: (
                0.4 * compression
                + 0.4 * _ramp(volume, 45.0, 80.0)
                + 0.2 * _ramp(breakout, 5.0, 35.0)
            ),
            # --- breakout family. Direction-gated: a bullish breakout score is zero
            # in a bearish market, so the two can never both be high.
            MarketRegime.BULLISH_BREAKOUT: (
                (0.5 * breakout + 0.3 * expansion + 0.2 * _ramp(volume, 50.0, 85.0))
                if direction is Direction.BULLISH
                else 0.0
            ),
            MarketRegime.BEARISH_BREAKOUT: (
                (0.5 * breakout + 0.3 * expansion + 0.2 * _ramp(volume, 50.0, 85.0))
                if direction is Direction.BEARISH
                else 0.0
            ),
            # --- trend family. A trend is sustained direction *without* an ongoing
            # breakout; the breakout penalty is what keeps the two separable.
            MarketRegime.BULLISH_TREND: (
                (0.6 * trend + 0.25 * _ramp(vol, 25.0, 70.0) + 0.15 * _ramp(breakout, 45.0, 5.0))
                if direction is Direction.BULLISH
                else 0.0
            ),
            MarketRegime.BEARISH_TREND: (
                (0.6 * trend + 0.25 * _ramp(vol, 25.0, 70.0) + 0.15 * _ramp(breakout, 45.0, 5.0))
                if direction is Direction.BEARISH
                else 0.0
            ),
            # --- extremes
            MarketRegime.EXTREME_VOLATILITY: (
                0.6 * _ramp(vol, 85.0, 99.0) + 0.4 * _ramp(expansion, 40.0, 90.0)
            ),
            # --- change-based regimes
            MarketRegime.REVERSAL: self._reversal_score(features),
            MarketRegime.POST_EVENT_NORMALIZATION: self._normalisation_score(features),
            # UNKNOWN is never scored competitively; the stabiliser selects it when
            # history is insufficient. Scoring it would let it win by accident.
            MarketRegime.UNKNOWN: 0.0,
        }

        self.observe(features, direction)
        return RegimeScores(scores={k: clamp(v) for k, v in scores.items()}, direction=direction)

    def _reversal_score(self, features: MarketFeaturesV1) -> float:
        """A reversal is a *direction flip out of an established move*.

        Uses the **fast** directional signal (the 5-bar return), deliberately, not the
        slope-confirmed :meth:`direction_of`. A reversal is by definition the moment
        when the fast signal has turned and the slow one has not yet: requiring the
        20-bar moving-average slope to confirm made this regime unreachable from any
        scenario, because by the time the slope flips, the earlier history has flipped
        with it and there is no longer any opposition to detect. Detecting a reversal
        only after it has completed is not detecting a reversal.

        Three conditions must hold together: the earlier fast direction was opposite,
        that earlier move had real trend strength, and the current bar carries
        conviction. Dropping the trend-strength requirement would classify every bit of
        chop in a range as a reversal — wrong, and musically exhausting.
        """
        current = fast_direction_of(features)
        if current is Direction.NEUTRAL or len(self._fast_directions) < 6:
            return 0.0
        earlier = self._fast_directions[: len(self._fast_directions) // 2]
        opposing = [d for d in earlier if d != 0 and d != current.value]
        if not opposing:
            return 0.0
        opposition_strength = len(opposing) / max(1, len(earlier))
        prior_trend = max(self._trends[: len(self._trends) // 2] or [0.0])
        conviction = max(features.trend_strength, features.breakout_strength)
        return (
            0.4 * 100.0 * opposition_strength
            + 0.3 * _ramp(prior_trend, 25.0, 70.0)
            + 0.3 * _ramp(conviction, 20.0, 65.0)
        )

    def _normalisation_score(self, features: MarketFeaturesV1) -> float:
        """Volatility falling steeply from a recent extreme.

        Distinct from COMPRESSION, which is volatility that is *already* low. This is
        the decay after an event, and it matters musically because the station should
        wind down rather than stay at peak intensity for an hour after a spike.
        """
        if len(self._volatilities) < 6:
            return 0.0
        peak = max(self._volatilities)
        current = features.atr_percentile
        if peak < 75.0:
            return 0.0  # Nothing elevated enough to be normalising from.
        decline = peak - current
        return 0.5 * _ramp(decline, 10.0, 45.0) + 0.5 * _ramp(current, 85.0, 40.0)


@dataclass
class RegimeDecision:
    """The stabiliser's output for one bar."""

    regime: MarketRegime
    direction: MarketDirection
    confidence: float
    #: Seconds the current regime has been in force.
    age_seconds: float
    changed: bool
    previous_regime: MarketRegime | None
    #: Scores after smoothing, for the §47 Market Lab.
    smoothed_scores: dict[MarketRegime, float] = field(default_factory=dict)
    #: Why a challenger was rejected, when one was. Blank otherwise.
    suppression_reason: str = ""
    candidate: MarketRegime | None = None
    candidate_streak: int = 0


class RegimeStabiliser:
    """Turns per-bar scores into a stable regime (§5).

    Holds the only mutable state in the regime engine, which is why §75 crash
    recovery and §96 persistence both target this object rather than the scorer.
    """

    def __init__(self, settings: RegimeSettings) -> None:
        self._settings = settings
        self._smoothers: dict[MarketRegime, ExponentialSmoother] = {
            regime: ExponentialSmoother(settings.score_smoothing_alpha)
            for regime in MarketRegime
        }
        self._current: MarketRegime = MarketRegime.UNKNOWN
        self._direction: MarketDirection = MarketDirection.NEUTRAL
        self._confidence = 0.0
        self._entered_at: datetime | None = None
        self._left_at: dict[MarketRegime, datetime] = {}
        self._candidate: MarketRegime | None = None
        self._candidate_streak = 0
        self._transition_count = 0

    # -- introspection -----------------------------------------------------

    @property
    def current(self) -> MarketRegime:
        return self._current

    @property
    def transition_count(self) -> int:
        """Total adopted transitions. The anti-flicker test asserts on this."""
        return self._transition_count

    def age_seconds(self, now: datetime) -> float:
        if self._entered_at is None:
            return 0.0
        return max(0.0, (now - self._entered_at).total_seconds())

    def reset(self) -> None:
        for smoother in self._smoothers.values():
            smoother.reset()
        self._current = MarketRegime.UNKNOWN
        self._direction = MarketDirection.NEUTRAL
        self._confidence = 0.0
        self._entered_at = None
        self._left_at.clear()
        self._candidate = None
        self._candidate_streak = 0
        self._transition_count = 0

    # -- decision ----------------------------------------------------------

    def update(
        self, scores: RegimeScores, *, features: MarketFeaturesV1, now: datetime
    ) -> RegimeDecision:
        """Fold in one bar's scores and return the stabilised decision."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        if not features.sufficient_history:
            # §4: refuse to classify on unreliable features rather than guess.
            return self._hold_unknown(now, "insufficient_history")

        smoothed = {
            regime: self._smoothers[regime].push(score) or 0.0
            for regime, score in scores.scores.items()
        }
        # UNKNOWN never competes; see RegimeScorer.score.
        smoothed[MarketRegime.UNKNOWN] = 0.0

        winner = max(smoothed, key=lambda regime: smoothed[regime])
        winner_score = smoothed[winner]
        ordered = sorted(smoothed.values(), reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else 100.0
        confidence = min(1.0, max(0.0, margin / 40.0))

        settings = self._settings
        age = self.age_seconds(now)
        reason = ""

        # First classification. The gate here is the winner's **absolute** score, not
        # the margin over the runner-up.
        #
        # This distinction is load-bearing. `min_confidence` exists to stop flicker
        # between near-ties, and that risk only exists once there is an incumbent to
        # flip away from. Applying the margin gate to the first classification instead
        # produced a far worse failure: neighbouring regimes legitimately score
        # closely — QUIET and LOW_VOLATILITY_RANGE in a dead market, the two trend
        # regimes in a steady one — so the margin never cleared the bar and the engine
        # sat in UNKNOWN *indefinitely*, broadcasting neutral programming and never
        # reacting to the market at all.
        #
        # With no incumbent, choosing between two similar quiet regimes is a
        # low-stakes decision: both map to adjacent musical territory. The reported
        # confidence remains the honest margin, so the UI still shows that it was a
        # close call.
        if self._current is MarketRegime.UNKNOWN:
            if winner_score >= FIRST_CLASSIFICATION_MIN_SCORE:
                return self._adopt(winner, scores, confidence, smoothed, now)
            return self._hold_unknown(
                now,
                f"no regime scores above {FIRST_CLASSIFICATION_MIN_SCORE:.0f} "
                f"(best {winner.value} at {winner_score:.1f})",
                smoothed,
            )

        if winner is self._current:
            # The incumbent still leads. Refresh confidence, clear any challenger.
            self._confidence = confidence
            self._direction = scores.direction.public
            self._candidate = None
            self._candidate_streak = 0
            return RegimeDecision(
                regime=self._current,
                direction=self._direction,
                confidence=confidence,
                age_seconds=age,
                changed=False,
                previous_regime=None,
                smoothed_scores=smoothed,
            )

        # A challenger leads. Every gate below must pass before it is adopted.
        if confidence < settings.min_confidence:
            reason = "below_min_confidence"
        elif age < settings.min_duration_seconds:
            reason = f"min_duration ({age:.0f}s < {settings.min_duration_seconds:.0f}s)"
        elif winner_score - smoothed[self._current] < settings.hysteresis_margin:
            reason = (
                f"hysteresis ({winner_score - smoothed[self._current]:.1f} < "
                f"{settings.hysteresis_margin:.1f})"
            )
        elif self._in_cooldown(winner, now):
            remaining = settings.cooldown_seconds - (
                now - self._left_at[winner]
            ).total_seconds()
            reason = f"cooldown ({remaining:.0f}s remaining)"

        if reason:
            # The challenger is suppressed, but its streak is still tracked: a
            # suppressed challenger that keeps winning should be adopted the instant
            # its gate clears, not have to start counting again.
            self._track_candidate(winner)
            return RegimeDecision(
                regime=self._current,
                direction=self._direction,
                confidence=self._confidence,
                age_seconds=age,
                changed=False,
                previous_regime=None,
                smoothed_scores=smoothed,
                suppression_reason=reason,
                candidate=self._candidate,
                candidate_streak=self._candidate_streak,
            )

        self._track_candidate(winner)
        if self._candidate_streak < settings.confirmation_bars:
            return RegimeDecision(
                regime=self._current,
                direction=self._direction,
                confidence=self._confidence,
                age_seconds=age,
                changed=False,
                previous_regime=None,
                smoothed_scores=smoothed,
                suppression_reason=(
                    f"awaiting_confirmation ({self._candidate_streak}/"
                    f"{settings.confirmation_bars})"
                ),
                candidate=self._candidate,
                candidate_streak=self._candidate_streak,
            )

        return self._adopt(winner, scores, confidence, smoothed, now)

    # -- internals ---------------------------------------------------------

    def _track_candidate(self, winner: MarketRegime) -> None:
        if self._candidate is winner:
            self._candidate_streak += 1
        else:
            self._candidate = winner
            self._candidate_streak = 1

    def _in_cooldown(self, regime: MarketRegime, now: datetime) -> bool:
        left_at = self._left_at.get(regime)
        if left_at is None:
            return False
        return now - left_at < timedelta(seconds=self._settings.cooldown_seconds)

    def _adopt(
        self,
        regime: MarketRegime,
        scores: RegimeScores,
        confidence: float,
        smoothed: dict[MarketRegime, float],
        now: datetime,
    ) -> RegimeDecision:
        previous = self._current
        if previous is not MarketRegime.UNKNOWN:
            self._left_at[previous] = now
        self._current = regime
        self._direction = scores.direction.public
        self._confidence = confidence
        self._entered_at = now
        self._candidate = None
        self._candidate_streak = 0
        self._transition_count += 1
        return RegimeDecision(
            regime=regime,
            direction=self._direction,
            confidence=confidence,
            age_seconds=0.0,
            changed=True,
            previous_regime=previous,
            smoothed_scores=smoothed,
        )

    def _hold_unknown(
        self,
        now: datetime,
        reason: str,
        smoothed: dict[MarketRegime, float] | None = None,
    ) -> RegimeDecision:
        """Stay in UNKNOWN, but still report the scores.

        Passing ``smoothed`` through matters for §47: the moment an operator most
        needs to see every regime's score is when the engine is *refusing* to pick
        one. Returning an empty map here would leave the Market Lab blank exactly
        when it has something to explain.
        """
        if self._entered_at is None:
            self._entered_at = now
        return RegimeDecision(
            regime=MarketRegime.UNKNOWN,
            direction=MarketDirection.NEUTRAL,
            # §32: UNKNOWN cannot carry confidence above 0.5, and the contract
            # enforces it; 0.0 is the honest value.
            confidence=0.0,
            age_seconds=self.age_seconds(now),
            changed=False,
            previous_regime=None,
            smoothed_scores=dict(smoothed or {}),
            suppression_reason=reason,
        )


class RegimeEngine:
    """Scorer plus stabiliser, the unit the market service consumes."""

    def __init__(self, settings: RegimeSettings) -> None:
        self._scorer = RegimeScorer()
        self._stabiliser = RegimeStabiliser(settings)

    @property
    def scorer(self) -> RegimeScorer:
        return self._scorer

    @property
    def stabiliser(self) -> RegimeStabiliser:
        return self._stabiliser

    @property
    def current(self) -> MarketRegime:
        return self._stabiliser.current

    @property
    def transition_count(self) -> int:
        return self._stabiliser.transition_count

    def reset(self) -> None:
        self._scorer.reset()
        self._stabiliser.reset()

    def classify(self, features: MarketFeaturesV1, *, now: datetime) -> RegimeDecision:
        scores = self._scorer.score(features)
        return self._stabiliser.update(scores, features=features, now=now)


__all__ = [
    "DIRECTION_THRESHOLD",
    "FIRST_CLASSIFICATION_MIN_SCORE",
    "Direction",
    "RegimeDecision",
    "RegimeEngine",
    "RegimeScorer",
    "RegimeScores",
    "RegimeStabiliser",
    "fast_direction_of",
]
