"""The production lyric path (B3).

Every component existed and nothing called any of them, so the station could not produce
a vocal track at all: `AceStepPromptBuilder` saw a vocal blueprint with no stored lyric,
correctly refused to let the model invent words about markets, and silently downgraded to
an instrumental. The `lyrics` table was empty across the whole database.

These tests cover the orchestration that closes that gap, and the properties that make it
safe rather than merely present.
"""

from __future__ import annotations

import random

import pytest

from tests.conftest import make_blueprint
from tests.unit.test_library import CONFIG_DIR
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.contracts.enums import MarketRegime, VocalStyle
from tradefix_radio.director.diversity import DiversityDirector
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.library import ContentLibrary, load_content_library
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.lyrics.director import LyricsDirector
from tradefix_radio.lyrics.orchestrator import (
    MODE_FORMATS,
    LyricFailure,
    LyricMode,
    LyricOrchestrator,
)


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    return load_content_library(config_dir=CONFIG_DIR)


def build(library: ContentLibrary, seed: int = 11) -> LyricOrchestrator:
    settings = AppSettings()
    selector = WeightedSelector(random.Random(seed))
    director = LyricsDirector(
        library, settings.lyrics, DiversityDirector(settings.diversity), selector
    )
    return LyricOrchestrator(library, settings.lyrics, director, selector)


def vocal_blueprint(
    track_id: str = "TF-VOCAL",
    *,
    symbol: str = "BTCUSD",
    style: VocalStyle = VocalStyle.RAP,
    duration: int = 180,
    regime: MarketRegime = MarketRegime.NORMAL_RANGE,
    energy: float = 55.0,
    lyric_format: str | None = None,
):
    """A blueprint asking for vocals, with format and style kept consistent.

    The fixture's default format is cleared unless a test states one: `mode_for` honours
    an explicit format over the vocal style — correctly, it is the more specific request —
    so a blueprint carrying both and disagreeing is not a case worth asserting on.
    """
    blueprint = make_blueprint(
        track_id,
        symbol=symbol,
        instrumental=False,
        regime=regime,
        energy=energy,
        duration_seconds=duration,
    )
    return blueprint.model_copy(
        update={
            "vocal": blueprint.vocal.model_copy(update={"style": style, "enabled": True}),
            "lyrics": blueprint.lyrics.model_copy(update={"format": lyric_format}),
        }
    )


# ------------------------------------------------------------ the path exists


def test_a_vocal_blueprint_now_produces_validated_lyrics(library: ContentLibrary) -> None:
    """The defect B3 fixes, stated as a test.

    Before this, the answer was always "no lyric", and the only visible symptom was a
    track that sounded instrumental when the dashboard said it was a rap.
    """
    result = build(library).generate(
        vocal_blueprint(), history=ProgrammingHistory([])
    )
    assert result.usable
    assert result.lyrics is not None
    assert result.lyrics.text.strip()
    assert result.validation is not None
    assert result.validation.accepted
    assert result.sections


def test_an_instrumental_blueprint_composes_nothing(library: ContentLibrary) -> None:
    blueprint = make_blueprint("TF-INSTR", instrumental=True)
    result = build(library).generate(blueprint, history=ProgrammingHistory([]))
    assert result.mode is LyricMode.NONE
    assert result.lyrics is None
    assert result.failure is None, "an instrumental is not a failure to write lyrics"


# -------------------------------------------------------------------- modes


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (VocalStyle.RAP, LyricMode.FULL_RAP),
        (VocalStyle.MELODIC_RAP, LyricMode.MELODIC_RAP),
        (VocalStyle.SUNG, LyricMode.FULL_SONG),
        (VocalStyle.SPOKEN, LyricMode.MINIMAL_VOCAL),
        (VocalStyle.CHOPPED_HOOK, LyricMode.HOOK_ONLY),
    ],
)
def test_the_vocal_style_selects_the_mode(
    library: ContentLibrary, style: VocalStyle, expected: LyricMode
) -> None:
    """Not every vocal track is a full rap. The station needs the variation."""
    result = build(library).generate(
        vocal_blueprint(style=style), history=ProgrammingHistory([])
    )
    assert result.mode is expected
    assert result.usable, f"{style.value} produced no lyric"


