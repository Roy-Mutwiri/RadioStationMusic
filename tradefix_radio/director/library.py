"""Content library: genres, topics, personas, formats (§10, §14, §15, §100).

Loads the YAML libraries into typed, validated objects. Everything creative about the
station is data; nothing in the code names a genre, a topic or a persona.

Validation is **aggressive and happens at startup**, per §71. The checks worth noting
are the referential and coverage ones, because they catch the mistakes that would
otherwise surface as a silently narrowed station hours later:

* a ``pairs_with`` naming a genre that does not exist
* a persona listing a genre, format or perspective that does not exist
* a ``regime_affinity`` key that is not a real §5 regime (a typo here would be
  silently ignored and the intended bias would simply never apply)
* **energy coverage**: every point on the 0–100 energy scale must be inside at least
  one non-experimental genre's band. Without this check a gap at, say, energy 95
  would leave the director with nothing to pick during a breakout — discovered at the
  worst possible moment rather than at startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradefix_radio.contracts.enums import MarketRegime, TradingSession, VocalStyle
from tradefix_radio.core.errors import ConfigurationError

#: Accepted values for a topic's §14 certainty qualifier.
CERTAINTY_LEVELS = ("established", "contextual", "speculative")

#: Certainty levels whose statements must be hedged in the lyric (§14).
HEDGED_CERTAINTIES = frozenset({"contextual", "speculative"})

#: Certainty level that may never be used to imply a price direction (§14).
NON_DIRECTIONAL_CERTAINTIES = frozenset({"speculative"})


class LibraryItem(BaseModel):
    """Base for every library definition: strict, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Band(LibraryItem):
    """An inclusive numeric band."""

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Band:
        if self.min > self.max:
            raise ValueError(f"band min ({self.min}) exceeds max ({self.max})")
        return self

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max

    @property
    def centre(self) -> float:
        return (self.min + self.max) / 2.0

    @property
    def width(self) -> float:
        return self.max - self.min

    def distance_to(self, value: float) -> float:
        """How far ``value`` lies outside the band; 0 when inside."""
        if value < self.min:
            return self.min - value
        if value > self.max:
            return value - self.max
        return 0.0


