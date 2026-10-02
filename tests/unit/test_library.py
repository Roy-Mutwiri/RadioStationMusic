"""ContentLibrary (§10, §14, §15, §100, milestone 3.1).

Two halves, and both matter:

**The shipped content is valid.** Every genre, topic, persona, format and perspective in
``tradefix_radio/config/*.yaml`` loads, cross-references resolve, and coverage is complete.
These tests are what caught a genre with no persona and an energy level no genre covered —
gaps that would otherwise have surfaced as a mid-broadcast selection failure.

**Broken content is rejected loudly.** §71's "fail clearly if invalid" applied to content:
each validation rule is tested against content that violates it, because a validation rule
that never fires is indistinguishable from no validation at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tradefix_radio.contracts.enums import MarketRegime, TradingSession
from tradefix_radio.core.errors import ConfigurationError
from tradefix_radio.director.library import (
    Band,
    ContentLibrary,
    GenreDefinition,
    LyricFormatDefinition,
    PersonaDefinition,
    TopicDefinition,
    load_content_library,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "tradefix_radio" / "config"


@pytest.fixture(scope="module")
def library() -> ContentLibrary:
    """The real shipped library. Module-scoped: loading it is pure and not cheap."""
    return load_content_library(config_dir=CONFIG_DIR)


# ---------------------------------------------------------------- Band


def test_a_band_contains_its_endpoints() -> None:
    band = Band(min=10.0, max=20.0)
    assert band.contains(10.0)
    assert band.contains(20.0)
    assert not band.contains(9.99)


def test_band_geometry() -> None:
    band = Band(min=10.0, max=20.0)
    assert band.centre == pytest.approx(15.0)
    assert band.width == pytest.approx(10.0)


def test_band_distance_is_zero_inside_and_signed_nowhere() -> None:
    """Distance is a magnitude: the selection weighting only needs how far outside."""
    band = Band(min=10.0, max=20.0)
    assert band.distance_to(15.0) == 0.0
    assert band.distance_to(4.0) == pytest.approx(6.0)
    assert band.distance_to(26.0) == pytest.approx(6.0)


def test_an_inverted_band_is_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds max"):
        Band(min=30.0, max=10.0)


def test_a_single_point_band_is_allowed() -> None:
    """Useful for a genre pinned to one BPM; only inversion is an error."""
    assert Band(min=5.0, max=5.0).contains(5.0)


# ---------------------------------------------------------------- shipped content


def test_the_shipped_library_loads(library: ContentLibrary) -> None:
    summary = library.summary()
    assert summary["genres"] >= 20
    assert summary["topics"] >= 40
    assert summary["personas"] >= 6
    assert summary["lyric_formats"] >= 8
    assert summary["perspectives"] >= 4


def test_every_energy_level_has_a_playable_genre(library: ContentLibrary) -> None:
    """The gap that would surface as a mid-broadcast selection failure.

    Checked at every point, not every fifth point as the loader's own validation does:
    the loader's 5-point grid could step over a one-point hole.
    """
    uncovered = [
        energy
        for energy in range(0, 101)
        if not library.genres_for_energy(float(energy), include_experimental=False)
    ]
    assert not uncovered, f"no non-experimental genre covers energy {uncovered}"


def test_every_energy_level_has_several_playable_genres(library: ContentLibrary) -> None:
    """One genre at an energy level means §11's variety rules have nothing to work with.

    §11 forbids more than two consecutive tracks of the same genre, so a market that
    parks at an energy level served by a single genre would deadlock the diversity
    constraints into relaxation every time.
    """
    thin = {
        energy: [g.key for g in library.genres_for_energy(float(energy), include_experimental=False)]
        for energy in range(0, 101, 5)
    }
    too_thin = {energy: keys for energy, keys in thin.items() if len(keys) < 3}
    assert not too_thin, f"fewer than three genres at: {too_thin}"


def test_every_genre_has_a_persona(library: ContentLibrary) -> None:
    """Otherwise vocal tracks in that genre are impossible."""
    orphans = [
        key
        for key in library.genre_keys
        if not any(key in persona.genres for persona in library.personas.values())
    ]
    assert not orphans, f"genres with no persona: {orphans}"


def test_every_genre_has_a_persona_whose_energy_band_overlaps(
    library: ContentLibrary,
) -> None:
    """A persona listing the genre is not enough if it never records at that energy.

    ``personas_for`` filters on both, so a genre whose only persona sits in a disjoint
    energy band still has no usable persona — a subtler version of the orphan check that
    the loader's validation does not make.
    """
    unusable = []
    for key, genre in library.genres.items():
        reachable = [
            persona.key
            for persona in library.personas.values()
            if key in persona.genres
            and persona.energy.min <= genre.energy.max
            and persona.energy.max >= genre.energy.min
        ]
        if not reachable:
            unusable.append(key)
    assert not unusable, f"genres whose personas never overlap their energy: {unusable}"


def test_no_genre_pairs_with_itself(library: ContentLibrary) -> None:
    for genre in library.genres.values():
        assert genre.key not in genre.pairs_with


def test_genre_pairings_resolve(library: ContentLibrary) -> None:
    for genre in library.genres.values():
        for other in genre.pairs_with:
            assert other in library.genres, f"{genre.key} -> {other}"


def test_every_genre_declares_a_structure_and_a_vocal_style(
    library: ContentLibrary,
) -> None:
    for genre in library.genres.values():
        assert genre.structures
        assert genre.vocal_styles
        for structure in genre.structures:
            assert structure, f"{genre.key} has an empty structure"


def test_genre_bpm_bands_are_plausible(library: ContentLibrary) -> None:
    """A BPM outside the contract's own 40–220 range would fail blueprint validation."""
    for genre in library.genres.values():
        assert 40 <= genre.bpm.min <= genre.bpm.max <= 220, genre.key


