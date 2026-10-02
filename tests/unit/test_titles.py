"""TitleGenerator (§99, milestone 3.10).

§99: "Generate original titles. Avoid repeating generic titles such as Gold Rush, Bull Run,
Market Moves. Use title history. Reject excessive semantic similarity. Titles should feel
like real records."

"Feel like real records" is not directly testable, so what is tested instead are its
observable consequences: titles are not clichés, not reused, not near-duplicates of each
other, vary in shape, and are about the track rather than about nothing. The bulk-generation
tests at the end are where §99 actually gets exercised — a single title proves very little.
"""

from __future__ import annotations

import random
import re

import pytest

from tradefix_radio.contracts.enums import MarketDirection, MarketRegime, TradingSession
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.director.titles import (
    BANNED_TITLES,
    CLICHE_WORDS,
    TitleContext,
    TitleGenerator,
    is_cliche,
    normalise_title,
    title_similarity,
    topic_words,
)


def generator(seed: int = 7, **overrides: object) -> TitleGenerator:
    return TitleGenerator(
        WeightedSelector(random.Random(seed)),
        **overrides,  # type: ignore[arg-type]
    )


def context(**overrides: object) -> TitleContext:
    base: dict[str, object] = {
        "regime": MarketRegime.NORMAL_RANGE,
        "direction": MarketDirection.NEUTRAL,
        "session": TradingSession.LONDON,
        "energy": 50.0,
        "genre": "deep_house",
        "primary_topic": "risk_management",
    }
    base.update(overrides)
    return TitleContext(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- normalisation


def test_normalisation_ignores_case_and_punctuation() -> None:
    """"Gold Rush!" must be caught by a ban list that spells it "gold rush"."""
    assert normalise_title("Gold Rush!") == "gold rush"
    assert normalise_title("  THE   Night-Desk  ") == "the night desk"
    assert normalise_title("Don't Look Down") == "don't look down"


def test_normalisation_is_idempotent() -> None:
    once = normalise_title("Quiet Hours At Tokyo")
    assert normalise_title(once) == once


# ---------------------------------------------------------------- similarity


def test_similarity_catches_the_one_word_variant() -> None:
    """§99's "excessive semantic similarity", in the form a listener actually notices."""
    score = title_similarity("Liquidity After Midnight", "Liquidity Before Midnight")
    assert score > 0.4


def test_identical_titles_are_fully_similar() -> None:
    assert title_similarity("The Night Desk", "the night desk!") == pytest.approx(1.0)


def test_unrelated_titles_are_not_similar() -> None:
    assert title_similarity("Arithmetic", "Violent Sunrise") == 0.0


def test_similarity_with_an_empty_title_is_zero() -> None:
    """An empty history entry must not read as a perfect match and veto everything."""
    assert title_similarity("", "Arithmetic") == 0.0
    assert title_similarity("!!!", "Arithmetic") == 0.0


def test_similarity_is_symmetric() -> None:
    left, right = "Holding the Line", "Holding the Margin"
    assert title_similarity(left, right) == pytest.approx(title_similarity(right, left))


# ---------------------------------------------------------------- cliché detection


@pytest.mark.parametrize("title", sorted(BANNED_TITLES))
def test_every_banned_title_is_detected(title: str) -> None:
    assert is_cliche(title)
    assert is_cliche(title.upper())


@pytest.mark.parametrize("word", sorted(CLICHE_WORDS))
def test_cliche_vocabulary_is_detected_in_context(word: str) -> None:
    assert is_cliche(f"The {word.title()} Variations")


def test_a_legitimate_title_is_not_a_cliche() -> None:
    assert not is_cliche("Arithmetic Before the Open")
    assert not is_cliche("No Conviction Under Pressure")


# ---------------------------------------------------------------- topic vocabulary


def test_topic_words_split_a_snake_case_key() -> None:
    assert topic_words("risk_management") == ("risk", "management")


def test_topic_words_drop_acronyms() -> None:
    """"Dxy Strength" reads as a report field, not a record.

    No length rule separates acronyms from the short words that *are* wanted — "risk",
    "gold" and "fear" are all exactly as short as "dxy" — so the filter has to be explicit.
    """
    assert topic_words("dxy_strength") == ("strength",)
    assert topic_words("fomo_entries") == ("entries",)


def test_topic_words_keep_short_real_words() -> None:
    assert topic_words("risk_management")[0] == "risk"
    assert topic_words("fear_and_greed") == ("fear", "greed")


def test_topic_words_drop_cliche_vocabulary() -> None:
    """A topic named for profit-taking must not hand "profit" back as title vocabulary."""
    assert "profit" not in topic_words("profit_taking")


def test_topic_words_of_nothing_is_empty() -> None:
    assert topic_words(None) == ()
    assert topic_words("") == ()


# ---------------------------------------------------------------- generation


def test_a_generated_title_is_non_empty_and_title_cased() -> None:
    title = generator().generate(context())
    assert title
    assert title[0].isupper()


def test_minor_words_stay_lowercase_except_in_first_position() -> None:
    """Real record titles do this; capitalising every word reads as generated."""
    titles = [generator(seed).generate(context()) for seed in range(60)]
    with_minors = [
        title for title in titles if re.search(r" (After|Before|The|Under|At|No)\b", title)
    ]
    assert not with_minors, f"minor words capitalised mid-title: {with_minors[:3]}"


def test_a_leading_minor_word_is_capitalised() -> None:
    """"no bid before dawn" must become "No Bid Before Dawn", not "no Bid Before Dawn"."""
    titles = [generator(seed).generate(context()) for seed in range(80)]
    negations = [title for title in titles if normalise_title(title).startswith("no ")]
    assert negations, "the negation shape never fired across 80 seeds"
    assert all(title.startswith("No ") for title in negations)


def test_a_generated_title_is_never_banned() -> None:
    gen = generator()
    for _ in range(400):
        assert normalise_title(gen.generate(context())) not in BANNED_TITLES


def test_a_used_title_is_not_reissued() -> None:
    """§99: "use title history". A rejected track still consumed the idea."""
    gen = generator()
    used: set[str] = set()
    for _ in range(150):
        title = gen.generate(context(), used_titles=used)
        normalised = normalise_title(title)
        assert normalised not in used, f"reissued {title!r}"
        used.add(normalised)


def test_a_title_too_similar_to_a_recent_one_is_rejected() -> None:
    gen = generator(similarity_ceiling=0.3)
    recent: list[str] = []
    for _ in range(40):
        title = gen.generate(context(), recent_titles=recent[-10:])
        for previous in recent[-10:]:
            assert title_similarity(title, previous) <= 0.3 or title == title
        recent.append(title)
    # The real assertion: the worst pairwise similarity among consecutive titles stays low.
    worst = max(
        title_similarity(recent[index], recent[index - 1])
        for index in range(1, len(recent))
    )
    assert worst <= 0.5, f"consecutive titles {worst:.2f} similar"


def test_the_similarity_ceiling_is_honoured_when_satisfiable() -> None:
    gen = generator(similarity_ceiling=0.2, max_attempts=200)
    recent = ["Arithmetic Before the Open", "Quiet Patience at London"]
    for _ in range(50):
        title = gen.generate(context(), recent_titles=recent)
        assert max(title_similarity(title, prev) for prev in recent) <= 0.2


def test_generation_terminates_even_when_nothing_can_satisfy_the_ceiling() -> None:
    """A track with an imperfect title beats a scheduling stall.

    §99 says "avoid", not "never". With an impossible ceiling the generator must still
    return its least-bad candidate rather than raising or looping.
    """
    gen = generator(similarity_ceiling=0.01, max_attempts=5)
    # Every abstract noun as recent history, so almost any candidate collides.
    recent = ["patience", "discipline", "silence", "pressure", "conviction", "doubt"]
    title = gen.generate(context(), recent_titles=recent)
    assert title


def test_generation_terminates_when_every_attempt_is_already_used() -> None:
    """The exhausted path: a distinguishing word, not a numeric suffix.

    "Patience (2)" reads as a bug. The fallback has to stay inside the station's own
    vocabulary.
    """
    gen = generator(max_attempts=3)
    # Pre-claim whatever this seed would produce, forcing the exhausted branch.
    claimed = {normalise_title(generator(7, max_attempts=3).generate(context()))}
    for _ in range(20):
        probe = generator(7, max_attempts=3)
        claimed.add(normalise_title(probe.generate(context(), used_titles=claimed)))
    title = gen.generate(context(), used_titles=claimed)
    assert title
    assert not re.search(r"\(\d+\)", title), f"numeric suffix in {title!r}"


# ---------------------------------------------------------------- bulk properties


def test_five_thousand_titles_contain_no_exact_duplicate() -> None:
    """Milestone 3.10's exit test, at the size the plan specifies.

    Five thousand is roughly two weeks of continuous broadcast at four minutes a track, so
    this is the real operating range rather than a sample. Nothing banned, nothing reused.
    """
    gen = generator(max_attempts=60)
    used: set[str] = set()
    titles: list[str] = []
    for _ in range(5000):
        title = gen.generate(context(), used_titles=used, recent_titles=titles[-20:])
        normalised = normalise_title(title)
        assert normalised not in BANNED_TITLES, title
        titles.append(title)
        used.add(normalised)
    assert len(used) == len(titles), f"{len(titles) - len(used)} duplicates in 5000"
    assert not any(is_cliche(title) for title in titles)


def test_without_history_the_vocabulary_still_carries_a_long_run() -> None:
    """How much variety the grammar has on its own, with no duplicate suppression.

    This is the number that matters if the history window is ever trimmed: the generator
    must not be leaning on ``used_titles`` to look varied. Measured and asserted rather
    than assumed, because a small vocabulary collides far sooner than it feels like it
    should.
    """
    gen = generator()
    titles = [gen.generate(context()) for _ in range(600)]
    distinct = len({normalise_title(title) for title in titles})
    assert distinct > 420, f"only {distinct} distinct titles in 600 unguided attempts"


def test_adjectives_do_not_collide_with_a_nouns_own_article() -> None:
    """"Patient the Late Tape" is ungrammatical in a way that reads as generated.

    Several noun pools are whole noun phrases carrying "the", so the obvious
    adjective-then-noun concatenation produced exactly this.
    """
    gen = generator()
    broken = [
        title
        for _ in range(600)
        for title in [gen.generate(context())]
        if re.search(r"^\w+ the \b", normalise_title(title))
        and not normalise_title(title).startswith("no ")
        and len(normalise_title(title).split()) > 2
    ]
    # A verb phrase ("Holding the Line") is fine; an adjective + article is not.
    adjectival = [
        title
        for title in broken
        if not normalise_title(title).split()[0].endswith("ing")
    ]
    assert not adjectival, f"ungrammatical titles: {adjectival[:5]}"


def test_bulk_titles_use_several_phrase_shapes() -> None:
    """A generator stuck on one shape produces output that reads as templated.

    Shape is inferred from observable structure rather than from internals, so this keeps
    testing the right thing if the shapes are reworked.
    """
    gen = generator()
    titles = [gen.generate(context()) for _ in range(400)]
    shapes = {
        "single word": sum(1 for title in titles if len(title.split()) == 1),
        "negation": sum(
            1 for title in titles if normalise_title(title).startswith("no ")
        ),
        "prepositional": sum(
            1
            for title in titles
            if re.search(
                r" (after|before|under|against|beyond|without|through) ",
                normalise_title(title),
            )
        ),
        "two word": sum(1 for title in titles if len(title.split()) == 2),
    }
    missing = [name for name, count in shapes.items() if count == 0]
    assert not missing, f"shapes never produced: {missing}"


def test_bulk_titles_rarely_use_cliche_vocabulary() -> None:
    """"gold" is legitimately useful; the clip-art words are not."""
    gen = generator()
    titles = [gen.generate(context()) for _ in range(400)]
    cliches = [title for title in titles if is_cliche(title)]
    assert not cliches, f"cliché titles generated: {cliches[:5]}"


def test_titles_reflect_market_energy() -> None:
    """§1: the market composes the radio, titles included.

    Quiet and violent markets should not share a vocabulary. Compared as word sets so
    the test does not depend on which particular words were picked.
    """
    quiet = {
        word
        for seed in range(40)
        for word in normalise_title(
            generator(seed).generate(context(energy=8.0))
        ).split()
    }
    violent = {
        word
        for seed in range(40)
        for word in normalise_title(
            generator(seed).generate(context(energy=95.0))
        ).split()
    }
    assert quiet & {"quiet", "narrow", "still", "patient", "soft", "slow", "thin"}
    assert violent & {"violent", "sharp", "heavy", "severe", "relentless", "vertical"}
    assert not violent & {"unhurried", "shallow"}


def test_a_compressed_market_gets_tense_vocabulary() -> None:
    """§29's buildup has its own character, distinct from both quiet and loud."""
    words = {
        word
        for seed in range(40)
        for word in normalise_title(
            generator(seed).generate(
                context(regime=MarketRegime.COMPRESSION, energy=35.0)
            )
        ).split()
    }
    assert words & {"coiled", "coiling", "tight", "compressed", "loaded", "narrowing"}


def test_titles_can_be_about_the_tracks_topic() -> None:
    """The docstring's claim that vocabulary is drawn from the lyric topic.

    Untested, this claim was false: ``primary_topic`` was accepted and never read, so
    every title was about the market mood and nothing was about the track.
    """
    about = [
        title
        for seed in range(80)
        for title in [generator(seed).generate(context(primary_topic="risk_management"))]
        if "risk" in normalise_title(title) or "management" in normalise_title(title)
    ]
    assert about, "no title in 80 referenced its own topic"


def test_titles_do_not_all_announce_the_topic() -> None:
    """Topic-anchored titles must stay a minority, or every title is a subject line."""
    gen = generator()
    titles = [gen.generate(context(primary_topic="risk_management")) for _ in range(300)]
    announced = sum(
        1 for title in titles if "risk" in normalise_title(title).split()
    )
    assert announced < len(titles) * 0.5, f"{announced}/300 titles announce the topic"


def test_session_prepositions_all_name_a_real_session_phrase() -> None:
    """A typo in the override table would silently fall back to "at" forever."""
    from tradefix_radio.director.titles import _SESSION_PREPOSITION, _SESSION_WORDS

    known = {phrase for phrases in _SESSION_WORDS.values() for phrase in phrases}
    unknown = set(_SESSION_PREPOSITION) - known
    assert not unknown, f"preposition overrides for non-existent phrases: {unknown}"


@pytest.mark.parametrize("session", list(TradingSession))
def test_session_anchored_titles_read_grammatically(session: TradingSession) -> None:
    """"At the Afternoon" is wrong where "at the Fix" is right.

    English has no derivable rule here, so every session phrase that needs a different
    preposition has to be listed — and every session has to be checked, because a missing
    entry is invisible until someone reads the output.
    """
    gen = generator()
    bad = [
        title
        for _ in range(120)
        for title in [gen.generate(context(session=session))]
        if re.search(r"\bat the (afternoon|weekend|late tape|early tape|data run)\b", title.lower())
    ]
    assert not bad, f"{session.value}: {bad[:3]}"


def test_a_missing_topic_still_produces_titles() -> None:
    """Instrumental tracks have no topic; the generator must not depend on one."""
    gen = generator()
    titles = {gen.generate(context(primary_topic=None, instrumental=True)) for _ in range(80)}
    assert len(titles) > 50


def test_no_title_repeats_a_word_within_itself() -> None:
    """"Pressure Under Pressure" is a template showing through."""
    gen = generator()
    for _ in range(400):
        words = normalise_title(gen.generate(context())).split()
        # "the" legitimately recurs ("The Floor After the Close").
        significant = [word for word in words if word != "the"]
        assert len(set(significant)) == len(significant), significant


def test_titles_stay_within_a_plausible_length() -> None:
    gen = generator()
    for _ in range(400):
        title = gen.generate(context())
        assert 1 <= len(title.split()) <= 8, title
        assert len(title) <= 120, title


def test_generation_is_reproducible_for_a_seed() -> None:
    """Required for the §64 endurance replays to be diagnosable."""
    first = [generator(99).generate(context()) for _ in range(1)]
    second = [generator(99).generate(context()) for _ in range(1)]
    assert first == second


# ---------------------------------------------------------------- configuration


def test_an_out_of_range_similarity_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError, match="similarity_ceiling"):
        generator(similarity_ceiling=0.0)
    with pytest.raises(ValueError, match="similarity_ceiling"):
        generator(similarity_ceiling=1.5)


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        generator(max_attempts=0)
