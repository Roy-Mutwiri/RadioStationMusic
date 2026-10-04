"""Lyric safety validation (§17, §14, milestone 3.9).

Milestone 3.9's exit test: "a labelled corpus of bad lyrics is **100 % rejected**; good
lyrics pass; each rule has its own case."

The corpus is the point. A validator tested only against well-formed input proves nothing
about detection, and §17's categories are exactly the ones where a false negative is
expensive — the station would broadcast a guaranteed-profit claim.

Two honesty notes carried from the module itself:

* The two §17 categories that cannot be detected from text alone — copyrighted lyrics and
  imitating a living artist — are tested as **blocklist** behaviour, not as detection.
  There is no test asserting that arbitrary copyrighted text is caught, because it is not,
  and a test that pretended otherwise would be worse than none.
* Hedging is checked per lyric rather than per sentence, so the tests assert that
  behaviour rather than a stricter one the implementation does not provide.
"""

from __future__ import annotations

import pytest

from tradefix_radio.config.schema import LyricsSettings
from tradefix_radio.contracts.lyrics import LyricLineV1, LyricsV1
from tradefix_radio.director.library import TopicDefinition
from tradefix_radio.lyrics.validators import (
    LyricValidator,
    ValidationContext,
    ValidatorConfig,
)


def settings(**overrides: object) -> LyricsSettings:
    return LyricsSettings.model_validate(overrides)


def validator(**overrides: object) -> LyricValidator:
    return LyricValidator(settings(**overrides))


def topic(
    key: str = "discipline",
    *,
    certainty: str = "established",
    forbidden: tuple[str, ...] = (),
    category: str = "psychology",
) -> TopicDefinition:
    return TopicDefinition.model_validate(
        {
            "key": key,
            "category": category,
            "label": key.title(),
            "certainty": certainty,
            "weight": 1.0,
            "educational": True,
            "teaching_points": ("a true thing about trading",),
            "phrases": ("a lyric-ready phrase",),
            "forbidden": forbidden,
        }
    )


#: A lyric long and varied enough to clear every quality threshold, so a test that adds
#: one bad line is isolating that line.
GOOD_BODY = (
    "I wrote the rule before the candle got loud",
    "the stop is where I am wrong not where it hurts",
    "size comes from the distance, never from the feeling",
    "a plan you abandon under pressure was never a plan",
    "flat is a position and it costs me nothing",
    "my notes said the same thing last week",
    "discipline is doing this when it is boring",
    "I checked the clock before I checked the chart",
    "patience is quiet and it pays the rent",
    "I let the level come to me instead",
)


def lyric(
    *extra_lines: str,
    body: tuple[str, ...] = GOOD_BODY,
    track_id: str = "TF-TEST-00001",
    topic_key: str = "discipline",
    mentions: int = 0,
    lines_per_section: int = 5,
) -> LyricsV1:
    """Build a lyric from the good body plus any extra lines under test."""
    all_lines = [*body, *extra_lines]
    tagged = [
        LyricLineV1(
            section="verse" if index < lines_per_section else "hook", text=text
        )
        for index, text in enumerate(all_lines)
    ]
    blocks: list[str] = ["[verse]"]
    for index, text in enumerate(all_lines):
        if index == lines_per_section:
            blocks.append("")
            blocks.append("[hook]")
        blocks.append(text)
    return LyricsV1(
        track_id=track_id,
        text="\n".join(blocks),
        lines=tuple(tagged),
        primary_topic=topic_key,
        format="full rap",
        perspective="first_person",
        tradefix_mentions=mentions,
        educational_intensity=0.5,
    )


def rules(result: object) -> set[str]:
    return {violation.rule for violation in result.violations}  # type: ignore[attr-defined]


# ---------------------------------------------------------------- the good case


def test_a_well_formed_lyric_is_accepted() -> None:
    """Without this the suite could pass by rejecting everything."""
    result = validator().validate(
        lyric(), ValidationContext(primary_topic=topic())
    )
    assert result.accepted, [v.message for v in result.violations]
    assert result.violations == ()


def test_the_good_lyric_clears_every_quality_threshold() -> None:
    """Pins the fixture: if it drifts, every isolation test below becomes unreliable."""
    result = validator().validate(lyric(), ValidationContext(primary_topic=topic()))
    assert "low_lexical_diversity" not in rules(result)
    assert "excessive_repetition" not in rules(result)
    assert "too_short" not in rules(result)
    assert "too_long" not in rules(result)