class GenreDefinition(LibraryItem):
    """One §10 genre."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=96)
    bpm: Band
    energy: Band

    vocal_affinity: float = Field(ge=0.0, le=1.0)
    vocal_styles: tuple[VocalStyle, ...]
    instrumentation: tuple[str, ...] = ()
    mood: tuple[str, ...] = ()

    rhythm_density: float = Field(ge=0.0, le=1.0)
    bass_intensity: float = Field(ge=0.0, le=1.0)
    drum_intensity: float = Field(ge=0.0, le=1.0)
    melodic_complexity: float = Field(ge=0.0, le=1.0)

    experimental: bool = False
    regime_affinity: dict[MarketRegime, float] = Field(default_factory=dict)
    session_affinity: dict[TradingSession, float] = Field(default_factory=dict)
    pairs_with: tuple[str, ...] = ()
    structures: tuple[tuple[str, ...], ...]

    @model_validator(mode="after")
    def _check(self) -> GenreDefinition:
        if not self.structures:
            raise ValueError("a genre must define at least one structure")
        if not self.vocal_styles:
            raise ValueError("a genre must list at least one vocal style")
        for name, mapping in (
            ("regime_affinity", self.regime_affinity),
            ("session_affinity", self.session_affinity),
        ):
            for key, weight in mapping.items():
                if weight < 0:
                    raise ValueError(f"{name}[{key}] is negative")
        if self.key in self.pairs_with:
            raise ValueError("a genre cannot pair with itself")
        return self

    def affinity_for(self, regime: MarketRegime) -> float:
        """Regime nudge. 1.0 (neutral) for anything unlisted."""
        return self.regime_affinity.get(regime, 1.0)

    def session_bias(self, session: TradingSession) -> float:
        return self.session_affinity.get(session, 1.0)


class TopicDefinition(LibraryItem):
    """One §14 trading topic."""

    key: str = Field(min_length=1, max_length=96)
    category: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=160)
    certainty: str
    weight: float = Field(ge=0.0)
    educational: bool = True
    regime_affinity: dict[MarketRegime, float] = Field(default_factory=dict)
    session_affinity: dict[TradingSession, float] = Field(default_factory=dict)
    teaching_points: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    pairs_with: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> TopicDefinition:
        if self.certainty not in CERTAINTY_LEVELS:
            raise ValueError(
                f"certainty must be one of {CERTAINTY_LEVELS}, got {self.certainty!r}"
            )
        if self.educational and not self.teaching_points:
            raise ValueError(
                "an educational topic must supply at least one teaching point; "
                "otherwise the composer has nothing accurate to say about it"
            )
        if not self.phrases:
            raise ValueError("a topic must supply at least one lyric-ready phrase")
        if self.key in self.pairs_with:
            raise ValueError("a topic cannot pair with itself")
        for phrase in self.forbidden:
            if len(phrase.split()) < 2:
                raise ValueError(
                    f"forbidden entry {phrase!r} is a single word; forbidden entries must "
                    "be phrases. A bare word is too blunt — listing 'always' rejected the "
                    "correct hedge 'not always, but often', which is exactly the phrasing "
                    "§14 asks for"
                )
        return self

    @property
    def requires_hedging(self) -> bool:
        """§14: contextual and speculative claims must be hedged."""
        return self.certainty in HEDGED_CERTAINTIES

    @property
    def forbids_direction(self) -> bool:
        """§14: a speculative relationship may never imply a price direction."""
        return self.certainty in NON_DIRECTIONAL_CERTAINTIES

    def affinity_for(self, regime: MarketRegime) -> float:
        return self.regime_affinity.get(regime, 1.0)

    def session_bias(self, session: TradingSession) -> float:
        return self.session_affinity.get(session, 1.0)


class PersonaThemes(LibraryItem):
    categories: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


class PersonaDefinition(LibraryItem):
    """One §100 fictional station persona.

    There is deliberately **no field** for "sounds like" or "in the style of". §86
    forbids intentionally imitating a living artist, so the schema gives that nowhere
    to live. A persona is described by what it is about, never by whom it resembles.
    """

    key: str = Field(min_length=1, max_length=48)
    call_sign: str = Field(min_length=1, max_length=32)
    handle: str = Field(min_length=1, max_length=64)
    genres: tuple[str, ...]
    energy: Band
    vocal_character: tuple[str, ...]
    vocal_styles: tuple[VocalStyle, ...]
    themes: PersonaThemes
    formats: tuple[str, ...]
    perspective: str = Field(min_length=1, max_length=48)
    signature: tuple[str, ...] = ()
    instrumental_affinity: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> PersonaDefinition:
        if not self.genres:
            raise ValueError("a persona must record in at least one genre")
        if not self.formats:
            raise ValueError("a persona must work in at least one lyric format")
        if not self.vocal_styles:
            raise ValueError("a persona must have at least one vocal style")
        return self

    @property
    def display_name(self) -> str:
        """``TF-03 Overlap`` — what the §42 card and OBS artist source show."""
        return f"{self.call_sign} {self.handle}"


class LyricFormatDefinition(LibraryItem):
    """One §15 lyric format."""

    key: str = Field(min_length=1, max_length=64)
    density: float = Field(ge=0.0, le=1.0)
    educational: float = Field(ge=0.0, le=1.0)
    sections: tuple[str, ...]
    min_words: int = Field(ge=1)
    max_words: int = Field(ge=2)
    energy: Band
    brand_forward: bool = False
    allows_instrumental: bool = False

    @model_validator(mode="after")
    def _check(self) -> LyricFormatDefinition:
        if self.min_words >= self.max_words:
            raise ValueError(
                f"min_words ({self.min_words}) must be below max_words ({self.max_words})"
            )
        if not self.sections:
            raise ValueError("a format must define at least one section")
        return self


class PerspectiveDefinition(LibraryItem):
    """A narrator viewpoint (§13)."""

    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=64)
    pronoun: str = Field(min_length=1, max_length=16)
    possessive: str = Field(min_length=1, max_length=16)
    description: str = Field(min_length=1, max_length=200)


class ContentLibrary:
    """Everything creative the director selects from, validated and cross-checked."""

    def __init__(
        self,
        *,
        genres: dict[str, GenreDefinition],
        topics: dict[str, TopicDefinition],
        personas: dict[str, PersonaDefinition],
        formats: dict[str, LyricFormatDefinition],
        perspectives: dict[str, PerspectiveDefinition],
    ) -> None:
        self.genres = genres
        self.topics = topics
        self.personas = personas
        self.formats = formats
        self.perspectives = perspectives
        self._validate_references()
        self._validate_coverage()

    # -- lookups -----------------------------------------------------------

    @property
    def genre_keys(self) -> tuple[str, ...]:
        return tuple(self.genres)

    @property
    def topic_keys(self) -> tuple[str, ...]:
        return tuple(self.topics)

    @property
    def persona_keys(self) -> tuple[str, ...]:
        return tuple(self.personas)

    @property
    def format_keys(self) -> tuple[str, ...]:
        return tuple(self.formats)

    def genre(self, key: str) -> GenreDefinition:
        try:
            return self.genres[key]
        except KeyError as exc:
            raise ConfigurationError(f"unknown genre {key!r}") from exc

    def topic(self, key: str) -> TopicDefinition:
        try:
            return self.topics[key]
        except KeyError as exc:
            raise ConfigurationError(f"unknown topic {key!r}") from exc

    def persona(self, key: str) -> PersonaDefinition:
        try:
            return self.personas[key]
        except KeyError as exc:
            raise ConfigurationError(f"unknown persona {key!r}") from exc

    def lyric_format(self, key: str) -> LyricFormatDefinition:
        try:
            return self.formats[key]
        except KeyError as exc:
            raise ConfigurationError(f"unknown lyric format {key!r}") from exc

    def perspective(self, key: str) -> PerspectiveDefinition:
        try:
            return self.perspectives[key]
        except KeyError as exc:
            raise ConfigurationError(f"unknown perspective {key!r}") from exc

    def topics_in_category(self, category: str) -> tuple[TopicDefinition, ...]:
        return tuple(t for t in self.topics.values() if t.category == category)

    @property
    def topic_categories(self) -> tuple[str, ...]:
        return tuple(sorted({topic.category for topic in self.topics.values()}))

    def genres_for_energy(
        self, energy: float, *, include_experimental: bool = True
    ) -> tuple[GenreDefinition, ...]:
        """Genres whose band contains ``energy``."""
        return tuple(
            genre
            for genre in self.genres.values()
            if genre.energy.contains(energy)
            and (include_experimental or not genre.experimental)
        )

    def personas_for(
        self, genre_key: str, energy: float
    ) -> tuple[PersonaDefinition, ...]:
        """Personas that record in ``genre_key`` and suit ``energy``."""
        return tuple(
            persona
            for persona in self.personas.values()
            if genre_key in persona.genres and persona.energy.contains(energy)
        )

    # -- validation --------------------------------------------------------

    def _validate_references(self) -> None:
        """Every cross-reference in the libraries must resolve."""
        errors: list[str] = []

        for genre in self.genres.values():
            for other in genre.pairs_with:
                if other not in self.genres:
                    errors.append(
                        f"genre {genre.key!r} pairs_with unknown genre {other!r}"
                    )

        for topic in self.topics.values():
            for other in topic.pairs_with:
                if other not in self.topics:
                    errors.append(
                        f"topic {topic.key!r} pairs_with unknown topic {other!r}"
                    )

        categories = set(self.topic_categories)
        for persona in self.personas.values():
            for genre_key in persona.genres:
                if genre_key not in self.genres:
                    errors.append(
                        f"persona {persona.key!r} lists unknown genre {genre_key!r}"
                    )
            for format_key in persona.formats:
                if format_key not in self.formats:
                    errors.append(
                        f"persona {persona.key!r} lists unknown format {format_key!r}"
                    )
            if persona.perspective not in self.perspectives:
                errors.append(
                    f"persona {persona.key!r} uses unknown perspective "
                    f"{persona.perspective!r}"
                )
            for category in persona.themes.categories:
                if category not in categories:
                    errors.append(
                        f"persona {persona.key!r} lists unknown topic category "
                        f"{category!r}"
                    )
            for topic_key in persona.themes.topics:
                if topic_key not in self.topics:
                    errors.append(
                        f"persona {persona.key!r} lists unknown topic {topic_key!r}"
                    )

        if errors:
            joined = "\n".join(f"  - {message}" for message in errors)
            raise ConfigurationError(
                f"content library has unresolved references:\n{joined}"
            )

    def _validate_coverage(self) -> None:
        """The director must always have something to choose.

        Three gaps are checked, each of which would otherwise be discovered mid-
        broadcast:

        * an energy level no non-experimental genre covers — the director would have
          nothing to pick at that energy;
        * a genre no persona records in — vocal tracks in that genre would be
          impossible;
        * a regime no topic prefers — lyrics would have nothing apt to say.
        """
        errors: list[str] = []

        for energy in range(0, 101, 5):
            playable = self.genres_for_energy(float(energy), include_experimental=False)
            if not playable:
                errors.append(
                    f"no non-experimental genre covers energy {energy}; the director "
                    "would have nothing to select at that market energy"
                )

        for genre_key in self.genres:
            if not any(genre_key in persona.genres for persona in self.personas.values()):
                errors.append(
                    f"genre {genre_key!r} has no persona; vocal tracks in it are "
                    "impossible"
                )

        if not self.topics:
            errors.append("the topic library is empty")
        if not self.formats:
            errors.append("the lyric format library is empty")
        if not self.perspectives:
            errors.append("the perspective library is empty")

        # Every format a persona can request must have at least one persona that
        # actually uses it; an orphan format is dead configuration.
        used_formats = {f for persona in self.personas.values() for f in persona.formats}
        for format_key in self.formats:
            if format_key not in used_formats:
                errors.append(f"lyric format {format_key!r} is used by no persona")

        if errors:
            joined = "\n".join(f"  - {message}" for message in errors)
            raise ConfigurationError(f"content library coverage is incomplete:\n{joined}")

    def summary(self) -> dict[str, int]:
        """Counts, for the §79 doctor table and the §50 settings page."""
        return {
            "genres": len(self.genres),
            "experimental_genres": sum(1 for g in self.genres.values() if g.experimental),
            "topics": len(self.topics),
            "topic_categories": len(self.topic_categories),
            "personas": len(self.personas),
            "lyric_formats": len(self.formats),
            "perspectives": len(self.perspectives),
        }


# ---------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"content library file not found: {path}", path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"{path} is not valid YAML", path=str(path), detail=str(exc)
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path} must contain a mapping at the top level")
    return raw


def _merge_defaults(defaults: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge of a defaults block under one entry.

    Shallow on purpose: a deep merge of ``regime_affinity`` would silently blend a
    default bias into every genre, which is the opposite of the sparse, explicit
    affinity design.
    """
    merged = dict(defaults)
    merged.update(entry)
    return merged


