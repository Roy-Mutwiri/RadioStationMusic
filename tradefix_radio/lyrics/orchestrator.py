"""The production lyric path (B3).

Every part of lyric generation existed and nothing called any of it. `LyricsDirector`
chose a subject, `LyricComposer` built text from the topic graph, `LyricValidator`
enforced §14 and §17, and `compare_lyrics` detected reuse — and the `lyrics` table was
empty across the entire database, because no production caller tied them together.

The consequence was not a missing feature but a silent one. `AceStepPromptBuilder` sees a
vocal blueprint with no composed lyric, correctly refuses to let the model invent its own
words about markets, and downgrades the track to instrumental. So every vocal blueprint
the director produced became an instrumental, with a warning nobody read. The real station
test confirmed it by ear: `vocal_style=chopped_hook`, audibly no vocals.

This module is the missing orchestration and deliberately nothing else. It owns no
policy of its own — it sequences the components that already hold it:

    blueprint + market context
      -> LyricsDirector      subject, format, perspective, brand budget
      -> LyricComposer       text from the topic graph
      -> LyricValidator      §14 trading claims, §17 safety, brand, repetition
      -> compare_lyrics      reuse against the station's own history
      -> LyricGenerationResult

Why a separate service
----------------------
Putting this in `MusicDirector` would make the director depend on lyric originality and
on the database. Putting it in `AceStepProvider` would put lyric authorship inside the
performer, which is exactly the boundary §7.10 draws: **ACE-Step is a performer, never an
author of trading claims.** The provider receives validated lyrics or an instrumental
instruction, and has no third option.
"""

from __future__ import annotations

import enum
import random
import zlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

import structlog

from tradefix_radio.contracts.enums import TradingSession, VocalStyle
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.lyrics.composer import LyricComposer
from tradefix_radio.lyrics.validators import (
    LyricValidator,
    ValidationContext,
    ValidatorConfig,
)
from tradefix_radio.originality.lyrics import compare_lyrics, fingerprint_lyrics

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from tradefix_radio.config.schema import LyricsSettings
    from tradefix_radio.contracts.lyrics import LyricsV1, LyricValidationResultV1
    from tradefix_radio.contracts.music import MusicBlueprintV1
    from tradefix_radio.director.history import ProgrammingHistory
    from tradefix_radio.director.library import (
        ContentLibrary,
        LyricFormatDefinition,
        PersonaDefinition,
    )
    from tradefix_radio.lyrics.director import LyricPlan, LyricsDirector
    from tradefix_radio.originality.lyrics import LyricFingerprint

_log = structlog.get_logger(__name__)

#: How many times the composer is re-run when validation rejects its output.
#:
#: Two. The composer is a seeded grammar over a fixed corpus, so a third attempt explores
#: very little new ground — and a vocal track that cannot be written in two tries is
#: better realised as an instrumental than chased while the buffer drains.
MAX_COMPOSE_ATTEMPTS: Final = 2

#: Words of lyric per second of track, used to size the format to the duration.
#:
#: Derived from the formats themselves rather than chosen: "full rap" asks for 140–400
#: words and the station's tracks run 150–240 s, which is 0.9–1.7 words per second. The
#: band below is deliberately wide because a hook-heavy format repeats its hook, so the
#: *distinct* word count understates what is sung.
MIN_WORDS_PER_SECOND: Final = 0.35
MAX_WORDS_PER_SECOND: Final = 2.2


class LyricMode(str, enum.Enum):
    """How much singing a track carries, and in what register.

    A deliberate layer above the §15 format keys. The formats are a content library an
    operator edits; these are the shapes the station programmes with, and the scheduler
    and the UI should not have to know which library entry currently implements
    "minimal vocal".
    """

    NONE = "none"
    """Instrumental. No lyric is composed and the provider is told so explicitly."""

    HOOK_ONLY = "hook_only"
    """Mostly instrumental with one sung or spoken hook."""

    MINIMAL_VOCAL = "minimal_vocal"
    """A few phrases, placed rather than performed."""

    FULL_RAP = "full_rap"
    MELODIC_RAP = "melodic_rap"
    FULL_SONG = "full_song"
    SPOKEN_WORD = "spoken_word"

    @property
    def is_instrumental(self) -> bool:
        return self is LyricMode.NONE

    @property
    def is_sparse(self) -> bool:
        """Whether most of the track is deliberately *not* sung.

        A hook over three minutes of instrumental is the format working, not a lyric that
        came up short — so the duration budget's lower bound does not apply to these. It
        applies to the modes that are supposed to fill the track.
        """
        return self in (LyricMode.NONE, LyricMode.HOOK_ONLY, LyricMode.MINIMAL_VOCAL)