# ---------------------------------------------------------------- §17 corpus
#
# One parametrised case per phrasing, so a regression names the exact phrase that stopped
# being caught rather than reporting "the corpus failed".


@pytest.mark.parametrize(
    "line",
    [
        "this setup is guaranteed to pay",
        "guaranteed profit every session",
        "this one can't lose",
        "it cannot lose when you follow me",
        "we never lose on this pattern",
        "there is no risk in this trade",
        "completely risk free money here",
        "this is a sure thing tonight",
        "a sure bet on the london open",
        "certain profit before the close",
        "zero risk on the retest",
        "a fool proof entry every session",
        "this pattern wins every single time",
        "my system never fails",
        "this strategy always wins",
        "you can't go wrong buying support",
        "100 percent win rate on this one",
    ],
)
def test_guaranteed_outcome_claims_are_rejected(line: str) -> None:
    """§17: "promise guaranteed profits", "say trades cannot lose"."""
    result = validator().validate(
        lyric(line), ValidationContext(primary_topic=topic())
    )
    assert not result.accepted, f"accepted a guaranteed-outcome claim: {line!r}"
    assert "guaranteed_outcome" in rules(result)


@pytest.mark.parametrize(
    "line",
    [
        "this will make you rich by friday",
        "follow the plan and get rich quick",
        "double your money on this break",
        "triple your account this month",
        "gold is free money right now",
        "easy money on the london open",
        "quit your job after this trade",
        "retire early off one position",
        "we are printing money today",
        "millionaire by christmas, watch",
    ],
)
def test_profit_promises_are_rejected(line: str) -> None:
    result = validator().validate(
        lyric(line), ValidationContext(primary_topic=topic())
    )
    assert not result.accepted, f"accepted a profit promise: {line!r}"
    assert "profit_promise" in rules(result)


@pytest.mark.parametrize(
    "line",
    [
        "buy now 2400 and hold it",
        "sell at 2412 before the close",
        "long at 2398 right here",
        "buy it now before london",
        "sell now, the top is in",
        "entry at 2401 on the retest",
        "target at 2440 by friday",
        "stop at 2390 and forget it",
        "load up now while it is cheap",
        "get in here before it runs",
        "going all in on this one",
        "max leverage on the breakout",
    ],
)
def test_fabricated_signals_are_rejected(line: str) -> None:
    """§17: "give fabricated live signals". The station issues no instructions."""
    result = validator().validate(
        lyric(line), ValidationContext(primary_topic=topic())
    )
    assert not result.accepted, f"accepted a trade signal: {line!r}"
    assert "fabricated_signal" in rules(result)


def test_an_ordinary_discipline_statement_is_not_a_guarantee() -> None:
    """The narrowed pattern must not reject the station's own content.

    "fixed fraction, every single time" is a statement about one's own behaviour and is
    exactly what the risk-management topics say. An earlier, broader pattern rejected it,
    which would have made 19 of 600 composed lyrics unusable for no reason.
    """
    result = validator().validate(
        lyric("fixed fraction, every single time"),
        ValidationContext(primary_topic=topic()),
    )
    assert result.accepted, [v.message for v in result.violations]


def test_discussing_risk_is_not_denying_risk() -> None:
    result = validator().validate(
        lyric("same stop, different risk today", "I count the whole book, not the ticket"),
        ValidationContext(primary_topic=topic()),
    )
    assert "guaranteed_outcome" not in rules(result)


# ---------------------------------------------------------------- §14 certainty


def test_a_contextual_topic_without_hedging_is_rejected() -> None:
    """§14: an uncertain relationship may not be stated as a fact."""
    flat = (
        "london brings volume and range",
        "the session opens and price expands",
        "participation arrives at eight sharp",
        "the book deepens when the desks wake",
        "volume steps up at the bell",
        "the range widens past the figure",
    )
    result = validator().validate(
        lyric(body=flat),
        ValidationContext(primary_topic=topic("london_session", certainty="contextual")),
    )
    assert not result.accepted
    assert "missing_hedge" in rules(result)