def test_experimental_genres_exist_but_are_a_minority(library: ContentLibrary) -> None:
    """§94 needs something to reward a healthy buffer with, and §86 needs a safe core."""
    summary = library.summary()
    assert summary["experimental_genres"] >= 1
    assert summary["experimental_genres"] < summary["genres"] / 2


# ---------------------------------------------------------------- §14 topics


def test_every_topic_category_is_populated(library: ContentLibrary) -> None:
    """§14 names four categories; an empty one means the station cannot talk about it."""
    assert len(library.topic_categories) >= 4
    for category in library.topic_categories:
        assert library.topics_in_category(category), category


def test_every_topic_has_lyric_ready_phrases(library: ContentLibrary) -> None:
    for topic in library.topics.values():
        assert topic.phrases, topic.key
        for phrase in topic.phrases:
            assert phrase.strip() == phrase
            assert len(phrase.split()) >= 2, f"{topic.key}: {phrase!r}"


def test_every_educational_topic_has_teaching_points(library: ContentLibrary) -> None:
    """§14: the composer must have something accurate to say."""
    for topic in library.topics.values():
        if topic.educational:
            assert topic.teaching_points, topic.key


def test_speculative_topics_forbid_direction(library: ContentLibrary) -> None:
    """§14: "must never pretend a simplified relationship guarantees price direction"."""
    speculative = [t for t in library.topics.values() if t.certainty == "speculative"]
    assert speculative, "no speculative topics — the certainty ladder is unused"
    for topic in speculative:
        assert topic.forbids_direction
        assert topic.requires_hedging


def test_contextual_topics_require_hedging(library: ContentLibrary) -> None:
    contextual = [t for t in library.topics.values() if t.certainty == "contextual"]
    assert contextual
    for topic in contextual:
        assert topic.requires_hedging


def test_established_topics_need_no_hedging(library: ContentLibrary) -> None:
    """Not everything is uncertain. "Risk management protects capital" is just true."""
    established = [t for t in library.topics.values() if t.certainty == "established"]
    assert established
    for topic in established:
        assert not topic.requires_hedging
        assert not topic.forbids_direction


def test_no_forbidden_entry_is_a_single_word(library: ContentLibrary) -> None:
    """A bare word is too blunt a veto.

    Listing ``always`` rejected the hedge "not always, but often" — which is exactly the
    phrasing §14 asks for. The rule exists because the content once did this.
    """
    for topic in library.topics.values():
        for phrase in topic.forbidden:
            assert len(phrase.split()) >= 2, f"{topic.key}: {phrase!r}"


def test_topic_pairings_resolve_and_are_not_self_referential(
    library: ContentLibrary,
) -> None:
    for topic in library.topics.values():
        assert topic.key not in topic.pairs_with
        for other in topic.pairs_with:
            assert other in library.topics, f"{topic.key} -> {other}"


def test_affinities_default_to_neutral(library: ContentLibrary) -> None:
    """Sparse affinity tables: anything unlisted must be 1.0, not 0.0.

    A 0.0 default would make every unlisted combination impossible, silently collapsing
    the library to whatever happened to be written down.
    """
    genre = next(iter(library.genres.values()))
    topic = next(iter(library.topics.values()))
    for regime in MarketRegime:
        assert genre.affinity_for(regime) > 0.0
        assert topic.affinity_for(regime) > 0.0
    for session in TradingSession:
        assert genre.session_bias(session) > 0.0
        assert topic.session_bias(session) > 0.0