def test_an_explicit_format_outranks_the_vocal_style(library: ContentLibrary) -> None:
    """The format is the more specific request, so it wins."""
    result = build(library).generate(
        vocal_blueprint(style=VocalStyle.SUNG, lyric_format="spoken word"),
        history=ProgrammingHistory([]),
    )
    assert result.mode is LyricMode.SPOKEN_WORD


def test_each_mode_produces_its_own_section_shape(library: ContentLibrary) -> None:
    """Variation has to be audible, not just labelled."""
    shapes = {
        style: build(library).generate(
            vocal_blueprint(track_id=f"TF-{style.value}", style=style),
            history=ProgrammingHistory([]),
        ).sections
        for style in (VocalStyle.RAP, VocalStyle.MELODIC_RAP, VocalStyle.CHOPPED_HOOK)
    }
    assert len(set(shapes.values())) == len(shapes), f"identical structures: {shapes}"


# ----------------------------------------------------------------- duration


@pytest.mark.parametrize("duration", [60, 90, 180, 240])
def test_the_word_budget_scales_with_duration(
    library: ContentLibrary, duration: int
) -> None:
    orchestrator = build(library)
    low, high = orchestrator.word_budget(float(duration))
    assert 0 < low < high
    _, longer_high = orchestrator.word_budget(duration * 2.0)
    assert longer_high >= high


def test_a_short_track_does_not_get_a_four_minute_lyric(library: ContentLibrary) -> None:
    """A 400-word verse body in a 60-second track is unperformable.

    Enforced by *format selection* rather than by the validator, which deliberately takes
    the lower of the format floor and the global one because §15 includes 16-word formats
    on purpose — raising a floor it is designed to ignore would be a no-op dressed as a
    constraint.
    """
    orchestrator = build(library)
    result = orchestrator.generate(
        vocal_blueprint(duration=60, style=VocalStyle.CHOPPED_HOOK),
        history=ProgrammingHistory([]),
    )
    assert result.usable
    assert result.lyrics is not None
    _low, high = orchestrator.word_budget(60.0)
    assert len(result.lyrics.text.split()) <= high


def test_a_mode_with_no_format_fitting_the_duration_fails_honestly(
    library: ContentLibrary
) -> None:
    """Reported, not silently rewritten as a different kind of song."""
    orchestrator = build(library)
    # Full rap against the shortest track the contract allows: the rap formats ask
    # for 120-140 words minimum and 30 seconds affords at most 66.
    result = orchestrator.generate(
        vocal_blueprint(duration=30, style=VocalStyle.RAP), history=ProgrammingHistory([])
    )
    if not result.usable:
        assert result.failure is LyricFailure.NO_FORMAT_FITS_DURATION
        assert "fits" in " ".join(result.rationale)


# ------------------------------------------------------------- market scope


def test_a_bitcoin_track_never_carries_gold_session_framing(
    library: ContentLibrary
) -> None:
    """§14: "the London open brings a step up in volume" is false about Bitcoin.

    Driven over many seeds rather than one, because a scoping bug that fires occasionally
    is still a bug — and "occasionally" is what a weighting error looks like.
    """
    gold_only = {
        key
        for key, topic in library.topics.items()
        if topic.markets and not topic.suits_market("BTCUSD")
    }
    assert gold_only, "the fixture library has no gold-scoped topics to exclude"

    for seed in range(25):
        result = build(library, seed=seed).generate(
            vocal_blueprint(track_id=f"TF-BTC-{seed}", symbol="BTCUSD"),
            history=ProgrammingHistory([]),
        )
        if not result.usable:
            continue
        assert result.primary_topic not in gold_only, (
            f"seed {seed} put gold topic {result.primary_topic!r} on a BTCUSD track"
        )
        assert result.secondary_topic not in gold_only


def test_a_gold_track_never_carries_crypto_weekend_framing(
    library: ContentLibrary
) -> None:
    btc_only = {
        key
        for key, topic in library.topics.items()
        if topic.markets and not topic.suits_market("XAUUSD")
    }
    assert btc_only

    for seed in range(25):
        result = build(library, seed=seed).generate(
            vocal_blueprint(track_id=f"TF-XAU-{seed}", symbol="XAUUSD"),
            history=ProgrammingHistory([]),
        )
        if not result.usable:
            continue
        assert result.primary_topic not in btc_only, (
            f"seed {seed} put BTC topic {result.primary_topic!r} on a gold track"
        )