def test_a_contextual_topic_with_hedging_is_accepted() -> None:
    hedged = (
        "london often brings a step up in volume and range",
        "the session usually opens with some expansion",
        "participation tends to arrive around eight sharp",
        "the book generally deepens once both desks wake",
        "volume frequently steps up at the morning bell",
        "the range sometimes widens past the round figure",
        "deeper liquidity absorbs size, not uncertainty",
    )
    result = validator().validate(
        lyric(body=hedged),
        ValidationContext(primary_topic=topic("london_session", certainty="contextual")),
    )
    assert result.accepted, [v.message for v in result.violations]


def test_an_established_topic_needs_no_hedging() -> None:
    """Position sizing determining risk is definitionally true; hedging it would be odd."""
    result = validator().validate(
        lyric(), ValidationContext(primary_topic=topic(certainty="established"))
    )
    assert "missing_hedge" not in rules(result)


@pytest.mark.parametrize(
    "line",
    [
        "a stronger dollar means gold goes down",
        "yields rise so gold will fall",
        "dxy up which means lower from here",
        "real yields climb, that means down",
        "the dollar bid so price must go lower",
    ],
)
def test_a_speculative_topic_may_not_imply_direction(line: str) -> None:
    """§14's strictest rule: an unreliable relationship cannot support a forecast."""
    result = validator().validate(
        lyric(line, "usually, not always"),
        ValidationContext(
            primary_topic=topic("dxy_relationship", certainty="speculative")
        ),
    )
    assert not result.accepted, f"accepted a directional speculative claim: {line!r}"
    assert "speculative_direction" in rules(result)


def test_a_speculative_topic_may_be_discussed_without_direction() -> None:
    result = validator().validate(
        lyric(
            "dollar up, gold down, usually, not always",
            "the correlation shows up when it feels like it",
        ),
        ValidationContext(
            primary_topic=topic("dxy_relationship", certainty="speculative")
        ),
    )
    assert result.accepted, [v.message for v in result.violations]


def test_per_topic_forbidden_phrasings_are_rejected() -> None:
    """The topic graph's own ``forbidden`` list, which generic patterns cannot capture."""
    result = validator().validate(
        lyric("support always holds at this level", "usually it does"),
        ValidationContext(
            primary_topic=topic(
                "support", certainty="contextual", forbidden=("always holds",)
            )
        ),
    )
    assert not result.accepted
    assert "topic_forbidden_phrasing" in rules(result)


def test_a_forbidden_phrasing_on_the_secondary_topic_is_also_caught() -> None:
    result = validator().validate(
        lyric("guaranteed inverse, every session", "usually"),
        ValidationContext(
            primary_topic=topic(),
            secondary_topic=topic(
                "dxy_relationship",
                certainty="speculative",
                forbidden=("guaranteed inverse",),
                category="gold",
            ),
        ),
    )
    assert not result.accepted
    assert "topic_forbidden_phrasing" in rules(result)


# ---------------------------------------------------------------- §17 quality


def test_gibberish_is_rejected() -> None:
    """Low lexical diversity reads as padding rather than a lyric."""
    repeated = tuple(["the market the market the market the market"] * 6)
    result = validator().validate(
        lyric(body=repeated), ValidationContext(primary_topic=topic())
    )
    assert not result.accepted
    assert "low_lexical_diversity" in rules(result)


def test_a_looping_generator_is_rejected() -> None:
    """§17: "contain excessive repeated phrases"."""
    looping = tuple(
        ["hold the line and wait for it"] * 9
        + ["something else entirely different here now"]
    )
    result = validator().validate(
        lyric(body=looping, lines_per_section=20),
        ValidationContext(primary_topic=topic()),
    )
    assert not result.accepted
    assert rules(result) & {"excessive_repetition", "low_lexical_diversity"}


def test_a_repeated_chorus_is_not_a_looping_generator() -> None:
    """The distinction that cost 69 of 600 composed lyrics before it was fixed.

    A hook repeated four times is a song. Measured on the rendered text it is
    indistinguishable from a model stuck in a loop, so structural checks run over the
    deduplicated line set.
    """
    hook = ("hold the line", "same plan", "same size", "hold the line")
    verse = GOOD_BODY[:4]
    tagged: list[LyricLineV1] = []
    blocks: list[str] = []
    for section, lines in (
        ("hook", hook),
        ("verse", verse),
        ("hook", hook),
        ("verse", GOOD_BODY[4:8]),
        ("hook", hook),
        ("hook", hook),
    ):
        blocks.append(f"[{section}]")
        for text in lines:
            tagged.append(LyricLineV1(section=section, text=text))
            blocks.append(text)
        blocks.append("")

    repeated_chorus = LyricsV1(
        track_id="TF-HOOK-00001",
        text="\n".join(blocks).strip(),
        lines=tuple(tagged),
        primary_topic="discipline",
        format="hook-heavy",
        perspective="first_person",
        tradefix_mentions=0,
        educational_intensity=0.3,
    )
    result = validator().validate(
        repeated_chorus, ValidationContext(primary_topic=topic())
    )
    assert result.accepted, [v.message for v in result.violations]