def test_every_regime_is_preferred_by_some_genre(library: ContentLibrary) -> None:
    """§1 requires the regime to be audible, which needs at least one genre to lean in."""
    unloved = [
        regime.value
        for regime in MarketRegime
        if not any(
            genre.affinity_for(regime) > 1.0 for genre in library.genres.values()
        )
    ]
    assert not unloved, f"no genre prefers: {unloved}"


# ---------------------------------------------------------------- personas, formats


def test_no_persona_describes_itself_by_resemblance(library: ContentLibrary) -> None:
    """§86: never intentionally imitate a living artist.

    The schema gives "sounds like" nowhere to live, and this checks the free-text fields
    did not smuggle it back in. ``extra="forbid"`` means a ``sounds_like:`` key in the
    YAML would fail to load, so this covers the prose.
    """
    banned = ("sounds like", "in the style of", "inspired by", "à la", "a la ")
    for persona in library.personas.values():
        prose = " ".join(
            (
                persona.handle,
                *persona.vocal_character,
                *persona.signature,
            )
        ).lower()
        for phrase in banned:
            assert phrase not in prose, f"{persona.key}: {phrase!r}"


def test_persona_display_names_are_unique(library: ContentLibrary) -> None:
    """Two personas sharing a display name would read as one artist with two voices."""
    names = [persona.display_name for persona in library.personas.values()]
    assert len(set(names)) == len(names)


def test_every_lyric_format_is_used_by_a_persona(library: ContentLibrary) -> None:
    """An orphan format is dead configuration."""
    used = {f for persona in library.personas.values() for f in persona.formats}
    assert set(library.format_keys) <= used


def test_every_persona_reference_resolves(library: ContentLibrary) -> None:
    for persona in library.personas.values():
        assert persona.perspective in library.perspectives
        for key in persona.genres:
            assert key in library.genres, f"{persona.key} -> {key}"
        for key in persona.formats:
            assert key in library.formats, f"{persona.key} -> {key}"
        for key in persona.themes.topics:
            assert key in library.topics, f"{persona.key} -> {key}"
        for category in persona.themes.categories:
            assert category in library.topic_categories


def test_format_word_bounds_are_ordered_and_reachable(library: ContentLibrary) -> None:
    for fmt in library.formats.values():
        assert fmt.min_words < fmt.max_words, fmt.key
        assert fmt.sections


def test_at_least_one_format_allows_instrumental_tracks(
    library: ContentLibrary,
) -> None:
    """§16's instrumental decision needs somewhere to land."""
    assert any(fmt.allows_instrumental for fmt in library.formats.values())


def test_formats_span_the_energy_range(library: ContentLibrary) -> None:
    uncovered = [
        energy
        for energy in range(0, 101, 5)
        if not any(fmt.energy.contains(float(energy)) for fmt in library.formats.values())
    ]
    assert not uncovered, f"no lyric format covers energy {uncovered}"


# ---------------------------------------------------------------- lookups


def test_an_unknown_key_raises_a_configuration_error(library: ContentLibrary) -> None:
    """Naming the missing key is the difference between a minute and an hour of debugging."""
    for lookup, label in (
        (library.genre, "genre"),
        (library.topic, "topic"),
        (library.persona, "persona"),
        (library.lyric_format, "lyric format"),
        (library.perspective, "perspective"),
    ):
        with pytest.raises(ConfigurationError, match=f"unknown {label} 'nope'"):
            lookup("nope")


def test_genres_for_energy_can_exclude_experimental(library: ContentLibrary) -> None:
    # Compared by key: a GenreDefinition holds affinity dicts, so it is frozen but not
    # hashable, and a set of definitions raises rather than comparing.
    everything = {genre.key for genre in library.genres_for_energy(55.0)}
    safe = library.genres_for_energy(55.0, include_experimental=False)
    assert {genre.key for genre in safe} <= everything
    assert all(not genre.experimental for genre in safe)
    assert len(safe) < len(everything) or not any(
        genre.experimental for genre in library.genres.values()
    )


def test_personas_for_filters_on_genre_and_energy(library: ContentLibrary) -> None:
    genre_key = library.genre_keys[0]
    genre = library.genre(genre_key)
    found = library.personas_for(genre_key, genre.energy.centre)
    for persona in found:
        assert genre_key in persona.genres
        assert persona.energy.contains(genre.energy.centre)


