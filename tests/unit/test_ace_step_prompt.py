"""The blueprint → ACE-Step translator (§7.9, §7.10, §7.30).

Pure functions, no GPU, no model, no network — which is the point. §7.30 asks for prompt
builder unit tests in normal CI, and the builder was designed to make that possible: if
translation needed a server, this file could not exist and the mapping would only ever be
exercised by a 30-second GPU test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_blueprint
from tradefix_radio.contracts.enums import VocalStyle
from tradefix_radio.contracts.lyrics import LyricLineV1, LyricsV1
from tradefix_radio.generation.ace_step.prompt import (
    CAPTION_MAX_CHARS,
    INSTRUMENTAL_MARKER,
    AceStepPromptBuilder,
)

BUILDER = AceStepPromptBuilder()


def build(blueprint, lyrics=None, *, steps=8, guidance=3.0, profile="balanced"):
    return BUILDER.build(
        blueprint,
        lyrics=lyrics,
        inference_steps=steps,
        guidance_scale=guidance,
        profile=profile,
    )


def lyrics_for(track_id: str, text: str) -> LyricsV1:
    return LyricsV1(
        track_id=track_id,
        text=text,
        lines=tuple(
            LyricLineV1(section="verse", text=line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ),
        primary_topic="liquidity_breakout",
        secondary_topic=None,
        format="full rap",
        perspective="first_person",
        tradefix_mentions=1,
        educational_intensity=0.5,
        concepts_used=("liquidity",),
    )


# ------------------------------------------------------------------ caption


def test_the_caption_carries_the_musical_decision() -> None:
    blueprint = make_blueprint(
        "TF-A", genre="uk_trap", bpm=148, key="F# minor", composition_energy=0.88
    )
    spec = build(blueprint)
    assert "UK trap" in spec.caption
    assert "148 BPM" in spec.caption
    assert "F# minor" in spec.caption
    assert spec.bpm == 148
    assert spec.key_scale == "F# Minor"


def test_genre_identifiers_become_english() -> None:
    """``uk_drill`` is an internal key; the model reads text."""
    assert "UK drill" in build(make_blueprint("TF-B", genre="uk_drill")).caption
    assert "lo-fi hip hop" in build(make_blueprint("TF-C", genre="lofi")).caption
    # An unmapped genre degrades to the readable form rather than leaking snake_case.
    assert "future bass" in build(make_blueprint("TF-D", genre="future_bass")).caption


@pytest.mark.parametrize(
    ("energy", "expected"),
    [(0.05, "very calm"), (0.4, "steady"), (0.8, "high energy"), (0.99, "explosive")],
)
def test_energy_becomes_an_adjective_not_a_number(energy: float, expected: str) -> None:
    """A text-conditioned model cannot read 0.73.

    The director's energy is continuous; its audible consequence is categorical. Passing the
    float through would put a number in the caption that the model has no way to interpret.
    """
    spec = build(make_blueprint("TF-E", composition_energy=energy))
    assert expected in spec.caption


def test_middling_intensities_are_left_unsaid() -> None:
    """A caption describing every axis as "moderate" has described nothing.

    The deadband is what keeps the caption about the track's distinguishing features.
    """
    neutral = make_blueprint("TF-F")
    neutral = neutral.model_copy(
        update={
            "composition": neutral.composition.model_copy(
                update={
                    "rhythm_density": 0.5,
                    "bass_intensity": 0.5,
                    "drum_intensity": 0.5,
                    "melodic_complexity": 0.5,
                }
            )
        }
    )
    caption = build(neutral).caption
    for word in ("sparse rhythm", "busy intricate", "light bass", "heavy sub"):
        assert word not in caption


def test_extreme_intensities_are_described() -> None:
    blueprint = make_blueprint("TF-G")
    blueprint = blueprint.model_copy(
        update={
            "composition": blueprint.composition.model_copy(
                update={"bass_intensity": 0.95, "melodic_complexity": 0.1}
            )
        }
    )
    caption = build(blueprint).caption
    assert "heavy sub bass" in caption
    assert "simple melody" in caption


def test_the_caption_is_capped_at_the_documented_limit() -> None:
    """ACE-Step documents 512 characters. A server-side truncation is a silent change."""
    blueprint = make_blueprint("TF-H")
    blueprint = blueprint.model_copy(
        update={
            "composition": blueprint.composition.model_copy(
                update={
                    "mood": tuple(f"mood-descriptor-{i}" for i in range(40)),
                    "instrumentation": tuple(f"instrument-name-{i}" for i in range(40)),
                }
            )
        }
    )
    spec = build(blueprint)
    assert len(spec.caption) <= CAPTION_MAX_CHARS
    assert spec.warnings, "a truncated caption must say so"
    # Trimmed on clause boundaries, so no descriptor is left as a fragment.
    assert not spec.caption.endswith(",")


def test_translation_is_deterministic() -> None:
    """§7.13 cannot measure seed behaviour if the prompt itself drifts."""
    blueprint = make_blueprint("TF-I")
    assert build(blueprint).caption == build(blueprint).caption


# ------------------------------------------------------------- instrumental


def test_an_instrumental_blueprint_produces_the_instrumental_marker() -> None:
    """§7.11: ``lyrics.enabled = false`` and ``vocal.enabled = false`` must mean no vocals."""
    spec = build(make_blueprint("TF-J", instrumental=True))
    assert spec.instrumental
    assert spec.lyrics == INSTRUMENTAL_MARKER
    assert "instrumental, no vocals" in spec.caption


def test_a_vocal_blueprint_without_composed_lyrics_falls_back_to_instrumental() -> None:
    """The model must not be left to invent words about trading.

    §14 forbids implying a guaranteed outcome and §17 forbids copyrighted lyrics; a model
    writing its own verse about liquidity has been validated against neither. Generating an
    instrumental is the safe realisation, and the warning records that the blueprint was not
    fulfilled as written.
    """
    spec = build(make_blueprint("TF-K", instrumental=False), lyrics=None)
    assert spec.instrumental
    assert spec.lyrics == INSTRUMENTAL_MARKER
    assert any("invent" in warning for warning in spec.warnings)


# ------------------------------------------------------------------ lyrics


def test_already_tagged_lyrics_pass_through_untouched() -> None:
    """§7.10: do not silently replace lyrics. Untouched has to mean untouched."""
    text = "[Verse]\nThe liquidity sweep took the stops below\n\n[Chorus]\nHold the line"
    spec = build(make_blueprint("TF-L"), lyrics_for("TF-L", text))
    assert spec.lyrics == text
    assert not spec.lyrics_modified
    assert spec.lyric_notes == ()


def test_untagged_lyrics_get_deterministic_structure_tags() -> None:
    """§7.10: create the tags deterministically from our own structure."""
    text = "First stanza line one\nFirst stanza line two\n\nSecond stanza line one"
    blueprint = make_blueprint("TF-M")
    spec = build(blueprint, lyrics_for("TF-M", text))

    assert spec.lyrics_modified
    assert spec.lyric_notes, "a modification must be recorded"
    assert "[Verse]" in spec.lyrics
    # The words themselves survive exactly; only tags were inserted.
    for line in text.split():
        assert line in spec.lyrics
    # Deterministic: same input, same tagging.
    assert build(blueprint, lyrics_for("TF-M", text)).lyrics == spec.lyrics


def test_structure_tags_follow_the_blueprint_not_a_guess() -> None:
    """The blueprint already said what shape the song is.

    Inventing a different structure here would mean the audio no longer matches the plan the
    director recorded, and the §46 detail page would show a structure the track does not have.
    """
    blueprint = make_blueprint("TF-N")
    blueprint = blueprint.model_copy(
        update={
            "composition": blueprint.composition.model_copy(
                update={"structure": ("intro", "verse", "hook", "outro")}
            )
        }
    )
    spec = build(blueprint, lyrics_for("TF-N", "Stanza one\n\nStanza two\n\nStanza three"))
    # intro/outro carry no words, so they must not consume a stanza and mislabel a verse.
    assert spec.lyrics.startswith("[Verse]")
    assert "[Chorus]" in spec.lyrics


def test_a_blank_lyric_cannot_reach_the_builder_at_all() -> None:
    """Defence in depth, and the outer layer is the stronger one.

    `LyricsV1` refuses blank text outright, so a blank lyric cannot be constructed to hand
    to the builder. The builder still handles the case — a contract can be relaxed, and the
    translator should not be the thing that breaks when it is — but the contract is what
    actually prevents it today, and this records which layer is load-bearing.
    """
    with pytest.raises(ValidationError):
        lyrics_for("TF-O", "   ")

    # The builder's own guard, exercised directly through a bypassing construction.
    bypassed = lyrics_for("TF-O", "placeholder").model_copy(update={"text": "   "})
    spec = build(make_blueprint("TF-O"), bypassed)
    assert spec.lyrics == INSTRUMENTAL_MARKER
    assert spec.lyrics_modified


# ------------------------------------------------------------------- misc


def test_the_vocal_style_reaches_the_caption() -> None:
    blueprint = make_blueprint("TF-P", instrumental=False)
    blueprint = blueprint.model_copy(
        update={"vocal": blueprint.vocal.model_copy(update={"style": VocalStyle.RAP})}
    )
    assert "rap vocal" in build(blueprint, lyrics_for("TF-P", "[Verse]\nwords here")).caption


def test_the_profile_supplies_the_sampling_settings_not_the_blueprint() -> None:
    """§7.8: steps and guidance are an operational choice, not a creative one.

    The blueprint says what the music should be; the profile says how hard to work on it.
    Keeping them separate is what lets §7.20 trade quality for speed under buffer pressure
    without touching what was asked for musically.
    """
    blueprint = make_blueprint("TF-Q")
    fast = build(blueprint, steps=4, guidance=2.0, profile="fast")
    quality = build(blueprint, steps=16, guidance=4.5, profile="quality")
    assert (fast.inference_steps, fast.profile) == (4, "fast")
    assert (quality.inference_steps, quality.profile) == (16, "quality")
    # The musical decision is identical either way.
    assert fast.caption == quality.caption
    assert fast.bpm == quality.bpm


def test_the_seed_comes_from_the_blueprint_by_default() -> None:
    """§23's registry only prevents reuse if the station chooses the seed."""
    blueprint = make_blueprint("TF-R", seed=4242)
    assert build(blueprint).seed == 4242


def test_metadata_records_everything_needed_to_reproduce() -> None:
    """§7.9: store the final provider prompt with the generation metadata."""
    spec = build(make_blueprint("TF-S"))
    payload = spec.as_metadata()
    for key in ("caption", "seed", "bpm", "key_scale", "inference_steps", "profile"):
        assert key in payload
    assert payload["caption"] == spec.caption