def test_a_lyric_below_the_word_floor_is_rejected() -> None:
    result = validator().validate(
        lyric(body=("two words",)), ValidationContext(primary_topic=topic())
    )
    assert not result.accepted
    assert "too_short" in rules(result)


def test_a_lyric_above_the_word_ceiling_is_rejected() -> None:
    long_body = tuple(f"line number {index} with several distinct words here" for index in range(80))
    result = validator().validate(
        lyric(body=long_body, lines_per_section=100),
        ValidationContext(primary_topic=topic()),
    )
    assert not result.accepted
    assert "too_long" in rules(result)


def test_a_short_format_may_be_short() -> None:
    """§15 includes "mostly instrumental with vocal hook"; a global floor rejected it.

    The format's own bound is the real spec; the global setting is a safety net.
    """
    short = (
        "hold the line tonight",
        "same plan and same size",
        "no rush at all here",
        "eyes on the tape",
    )
    context = ValidationContext(
        primary_topic=topic(), format_min_words=16, format_max_words=60
    )
    result = validator().validate(lyric(body=short), context)
    assert result.accepted, [v.message for v in result.violations]

    # And without the format bound the same lyric is correctly rejected by the global floor.
    assert not validator().validate(
        lyric(body=short), ValidationContext(primary_topic=topic())
    ).accepted


def test_a_format_ceiling_lower_than_the_global_one_is_enforced() -> None:
    result = validator().validate(
        lyric(),
        ValidationContext(primary_topic=topic(), format_max_words=20),
    )
    assert not result.accepted
    assert "too_long" in rules(result)


def test_very_short_text_skips_the_quality_checks() -> None:
    """Below a handful of words the ratios are meaningless, so only length applies."""
    result = validator().validate(
        lyric(body=("a b c",), lines_per_section=1),
        ValidationContext(primary_topic=topic()),
    )
    assert rules(result) == {"too_short"}


# ---------------------------------------------------------------- §16 branding


def test_brand_spam_is_rejected() -> None:
    """§16: "Do NOT spam: Trade Fix, Trade Fix, Trade Fix"."""
    spam = (
        "trade fix on the radio and trade fix in my ear",
        "trade fix all day, trade fix all night",
        "trade fix again because trade fix",
    )
    result = validator().validate(
        lyric(body=(*GOOD_BODY, *spam)),
        ValidationContext(primary_topic=topic(), expected_mentions=1),
    )
    assert not result.accepted
    assert "brand_spam" in rules(result)


def test_the_expected_mention_count_is_honoured() -> None:
    result = validator().validate(
        lyric("this is Trade Fix, the market sets the tempo"),
        ValidationContext(primary_topic=topic(), expected_mentions=1),
    )
    assert result.accepted
    assert "brand_count_mismatch" not in rules(result)


def test_a_mention_count_mismatch_is_reported_but_not_fatal() -> None:
    """The lyric is not unsafe, but it has drifted from its brief and should regenerate."""
    result = validator().validate(
        lyric(), ValidationContext(primary_topic=topic(), expected_mentions=2)
    )
    assert result.accepted
    assert "brand_count_mismatch" in rules(result)
    mismatch = next(v for v in result.violations if v.rule == "brand_count_mismatch")
    assert mismatch.fatal is False
    assert mismatch.measured == 0.0
    assert mismatch.threshold == 2.0


def test_zero_mentions_is_the_normal_case() -> None:
    """§16: "Most songs should remain musically credible"."""
    result = validator().validate(
        lyric(), ValidationContext(primary_topic=topic(), expected_mentions=0)
    )
    assert result.accepted
    assert result.violations == ()


# ---------------------------------------------------------------- blocklists


def test_a_blocked_third_party_slogan_is_rejected() -> None:
    """§17: "accidentally contain another brand's slogan excessively"."""
    result = validator().validate(
        lyric("just do it, the market does not wait"),
        ValidationContext(primary_topic=topic()),
    )
    assert not result.accepted
    assert "blocked_phrase" in rules(result)