# ---------------------------------------------------------------- broken content
#
# Built by mutating the real YAML, so a fixture cannot drift away from the shipped shape
# and quietly stop exercising the rule it was written for.


def _raw(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def _write(directory: Path, name: str, data: dict[str, Any]) -> None:
    (directory / name).write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    """A writable copy of the shipped content."""
    for name in ("genres.yaml", "topics.yaml", "personas.yaml"):
        _write(tmp_path, name, _raw(name))
    return tmp_path


def test_the_copied_content_still_loads(content_dir: Path) -> None:
    """Guards the mutation tests below: they only mean something if the base is valid."""
    assert load_content_library(config_dir=content_dir).summary()["genres"] >= 20


def test_a_dangling_genre_pairing_is_rejected(content_dir: Path) -> None:
    data = _raw("genres.yaml")
    first = next(iter(data["genres"]))
    data["genres"][first]["pairs_with"] = ["no_such_genre"]
    _write(content_dir, "genres.yaml", data)
    with pytest.raises(ConfigurationError, match="pairs_with unknown genre"):
        load_content_library(config_dir=content_dir)


def test_a_dangling_persona_genre_is_rejected(content_dir: Path) -> None:
    data = _raw("personas.yaml")
    first = next(iter(data["personas"]))
    data["personas"][first]["genres"] = ["no_such_genre"]
    _write(content_dir, "personas.yaml", data)
    with pytest.raises(ConfigurationError, match="unknown genre"):
        load_content_library(config_dir=content_dir)


def test_a_dangling_perspective_is_rejected(content_dir: Path) -> None:
    data = _raw("personas.yaml")
    first = next(iter(data["personas"]))
    data["personas"][first]["perspective"] = "no_such_perspective"
    _write(content_dir, "personas.yaml", data)
    with pytest.raises(ConfigurationError, match="unknown perspective"):
        load_content_library(config_dir=content_dir)


def test_a_genre_with_no_persona_is_rejected(content_dir: Path) -> None:
    """The defect this validation was written for, reproduced.

    ``tech_house`` shipped without a persona; the gap would have appeared as a failed
    vocal selection somewhere in hour three.
    """
    data = _raw("personas.yaml")
    orphan = "__orphan_genre__"
    genres = _raw("genres.yaml")
    template = dict(genres["genres"][next(iter(genres["genres"]))])
    genres["genres"][orphan] = {**template, "label": "Orphan"}
    _write(content_dir, "genres.yaml", genres)
    _write(content_dir, "personas.yaml", data)
    with pytest.raises(ConfigurationError, match=f"genre '{orphan}' has no persona"):
        load_content_library(config_dir=content_dir)


def test_an_energy_hole_is_rejected(content_dir: Path) -> None:
    """Narrow every genre to a sliver so most of the range is uncovered."""
    data = _raw("genres.yaml")
    for entry in data["genres"].values():
        entry["energy"] = {"min": 50, "max": 52}
    _write(content_dir, "genres.yaml", data)
    with pytest.raises(ConfigurationError, match="no non-experimental genre covers energy"):
        load_content_library(config_dir=content_dir)


def test_an_orphan_lyric_format_is_rejected(content_dir: Path) -> None:
    data = _raw("personas.yaml")
    template = dict(data["formats"][next(iter(data["formats"]))])
    data["formats"]["__orphan_format__"] = template
    _write(content_dir, "personas.yaml", data)
    with pytest.raises(ConfigurationError, match="used by no persona"):
        load_content_library(config_dir=content_dir)


def test_every_validation_failure_is_reported_together(content_dir: Path) -> None:
    """§71: fail clearly. One error at a time means one restart per typo."""
    data = _raw("genres.yaml")
    keys = list(data["genres"])[:3]
    for key in keys:
        data["genres"][key]["pairs_with"] = ["no_such_genre"]
    _write(content_dir, "genres.yaml", data)
    with pytest.raises(ConfigurationError) as caught:
        load_content_library(config_dir=content_dir)
    for key in keys:
        assert key in str(caught.value)


def test_a_malformed_entry_names_the_key_that_failed(content_dir: Path) -> None:
    data = _raw("genres.yaml")
    key = next(iter(data["genres"]))
    data["genres"][key]["bpm"] = {"min": 200, "max": 100}
    _write(content_dir, "genres.yaml", data)
    with pytest.raises(ConfigurationError, match=f"genres.{key}"):
        load_content_library(config_dir=content_dir)


def test_an_unknown_field_is_rejected(content_dir: Path) -> None:
    """``extra="forbid"`` is what stops a typo'd key being silently ignored."""
    data = _raw("genres.yaml")
    key = next(iter(data["genres"]))
    data["genres"][key]["vocal_afinity"] = 0.5
    _write(content_dir, "genres.yaml", data)
    with pytest.raises(ConfigurationError, match="vocal_afinity"):
        load_content_library(config_dir=content_dir)


def test_a_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="content library file not found"):
        load_content_library(config_dir=tmp_path)