#: Which §15 format keys realise each mode.
#:
#: A mapping rather than a rename, because the formats carry word bounds, section lists
#: and energy ranges that the modes deliberately do not. Several formats per mode is the
#: point: "the station needs variation" is satisfied by the director choosing between
#: them, not by adding modes.
MODE_FORMATS: Final[dict[LyricMode, tuple[str, ...]]] = {
    LyricMode.HOOK_ONLY: ("mostly instrumental with vocal hook",),
    LyricMode.MINIMAL_VOCAL: ("minimal vocal", "call-and-response"),
    LyricMode.FULL_RAP: ("full rap", "educational bars"),
    LyricMode.MELODIC_RAP: ("melodic rap", "hook-heavy"),
    LyricMode.FULL_SONG: ("storytelling", "motivational", "station anthem"),
    LyricMode.SPOKEN_WORD: ("spoken word",),
}

#: Vocal styles that each mode is compatible with, for reporting and for the UI.
MODE_VOCAL_STYLES: Final[dict[LyricMode, tuple[VocalStyle, ...]]] = {
    LyricMode.NONE: (VocalStyle.NONE,),
    LyricMode.HOOK_ONLY: (VocalStyle.CHOPPED_HOOK, VocalStyle.AD_LIB),
    LyricMode.MINIMAL_VOCAL: (VocalStyle.CHOPPED_HOOK, VocalStyle.SPOKEN),
    LyricMode.FULL_RAP: (VocalStyle.RAP,),
    LyricMode.MELODIC_RAP: (VocalStyle.MELODIC_RAP,),
    LyricMode.FULL_SONG: (VocalStyle.SUNG, VocalStyle.MELODIC_RAP),
    LyricMode.SPOKEN_WORD: (VocalStyle.SPOKEN,),
}


class LyricFailure(str, enum.Enum):
    """Why a lyric could not be produced. Recorded, never swallowed."""

    NO_FORMAT_FITS_DURATION = "no_format_fits_duration"
    COMPOSER_RETURNED_NOTHING = "composer_returned_nothing"
    VALIDATION_REJECTED = "validation_rejected"
    LYRIC_REUSE = "lyric_reuse"
    #: The orchestrator itself raised. Named rather than folded into one of the above so a
    #: defect in this module is never read later as the composer legitimately declining.
    ORCHESTRATOR_ERROR = "orchestrator_error"