def test_a_blocked_artist_name_is_rejected() -> None:
    """The operator blocklist for §86's "do not imitate a living artist".

    Note what this does *not* claim: it detects configured names, not imitation. Imitation
    is not detectable from text, and the persona schema has no field in which a real artist
    could be referenced, which is the actual mitigation.
    """
    config = ValidatorConfig(blocked_names=("Some Real Artist",))
    result = validator().validate(
        lyric("flowing like some real artist on a sunday"),
        ValidationContext(primary_topic=topic(), config=config),
    )
    assert not result.accepted
    assert "blocked_name" in rules(result)


def test_an_empty_blocklist_blocks_nothing() -> None:
    config = ValidatorConfig(blocked_phrases=(), blocked_names=())
    result = validator().validate(
        lyric("just do it anyway"),
        ValidationContext(primary_topic=topic(), config=config),
    )
    assert result.accepted


# ---------------------------------------------------------------- duplication


def test_an_exact_duplicate_is_rejected() -> None:
    """§17: "repeat previous Trade Fix lyrics"."""
    first = lyric()
    result = validator().validate(
        lyric(track_id="TF-TEST-00002"),
        ValidationContext(
            primary_topic=topic(), previous_hashes=frozenset({first.content_hash})
        ),
    )
    assert not result.accepted
    assert "duplicate_lyrics" in rules(result)
    assert result.max_similarity == 1.0


def test_a_near_duplicate_is_rejected() -> None:
    """The case that actually happens: a reworded couplet, not a literal copy."""
    original = lyric()
    reworded = lyric(
        body=(
            "I wrote the rule before the candle got noisy",
            "the stop is where I am wrong not where it stings",
            "size comes from the distance, never from the mood",
            "a plan you abandon under pressure was never a plan",
            "flat is a position and it costs me nothing",
            "my notes said the same thing last week",
            "discipline is doing this when it is boring",
            "I checked the clock before I checked the chart",
            "patience is quiet and it pays the rent",
            "I let the level come to me instead",
        ),
        track_id="TF-TEST-00003",
    )
    result = validator().validate(
        reworded,
        ValidationContext(
            primary_topic=topic(),
            previous_shingles=((original.track_id, original.shingles()),),
        ),
    )
    assert not result.accepted
    assert "duplicate_lyrics" in rules(result)
    assert result.closest_track_id == original.track_id


def test_an_unrelated_lyric_is_not_a_duplicate() -> None:
    original = lyric()
    different = lyric(
        body=(
            "london walks in and the book gets deeper",
            "volume arrives but direction does not",
            "the overlap is the deepest part of the day",
            "depth absorbs size, not uncertainty",
            "both desks awake and the spread tightens",
            "the handover happens without a sound",
        ),
        track_id="TF-TEST-00004",
        topic_key="london_session",
    )
    result = validator().validate(
        different,
        ValidationContext(
            primary_topic=topic("london_session", certainty="contextual"),
            previous_shingles=((original.track_id, original.shingles()),),
        ),
    )
    assert result.max_similarity < 0.3


def test_duplication_with_no_history_is_zero() -> None:
    result = validator().validate(lyric(), ValidationContext(primary_topic=topic()))
    assert result.max_similarity == 0.0
    assert result.closest_track_id is None


# ---------------------------------------------------------------- reporting


def test_every_violation_carries_an_explanation() -> None:
    """§48 requires a rejection to be explainable."""
    result = validator().validate(
        lyric("this is guaranteed to pay, buy now 2400"),
        ValidationContext(primary_topic=topic()),
    )
    assert not result.accepted
    for violation in result.violations:
        assert violation.rule
        assert len(violation.message) > 10


def test_threshold_violations_report_the_measurement() -> None:
    """"diversity 0.18 below the 0.26 minimum" is actionable; "low diversity" is not."""
    # One section, many repeats: a block the dedup must keep whole, so the ratios are
    # genuinely low rather than collapsed away.
    repeated = tuple(["same words again and again once more"] * 10)
    result = validator().validate(
        lyric(body=repeated, lines_per_section=20),
        ValidationContext(primary_topic=topic()),
    )
    threshold_violations = [v for v in result.violations if v.measured is not None]
    assert threshold_violations
    for violation in threshold_violations:
        assert violation.threshold is not None