def test_invalid_yaml_is_reported_as_such(content_dir: Path) -> None:
    (content_dir / "genres.yaml").write_text("genres: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_content_library(config_dir=content_dir)


def test_a_non_mapping_top_level_is_rejected(content_dir: Path) -> None:
    (content_dir / "genres.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must contain a mapping"):
        load_content_library(config_dir=content_dir)


def test_an_empty_section_is_rejected(content_dir: Path) -> None:
    (content_dir / "genres.yaml").write_text("genres: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="no usable 'genres' section"):
        load_content_library(config_dir=content_dir)


# ---------------------------------------------------------------- model rules


def test_a_genre_may_not_pair_with_itself() -> None:
    with pytest.raises(ValueError, match="cannot pair with itself"):
        GenreDefinition(
            key="x",
            label="X",
            bpm=Band(min=120, max=124),
            energy=Band(min=40, max=60),
            vocal_affinity=0.5,
            vocal_styles=("rap",),
            rhythm_density=0.5,
            bass_intensity=0.5,
            drum_intensity=0.5,
            melodic_complexity=0.5,
            structures=(("intro", "verse"),),
            pairs_with=("x",),
        )


def test_a_genre_needs_at_least_one_structure() -> None:
    with pytest.raises(ValueError, match="at least one structure"):
        GenreDefinition(
            key="x",
            label="X",
            bpm=Band(min=120, max=124),
            energy=Band(min=40, max=60),
            vocal_affinity=0.5,
            vocal_styles=("rap",),
            rhythm_density=0.5,
            bass_intensity=0.5,
            drum_intensity=0.5,
            melodic_complexity=0.5,
            structures=(),
        )


def test_an_unknown_certainty_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="certainty must be one of"):
        TopicDefinition(
            key="x",
            category="mindset",
            label="X",
            certainty="definitely",
            weight=1.0,
            teaching_points=("a point",),
            phrases=("a phrase",),
        )


def test_an_educational_topic_without_teaching_points_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one teaching point"):
        TopicDefinition(
            key="x",
            category="mindset",
            label="X",
            certainty="established",
            weight=1.0,
            educational=True,
            phrases=("a phrase",),
        )


def test_a_topic_without_phrases_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one lyric-ready phrase"):
        TopicDefinition(
            key="x",
            category="mindset",
            label="X",
            certainty="established",
            weight=1.0,
            educational=False,
        )


def test_a_single_word_forbidden_entry_is_rejected() -> None:
    with pytest.raises(ValueError, match="is a single word"):
        TopicDefinition(
            key="x",
            category="mindset",
            label="X",
            certainty="established",
            weight=1.0,
            educational=False,
            phrases=("a phrase",),
            forbidden=("always",),
        )


def test_inverted_format_word_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="must be below max_words"):
        LyricFormatDefinition(
            key="x",
            density=0.5,
            educational=0.5,
            sections=("verse",),
            min_words=200,
            max_words=100,
            energy=Band(min=0, max=100),
        )


def test_a_persona_needs_a_genre_a_format_and_a_voice() -> None:
    base: dict[str, Any] = {
        "key": "x",
        "call_sign": "TF-99",
        "handle": "Test",
        "genres": ("deep_house",),
        "energy": Band(min=0, max=100),
        "vocal_character": ("calm",),
        "vocal_styles": ("rap",),
        "themes": {"categories": (), "topics": ()},
        "formats": ("verse_chorus",),
        "perspective": "first_person",
        "instrumental_affinity": 0.2,
    }
    for field, message in (
        ("genres", "at least one genre"),
        ("formats", "at least one lyric format"),
        ("vocal_styles", "at least one vocal style"),
    ):
        with pytest.raises(ValueError, match=message):
            PersonaDefinition(**{**base, field: ()})


def test_library_items_are_immutable() -> None:
    """Content is loaded once and read everywhere; a mutable definition is a data race."""
    band = Band(min=1.0, max=2.0)
    with pytest.raises(ValueError, match="frozen"):
        band.min = 5.0  # type: ignore[misc]