def _build(
    path: Path,
    section: str,
    model: type[LibraryItem],
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Validate every entry in a section, reporting the key that failed."""
    entries = raw.get(section)
    if not isinstance(entries, dict) or not entries:
        raise ConfigurationError(
            f"{path} has no usable {section!r} section", path=str(path)
        )
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigurationError(f"{path} 'defaults' must be a mapping", path=str(path))

    built: dict[str, Any] = {}
    errors: list[str] = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            errors.append(f"{section}.{key} must be a mapping")
            continue
        # Only genres and topics have a defaults block; formats, personas and
        # perspectives declare every field explicitly.
        if section in {"genres", "topics"}:
            merged = _merge_defaults(defaults, entry)
        else:
            merged = dict(entry)
        merged["key"] = key
        try:
            built[key] = model.model_validate(merged)
        except Exception as exc:  # noqa: BLE001 - re-raised with the key attached
            errors.append(f"{section}.{key}: {exc}")

    if errors:
        joined = "\n".join(f"  - {message}" for message in errors)
        raise ConfigurationError(f"{path} is invalid:\n{joined}", path=str(path))
    return built


def load_content_library(
    *,
    config_dir: Path,
    genre_file: str = "genres.yaml",
    topic_file: str = "topics.yaml",
    persona_file: str = "personas.yaml",
) -> ContentLibrary:
    """Load and cross-validate every creative library.

    Called once at startup. A failure here is a :class:`ConfigurationError` listing
    every problem at once — §71's "fail clearly if invalid" applied to content as well
    as to numbers.
    """
    genre_raw = _read_yaml(config_dir / genre_file)
    topic_raw = _read_yaml(config_dir / topic_file)
    persona_raw = _read_yaml(config_dir / persona_file)

    genres = _build(config_dir / genre_file, "genres", GenreDefinition, genre_raw)
    topics = _build(config_dir / topic_file, "topics", TopicDefinition, topic_raw)
    personas = _build(config_dir / persona_file, "personas", PersonaDefinition, persona_raw)
    formats = _build(
        config_dir / persona_file, "formats", LyricFormatDefinition, persona_raw
    )
    perspectives = _build(
        config_dir / persona_file, "perspectives", PerspectiveDefinition, persona_raw
    )

    return ContentLibrary(
        genres=genres,
        topics=topics,
        personas=personas,
        formats=formats,
        perspectives=perspectives,
    )


__all__ = [
    "CERTAINTY_LEVELS",
    "HEDGED_CERTAINTIES",
    "NON_DIRECTIONAL_CERTAINTIES",
    "Band",
    "ContentLibrary",
    "GenreDefinition",
    "LyricFormatDefinition",
    "PersonaDefinition",
    "PerspectiveDefinition",
    "TopicDefinition",
    "load_content_library",
]