def test_all_rules_run_even_after_the_first_failure() -> None:
    """A rewrite prompted by one violation would trip the next; the composer needs all."""
    result = validator().validate(
        lyric("this is guaranteed money", "buy now 2400", "just do it"),
        ValidationContext(primary_topic=topic()),
    )
    assert len(rules(result)) >= 3


def test_fatal_violations_are_separable_from_advisory_ones() -> None:
    result = validator().validate(
        lyric("this is guaranteed to pay"),
        ValidationContext(primary_topic=topic(), expected_mentions=1),
    )
    assert not result.accepted
    assert any(v.fatal for v in result.violations)
    assert any(not v.fatal for v in result.violations)
    assert len(result.fatal_violations) < len(result.violations)


def test_validation_works_without_a_context() -> None:
    """The validator must be usable on an externally supplied lyric."""
    result = validator().validate(lyric())
    assert result.accepted


def test_a_lyric_with_no_line_breakdown_is_still_checked() -> None:
    """An external lyric may arrive as plain text with no section structure."""
    plain = LyricsV1(
        track_id="TF-PLAIN-00001",
        text="this trade is guaranteed to pay and you cannot lose",
        primary_topic="discipline",
        format="full rap",
        perspective="first_person",
        tradefix_mentions=0,
        educational_intensity=0.4,
    )
    result = validator().validate(plain, ValidationContext(primary_topic=topic()))
    assert not result.accepted
    assert "guaranteed_outcome" in rules(result)


# ------------------------------------------------- section variety (B3, §15)
#
# §15 asks for repetition tracking that distinguishes an intentional chorus from lazy
# duplication. `_check_repetition` cannot do it: it reads the line-deduplicated word set,
# precisely so a hook repeated four times by design passes, and that dedup makes a lyric
# built from one section emitted three times collapse to a single clean copy.


def _sectioned(*blocks: str) -> LyricsV1:
    """A lyric assembled from raw blocks, so section structure survives into the text."""
    return LyricsV1(
        track_id="TF-SECTIONS",
        text="\n\n".join(blocks),
        primary_topic="discipline",
        format="minimal vocal",
        perspective="observer",
        tradefix_mentions=0,
        educational_intensity=0.4,
    )


#: Verbatim from the first track the station generated at the vocal profile. Three
#: identical blocks, 54 words of which 18 were distinct, every section the same.
_LOOPED_BLOCK = (
    "[phrase]\n"
    "decided before the candle, not during\n"
    "flat is fine\n"
    "read it twice\n"
    "quiet hands\n"
    "flat is fine"
)


def test_a_lyric_of_three_identical_sections_is_rejected() -> None:
    result = validator().validate(
        _sectioned(_LOOPED_BLOCK, _LOOPED_BLOCK, _LOOPED_BLOCK), ValidationContext()
    )
    assert not result.accepted
    assert "no_section_variety" in {v.rule for v in result.violations}


def test_a_chorus_between_verses_is_not_duplication() -> None:
    """The case the rule must never catch: a hook is supposed to come back."""
    hook = "[hook]\nread it twice\nquiet hands\nmind on the plan"
    result = validator().validate(
        _sectioned(
            hook,
            "[verse]\nmomentum measures how fast price is moving, not how far it will go\n"
            "breakouts frequently fail; that is why the stop exists",
            hook,
            "[verse]\nthe rule was written before the candle got loud\n"
            "position size is the only promise a trader can actually keep",
            hook,
        ),
        ValidationContext(),
    )
    assert "no_section_variety" not in {v.rule for v in result.violations}


def test_a_tag_rename_does_not_disguise_identical_words() -> None:
    """`[hook]` and `[phrase]` holding the same words are the same section."""
    words = "flat is fine\nread it twice\nquiet hands"
    result = validator().validate(
        _sectioned(f"[hook]\n{words}", f"[phrase]\n{words}", f"[refrain]\n{words}"),
        ValidationContext(),
    )
    assert "no_section_variety" in {v.rule for v in result.violations}


def test_two_sections_are_not_enough_to_call_it_a_loop() -> None:
    """A statement and its restatement is a song. The rule abstains below three.

    Asserted on the rule rather than on acceptance: a two-section lyric is short enough
    that `too_short` may legitimately fire, and that is a different finding.
    """
    result = validator().validate(
        _sectioned(_LOOPED_BLOCK, _LOOPED_BLOCK), ValidationContext()
    )
    assert "no_section_variety" not in {v.rule for v in result.violations}
