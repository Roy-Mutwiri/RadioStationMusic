"""Lyric safety and quality validation (§17, §14).

§17 lists ten rejection categories. They are not equally checkable, and pretending
otherwise would be the kind of overclaiming §86 forbids. So they are split explicitly:

**Reliably enforced** — these are mechanical and the tests prove them:

* guaranteed-profit and cannot-lose claims
* fabricated live signals (an instruction to buy or sell at a price)
* impossible certainty, including per-topic forbidden phrasings from the topic graph
* missing hedging on a ``contextual`` or ``speculative`` topic (§14)
* a directional claim on a ``speculative`` relationship (§14)
* gibberish and padding, via lexical diversity
* excessive phrase repetition
* duplication against previous Trade Fix lyrics, exact and near
* brand over-use, both Trade Fix and third-party
* word-count bounds

**Mitigated structurally, not detected** — stated plainly:

* *Copyrighted song lyrics.* There is no way to detect these from a text alone, and a
  curated list of famous lines would be theatre. The real mitigation is architectural:
  the composer builds from this station's own topic graph, so it has no source material
  to copy from. A configurable ``blocked_phrases`` list exists for operators and becomes
  genuinely important if an LLM lyric provider is ever added, which is the only path by
  which outside text could enter.
* *Imitating a living artist.* Also undetectable from text. Mitigated by the persona
  schema having **no field** in which a real artist could be referenced (see
  :class:`~tradefix_radio.director.library.PersonaDefinition`), plus a configurable
  ``blocked_names`` list.

Calling that out is the point. A validator that claimed to detect copyright infringement
would be worse than one that says it cannot.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from tradefix_radio.config.schema import LyricsSettings
from tradefix_radio.contracts.lyrics import (
    LyricsV1,
    LyricValidationResultV1,
    LyricViolationV1,
)
from tradefix_radio.director.library import TopicDefinition

# ---------------------------------------------------------------- patterns
#
# Written as word-boundary regexes over the normalised (lowercased, punctuation-stripped)
# text, so "Guaranteed!" and "guaranteed" are the same thing. Each pattern is paired with
# the §17 clause it enforces.

#: Claims that a trade or a strategy cannot lose. §17's first two bullets.
GUARANTEED_OUTCOME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bguarantee(d|s)?\b", "guarantees an outcome"),
    (r"\bcan'?t lose\b", "claims a trade cannot lose"),
    (r"\bcannot lose\b", "claims a trade cannot lose"),
    (r"\bnever lose(s)?\b", "claims losses do not happen"),
    (r"\bno(t a)? ?risk\b", "denies risk"),
    (r"\brisk ?free\b", "claims the absence of risk"),
    (r"\bsure thing\b", "claims certainty"),
    (r"\bsure bet\b", "claims certainty"),
    (r"\bcertain profit\b", "promises profit"),
    (r"\bzero risk\b", "denies risk"),
    (r"\bfool ?proof\b", "claims infallibility"),
    # Narrowed to market *outcomes*. A bare "every single time" is ordinary English about
    # one's own discipline — "fixed fraction, every single time" is exactly the kind of
    # statement the station should make — and the broad form rejected the station's own
    # risk-management content.
    (
        r"\b(wins?|works?|pays?|profits?|hits?) every (single )?time\b",
        "claims an invariable outcome",
    ),
    (r"\bnever fails?\b", "claims infallibility"),
    (r"\balways wins?\b", "claims an invariable outcome"),
    (r"\bcan'?t go wrong\b", "claims infallibility"),
    (r"\b100 ?(percent|%) (win|accurate|accuracy)\b", "claims a perfect record"),
)

#: Promises of wealth. §17: "promise guaranteed profits".
PROFIT_PROMISE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bmake you rich\b", "promises wealth"),
    (r"\bget rich\b", "promises wealth"),
    (r"\bdouble your (money|account|capital)\b", "promises a specific return"),
    (r"\btriple your (money|account|capital)\b", "promises a specific return"),
    (r"\bfree money\b", "describes trading as free money"),
    (r"\beasy money\b", "describes trading as easy money"),
    (r"\bquit your job\b", "implies assured income"),
    (r"\bretire (early|young|on this)\b", "implies assured income"),
    (r"\bprinting money\b", "implies assured income"),
    (r"\bmillionaire by\b", "promises a specific outcome"),
)

#: §17: "give fabricated live signals". An instruction to trade at a level is a signal
#: regardless of how it is phrased, and the station has no business issuing one.
SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(buy|sell|long|short) (now|here|at) \d", "issues a trade instruction at a price"),
    (r"\b(buy|sell) (it )?(now|right now)\b", "issues a trade instruction"),
    (r"\b(entry|entries) at \d", "publishes an entry level"),
    (r"\b(target|tp) (at|is) \d", "publishes a target level"),
    (r"\b(stop|sl) (at|is) \d", "publishes a stop level"),
    (r"\bload up (at|now|here)\b", "issues a trade instruction"),
    (r"\bget in (now|here)\b", "issues a trade instruction"),
    (r"\ball in\b", "instructs maximum exposure"),
    (r"\bmax (leverage|lot|size)\b", "instructs maximum exposure"),
)

#: Hedging vocabulary. §14 requires contextual and speculative claims to be qualified.
HEDGE_WORDS: frozenset[str] = frozenset(
    {
        "often", "usually", "sometimes", "mostly", "tends", "tend", "tended",
        "typically", "frequently", "generally", "can", "could", "may", "might",
        "maybe", "occasionally", "mainly", "largely", "roughly",
        "about", "around", "seems", "seem", "looks", "appears", "appear",
        "no", "not", "rarely", "seldom", "some", "somedays", "likely",
        "probably", "perhaps", "possibly", "allowed", "doesn't", "don't",
        # "most days" and "most sessions" are hedges; omitting "most" meant one of the
        # composer's own hedge openers contributed no recognised qualifier.
        "most", "almost", "nearly",
    }
)

#: Directional assertions. On a ``speculative`` topic these are forbidden outright (§14):
#: the relationship is not reliable enough to support a claim about where price goes.
DIRECTIONAL_PATTERNS: tuple[str, ...] = (
    r"\bmeans (gold )?(goes|going|will go)\b",
    r"\bso (gold|price|it) (goes|will|must)\b",
    r"\b(gold|price) (goes|will go|must go) (up|down|higher|lower)\b",
    r"\bthat means (up|down|higher|lower)\b",
    r"\bwhich means (up|down|higher|lower)\b",
)

#: Third-party brand and slogan fragments. §17: "accidentally contain another brand's
#: slogan excessively". Deliberately short and generic — a long list of real company
#: slogans in source would be its own problem.
DEFAULT_BLOCKED_PHRASES: tuple[str, ...] = (
    "just do it",
    "think different",
    "finger lickin",
    "im lovin it",
    "because youre worth it",
    "the ultimate driving machine",
)

#: Trade Fix brand spellings, for counting mentions (§16).
BRAND_PATTERN = re.compile(r"\btrade ?fix\b")

#: Inline section markers, e.g. ``[verse]``. Must match the contract's own tag pattern so
#: the two agree about what counts as structure rather than content.
_SECTION_TAG_RE = re.compile(r"^\[[a-z0-9 _-]{2,24}\]$", re.IGNORECASE)

#: Minimum distinct-word ratio by word count. Short lyrics legitimately repeat more (a
#: hook is repetition), so a single flat threshold would reject every well-formed hook.
_DIVERSITY_BY_LENGTH: tuple[tuple[int, float], ...] = (
    (60, 0.30),
    (150, 0.26),
    (10_000, 0.22),
)


@dataclass
class ValidatorConfig:
    """Operator-extensible blocklists.

    Both default to the built-in lists plus nothing. They exist because the two §17
    categories that cannot be detected from text — copyrighted lyrics and artist
    imitation — are exactly the ones an operator may have specific knowledge about, and
    because an LLM lyric provider would make them matter.
    """

    blocked_phrases: tuple[str, ...] = DEFAULT_BLOCKED_PHRASES
    #: Real artist or band names that must never appear. Empty by default: the station
    #: composes from its own topic graph and has no reason to name anyone.
    blocked_names: tuple[str, ...] = ()
    #: How many times a blocked phrase may appear before it is a violation. 1 means never.
    blocked_phrase_tolerance: int = 1


@dataclass
class ValidationContext:
    """Everything a validator needs beyond the lyric itself."""

    primary_topic: TopicDefinition | None = None
    secondary_topic: TopicDefinition | None = None
    #: Expected brand mention count from the blueprint (§16).
    expected_mentions: int = 0
    #: Normalised text and content hashes of previous lyrics, for duplication checks.
    previous_hashes: frozenset[str] = frozenset()
    #: Shingle sets of recent lyrics, for near-duplicate detection.
    previous_shingles: Sequence[tuple[str, frozenset[str]]] = ()
    config: ValidatorConfig = field(default_factory=ValidatorConfig)
    #: The §15 format's own word bounds, when known. The global
    #: ``lyrics.min_lyric_words`` is a station-wide safety net; the format is the real
    #: spec. §15 includes "minimal vocal" and "mostly instrumental with vocal hook",
    #: which are *supposed* to be 16-24 words — a single global floor rejected every one
    #: of them.
    format_min_words: int | None = None
    format_max_words: int | None = None


class LyricValidator:
    """Runs every §17 rule over a candidate lyric."""

    def __init__(self, settings: LyricsSettings) -> None:
        self._settings = settings

    def validate(
        self, lyrics: LyricsV1, context: ValidationContext | None = None
    ) -> LyricValidationResultV1:
        """Check a lyric, returning every violation found.

        Every rule runs even after one fails. A rewrite prompted by a single reported
        violation would likely trip the next one, so the composer needs the whole list.
        """
        ctx = context or ValidationContext()
        text = lyrics.normalised_text
        words = text.split()
        violations: list[LyricViolationV1] = []

        # Structural checks run over the DEDUPLICATED line set.
        #
        # A chorus repeated four times is a song; the same phrase emitted twenty times is
        # a generator stuck in a loop. Measured on the rendered text those are
        # indistinguishable, and the hook-heavy format — which repeats its hook four times
        # by design — failed both the diversity and repetition checks. Deduplicating by
        # line separates the two: structural repetition collapses to one instance, while a
        # model looping *within* a section still shows up because its repeated lines sit
        # inside the same section body.
        structural_words = _unique_line_words(lyrics)

        violations.extend(self._check_length(words, ctx))
        violations.extend(self._check_guaranteed_outcomes(text))
        violations.extend(self._check_profit_promises(text))
        violations.extend(self._check_signals(text))
        violations.extend(self._check_topic_forbidden(text, ctx))
        violations.extend(self._check_hedging(words, ctx))
        violations.extend(self._check_speculative_direction(text, ctx))
        violations.extend(self._check_gibberish(structural_words))
        violations.extend(self._check_repetition(structural_words))
        violations.extend(self._check_brand(text, ctx))
        violations.extend(self._check_blocklists(text, ctx))

        max_similarity, closest = self._duplication(lyrics, ctx)
        if max_similarity >= self._settings.similarity_threshold:
            violations.append(
                LyricViolationV1(
                    rule="duplicate_lyrics",
                    message=(
                        f"too similar to a previous Trade Fix lyric "
                        f"({max_similarity:.0%} overlap, threshold "
                        f"{self._settings.similarity_threshold:.0%})"
                    ),
                    excerpt=closest,
                    fatal=True,
                )
            )

        fatal = [violation for violation in violations if violation.fatal]
        return LyricValidationResultV1(
            track_id=lyrics.track_id,
            accepted=not fatal,
            violations=tuple(violations),
            max_similarity=max_similarity,
            closest_track_id=closest,
        )

    # -- length ------------------------------------------------------------

    def _check_length(
        self, words: Sequence[str], ctx: ValidationContext
    ) -> list[LyricViolationV1]:
        """Word count against the format's bounds, falling back to the global ones.

        The format's minimum wins when it is *lower*, because §15 deliberately includes
        very short formats. Its maximum wins when it is *lower* too, because a format that
        says 60 words means it. In both directions the tighter-for-this-format value is the
        real spec, with the global settings as the outer safety net.
        """
        settings = self._settings
        count = len(words)

        minimum = settings.min_lyric_words
        if ctx.format_min_words is not None:
            minimum = min(minimum, ctx.format_min_words)
        maximum = settings.max_lyric_words
        if ctx.format_max_words is not None:
            maximum = min(maximum, ctx.format_max_words)

        if count < minimum:
            return [
                LyricViolationV1(
                    rule="too_short",
                    message=(
                        f"{count} words is below the {minimum}-word minimum; the "
                        "generator would produce near-silence"
                    ),
                    measured=float(count),
                    threshold=float(minimum),
                    fatal=True,
                )
            ]
        if count > maximum:
            return [
                LyricViolationV1(
                    rule="too_long",
                    message=(
                        f"{count} words exceeds the {maximum}-word maximum for this "
                        "format and track length"
                    ),
                    measured=float(count),
                    threshold=float(maximum),
                    fatal=True,
                )
            ]
        return []

    # -- §17 safety --------------------------------------------------------

    @staticmethod
    def _scan(
        text: str, patterns: Iterable[tuple[str, str]], rule: str
    ) -> list[LyricViolationV1]:
        found: list[LyricViolationV1] = []
        for pattern, description in patterns:
            match = re.search(pattern, text)
            if match is not None:
                found.append(
                    LyricViolationV1(
                        rule=rule,
                        message=description,
                        excerpt=_excerpt(text, match.start(), match.end()),
                        fatal=True,
                    )
                )
        return found

    def _check_guaranteed_outcomes(self, text: str) -> list[LyricViolationV1]:
        return self._scan(text, GUARANTEED_OUTCOME_PATTERNS, "guaranteed_outcome")

    def _check_profit_promises(self, text: str) -> list[LyricViolationV1]:
        return self._scan(text, PROFIT_PROMISE_PATTERNS, "profit_promise")

    def _check_signals(self, text: str) -> list[LyricViolationV1]:
        return self._scan(text, SIGNAL_PATTERNS, "fabricated_signal")

    def _check_topic_forbidden(
        self, text: str, ctx: ValidationContext
    ) -> list[LyricViolationV1]:
        """Per-topic forbidden phrasings from the §14 topic graph.

        These are the *specific* ways each idea gets overclaimed — "always inverse" for
        the dollar relationship, "always holds" for a support level. A generic pattern
        list cannot capture them, which is why they live alongside the topic.
        """
        found: list[LyricViolationV1] = []
        for topic in (ctx.primary_topic, ctx.secondary_topic):
            if topic is None:
                continue
            for phrase in topic.forbidden:
                normalised = _normalise(phrase)
                if normalised and normalised in text:
                    found.append(
                        LyricViolationV1(
                            rule="topic_forbidden_phrasing",
                            message=(
                                f"{phrase!r} is listed as forbidden for the topic "
                                f"{topic.key!r} ({topic.certainty})"
                            ),
                            excerpt=phrase,
                            fatal=True,
                        )
                    )
        return found

    def _check_hedging(
        self, words: Sequence[str], ctx: ValidationContext
    ) -> list[LyricViolationV1]:
        """§14: a contextual or speculative claim must be qualified.

        Checked as the presence of *any* hedging vocabulary rather than per-sentence,
        because lyrics are not prose: a hedge in the hook qualifies the verse that follows
        it, and demanding one per line would produce stilted text.
        """
        found: list[LyricViolationV1] = []
        hedged = bool(set(words) & HEDGE_WORDS)
        for topic in (ctx.primary_topic, ctx.secondary_topic):
            if topic is None or not topic.requires_hedging:
                continue
            if not hedged:
                found.append(
                    LyricViolationV1(
                        rule="missing_hedge",
                        message=(
                            f"topic {topic.key!r} is {topic.certainty} and the lyric "
                            "contains no qualifying language; §14 forbids presenting an "
                            "uncertain market relationship as a fact"
                        ),
                        fatal=True,
                    )
                )
                # One violation is enough; both topics failing says the same thing.
                break
        return found

    def _check_speculative_direction(
        self, text: str, ctx: ValidationContext
    ) -> list[LyricViolationV1]:
        """§14: a speculative relationship may never imply a price direction."""
        topics = [
            topic
            for topic in (ctx.primary_topic, ctx.secondary_topic)
            if topic is not None and topic.forbids_direction
        ]
        if not topics:
            return []
        found: list[LyricViolationV1] = []
        for pattern in DIRECTIONAL_PATTERNS:
            match = re.search(pattern, text)
            if match is not None:
                found.append(
                    LyricViolationV1(
                        rule="speculative_direction",
                        message=(
                            f"implies a price direction while discussing "
                            f"{topics[0].key!r}, which is a speculative relationship"
                        ),
                        excerpt=_excerpt(text, match.start(), match.end()),
                        fatal=True,
                    )
                )
        return found

    # -- §17 quality -------------------------------------------------------

    def _check_gibberish(self, words: Sequence[str]) -> list[LyricViolationV1]:
        """Lexical diversity, with the threshold scaled by length.

        A hook legitimately repeats, so a flat threshold would reject well-formed short
        lyrics. The floor comes from the configured minimum, and the length-scaled table
        tightens it for longer texts where low diversity really does mean padding.
        """
        count = len(words)
        if count < 8:
            return []
        distinct = len(set(words))
        ratio = distinct / count
        threshold = self._settings.min_lexical_diversity
        for limit, required in _DIVERSITY_BY_LENGTH:
            if count <= limit:
                threshold = max(threshold, required)
                break
        if ratio < threshold:
            return [
                LyricViolationV1(
                    rule="low_lexical_diversity",
                    message=(
                        f"only {distinct} distinct words in {count} "
                        f"({ratio:.0%}, minimum {threshold:.0%}); reads as padding or "
                        "gibberish rather than a lyric"
                    ),
                    measured=round(ratio, 4),
                    threshold=round(threshold, 4),
                    fatal=True,
                )
            ]
        return []

    def _check_repetition(self, words: Sequence[str]) -> list[LyricViolationV1]:
        """§17: "contain excessive repeated phrases".

        Counts 4-word phrases rather than single words. Repeating a *word* is ordinary;
        repeating a four-word phrase eight times is a model stuck in a loop, which is the
        actual failure this catches.
        """
        if len(words) < 8:
            return []
        size = 4
        phrases = Counter(
            " ".join(words[index : index + size])
            for index in range(len(words) - size + 1)
        )
        worst, count = phrases.most_common(1)[0]
        limit = self._settings.max_phrase_repetitions
        if count > limit:
            return [
                LyricViolationV1(
                    rule="excessive_repetition",
                    message=(
                        f"the phrase {worst!r} appears {count} times "
                        f"(limit {limit}); the generator is looping"
                    ),
                    excerpt=worst,
                    measured=float(count),
                    threshold=float(limit),
                    fatal=True,
                )
            ]
        return []

    # -- branding ----------------------------------------------------------

    def _check_brand(self, text: str, ctx: ValidationContext) -> list[LyricViolationV1]:
        """§16: Trade Fix must appear naturally, and never be spammed."""
        actual = len(BRAND_PATTERN.findall(text))
        maximum = max(self._settings.tradefix_mention_weights)
        found: list[LyricViolationV1] = []
        if actual > maximum:
            found.append(
                LyricViolationV1(
                    rule="brand_spam",
                    message=(
                        f"Trade Fix appears {actual} times; the configured maximum is "
                        f"{maximum} (§16 forbids brand spam)"
                    ),
                    measured=float(actual),
                    threshold=float(maximum),
                    fatal=True,
                )
            )
        elif actual != ctx.expected_mentions:
            # Not fatal: a mismatch means the composer drifted from its brief, which is
            # worth reporting and regenerating on, but the lyric itself is not unsafe.
            found.append(
                LyricViolationV1(
                    rule="brand_count_mismatch",
                    message=(
                        f"Trade Fix appears {actual} times but the blueprint asked for "
                        f"{ctx.expected_mentions}"
                    ),
                    measured=float(actual),
                    threshold=float(ctx.expected_mentions),
                    fatal=False,
                )
            )
        return found

    def _check_blocklists(
        self, text: str, ctx: ValidationContext
    ) -> list[LyricViolationV1]:
        """Operator blocklists for the two §17 categories text cannot reveal."""
        found: list[LyricViolationV1] = []
        config = ctx.config
        for phrase in config.blocked_phrases:
            normalised = _normalise(phrase)
            if not normalised:
                continue
            occurrences = text.count(normalised)
            if occurrences >= config.blocked_phrase_tolerance:
                found.append(
                    LyricViolationV1(
                        rule="blocked_phrase",
                        message=(
                            f"contains the blocked phrase {phrase!r} "
                            f"{occurrences} time(s)"
                        ),
                        excerpt=phrase,
                        fatal=True,
                    )
                )
        for name in config.blocked_names:
            normalised = _normalise(name)
            if normalised and normalised in text:
                found.append(
                    LyricViolationV1(
                        rule="blocked_name",
                        message=(
                            f"references {name!r}; §86 forbids imitating or naming a "
                            "real artist"
                        ),
                        excerpt=name,
                        fatal=True,
                    )
                )
        return found

    # -- duplication -------------------------------------------------------

    def _duplication(
        self, lyrics: LyricsV1, ctx: ValidationContext
    ) -> tuple[float, str | None]:
        """Highest similarity against previous lyrics, and which track it was.

        Exact hash first — it is free and catches the literal repeat — then Jaccard over
        word shingles for the reworded case, which is the one that actually happens.
        """
        if lyrics.content_hash in ctx.previous_hashes:
            return 1.0, None

        shingles = lyrics.shingles()
        if not shingles or not ctx.previous_shingles:
            return 0.0, None

        best = 0.0
        closest: str | None = None
        for track_id, previous in ctx.previous_shingles:
            if not previous:
                continue
            union = len(shingles | previous)
            if not union:
                continue
            similarity = len(shingles & previous) / union
            if similarity > best:
                best, closest = similarity, track_id
        return best, closest


def _unique_line_words(lyrics: LyricsV1) -> list[str]:
    """Words from each *distinct section block*, in order of first appearance.

    Deduplication is by **block, not by line**, and the difference is the whole point.

    Two failures look identical in rendered text:

    * a chorus repeated four times — a song;
    * one line emitted nine times — a generator stuck in a loop.

    Collapsing by *line* catches the first and hides the second: the loop's nine identical
    lines become one and the repetition check finds nothing. Collapsing by *block* keeps
    them apart. A repeated chorus is a repeated block, counted once; a line repeated inside
    a single block is still nine lines, and both the diversity and repetition checks see
    them.

    Falls back to the full normalised text when the lyric carries no line breakdown — an
    externally supplied lyric may not have one.
    """
    blocks = _section_blocks(lyrics.text)
    if not blocks:
        return lyrics.normalised_text.split()

    seen: set[tuple[str, ...]] = set()
    words: list[str] = []
    for block in blocks:
        if block in seen:
            continue
        seen.add(block)
        for line_text in block:
            words.extend(line_text.split())
    return words or lyrics.normalised_text.split()


def _section_blocks(text: str) -> list[tuple[str, ...]]:
    """Split a lyric into blocks at its ``[section]`` markers.

    Parsed from the rendered text rather than from ``lyrics.lines``, because the text is
    the authoritative structure and ``lines`` is not.
    ``LyricLineV1`` records only a section *name*, so two consecutive ``[hook]`` sections
    are indistinguishable there from one long hook — and merging them made a legitimately
    repeated chorus look like an intra-section repeat, failing 49 of 600 composed lyrics.
    The ``[tag]`` markers have no such ambiguity.

    Also works for an externally supplied lyric that carries tags, and returns an empty
    list for one that does not, so the caller can fall back.
    """
    blocks: list[tuple[str, ...]] = []
    current: list[str] = []
    saw_marker = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SECTION_TAG_RE.match(line):
            saw_marker = True
            if current:
                blocks.append(tuple(current))
                current = []
            continue
        normalised = _normalise(line)
        if normalised:
            current.append(normalised)
    if current:
        blocks.append(tuple(current))
    return blocks if saw_marker else []


def _normalise(text: str) -> str:
    """Same normalisation the lyric contract applies, for comparing fragments."""
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _excerpt(text: str, start: int, end: int, window: int = 28) -> str:
    """The offending fragment plus surrounding context, for the §48 explanation."""
    left = max(0, start - window)
    right = min(len(text), end + window)
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{text[left:right]}{suffix}"[:200]


__all__ = [
    "BRAND_PATTERN",
    "DEFAULT_BLOCKED_PHRASES",
    "DIRECTIONAL_PATTERNS",
    "GUARANTEED_OUTCOME_PATTERNS",
    "HEDGE_WORDS",
    "PROFIT_PROMISE_PATTERNS",
    "SIGNAL_PATTERNS",
    "LyricValidator",
    "ValidationContext",
    "ValidatorConfig",
]