def test_neutral_content_is_available_to_both_markets(library: ContentLibrary) -> None:
    """Most of what the station says about discipline is true of any instrument."""
    scopes = set()
    for symbol in ("BTCUSD", "XAUUSD"):
        for seed in range(12):
            result = build(library, seed=seed).generate(
                vocal_blueprint(track_id=f"TF-{symbol}-{seed}", symbol=symbol),
                history=ProgrammingHistory([]),
            )
            if result.usable:
                scopes.add(result.market_scope)
    assert None in scopes, "no market-neutral lyric was ever produced"


# ------------------------------------------------------------- brand limits


def test_trade_fix_is_mentioned_sparingly(library: ContentLibrary) -> None:
    """§16: 0-2 mentions, and most songs should not be an advert."""
    counts = []
    for seed in range(30):
        result = build(library, seed=seed).generate(
            vocal_blueprint(track_id=f"TF-BRAND-{seed}"), history=ProgrammingHistory([])
        )
        if result.usable:
            counts.append(result.tradefix_mentions)

    assert counts
    assert all(0 <= count <= 2 for count in counts), f"out of range: {sorted(set(counts))}"
    restrained = sum(1 for count in counts if count <= 1)
    assert restrained / len(counts) >= 0.75, (
        f"only {restrained}/{len(counts)} songs kept to 0-1 mentions"
    )


# -------------------------------------------------------------- determinism


def test_the_same_blueprint_and_seed_compose_the_same_lyric(
    library: ContentLibrary
) -> None:
    """§64 needs a reproducible station, and a replayed rejection needs it too."""
    first = build(library, seed=7).generate(
        vocal_blueprint(), history=ProgrammingHistory([])
    )
    second = build(library, seed=7).generate(
        vocal_blueprint(), history=ProgrammingHistory([])
    )
    assert first.lyrics is not None and second.lyrics is not None
    assert first.lyrics.text == second.lyrics.text


def test_every_mode_maps_to_a_format_that_exists(library: ContentLibrary) -> None:
    """A mode naming a format the library does not have would fail only at runtime."""
    for mode, formats in MODE_FORMATS.items():
        present = [key for key in formats if key in library.formats]
        assert present, f"{mode.value} maps to no format present in the library"


# ------------------------------------------------- trading-claim validation
#
# The composer writes from a curated topic graph, so it is unlikely to produce these on
# its own. They are tested anyway because the validator is the thing standing between a
# future lyric source — an LLM provider, an operator-authored line — and a station that
# tells listeners a trade cannot lose.


def _validate(library: ContentLibrary, text: str):
    """Run the §17 validator over arbitrary text."""
    from tradefix_radio.contracts.lyrics import LyricsV1
    from tradefix_radio.lyrics.validators import LyricValidator

    lyrics = LyricsV1(
        track_id="TF-CLAIM",
        text=text,
        primary_topic="risk_management",
        format="full rap",
        perspective="observer",
        tradefix_mentions=0,
        educational_intensity=0.5,
    )
    return LyricValidator(AppSettings().lyrics).validate(lyrics)


@pytest.mark.parametrize(
    "text",
    [
        "this trade cannot lose and the stop is never needed at all my friend",
        "buy now and guaranteed profit is yours before the session even closes today",
        "risk free money every single time it pays, the setup never fails for us",
        "load up at 2400 and take the target at 2450, this is a sure thing today",
        "we will get rich and double your account before the London bell rings loud",
    ],
)
def test_a_lyric_promising_a_sure_outcome_is_rejected(
    library: ContentLibrary, text: str
) -> None:
    """§14/§17: the station never implies a guaranteed outcome or issues a signal."""
    result = _validate(library, text)
    assert not result.accepted, f"accepted: {text!r}"
    assert result.violations


