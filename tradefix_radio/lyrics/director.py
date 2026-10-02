"""LyricsDirector — subject, format, branding and perspective (§13, §14, §16).

§13: "Build a standalone LyricsDirector. The music model must not independently invent
all lyrics without historical awareness. LyricsDirector owns: subject selection, topic
history, educational concepts, Trade Fix branding frequency, lyrical uniqueness, mood,
narrator perspective, structure."

The key word is *standalone*. Topic choice has to be made with knowledge of what the
station has already said, which the generator cannot have. Hand a model an empty prompt
and it will produce a song about discipline every time, because discipline is the most
obvious thing to write about — and §12's "lyrical concept repetition rules over last 100"
would be violated continuously.

This class makes the **plan** (a :class:`LyricsSpecV1`: what to talk about, in what
format, from whose viewpoint, with how many brand mentions). The actual words are
composed separately by :mod:`tradefix_radio.lyrics.composer`, so a §17 validation failure
can trigger a rewrite of the text without discarding the musical decision — regenerating
audio is expensive and rewriting words is not.

§16's branding rule gets a note of its own. "Trade Fix should appear naturally. Do NOT
spam." The mention count is drawn from a configured weight distribution whose default is
55 % zero mentions, so most songs carry no brand at all and stay musically credible. The
one exception is the ``station anthem`` format, where a brand mention is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradefix_radio.config.schema import LyricsSettings
from tradefix_radio.contracts.enums import MarketRegime, TradingSession, VocalStyle
from tradefix_radio.contracts.music import LyricsSpecV1
from tradefix_radio.director.diversity import DiversityDirector
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.library import (
    ContentLibrary,
    LyricFormatDefinition,
    PersonaDefinition,
    TopicDefinition,
)
from tradefix_radio.director.selection import Candidate, WeightedSelector, band_fit

#: Weight multiplier for a topic the chosen persona names among its themes. §100 gives
#: each persona "a distinctive internal creative profile"; honouring it is what makes
#: personas feel like different artists rather than different labels.
PERSONA_THEME_BONUS = 2.2
#: Smaller bonus for a topic merely in one of the persona's categories.
PERSONA_CATEGORY_BONUS = 1.4

#: Weight multiplier applied to a speculative topic (§14). Kept well below 1 so the
#: station's educational content is dominated by things that are actually reliable, while
#: leaving the hedged macro topics available.
SPECULATIVE_WEIGHT = 0.35
CONTEXTUAL_WEIGHT = 0.85

#: Probability a track with lyrics also carries a secondary topic. Below 1 on purpose:
#: a song about one idea is usually better than a song about two.
SECONDARY_TOPIC_PROBABILITY = 0.55

#: How far outside its declared energy band a §15 format may still be chosen.
#:
#: Mirrors the music director's ``MAX_ENERGY_DISTANCE``. §15's formats run from "minimal
#: vocal" to "station anthem", and those are energy statements: picking an anthem for a
#: dead-quiet market contradicts §1 as plainly as picking the wrong genre would.
MAX_FORMAT_ENERGY_DISTANCE = 28.0


@dataclass(frozen=True)
class LyricPlan:
    """The lyric brief, plus the resolved library objects and the reasoning."""

    spec: LyricsSpecV1
    topic: TopicDefinition | None
    secondary: TopicDefinition | None
    lyric_format: LyricFormatDefinition | None
    rationale: tuple[str, ...]

    @property
    def enabled(self) -> bool:
        return self.spec.enabled


class LyricsDirector:
    """Chooses what the station talks about, and how."""

    def __init__(
        self,
        library: ContentLibrary,
        settings: LyricsSettings,
        diversity: DiversityDirector,
        selector: WeightedSelector,
    ) -> None:
        self._library = library
        self._settings = settings
        self._diversity = diversity
        self._selector = selector

    # -- the plan ----------------------------------------------------------

    def plan(
        self,
        *,
        history: ProgrammingHistory,
        regime: MarketRegime,
        session: TradingSession,
        energy: float,
        persona: PersonaDefinition | None,
        vocal_style: VocalStyle,
        instrumental: bool,
        temperature: float = 1.0,
        divergence_strength: float = 0.0,
    ) -> LyricPlan:
        """Build the §8 ``lyrics`` block for one track.

        An instrumental track returns a disabled spec rather than ``None``, so the
        blueprint's shape never varies and the §8 contract's coherence checks always
        apply.
        """
        if instrumental:
            return LyricPlan(
                spec=LyricsSpecV1(
                    enabled=False,
                    primary_topic=None,
                    secondary_topic=None,
                    tradefix_mentions=0,
                    educational_intensity=0.0,
                ),
                topic=None,
                secondary=None,
                lyric_format=None,
                rationale=("instrumental track: no lyric plan",),
            )

        rationale: list[str] = []

        lyric_format = self._choose_format(
            persona=persona, energy=energy, vocal_style=vocal_style, temperature=temperature
        )
        rationale.append(f"format {lyric_format.key!r}")

        primary = self._choose_topic(
            history=history,
            regime=regime,
            session=session,
            persona=persona,
            temperature=temperature,
            divergence_strength=divergence_strength,
        )
        rationale.append(f"topic {primary.key!r} ({primary.certainty})")

        secondary = self._choose_secondary(
            primary=primary, history=history, regime=regime, session=session,
            temperature=temperature,
        )
        if secondary is not None:
            rationale.append(f"secondary topic {secondary.key!r}")

        mentions = self._choose_mentions(lyric_format)
        if mentions:
            rationale.append(f"{mentions} Trade Fix mention(s)")

        educational = self._educational_intensity(lyric_format, primary)
        rationale.append(f"educational intensity {educational:.2f}")

        perspective = (
            persona.perspective if persona is not None else self._default_perspective()
        )

        return LyricPlan(
            spec=LyricsSpecV1(
                enabled=True,
                primary_topic=primary.key,
                secondary_topic=secondary.key if secondary is not None else None,
                tradefix_mentions=mentions,
                educational_intensity=educational,
                format=lyric_format.key,
                perspective=perspective,
            ),
            topic=primary,
            secondary=secondary,
            lyric_format=lyric_format,
            rationale=tuple(rationale),
        )

    # -- format ------------------------------------------------------------

    def _choose_format(
        self,
        *,
        persona: PersonaDefinition | None,
        energy: float,
        vocal_style: VocalStyle,
        temperature: float,
    ) -> LyricFormatDefinition:
        """Pick a §15 format the persona works in and the energy supports."""
        keys = (
            persona.formats
            if persona is not None
            else tuple(self._library.format_keys)
        )
        candidates: list[Candidate[str]] = []
        for key in keys:
            definition = self._library.lyric_format(key)
            candidate = Candidate(value=key)
            candidate.multiply(
                "energy_fit",
                band_fit(
                    energy,
                    definition.energy.min,
                    definition.energy.max,
                    falloff=12.0,
                    floor=0.02,
                ),
            )
            # Hard exclusion far outside the band, for the same reason genre selection
            # has one: a weight floor plus a high creative temperature still lets a long
            # tail win occasionally, and a call-and-response format on a dead-quiet
            # energy-5 track is as incoherent as 172 BPM drum & bass in the same market.
            #
            # Safe to veto: when every candidate is vetoed the selector's relaxation
            # ladder drops the vetoes rather than failing, so a persona whose own format
            # list does not span the current energy still gets its best available option.
            if definition.energy.distance_to(energy) > MAX_FORMAT_ENERGY_DISTANCE:
                candidate.veto(
                    f"energy {energy:.0f} is far outside the "
                    f"{definition.energy.min:.0f}-{definition.energy.max:.0f} band"
                )
            # A spoken-word format over a rap delivery is incoherent; density is the
            # proxy. A high-density format needs a style that can carry it.
            if vocal_style in {VocalStyle.CHOPPED_HOOK, VocalStyle.AD_LIB}:
                candidate.multiply(
                    "style_fit", 1.4 if definition.density <= 0.55 else 0.5
                )
            elif vocal_style in {VocalStyle.RAP, VocalStyle.MELODIC_RAP}:
                candidate.multiply(
                    "style_fit", 1.3 if definition.density >= 0.5 else 0.6
                )
            elif vocal_style is VocalStyle.SPOKEN:
                candidate.multiply(
                    "style_fit", 1.6 if definition.density <= 0.4 else 0.4
                )
            candidates.append(candidate)

        if not candidates:
            # Should be impossible: the library validator guarantees every persona has at
            # least one format. Raising here would be correct but unhelpful mid-broadcast.
            return self._library.lyric_format(self._library.format_keys[0])
        chosen = self._selector.select(candidates, temperature=temperature).chosen
        return self._library.lyric_format(chosen)

    # -- topic -------------------------------------------------------------

    def _choose_topic(
        self,
        *,
        history: ProgrammingHistory,
        regime: MarketRegime,
        session: TradingSession,
        persona: PersonaDefinition | None,
        temperature: float,
        divergence_strength: float,
    ) -> TopicDefinition:
        """Choose the primary subject, honouring §12's long repetition horizon.

        Selects over topic **keys**. The §11 topic-recency constraint is a predicate over
        keys, so selecting over ``TopicDefinition`` objects would make every comparison
        compare an object to a string — trivially unequal — and the repetition rule would
        silently never fire.
        """
        candidates: list[Candidate[str]] = []
        for topic in self._library.topics.values():
            candidate = Candidate(value=topic.key, weight=topic.weight)
            candidate.multiply("regime", topic.affinity_for(regime))
            candidate.multiply("session", topic.session_bias(session))
            candidate.multiply("certainty", _certainty_weight(topic))
            candidate.multiply(
                "recency", self._diversity.topic_penalty(history, topic.key)
            )
            if persona is not None:
                candidate.multiply("persona", _persona_affinity(persona, topic))
            candidates.append(candidate)

        constraints = [self._diversity.topic_constraint(history)]
        # Divergence pressure raises temperature so a stale station reaches further down
        # the topic list rather than cycling the top few.
        effective = temperature * (1.0 + 0.5 * divergence_strength)
        chosen = self._selector.select(
            candidates, constraints, temperature=effective
        ).chosen
        return self._library.topic(chosen)

    def _choose_secondary(
        self,
        *,
        primary: TopicDefinition,
        history: ProgrammingHistory,
        regime: MarketRegime,
        session: TradingSession,
        temperature: float,
    ) -> TopicDefinition | None:
        """Optionally add a second subject, never repeating a pair (§11)."""
        if self._selector.rng.random() > SECONDARY_TOPIC_PROBABILITY:
            return None

        pool = [
            self._library.topic(key)
            for key in primary.pairs_with
            if key in self._library.topics
        ]
        if not pool:
            # No declared pairing: fall back to the same category, which is more coherent
            # than an arbitrary topic from anywhere in the graph.
            pool = [
                topic
                for topic in self._library.topics_in_category(primary.category)
                if topic.key != primary.key
            ]
        if not pool:
            return None

        used_pairs = history.topic_pairs()
        candidates: list[Candidate[str]] = []
        for topic in pool:
            pair = (min(primary.key, topic.key), max(primary.key, topic.key))
            candidate = Candidate(value=topic.key, weight=topic.weight)
            candidate.multiply("regime", topic.affinity_for(regime))
            candidate.multiply("session", topic.session_bias(session))
            candidate.multiply("certainty", _certainty_weight(topic))
            candidate.multiply(
                "recency", self._diversity.topic_penalty(history, topic.key)
            )
            if pair in used_pairs:
                candidate.veto("that topic pair has already aired")
            candidates.append(candidate)

        if not any(candidate.is_viable for candidate in candidates):
            # Every pairing used. A single-topic song is a perfectly good outcome and far
            # better than repeating a combination §11 says never to repeat.
            return None
        chosen = self._selector.select(candidates, temperature=temperature).chosen
        return self._library.topic(chosen)

    # -- branding and payload ---------------------------------------------

    def _choose_mentions(self, lyric_format: LyricFormatDefinition) -> int:
        """§16 Trade Fix mention count, from the configured distribution.

        The ``station anthem`` format is the documented exception: it exists to carry the
        brand, so it never draws zero.
        """
        weights = self._settings.tradefix_mention_weights
        counts = sorted(weights)
        if lyric_format.brand_forward:
            branded = [count for count in counts if count > 0]
            if branded:
                return self._selector.choose_from(
                    branded, weight_of=lambda count: weights[count]
                )
        return self._selector.choose_from(counts, weight_of=lambda count: weights[count])

    def _educational_intensity(
        self, lyric_format: LyricFormatDefinition, topic: TopicDefinition
    ) -> float:
        """How much teaching payload to carry.

        The format sets the baseline; a non-educational topic pulls it down. The result is
        clamped into the configured range so an operator can cap didacticism station-wide
        — §16's "most songs should remain musically credible" applies to lecturing as
        much as to branding.
        """
        low, high = self._settings.educational_intensity_range
        base = lyric_format.educational
        if not topic.educational or not topic.teaching_points:
            base *= 0.4
        jitter = self._selector.rng.uniform(-0.08, 0.08)
        return max(low, min(high, base + jitter))

    def _default_perspective(self) -> str:
        """A perspective for the rare case of lyrics with no persona."""
        return self._selector.choose_from(tuple(self._library.perspectives))


def _certainty_weight(topic: TopicDefinition) -> float:
    """§14: reliable material dominates; speculative material stays rare."""
    if topic.certainty == "speculative":
        return SPECULATIVE_WEIGHT
    if topic.certainty == "contextual":
        return CONTEXTUAL_WEIGHT
    return 1.0


def _persona_affinity(persona: PersonaDefinition, topic: TopicDefinition) -> float:
    """How much this persona cares about this topic (§100)."""
    if topic.key in persona.themes.topics:
        return PERSONA_THEME_BONUS
    if topic.category in persona.themes.categories:
        return PERSONA_CATEGORY_BONUS
    # Not excluded: a persona straying from its usual subjects occasionally is variety,
    # not incoherence.
    return 0.5


__all__ = [
    "CONTEXTUAL_WEIGHT",
    "PERSONA_CATEGORY_BONUS",
    "PERSONA_THEME_BONUS",
    "SECONDARY_TOPIC_PROBABILITY",
    "SPECULATIVE_WEIGHT",
    "LyricPlan",
    "LyricsDirector",
]