@dataclass(frozen=True)
class LyricGenerationResult:
    """What the orchestrator produced, and the evidence behind it."""

    track_id: str
    mode: LyricMode
    #: ``None`` for an instrumental, or when every attempt failed.
    lyrics: LyricsV1 | None = None
    fingerprint: LyricFingerprint | None = None
    #: Section names in order, as composed. Part of what the provider is told.
    sections: tuple[str, ...] = ()
    #: Teaching points actually used, for the §12 concept history.
    concepts: tuple[str, ...] = ()
    primary_topic: str | None = None
    secondary_topic: str | None = None
    #: The active symbol this lyric was scoped to. ``None`` for market-neutral content.
    market_scope: str | None = None
    tradefix_mentions: int = 0
    validation: LyricValidationResultV1 | None = None
    #: Highest lyric similarity found against the station's own history.
    max_similarity: float = 0.0
    closest_track_id: str | None = None
    attempts: int = 0
    failure: LyricFailure | None = None
    #: One line per decision, in the order taken.
    rationale: tuple[str, ...] = ()
    #: Set when a vocal track was realised as an instrumental instead, with the reason.
    fallback_reason: str | None = None

    @classmethod
    def crashed(
        cls, blueprint: MusicBlueprintV1, error: BaseException
    ) -> LyricGenerationResult:
        """A result standing in for an unhandled orchestrator exception.

        So the caller has one failure path rather than two. The alternative — letting the
        exception escape — made a lyric defect indistinguishable from a scheduler defect,
        and stopped the station planning tracks at all.
        """
        return cls(
            track_id=blueprint.track_id,
            mode=LyricMode.NONE,
            attempts=0,
            failure=LyricFailure.ORCHESTRATOR_ERROR,
            rationale=(f"{type(error).__name__}: {error}"[:200],),
            fallback_reason="the lyric orchestrator raised",
        )

    @property
    def usable(self) -> bool:
        """Whether the provider may be given these words."""
        return self.lyrics is not None and self.failure is None

    @property
    def fell_back(self) -> bool:
        return self.fallback_reason is not None

    def as_payload(self) -> dict[str, object]:
        """For the §46 detail page and the generation log."""
        return {
            "mode": self.mode.value,
            "sections": list(self.sections),
            "primary_topic": self.primary_topic,
            "secondary_topic": self.secondary_topic,
            "market_scope": self.market_scope,
            "tradefix_mentions": self.tradefix_mentions,
            "word_count": 0 if self.lyrics is None else len(self.lyrics.text.split()),
            "attempts": self.attempts,
            "max_similarity": round(self.max_similarity, 4),
            "closest_track_id": self.closest_track_id,
            "failure": None if self.failure is None else self.failure.value,
            "fallback_reason": self.fallback_reason,
            "violations": (
                []
                if self.validation is None
                else [
                    {
                        "rule": v.rule,
                        "message": v.message,
                        "fatal": v.fatal,
                        "excerpt": v.excerpt,
                        "measured": v.measured,
                    }
                    for v in self.validation.violations
                ]
            ),
            "concepts": list(self.concepts),
        }


