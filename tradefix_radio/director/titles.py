"""Original track titles (§99).

§99: "Generate original titles. Avoid repeating generic titles such as Gold Rush, Bull
Run, Market Moves. Use title history. Reject excessive semantic similarity. Titles should
feel like real records."

Four requirements, each handled separately:

**Not generic.** A banned list holds exactly the clichés §99 names, plus the obvious
neighbours. Checked on the *normalised* form, so "Gold Rush!" and "gold rush" are both
caught.

**Not previously used.** Exact (normalised) duplicates are rejected against the full
title history, which includes titles claimed by *rejected* candidates — a rejected track
still consumed the idea.

**Not too similar to a recent title.** Token-overlap (Jaccard) against recent titles,
with a configurable ceiling. This is what stops "Liquidity After Midnight" being followed
by "Liquidity Before Midnight".

**Sounds like a record.** The grammar is built from phrase shapes real song titles use —
a noun phrase, a prepositional phrase, a short clause — rather than from a template like
``{adjective} {noun}``, which produces output that reads as generated. Vocabulary is
drawn from the market and the lyric topic so a title is *about* the track.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from tradefix_radio.contracts.enums import MarketDirection, MarketRegime, TradingSession
from tradefix_radio.director.selection import WeightedSelector

_WORD_RE = re.compile(r"[a-z0-9']+")

#: Titles §99 names explicitly, plus the obvious neighbours. Normalised form.
BANNED_TITLES: frozenset[str] = frozenset(
    {
        "gold rush",
        "bull run",
        "market moves",
        "to the moon",
        "diamond hands",
        "buy the dip",
        "money moves",
        "golden hour",
        "bulls and bears",
        "risk on",
        "risk off",
        "stacking paper",
        "secure the bag",
        "profit season",
        "trading places",
        "market mayhem",
        "chart life",
        "candle light",
    }
)

#: Words that make a title sound like financial clip-art. Not banned outright — "gold"
#: is legitimately useful — but heavily penalised so they stay rare.
CLICHE_WORDS: frozenset[str] = frozenset(
    {"rush", "moon", "rocket", "lambo", "millionaire", "rich", "profit", "bag"}
)


@dataclass(frozen=True)
class TitleContext:
    """What the title should be about."""

    regime: MarketRegime
    direction: MarketDirection
    session: TradingSession
    energy: float
    genre: str
    primary_topic: str | None = None
    instrumental: bool = False


# ---------------------------------------------------------------- vocabulary
#
# Grouped by the *feeling* they carry rather than by part of speech, so a phrase shape
# can request "something cold" and get a coherent set. Deliberately avoids trading
# jargon as the primary vocabulary: a title built from "liquidity sweep confirmation"
# reads like a report, not a record.

_TIME_NOUNS = (
    "midnight", "the open", "the close", "first light", "the small hours",
    "last orders", "the late tape", "sunrise", "the overlap", "the handover",
)

_PLACE_NOUNS = (
    "the floor", "the desk", "the wire", "the ledger", "the margin",
    "the terminal", "the back office", "the long room", "the night desk",
)

_ABSTRACT_NOUNS = (
    "patience", "discipline", "silence", "pressure", "conviction", "doubt",
    "distance", "appetite", "attention", "momentum", "weight", "gravity",
    "hesitation", "arithmetic", "consequence", "instinct",
)

_QUIET_ADJECTIVES = (
    "quiet", "narrow", "still", "patient", "soft", "slow", "thin", "shallow",
    "unhurried", "dim",
)

_TENSE_ADJECTIVES = (
    "tight", "coiled", "held", "waiting", "wound", "compressed", "loaded",
)

_LOUD_ADJECTIVES = (
    "wide", "violent", "open", "loud", "sharp", "heavy", "fast", "severe",
    "relentless", "vertical",
)

_QUIET_VERBS = ("drifting", "waiting", "holding", "settling", "breathing", "listening")
_TENSE_VERBS = ("coiling", "loading", "narrowing", "building", "gathering")
_LOUD_VERBS = ("breaking", "running", "expanding", "tearing", "lifting", "falling")

_PREPOSITIONS = ("after", "before", "under", "against", "beyond", "without", "through")

#: Session flavour words, used sparingly so a title is not a timestamp.
_SESSION_WORDS: dict[TradingSession, tuple[str, ...]] = {
    TradingSession.ASIAN: ("tokyo", "the east", "the quiet hours"),
    TradingSession.SYDNEY: ("the first bell", "the early tape"),
    TradingSession.ASIAN_LONDON_OVERLAP: ("the handover", "two clocks"),
    TradingSession.LONDON: ("london", "the fix", "the morning desk"),
    TradingSession.LONDON_NEW_YORK_OVERLAP: ("the overlap", "both desks", "the deep end"),
    TradingSession.NEW_YORK: ("new york", "the afternoon", "the data run"),
    TradingSession.NEW_YORK_LATE: ("the late tape", "last orders", "the thin hour"),
    TradingSession.CLOSED: ("the closed book", "the weekend", "no bid"),
}


#: Topic-key fragments that must not reach a title: desk acronyms, and the glue words a
#: snake_case identifier uses to join its parts.
#:
#: An acronym is unmistakably jargon in a title — "Dxy Strength" reads as a report field,
#: not a record — and no length rule separates them from the words that *are* wanted
#: ("risk", "gold", "fear" are all as short as "dxy").
NON_TITLE_FRAGMENTS: frozenset[str] = frozenset(
    {"dxy", "fomo", "cpi", "nfp", "pmi", "etf", "per", "and", "the", "vs", "of"}
)


#: Preposition that reads correctly before a given session phrase; "at" otherwise.
#:
#: A single fixed preposition cannot work across these: "at the Fix" is right and "at the
#: Afternoon" is wrong, and English has no rule here to derive — only usage.
_SESSION_PREPOSITION: dict[str, str] = {
    "the afternoon": "in",
    "the quiet hours": "in",
    "the thin hour": "in",
    "the deep end": "in",
    "the closed book": "in",
    "the weekend": "on",
    "the early tape": "on",
    "the late tape": "on",
    "the data run": "during",
    "two clocks": "between",
    "both desks": "across",
}


def topic_words(primary_topic: str | None) -> tuple[str, ...]:
    """Title-usable words from a §14 topic key.

    Topic keys are snake_case identifiers (``risk_management``, ``treasury_yields``), so
    their words already state the subject matter plainly — which is what a title wants.

    Two filters. Cliché words go because a topic named for profit-taking must not hand
    "profit" back as title vocabulary; §99's whole point is that a title should not read as
    financial clip-art. Acronyms and glue words go per :data:`NON_TITLE_FRAGMENTS`.
    """
    if not primary_topic:
        return ()
    return tuple(
        word
        for word in primary_topic.replace("-", "_").split("_")
        if word and word not in CLICHE_WORDS and word not in NON_TITLE_FRAGMENTS
    )


def normalise_title(title: str) -> str:
    """Comparison form: lowercase, alphanumeric words, single-spaced.

    Shared with the persistence layer's title history so both sides compare identically.
    """
    return " ".join(_WORD_RE.findall(title.lower()))


def title_similarity(first: str, second: str) -> float:
    """Jaccard overlap of the two titles' word sets, 0–1.

    Word sets rather than character n-grams, because the repetition a listener notices in
    a title list is a shared *word* ("Liquidity After Midnight" / "Liquidity Before
    Midnight"), not a shared substring.
    """
    left = set(normalise_title(first).split())
    right = set(normalise_title(second).split())
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _mood_vocabulary(energy: float, regime: MarketRegime) -> tuple[
    Sequence[str], Sequence[str]
]:
    """Adjectives and verbs matching the market's character."""
    if regime in {MarketRegime.COMPRESSION, MarketRegime.BREAKOUT_BUILDUP}:
        return _TENSE_ADJECTIVES, _TENSE_VERBS
    if energy >= 68:
        return _LOUD_ADJECTIVES, _LOUD_VERBS
    if energy <= 32:
        return _QUIET_ADJECTIVES, _QUIET_VERBS
    # Mid energy draws on both, which is what keeps the middle of the range from having
    # its own obvious house style.
    return (*_QUIET_ADJECTIVES, *_LOUD_ADJECTIVES), (*_QUIET_VERBS, *_LOUD_VERBS)


class TitleGenerator:
    """Builds original titles and rejects the ones §99 forbids."""

    def __init__(
        self,
        selector: WeightedSelector,
        *,
        similarity_ceiling: float = 0.5,
        max_attempts: int = 40,
    ) -> None:
        if not 0.0 < similarity_ceiling <= 1.0:
            raise ValueError("similarity_ceiling must be within (0, 1]")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._selector = selector
        self._ceiling = similarity_ceiling
        self._max_attempts = max_attempts

    # -- generation --------------------------------------------------------

    def generate(
        self,
        context: TitleContext,
        *,
        used_titles: Iterable[str] = (),
        recent_titles: Sequence[str] = (),
    ) -> str:
        """Produce a title that is not banned, used, or too similar to a recent one.

        Falls back to the least-similar candidate after ``max_attempts`` rather than
        raising or looping forever: a track with an imperfect title is far better than a
        scheduling stall, and §99's requirement is "avoid", not "never".
        """
        used = {normalise_title(title) for title in used_titles}
        best: tuple[float, str] | None = None

        for _ in range(self._max_attempts):
            candidate = self._compose(context)
            normalised = normalise_title(candidate)

            if normalised in BANNED_TITLES or normalised in used:
                continue

            worst_similarity = max(
                (title_similarity(candidate, previous) for previous in recent_titles),
                default=0.0,
            )
            if worst_similarity <= self._ceiling:
                return candidate
            if best is None or worst_similarity < best[0]:
                best = (worst_similarity, candidate)

        if best is not None:
            return best[1]
        # Every attempt was banned or already used. Append a distinguishing word drawn
        # from the abstract pool rather than a number: "Patience (2)" reads as a bug.
        base = self._compose(context)
        suffix = self._selector.choose_from(_ABSTRACT_NOUNS)
        return f"{base} in {suffix.title()}"

    def _compose(self, context: TitleContext) -> str:
        """Build one candidate from a randomly chosen phrase shape."""
        adjectives, verbs = _mood_vocabulary(context.energy, context.regime)
        session_words = _SESSION_WORDS.get(context.session, ())
        topic = topic_words(context.primary_topic)

        # Shapes paired with weights rather than kept in a dict keyed by bound method:
        # method objects make poor dict keys for the type checker, and the pairing reads
        # better anyway.
        #
        # Session-anchored titles are good but must stay rare, or every title becomes a
        # timestamp. Bare abstracts are effective but weighted down for the same reason.
        shapes: tuple[tuple[str, float], ...] = (
            ("noun_prepositional", 1.0),
            ("adjective_noun", 1.0),
            ("verb_phrase", 1.0),
            ("bare_abstract", 0.7),
            ("session_anchored", 0.5 if session_words else 0.0),
            ("negation", 1.0),
            # Topic-anchored titles make the title about the *track*, which is the
            # difference between a title and a mood. Weighted below the mood shapes so
            # the station does not announce its subject matter every time.
            ("topic_anchored", 0.8 if topic else 0.0),
        )
        name = self._selector.choose_from(
            [shape for shape, _ in shapes],
            weight_of=lambda key: dict(shapes)[key],
        )
        builders = {
            "noun_prepositional": self._shape_noun_prepositional,
            "adjective_noun": self._shape_adjective_noun,
            "verb_phrase": self._shape_verb_phrase,
            "bare_abstract": self._shape_bare_abstract,
            "session_anchored": self._shape_session_anchored,
            "negation": self._shape_negation,
            "topic_anchored": self._shape_topic_anchored,
        }
        title = builders[name](context, adjectives, verbs, session_words, topic)
        return _titlecase(title)

    # -- phrase shapes -----------------------------------------------------
    #
    # Each returns lowercase text; casing is applied once at the end.

    def _shape_noun_prepositional(
        self,
        _context: TitleContext,
        adjectives: Sequence[str],
        _verbs: Sequence[str],
        _session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"Liquidity After Midnight" — noun + preposition + noun."""
        head = self._selector.choose_from((*_ABSTRACT_NOUNS, *_PLACE_NOUNS))
        preposition = self._selector.choose_from(_PREPOSITIONS)
        tail = self._selector.choose_from((*_TIME_NOUNS, *_PLACE_NOUNS))
        return self._avoid_echo(f"{head} {preposition} {tail}", adjectives)

    def _shape_adjective_noun(
        self,
        _context: TitleContext,
        adjectives: Sequence[str],
        _verbs: Sequence[str],
        _session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"Narrow Hours" / "The Patient Late Tape" — adjective + noun."""
        noun = self._selector.choose_from((*_ABSTRACT_NOUNS, *_TIME_NOUNS, *_PLACE_NOUNS))
        return _insert_adjective(self._adjective_for(noun, adjectives), noun)

    def _shape_verb_phrase(
        self,
        _context: TitleContext,
        _adjectives: Sequence[str],
        verbs: Sequence[str],
        _session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"Holding the Line" — gerund phrase."""
        verb = self._selector.choose_from(verbs)
        noun = self._selector.choose_from((*_PLACE_NOUNS, *_ABSTRACT_NOUNS))
        return f"{verb} {noun}"

    def _shape_bare_abstract(
        self,
        _context: TitleContext,
        _adjectives: Sequence[str],
        _verbs: Sequence[str],
        _session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"Arithmetic" — a single word. Sparse and effective, so weighted down."""
        return self._selector.choose_from(_ABSTRACT_NOUNS)

    def _shape_session_anchored(
        self,
        context: TitleContext,
        adjectives: Sequence[str],
        _verbs: Sequence[str],
        session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"Quiet Hours in Tokyo" — adjective + noun + session."""
        if not session:
            return self._shape_adjective_noun(context, adjectives, (), (), ())
        noun = self._selector.choose_from(_ABSTRACT_NOUNS)
        place = self._selector.choose_from(session)
        adjective = self._adjective_for(f"{noun} {place}", adjectives)
        preposition = _SESSION_PREPOSITION.get(place, "at")
        return f"{adjective} {noun} {preposition} {place}"

    def _shape_negation(
        self,
        _context: TitleContext,
        _adjectives: Sequence[str],
        _verbs: Sequence[str],
        _session: Sequence[str],
        _topic: Sequence[str],
    ) -> str:
        """"No Bid Before Dawn" — a negation, which reads very much like a record."""
        noun = self._selector.choose_from(_ABSTRACT_NOUNS)
        preposition = self._selector.choose_from(_PREPOSITIONS)
        tail = self._selector.choose_from((*_TIME_NOUNS, *_PLACE_NOUNS))
        return f"no {noun} {preposition} {tail}"

    def _shape_topic_anchored(
        self,
        context: TitleContext,
        adjectives: Sequence[str],
        _verbs: Sequence[str],
        _session: Sequence[str],
        topic: Sequence[str],
    ) -> str:
        """"Patient Risk Management" — the track's §14 subject, given a mood.

        The one shape that makes the title about the *track* rather than about the market
        mood. Falls back to the adjective/noun shape when the topic contributed no usable
        words, which happens whenever a topic key is entirely cliché vocabulary.
        """
        if not topic:
            return self._shape_adjective_noun(context, adjectives, (), (), ())
        subject = " ".join(topic)
        # Two sub-shapes so a run of topic-anchored titles does not all read identically.
        if self._selector.rng.random() < 0.5:
            adjective = self._adjective_for(subject, adjectives)
            return self._avoid_echo(_insert_adjective(adjective, subject), adjectives)
        preposition = self._selector.choose_from(_PREPOSITIONS)
        tail = self._selector.choose_from((*_TIME_NOUNS, *_PLACE_NOUNS))
        return self._avoid_echo(f"{subject} {preposition} {tail}", adjectives)

    def _avoid_echo(self, title: str, adjectives: Sequence[str]) -> str:
        """Replace a repeated word rather than emitting "Pressure Under Pressure".

        Two details that a simpler version got wrong, both found by the bulk tests:

        * Minor words are exempt. "The Desk Beyond the Night Desk" repeats "the"
          legitimately; rewriting it produced "The Desk Beyond Fast Night Desk".
        * Each replacement is drawn afresh and checked against the words already placed.
          Reusing one replacement for two repeats simply moved the duplicate:
          "The Desk Beyond Fast Night Fast".
        """
        words = title.split()
        seen: set[str] = set()
        out: list[str] = []
        for word in words:
            if word in _MINOR_WORDS or word not in seen:
                out.append(word)
                seen.add(word)
                continue
            out.append(self._fresh_word(adjectives, seen))
            seen.add(out[-1])
        return " ".join(out)

    def _adjective_for(self, noun: str, adjectives: Sequence[str]) -> str:
        """An adjective that is not already one of ``noun``'s own words.

        The pools overlap: "open" is a loud adjective and also the whole of "the open",
        so the naive independent pick produced "The Open Open". Checking at selection time
        is better than repairing afterwards, because the repair would replace the *noun*
        and change what the title is about.
        """
        return self._fresh_word(adjectives, set(noun.split()))

    def _fresh_word(self, pool: Sequence[str], taken: set[str]) -> str:
        """A word from ``pool`` not already in ``taken``, if one exists."""
        available = [word for word in pool if word not in taken]
        if not available:
            # Every adjective is already in the title — impossible for real pools, but
            # falling back keeps the function total rather than raising on an empty choice.
            return self._selector.choose_from(_ABSTRACT_NOUNS)
        return self._selector.choose_from(available)


#: Words kept lowercase in title case, as real record titles do.
_MINOR_WORDS = frozenset(
    {"a", "an", "and", "at", "the", "in", "of", "on", "to", "for", "no", "before",
     "after", "under", "against", "beyond", "without", "through", "during", "between",
     "across"}
)


def _insert_adjective(adjective: str, noun: str) -> str:
    """Place ``adjective`` grammatically, even when ``noun`` carries its own article.

    Several noun pools are whole noun *phrases* ("the late tape", "the night desk"), and
    the obvious ``f"{adjective} {noun}"`` produced "Patient the Late Tape" — ungrammatical
    in a way that immediately reads as generated, which is precisely what §99's "should
    feel like real records" rules out.
    """
    if noun.startswith("the "):
        return f"the {adjective} {noun[4:]}"
    return f"{adjective} {noun}"


def _titlecase(text: str) -> str:
    """Title-case with minor words lowercased, except in first position."""
    words = text.split()
    out: list[str] = []
    for index, word in enumerate(words):
        if index > 0 and word in _MINOR_WORDS:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def is_cliche(title: str) -> bool:
    """Whether a title is banned outright or built from cliché vocabulary."""
    normalised = normalise_title(title)
    if normalised in BANNED_TITLES:
        return True
    return bool(set(normalised.split()) & CLICHE_WORDS)


__all__ = [
    "BANNED_TITLES",
    "CLICHE_WORDS",
    "NON_TITLE_FRAGMENTS",
    "TitleContext",
    "TitleGenerator",
    "is_cliche",
    "normalise_title",
    "title_similarity",
    "topic_words",
]
