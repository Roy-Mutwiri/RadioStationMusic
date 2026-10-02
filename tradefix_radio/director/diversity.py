"""DiversityDirector — the anti-boredom system (§11, §12).

§11 opens with "This requirement is extremely important", and it is: a 24/7 station that
collapses into four genres on rotation is indistinguishable from a broken one, and the
failure is gradual enough that nobody notices for a week.

Two mechanisms, deliberately distinct:

**Hard constraints** express the §11 rules that are absolute — "same genre maximum 2
consecutive", "same blueprint: never repeat". They are
:class:`~tradefix_radio.director.selection.Constraint` objects with a ``severity``, so
if they ever conflict to the point of leaving no option, the relaxation ladder gives way
in a defined order and reports that it did.

**Soft penalties** express the rest: "prefer something we have not played lately". These
are multiplicative weights, because a graded preference is what keeps a hundred-track
stretch interesting where no single rule is broken.

Conflating the two is the obvious mistake. All-hard rules deadlock; all-soft rules
permit four consecutive identical genres with low-but-nonzero probability — which over a
week of unattended operation *will* happen.

The §11 **diversity score** (0–100) is a measurement, not a control signal in itself.
When it falls below ``diversity.diversity_floor`` the director is pushed toward a
different creative region via :meth:`DiversityDirector.divergence_pressure`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from tradefix_radio.config.schema import DiversitySettings
from tradefix_radio.director.history import (
    ProgrammedTrack,
    ProgrammingHistory,
    spread_score,
    variety_score,
)
from tradefix_radio.director.selection import Constraint, recency_penalty

#: Severity ordering for the relaxation ladder (lower relaxes first).
#:
#: The ordering encodes how badly the brief wants each rule kept. A repeated BPM is
#: mildly dull; a repeated blueprint is §11's one "never". Soft preferences give way
#: long before identity rules do.
SEVERITY_BPM = 1
SEVERITY_KEY = 2
SEVERITY_DURATION = 3
SEVERITY_TOPIC = 4
SEVERITY_PERSONA = 5
SEVERITY_VOCAL_RUN = 6
SEVERITY_INSTRUMENTAL_RUN = 7
SEVERITY_GENRE_RUN = 8
SEVERITY_TOPIC_PAIR = 9
SEVERITY_SIGNATURE = 10

#: BPM standard deviation treated as fully varied, for the score.
BPM_SPREAD_FULL_SCALE = 22.0
#: Energy standard deviation treated as fully varied.
ENERGY_SPREAD_FULL_SCALE = 18.0
#: Instrumental fraction the station aims for; §11 tracks the vocal/instrumental ratio
#: but gives no target, so this is a judgement: roughly a third instrumental keeps the
#: station from feeling either relentlessly vocal or like a background playlist.
TARGET_INSTRUMENTAL_RATIO = 0.35

#: How many distinct values a healthily varied window should show, per dimension.
#:
#: Modest on purpose. These are not "use the whole library" targets: §97 gives each session
#: a musical personality, so a quiet Asian session drawing on five or six genres is correct
#: programming, not a collapse. The figures are what a listener would notice as variety
#: across twenty tracks.
TARGET_DISTINCT_GENRES = 7
TARGET_DISTINCT_KEYS = 8
TARGET_DISTINCT_TOPICS = 10
TARGET_DISTINCT_PERSONAS = 4


@dataclass(frozen=True)
class DiversityComponent:
    """One contributor to the §11 diversity score."""

    name: str
    score: float
    weight: float
    detail: str = ""

    @property
    def points(self) -> float:
        return self.score * self.weight


@dataclass
class DiversityReport:
    """The §11 diversity score with its full breakdown.

    Exposed through the API so the §41 dashboard can show the score *and* why it is
    what it is. A bare number would tell an operator that programming is getting stale
    without telling them in which dimension.
    """

    score: float
    components: list[DiversityComponent] = field(default_factory=list)
    tracks_considered: int = 0

    def weakest(self, count: int = 3) -> list[DiversityComponent]:
        """The dimensions dragging the score down — what to diversify next."""
        return sorted(self.components, key=lambda component: component.score)[:count]

    def as_dict(self) -> dict[str, float]:
        return {component.name: round(component.score * 100, 1) for component in self.components}


@dataclass(frozen=True)
class DivergencePressure:
    """How hard to push away from recent programming, and in which dimensions.

    §11: "If diversity falls below threshold, DiversityDirector should deliberately
    push the next song toward a different creative region."
    """

    #: 0 = no pressure, 1 = maximum push away from recent choices.
    strength: float
    #: Dimension names to prioritise, weakest first.
    dimensions: tuple[str, ...]

    @property
    def is_active(self) -> bool:
        return self.strength > 0.0


class DiversityDirector:
    """Builds §11 constraints and penalties, and measures diversity."""

    def __init__(self, settings: DiversitySettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> DiversitySettings:
        return self._settings

    # -- hard constraints --------------------------------------------------

    def genre_constraints(
        self, history: ProgrammingHistory
    ) -> list[Constraint[str]]:
        """§11's genre rules, as hard vetoes over genre keys."""
        settings = self._settings
        constraints: list[Constraint[str]] = []

        # "same genre maximum: 2 consecutive tracks"
        most_recent = history.most_recent
        if most_recent is not None:
            run_genre = most_recent.genre
            run_length = history.consecutive_leading(
                lambda entry: entry.genre == run_genre
            )
            if run_length >= settings.max_same_genre_consecutive:
                # An annotated inner function rather than a lambda with a default-argument
                # closure. The lambda form defeats type inference, which silently widens
                # Constraint[str] to Constraint[Any] and propagates Any through every
                # selection that uses it.
                def not_the_run_genre(genre: str, blocked: str = run_genre) -> bool:
                    return genre != blocked

                constraints.append(
                    Constraint(
                        name="genre_run",
                        predicate=not_the_run_genre,
                        reason=(
                            f"{run_genre} has aired {run_length} times in a row "
                            f"(limit {settings.max_same_genre_consecutive})"
                        ),
                        severity=SEVERITY_GENRE_RUN,
                    )
                )

        # Medium-horizon share ceiling: no genre may dominate the last N tracks.
        # Catches the slow collapse that consecutive-run limits miss — alternating
        # A,B,A,B,A,B breaks no run rule and is still monotonous.
        counts = history.genre_counts(settings.horizon_medium)
        total = sum(counts.values())
        if total >= max(4, settings.horizon_medium // 2):
            ceiling = settings.max_genre_share_medium
            over = {
                genre
                for genre, count in counts.items()
                if count / total > ceiling
            }
            if over:
                blocked_genres = frozenset(over)

                def not_over_share(genre: str, blocked: frozenset[str] = blocked_genres) -> bool:
                    return genre not in blocked

                constraints.append(
                    Constraint(
                        name="genre_share",
                        predicate=not_over_share,
                        reason=(
                            f"{', '.join(sorted(over))} exceed "
                            f"{ceiling:.0%} of the last {total} tracks"
                        ),
                        severity=SEVERITY_GENRE_RUN,
                    )
                )

        return constraints

    def bpm_constraint(self, history: ProgrammingHistory) -> Constraint[int]:
        """§11: "same BPM +/- 4: not within previous 4 tracks"."""
        tolerance = self._settings.bpm_tolerance
        horizon = self._settings.bpm_repeat_horizon

        def bpm_is_fresh(bpm: int) -> bool:
            return not history.bpm_within(bpm, tolerance, horizon)

        return Constraint(
            name="bpm_recency",
            predicate=bpm_is_fresh,
            reason=(
                f"a BPM within +/-{tolerance} aired in the last {horizon} tracks"
            ),
            severity=SEVERITY_BPM,
        )

    def key_constraint(self, history: ProgrammingHistory) -> Constraint[str]:
        """§11: "same key: avoid within previous 4 tracks"."""
        settings = self._settings
        horizon = settings.key_repeat_horizon

        def key_is_fresh(key: str) -> bool:
            def matches(entry: ProgrammedTrack) -> bool:
                return entry.musical_key == key

            return not history.appears_within(matches, horizon)

        return Constraint(
            name="key_recency",
            predicate=key_is_fresh,
            reason=f"that key aired in the last {horizon} tracks",
            severity=SEVERITY_KEY,
        )

    def duration_constraint(self, history: ProgrammingHistory) -> Constraint[int]:
        tolerance = float(self._settings.duration_tolerance_seconds)
        horizon = self._settings.duration_repeat_horizon

        def duration_is_fresh(duration: int) -> bool:
            return not history.duration_within(float(duration), tolerance, horizon)

        return Constraint(
            name="duration_recency",
            predicate=duration_is_fresh,
            reason=(
                f"a duration within {tolerance:.0f}s aired in the last {horizon} tracks"
            ),
            severity=SEVERITY_DURATION,
        )

    def topic_constraint(self, history: ProgrammingHistory) -> Constraint[str]:
        """§11: "same lyric topic: avoid within previous 10 tracks"."""
        horizon = self._settings.topic_repeat_horizon

        def topic_is_fresh(topic: str) -> bool:
            def matches(entry: ProgrammedTrack) -> bool:
                return topic in (entry.primary_topic, entry.secondary_topic)

            return not history.appears_within(matches, horizon)

        return Constraint(
            name="topic_recency",
            predicate=topic_is_fresh,
            reason=f"that topic aired in the last {horizon} tracks",
            severity=SEVERITY_TOPIC,
        )

    def topic_pair_constraint(
        self, history: ProgrammingHistory
    ) -> Constraint[tuple[str, str]]:
        """§11: "same primary + secondary topic combination: never intentionally repeat".

        Checked against **all** history rather than a horizon, because the rule says
        never. Order-insensitive: a listener does not distinguish (A,B) from (B,A).
        """
        used = history.topic_pairs()
        return Constraint(
            name="topic_pair_unique",
            predicate=lambda pair: (min(pair), max(pair)) not in used,
            reason="that primary+secondary topic combination has already aired",
            severity=SEVERITY_TOPIC_PAIR,
        )

    def signature_constraint(self, history: ProgrammingHistory) -> Constraint[str]:
        """§11: "same blueprint: never repeat".

        The highest severity, so it is the last rule the relaxation ladder gives up.
        """
        used = history.signatures()
        return Constraint(
            name="blueprint_unique",
            predicate=lambda signature: signature not in used,
            reason="that exact blueprint has already been produced",
            severity=SEVERITY_SIGNATURE,
        )

    def persona_constraint(self, history: ProgrammingHistory) -> Constraint[str]:
        horizon = self._settings.persona_repeat_horizon

        def persona_is_fresh(persona: str) -> bool:
            def matches(entry: ProgrammedTrack) -> bool:
                return entry.persona_id == persona

            return not history.appears_within(matches, horizon)

        return Constraint(
            name="persona_recency",
            predicate=persona_is_fresh,
            reason=f"that persona aired in the last {horizon} tracks",
            severity=SEVERITY_PERSONA,
        )

    def vocal_constraints(
        self, history: ProgrammingHistory
    ) -> list[Constraint[bool]]:
        """§11's instrumental and vocal-type run limits, over "is instrumental".

        Returns constraints on a boolean, which reads oddly but is exactly right: the
        decision being constrained is "may the next track be instrumental?".
        """
        settings = self._settings
        constraints: list[Constraint[bool]] = []

        instrumental_run = history.consecutive_leading(lambda entry: entry.is_instrumental)
        if instrumental_run >= settings.max_instrumental_consecutive:
            constraints.append(
                Constraint(
                    name="instrumental_run",
                    predicate=lambda is_instrumental: not is_instrumental,
                    reason=(
                        f"{instrumental_run} instrumental tracks in a row "
                        f"(limit {settings.max_instrumental_consecutive})"
                    ),
                    severity=SEVERITY_INSTRUMENTAL_RUN,
                )
            )

        vocal_run = history.consecutive_leading(lambda entry: not entry.is_instrumental)
        if vocal_run >= settings.max_same_vocal_type_consecutive * 2:
            # Twice the per-style limit: a long unbroken run of *any* vocals, regardless
            # of style, eventually wants an instrumental for contrast.
            constraints.append(
                Constraint(
                    name="vocal_run",
                    predicate=lambda is_instrumental: is_instrumental,
                    reason=f"{vocal_run} vocal tracks in a row",
                    severity=SEVERITY_VOCAL_RUN,
                )
            )

        return constraints

    def vocal_style_constraint(self, history: ProgrammingHistory) -> Constraint[str]:
        """§11: "same vocal type maximum: 2 consecutive tracks"."""
        settings = self._settings
        most_recent = history.most_recent
        if most_recent is None or most_recent.is_instrumental:
            return _permissive_style_constraint()
        style = most_recent.vocal_style
        run = history.consecutive_leading(
            lambda entry: not entry.is_instrumental and entry.vocal_style == style
        )
        if run < settings.max_same_vocal_type_consecutive:
            return _permissive_style_constraint()

        def style_differs(candidate: str, blocked: str = style) -> bool:
            return candidate != blocked

        return Constraint(
            name="vocal_style_run",
            predicate=style_differs,
            reason=(
                f"vocal style {style} used {run} times in a row "
                f"(limit {settings.max_same_vocal_type_consecutive})"
            ),
            severity=SEVERITY_VOCAL_RUN,
        )

    # -- soft penalties ----------------------------------------------------

    def genre_penalty(self, history: ProgrammingHistory, genre: str) -> float:
        """Graded discouragement for a recently used genre (§12 medium horizon)."""
        positions = history.positions_ago(
            lambda entry: entry.genre == genre or entry.secondary_genre == genre
        )
        return recency_penalty(
            positions, horizon=self._settings.horizon_medium, strength=0.7
        )

    def topic_penalty(self, history: ProgrammingHistory, topic: str) -> float:
        """§12: lyrical concept repetition over the long horizon."""
        positions = history.positions_ago(
            lambda entry: topic in (entry.primary_topic, entry.secondary_topic)
        )
        return recency_penalty(
            positions, horizon=self._settings.horizon_long, strength=0.8
        )

    def persona_penalty(self, history: ProgrammingHistory, persona: str) -> float:
        positions = history.positions_ago(lambda entry: entry.persona_id == persona)
        return recency_penalty(
            positions, horizon=self._settings.horizon_medium, strength=0.6
        )

    def key_penalty(self, history: ProgrammingHistory, key: str) -> float:
        positions = history.positions_ago(lambda entry: entry.musical_key == key)
        return recency_penalty(
            positions, horizon=self._settings.horizon_medium, strength=0.5
        )

    # -- measurement -------------------------------------------------------

    def score(self, history: ProgrammingHistory) -> DiversityReport:
        """The §11 diversity score, 0–100, with its breakdown.

        An empty or very short history scores 100 rather than 0: a freshly started
        station has not repeated itself, and reporting maximum staleness at launch would
        trigger divergence pressure for no reason.
        """
        settings = self._settings
        if len(history) < 3:
            return DiversityReport(score=100.0, components=[], tracks_considered=len(history))

        medium = settings.horizon_medium
        long = settings.horizon_long

        # Target distinct counts scale with the window but are capped, because §97
        # sessions legitimately narrow programming and demanding library-wide coverage
        # would report a correctly-focused quiet session as a collapse.
        observed = len(history)
        genre_target = min(TARGET_DISTINCT_GENRES, max(2, min(medium, observed)))
        key_target = min(TARGET_DISTINCT_KEYS, max(2, min(medium, observed)))
        topic_target = min(TARGET_DISTINCT_TOPICS, max(2, min(long, observed)))
        persona_target = min(TARGET_DISTINCT_PERSONAS, max(2, min(medium, observed)))

        components = [
            DiversityComponent(
                name="genre_variety",
                score=variety_score(
                    history.genre_counts(medium).values(), target_distinct=genre_target
                ),
                weight=0.22,
                detail=f"{len(history.genre_counts(medium))} genres in last {medium}",
            ),
            DiversityComponent(
                name="bpm_spread",
                score=spread_score(
                    [float(bpm) for bpm in history.bpm_values(medium)],
                    full_scale=BPM_SPREAD_FULL_SCALE,
                ),
                weight=0.15,
            ),
            DiversityComponent(
                name="key_variety",
                score=variety_score(
                    history.key_counts(medium).values(), target_distinct=key_target
                ),
                weight=0.12,
                detail=f"{len(history.key_counts(medium))} keys in last {medium}",
            ),
            DiversityComponent(
                name="topic_variety",
                score=variety_score(
                    history.topic_counts(long).values(), target_distinct=topic_target
                ),
                weight=0.18,
                detail=f"{len(history.topic_counts(long))} topics in last {long}",
            ),
            DiversityComponent(
                name="vocal_balance",
                score=self._balance_score(history.instrumental_ratio(medium)),
                weight=0.13,
                detail=f"{history.instrumental_ratio(medium):.0%} instrumental",
            ),
            DiversityComponent(
                name="persona_variety",
                score=variety_score(
                    history.persona_counts(medium).values(),
                    target_distinct=persona_target,
                ),
                weight=0.10,
                detail=f"{len(history.persona_counts(medium))} personas in last {medium}",
            ),
            DiversityComponent(
                name="energy_spread",
                score=spread_score(
                    list(history.energy_values(medium)),
                    full_scale=ENERGY_SPREAD_FULL_SCALE,
                ),
                weight=0.10,
            ),
        ]

        total_weight = sum(component.weight for component in components)
        raw = sum(component.points for component in components) / total_weight
        return DiversityReport(
            score=max(0.0, min(100.0, raw * 100.0)),
            components=components,
            tracks_considered=min(len(history), long),
        )

    @staticmethod
    def _balance_score(instrumental_ratio: float) -> float:
        """1.0 at the target instrumental ratio, falling off toward either extreme.

        Both extremes are penalised: a station that is 100 % vocal is exhausting and one
        that is 100 % instrumental has stopped being a radio station with something to
        say. §11 tracks the ratio; the target is a judgement recorded in
        :data:`TARGET_INSTRUMENTAL_RATIO`.
        """
        distance = abs(instrumental_ratio - TARGET_INSTRUMENTAL_RATIO)
        # Normalised by the larger distance to an extreme, so the score reaches 0 only
        # at a fully one-sided station.
        worst = max(TARGET_INSTRUMENTAL_RATIO, 1.0 - TARGET_INSTRUMENTAL_RATIO)
        return max(0.0, 1.0 - distance / worst)

    def divergence_pressure(self, history: ProgrammingHistory) -> DivergencePressure:
        """How hard to push away from recent programming (§11).

        Strength rises linearly from 0 at the configured floor to 1 at a score of 0, so
        a marginally stale station nudges and a badly collapsed one shoves.
        """
        report = self.score(history)
        floor = self._settings.diversity_floor
        if report.score >= floor or floor <= 0:
            return DivergencePressure(strength=0.0, dimensions=())
        strength = min(1.0, (floor - report.score) / floor)
        return DivergencePressure(
            strength=strength,
            dimensions=tuple(component.name for component in report.weakest(3)),
        )

    def is_below_floor(self, history: ProgrammingHistory) -> bool:
        return self.score(history).score < self._settings.diversity_floor

    # -- long-horizon audit ------------------------------------------------

    def collapse_warnings(self, history: ProgrammingHistory) -> list[str]:
        """Human-readable warnings about programming collapse (§81-18, §64).

        Used by the endurance report. Deliberately returns prose rather than booleans:
        the output is read by a person deciding whether a week-long run was acceptable.
        """
        settings = self._settings
        warnings: list[str] = []
        long = settings.horizon_long

        genre_counts = history.genre_counts(long)
        total = sum(genre_counts.values())
        if total >= 20:
            dominant, count = genre_counts.most_common(1)[0]
            share = count / total
            if share > settings.max_genre_share_medium:
                warnings.append(
                    f"genre {dominant!r} is {share:.0%} of the last {total} tracks "
                    f"(ceiling {settings.max_genre_share_medium:.0%})"
                )
            if len(genre_counts) < 4:
                warnings.append(
                    f"only {len(genre_counts)} distinct genres in the last {total} tracks"
                )

        topic_counts = history.topic_counts(long)
        if sum(topic_counts.values()) >= 20 and len(topic_counts) < 5:
            warnings.append(
                f"only {len(topic_counts)} distinct lyric topics in the last "
                f"{sum(topic_counts.values())} vocal tracks"
            )

        ratio = history.instrumental_ratio(long)
        if total >= 20 and (ratio > 0.8 or ratio < 0.05):
            warnings.append(f"instrumental ratio has drifted to {ratio:.0%}")

        signatures = history.signatures(long)
        if total >= 20 and len(signatures) < total:
            warnings.append(
                f"{total - len(signatures)} repeated blueprint signatures in the last "
                f"{total} tracks"
            )

        return warnings


def _permissive_style_constraint() -> Constraint[str]:
    """A no-op vocal-style constraint.

    Returned when there is no run to break. An empty ``reason`` is the signal callers use
    to skip applying it, so a no-op constraint never appears in a relaxation report as
    though a real rule had been given up.
    """

    def always_permitted(_style: str) -> bool:
        return True

    return Constraint(
        name="vocal_style_run",
        predicate=always_permitted,
        reason="",
        severity=SEVERITY_VOCAL_RUN,
    )


def default_horizons(settings: DiversitySettings) -> dict[str, int | timedelta]:
    """The §12 horizon set, named, for reporting and tests."""
    return {
        "short": settings.horizon_short,
        "medium": settings.horizon_medium,
        "long": settings.horizon_long,
        "day": timedelta(hours=24),
        "week": timedelta(days=7),
    }


__all__ = [
    "BPM_SPREAD_FULL_SCALE",
    "ENERGY_SPREAD_FULL_SCALE",
    "SEVERITY_BPM",
    "SEVERITY_GENRE_RUN",
    "SEVERITY_SIGNATURE",
    "SEVERITY_TOPIC",
    "SEVERITY_TOPIC_PAIR",
    "TARGET_DISTINCT_GENRES",
    "TARGET_DISTINCT_KEYS",
    "TARGET_DISTINCT_PERSONAS",
    "TARGET_DISTINCT_TOPICS",
    "TARGET_INSTRUMENTAL_RATIO",
    "DivergencePressure",
    "DiversityComponent",
    "DiversityDirector",
    "DiversityReport",
    "default_horizons",
]