class LyricOrchestrator:
    """Produces validated lyrics for a blueprint, or an honest refusal."""

    def __init__(
        self,
        library: ContentLibrary,
        settings: LyricsSettings,
        director: LyricsDirector,
        selector: WeightedSelector,
        *,
        validator_config: ValidatorConfig | None = None,
    ) -> None:
        self._library = library
        self._settings = settings
        self._director = director
        self._selector = selector
        self._composer = LyricComposer(library, selector)
        self._validator = LyricValidator(settings)
        self._validator_config = validator_config or ValidatorConfig()

    def mode_for(self, blueprint: MusicBlueprintV1) -> LyricMode:
        """Which mode a blueprint is asking for.

        Read from the blueprint rather than chosen here: the director already decided how
        much singing this track carries when it picked the vocal style and the format, and
        a second opinion at this layer could disagree with the audio it is about to ask
        for.
        """
        if blueprint.is_instrumental or not blueprint.lyrics.enabled:
            return LyricMode.NONE

        requested = blueprint.lyrics.format
        if requested:
            for mode, formats in MODE_FORMATS.items():
                if requested in formats:
                    return mode

        style = blueprint.vocal.style
        for mode, styles in MODE_VOCAL_STYLES.items():
            if mode is not LyricMode.NONE and style in styles:
                return mode
        return LyricMode.FULL_RAP

    def _mode_of_format(self, key: str, *, fallback: LyricMode) -> LyricMode:
        """Which mode a §15 format realises."""
        for mode, formats in MODE_FORMATS.items():
            if key in formats:
                return mode
        return fallback

    def _pick_format(
        self, mode: LyricMode, duration_seconds: float, *, fallback: LyricFormatDefinition
    ) -> LyricFormatDefinition | None:
        """A format that realises ``mode`` and fits a track of this length.

        Several formats per mode is what keeps the station from sounding the same, so the
        choice among the fitting ones is weighted rather than first-wins.

        ``fallback`` is the director's own pick, kept when it already realises the mode
        and fits — there is no reason to overrule a choice that satisfies both.
        """
        low, high = self.word_budget(duration_seconds)

        def fits(definition: LyricFormatDefinition) -> bool:
            # Overlap, not containment: a format asking 140-400 words still suits a
            # budget of 62-396, because the composer writes within the intersection.
            #
            # The lower bound is skipped for sparse modes. "Mostly instrumental with
            # vocal hook" is 16-60 words *by definition*, and a 180-second track asks for
            # at least 62 — so applying the floor rejected the one format that mode has
            # and made every hook-only blueprint fall back to an instrumental. A hook
            # over three minutes of instrumental is the format doing its job.
            if definition.min_words > high:
                return False
            return mode.is_sparse or definition.max_words >= low

        if self._mode_of_format(fallback.key, fallback=mode) is mode and fits(fallback):
            return fallback

        candidates = [
            self._library.formats[key]
            for key in MODE_FORMATS.get(mode, ())
            if key in self._library.formats
        ]
        fitting = [definition for definition in candidates if fits(definition)]
        if not fitting:
            # No format for the requested mode fits. Rather than silently writing a
            # different kind of song, this is reported as a failure and the caller
            # applies its fallback policy — which for a radio station is usually an
            # instrumental, recorded with the reason.
            return None
        return self._selector.rng.choice(fitting)

    def word_budget(self, duration_seconds: float) -> tuple[int, int]:
        """The word range a track of this length can carry.

        §15's formats span 16 to 400 words while tracks run 60 to 240 seconds, so the two
        have to be reconciled somewhere. A 400-word verse body in a 60-second track is
        unperformable, and a 24-word lyric in a four-minute track leaves three and a half
        minutes of instrumental the blueprint did not ask for.
        """
        low = max(
            self._settings.min_lyric_words, int(duration_seconds * MIN_WORDS_PER_SECOND)
        )
        high = min(
            self._settings.max_lyric_words, int(duration_seconds * MAX_WORDS_PER_SECOND)
        )
        if high <= low:
            # A very short track: honour the duration and let the station-wide floor go.
            # The alternative is refusing to write a hook for a 45-second bumper.
            high = max(low + 1, int(duration_seconds * MAX_WORDS_PER_SECOND))
            low = min(low, high - 1)
        return low, high

    def generate(
        self,
        blueprint: MusicBlueprintV1,
        *,
        history: ProgrammingHistory,
        persona: PersonaDefinition | None = None,
        previous_hashes: frozenset[str] = frozenset(),
        previous_shingles: Sequence[tuple[str, frozenset[str]]] = (),
        recent_lyrics: Sequence[LyricFingerprint] = (),
    ) -> LyricGenerationResult:
        """Compose, validate and check a lyric for one blueprint.

        Never raises for a content reason. A lyric that cannot be written is a result with
        a `failure`, because the caller has to decide between an instrumental fallback and
        abandoning the track, and that decision is policy rather than an exception.

        There is deliberately no live `MarketStateV1` parameter. The market context the
        lyric is written against is the one recorded **on the blueprint** — regime,
        energy, session and symbol, captured when the director decided. A fresher reading
        would let the words describe a market the music was not written for, and §14's
        rule that the station must not state an unbacked market claim is satisfied by
        using the same structured state the composition itself came from.
        """
        mode = self.mode_for(blueprint)
        track_id = blueprint.track_id
        rationale: list[str] = [f"mode {mode.value}"]

        if mode.is_instrumental:
            return LyricGenerationResult(
                track_id=track_id,
                mode=mode,
                rationale=(*rationale, "instrumental blueprint; no lyric composed"),
            )

        symbol = blueprint.market.symbol
        duration = float(blueprint.composition.duration_seconds)
        low, high = self.word_budget(duration)
        rationale.append(f"word budget {low}-{high} for {duration:.0f}s")

        plan = self._plan_for(blueprint, history=history, persona=persona, symbol=symbol)
        if plan is None or plan.lyric_format is None:
            return LyricGenerationResult(
                track_id=track_id,
                mode=mode,
                market_scope=symbol,
                failure=LyricFailure.COMPOSER_RETURNED_NOTHING,
                rationale=(*rationale, "the director declined to plan a lyric"),
            )
        rationale.extend(plan.rationale)

        # Hold the director to the requested mode and the track's length.
        #
        # `LyricsDirector` picks a format from persona, energy and vocal style, and knows
        # nothing about duration. Left alone it produced 160 words of full rap for a
        # 90-second hook and a 54-word lyric for a 210-second track, and reported a
        # `rap` blueprint as `minimal_vocal`. Neither is the director being wrong — it is
        # being asked a question that does not include the duration.
        #
        # Enforced here by *choosing the format* rather than by tightening the validator.
        # `_check_length` deliberately takes the lower of the format floor and the global
        # one, because §15 includes 16-word formats on purpose; raising a floor it is
        # designed to ignore would be a no-op that looked like a constraint.
        chosen = self._pick_format(mode, duration, fallback=plan.lyric_format)
        if chosen is None:
            return LyricGenerationResult(
                track_id=track_id,
                mode=mode,
                market_scope=symbol,
                failure=LyricFailure.NO_FORMAT_FITS_DURATION,
                rationale=(
                    *rationale,
                    f"no {mode.value} format fits a {duration:.0f}s track "
                    f"({low}-{high} words)",
                ),
            )
        if chosen.key != plan.lyric_format.key:
            rationale.append(
                f"format {plan.lyric_format.key!r} -> {chosen.key!r} "
                f"for mode {mode.value} at {duration:.0f}s"
            )
            plan = replace(
                plan,
                lyric_format=chosen,
                spec=plan.spec.model_copy(update={"format": chosen.key}),
            )
        assert plan.lyric_format is not None
        rationale.append(f"realised as {mode.value} via {plan.lyric_format.key!r}")

        scope = self._scope_of(plan)
        context = ValidationContext(
            primary_topic=plan.topic,
            secondary_topic=plan.secondary,
            expected_mentions=plan.spec.tradefix_mentions,
            previous_hashes=previous_hashes,
            previous_shingles=previous_shingles,
            config=self._validator_config,
            # The format's own bounds intersected with what the duration allows. The
            # station-wide floor is a safety net; neither alone is the real spec.
            # The format's own bounds, not an intersection with the duration budget.
            # `_check_length` takes the *lower* of the format floor and the global one by
            # design, so a raised floor here would do nothing; duration is enforced by
            # `_pick_format` above, where it can actually bind. The ceiling is capped
            # because the check does take the lower maximum.
            format_min_words=plan.lyric_format.min_words,
            format_max_words=min(plan.lyric_format.max_words, high),
        )

        last_validation: LyricValidationResultV1 | None = None
        for attempt in range(1, MAX_COMPOSE_ATTEMPTS + 1):
            # A fresh composer per attempt, seeded differently.
            #
            # The composer is a *seeded* grammar, so re-running it with the same selector
            # reproduces the same lyric exactly — the retry loop would re-do the
            # computation and re-fail on the identical text. Observed: a short lyric
            # rejected for 19 words against a 40-word floor, twice, with the same words.
            #
            # The seed is derived from the track id and the attempt so the retry is still
            # reproducible, which §64 needs and a replayed rejection depends on. CRC32
            # rather than `hash()`: Python salts string hashing per process, so `hash()`
            # would give a different lyric on every run and quietly break the
            # reproducibility this comment claims.
            composer = (
                self._composer
                if attempt == 1
                else LyricComposer(
                    self._library,
                    WeightedSelector(
                        random.Random(  # noqa: S311 - creative choice, not security
                            zlib.crc32(f"{track_id}:{attempt}".encode())
                        )
                    ),
                )
            )
            composed = composer.compose(track_id=track_id, plan=plan, persona=persona)
            if composed is None:
                return LyricGenerationResult(
                    track_id=track_id,
                    mode=mode,
                    market_scope=scope,
                    attempts=attempt,
                    failure=LyricFailure.COMPOSER_RETURNED_NOTHING,
                    rationale=(*rationale, "the composer produced nothing"),
                )

            validation = self._validator.validate(composed.lyrics, context)
            last_validation = validation
            if not validation.accepted:
                rationale.append(
                    f"attempt {attempt} rejected: "
                    + "; ".join(v.rule for v in validation.violations[:4])
                )
                continue

            reuse, closest = self._reuse(composed.lyrics, recent_lyrics)
            if reuse >= self._settings.similarity_threshold:
                rationale.append(
                    f"attempt {attempt} too similar to {closest} ({reuse:.2f})"
                )
                continue

            _log.info(
                "lyrics.composed",
                track_id=track_id,
                mode=mode.value,
                format=plan.lyric_format.key,
                topic=plan.topic.key if plan.topic else None,
                symbol=symbol,
                scope=scope,
                words=len(composed.lyrics.text.split()),
                mentions=composed.lyrics.tradefix_mentions,
                attempt=attempt,
                max_similarity=round(reuse, 3),
            )
            return LyricGenerationResult(
                track_id=track_id,
                mode=mode,
                lyrics=composed.lyrics,
                sections=composed.sections,
                concepts=composed.concepts,
                primary_topic=composed.lyrics.primary_topic,
                secondary_topic=composed.lyrics.secondary_topic,
                market_scope=scope,
                tradefix_mentions=composed.lyrics.tradefix_mentions,
                validation=validation,
                max_similarity=reuse,
                closest_track_id=closest,
                attempts=attempt,
                rationale=(*rationale, *composed.rationale),
            )

        failure = (
            LyricFailure.VALIDATION_REJECTED
            if last_validation is not None and not last_validation.accepted
            else LyricFailure.LYRIC_REUSE
        )
        _log.warning(
            "lyrics.generation_failed",
            track_id=track_id,
            mode=mode.value,
            attempts=MAX_COMPOSE_ATTEMPTS,
            failure=failure.value,
            violations=(
                []
                if last_validation is None
                else [v.rule for v in last_validation.violations][:6]
            ),
        )
        return LyricGenerationResult(
            track_id=track_id,
            mode=mode,
            market_scope=scope,
            validation=last_validation,
            attempts=MAX_COMPOSE_ATTEMPTS,
            failure=failure,
            rationale=tuple(rationale),
        )

    # -- internals ---------------------------------------------------------

    def _plan_for(
        self,
        blueprint: MusicBlueprintV1,
        *,
        history: ProgrammingHistory,
        persona: PersonaDefinition | None,
        symbol: str,
    ) -> LyricPlan | None:
        """Re-plan the lyric against the blueprint the director actually produced.

        The blueprint already records a format, a perspective and a brand budget. Those
        are honoured; what this adds is the resolved library objects the composer needs,
        which the stored spec holds only by key.
        """
        plan = self._director.plan(
            history=history,
            regime=blueprint.market.regime,
            session=_session_of(blueprint),
            energy=blueprint.market.energy,
            persona=persona,
            vocal_style=blueprint.vocal.style,
            instrumental=False,
            symbol=symbol,
        )
        return plan if plan.enabled else None

    def _scope_of(self, plan: LyricPlan) -> str | None:
        """The market a lyric is scoped to, or ``None`` when it is market-neutral.

        Reported rather than asserted. The director's topic filter already guarantees a
        scoped topic matches the active symbol; this records which of the two cases
        happened so the UI can say "neutral trading content" instead of implying the song
        is about a market it never mentions.
        """
        scopes: list[str] = []
        for topic in (plan.topic, plan.secondary):
            if topic is not None and topic.markets:
                scopes.extend(topic.markets)
        if not scopes:
            return None
        return sorted({scope.upper() for scope in scopes})[0]

    def _reuse(
        self, lyrics: LyricsV1, recent: Sequence[LyricFingerprint]
    ) -> tuple[float, str | None]:
        """Highest similarity against recent lyrics, and whose.

        Compared through `compare_lyrics`, which works on shingles rather than on word
        overlap — "risk", "market", "discipline" and "gold" are the station's vocabulary,
        and a measure that punished their recurrence would reject every lyric the topic
        graph can produce.
        """
        if not recent:
            return 0.0, None
        candidate = fingerprint_lyrics(
            lyrics.track_id, lyrics.text, tradefix_mentions=lyrics.tradefix_mentions
        )
        worst = 0.0
        closest: str | None = None
        for existing in recent:
            comparison = compare_lyrics(candidate, existing)
            if comparison.score > worst:
                worst = comparison.score
                closest = existing.track_id
        return worst, closest


def _session_of(blueprint: MusicBlueprintV1) -> TradingSession:
    """The trading session recorded on the blueprint, as the director's enum.

    Falls back to London rather than raising: the session only steers topic weighting,
    and an unrecognised string is not a reason to refuse to write a lyric.
    """
    try:
        return TradingSession(blueprint.market.session)
    except ValueError:
        return TradingSession.LONDON


__all__ = [
    "MAX_COMPOSE_ATTEMPTS",
    "MODE_FORMATS",
    "LyricFailure",
    "LyricGenerationResult",
    "LyricMode",
    "LyricOrchestrator",
]