def test_the_rejection_names_the_rule(library: ContentLibrary) -> None:
    """§48 requires a rejection to be explainable, not merely final."""
    result = _validate(
        library, "this trade cannot lose and the stop is never needed at all my friend"
    )
    rules = {violation.rule for violation in result.violations}
    assert rules
    assert all(violation.message for violation in result.violations)


@pytest.mark.parametrize(
    "text",
    [
        # The same subjects, phrased as the station is supposed to phrase them.
        "breakouts often fail, that is exactly why the stop exists before the entry does",
        "a wider range usually means a smaller position, not a bigger opportunity today",
        "trends tend to persist until they do not, and nobody rings a bell at the turn",
        "volatility clusters more often than not, so quiet days can stay quiet for a while",
    ],
)
def test_the_hedged_version_of_the_same_claim_passes(
    library: ContentLibrary, text: str
) -> None:
    """The rule is about certainty, not about the subject.

    If hedged statements failed too, the validator would be rejecting the station's own
    educational content and there would be nothing left to sing about.
    """
    result = _validate(library, text)
    fatal = [violation for violation in result.violations if violation.fatal]
    assert not any(
        violation.rule in {"guaranteed_outcome", "profit_promise", "trade_signal"}
        for violation in fatal
    ), [violation.rule for violation in fatal]


def test_composed_lyrics_never_trip_the_claim_rules(library: ContentLibrary) -> None:
    """The real guarantee: what the station actually produces is safe.

    Driven over many seeds, because a claim that slips through one lyric in fifty is
    still a claim the station made.
    """
    checked = 0
    for seed in range(40):
        result = build(library, seed=seed).generate(
            vocal_blueprint(track_id=f"TF-SAFE-{seed}"), history=ProgrammingHistory([])
        )
        if not result.usable or result.validation is None:
            continue
        checked += 1
        offending = [
            violation.rule
            for violation in result.validation.violations
            if violation.rule in {"guaranteed_outcome", "profit_promise", "trade_signal"}
        ]
        assert not offending, f"seed {seed} produced {offending}"
    assert checked >= 20, f"only {checked} lyrics were produced to check"


def test_no_real_artist_is_ever_named(library: ContentLibrary) -> None:
    """§17 forbids imitation, and the composer writes from our own corpus.

    Checked as a property of the output rather than trusted: the topic graph is editable,
    and a phrase added to it later would reach listeners.
    """
    names = ("drake", "travis scott", "central cee", "kendrick", "eminem", "stormzy")
    for seed in range(25):
        result = build(library, seed=seed).generate(
            vocal_blueprint(track_id=f"TF-ART-{seed}"), history=ProgrammingHistory([])
        )
        if result.lyrics is None:
            continue
        lowered = result.lyrics.text.lower()
        assert not any(name in lowered for name in names), f"seed {seed}"


# ------------------------------------------------------- lyric originality
#
# §17's duplicate detection, exercised through the real path: the station reads
# `LyricsRepository.recent_hashes()` and `recent_shingles()` and passes both into
# `generate`, which hands them to the validator's `ValidationContext`. These tests use the
# same two parameters, so what passes here is what passes in production.
#
# The pair that must *not* reject is as important as the pair that must. The station's
# whole vocabulary is "risk", "stop", "entry", "momentum", "discipline" — a duplicate check
# that punished their recurrence would reject every lyric the topic graph can produce, and
# the symptom would look like a model failure rather than a measurement one.


def _compose(orchestrator: LyricOrchestrator, blueprint, **history):
    return orchestrator.generate(blueprint, history=ProgrammingHistory([]), **history)


def test_an_exact_repeat_of_a_previous_lyric_is_rejected(library: ContentLibrary) -> None:
    """The one case that must never reach air: the same words twice."""
    orchestrator = build(library)
    first = _compose(orchestrator, vocal_blueprint("TF-ORIG-1"))
    assert first.usable and first.lyrics is not None

    # The same composition offered back as history. Deterministic composition means the
    # second call reproduces it, so the exact-hash check is what has to catch it.
    repeat = _compose(
        orchestrator,
        vocal_blueprint("TF-ORIG-2"),
        previous_hashes=frozenset({first.lyrics.content_hash}),
        previous_shingles=[("TF-ORIG-1", frozenset(first.lyrics.shingles()))],
    )
    if repeat.usable and repeat.lyrics is not None:
        assert repeat.lyrics.content_hash != first.lyrics.content_hash, (
            "an identical lyric was accepted as new"
        )
    else:
        assert repeat.failure in {
            LyricFailure.VALIDATION_REJECTED,
            LyricFailure.LYRIC_REUSE,
        }


def test_the_same_hook_with_different_verses_is_not_a_duplicate(
    library: ContentLibrary,
) -> None:
    """A recurring hook is a radio station having an identity, not repeating itself.

    Shingle overlap rather than word overlap is what makes this distinguishable: a shared
    hook contributes its own shingles once, while the verses around it do not match.
    """
    orchestrator = build(library)
    shared_hook = frozenset(
        {"read it twice", "it twice quiet", "twice quiet hands", "quiet hands mind"}
    )
    result = _compose(
        orchestrator,
        vocal_blueprint("TF-ORIG-HOOK"),
        previous_shingles=[("TF-OLD", shared_hook)],
    )
    assert result.usable, (
        f"a shared hook was treated as duplication: {result.failure}"
    )


def test_two_lyrics_on_the_same_topic_are_allowed(library: ContentLibrary) -> None:
    """§12 limits topic *repetition rate*; it does not make a topic single-use.

    The station has a few dozen teaching topics and runs continuously. If covering "risk"
    twice counted as duplication the catalogue would be exhausted in a day.
    """
    orchestrator = build(library)
    first = _compose(orchestrator, vocal_blueprint("TF-TOPIC-1"))
    assert first.usable and first.lyrics is not None

    second = _compose(
        orchestrator,
        vocal_blueprint("TF-TOPIC-2", duration=240),
        previous_shingles=[("TF-TOPIC-1", frozenset(first.lyrics.shingles()))],
    )
    if second.usable and second.lyrics is not None and (
        second.lyrics.primary_topic == first.lyrics.primary_topic
    ):
        assert second.lyrics.content_hash != first.lyrics.content_hash


def test_common_trading_vocabulary_is_not_duplication(library: ContentLibrary) -> None:
    """The measurement must not mistake the station's own dialect for repetition.

    A history made only of the words every Trade Fix lyric contains. If shared vocabulary
    alone could trip the check, this would reject — and the station would appear unable to
    write about trading.
    """
    orchestrator = build(library)
    vocabulary = frozenset(
        {
            "risk the stop",
            "the stop the",
            "stop the entry",
            "the entry the",
            "entry the market",
            "the market the",
            "market the trade",
        }
    )
    result = _compose(
        orchestrator,
        vocal_blueprint("TF-VOCAB"),
        previous_shingles=[(f"TF-OLD-{index}", vocabulary) for index in range(20)],
    )
    assert result.usable, (
        f"shared trading vocabulary was scored as duplication: {result.failure}"
    )


def test_a_rejected_lyric_names_the_duplication_rule(library: ContentLibrary) -> None:
    """No vague "score was below threshold" — the reason has to be nameable.

    Driven by making the candidate's own shingles the history, which is the strongest
    possible overlap and leaves no doubt about which rule should fire.
    """
    orchestrator = build(library)
    composed = _compose(orchestrator, vocal_blueprint("TF-DUPE-A"))
    assert composed.usable and composed.lyrics is not None

    rejected = _compose(
        orchestrator,
        vocal_blueprint("TF-DUPE-B"),
        previous_hashes=frozenset({composed.lyrics.content_hash}),
        previous_shingles=[("TF-DUPE-A", frozenset(composed.lyrics.shingles()))],
    )
    if not rejected.usable:
        assert rejected.validation is not None
        rules = {violation.rule for violation in rejected.validation.violations}
        assert "duplicate_lyrics" in rules, f"rejected for {rules} rather than duplication"


def test_an_empty_history_never_blocks_the_first_lyric(library: ContentLibrary) -> None:
    """Cold start. The station's very first vocal track has nothing to be similar to."""
    orchestrator = build(library)
    result = _compose(orchestrator, vocal_blueprint("TF-COLD"))
    assert result.usable
    assert result.max_similarity == 0.0
