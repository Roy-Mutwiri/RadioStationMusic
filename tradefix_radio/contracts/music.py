"""The MusicBlueprint — the only thing a generator ever receives (§8, §18).

This contract is the architectural seam the brief insists on: "The music
generator should only receive a MusicBlueprint object" (§2). Everything
market-aware happens upstream; everything model-specific happens downstream. A
new provider (§18) implements one method against this object and nothing else
changes.

``market`` is embedded as a *snapshot of the decision context*, not a live
reference. §8 requires the complete blueprint be saved for every track, and the
point of saving it is forensic: six days into a run, an operator asking "why is
this 151 BPM" needs the regime and energy **as they were when the decision was
made**, not as they are now.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field, computed_field, field_validator, model_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.enums import (
    GenerationPriority,
    MarketDirection,
    MarketRegime,
    VocalStyle,
)
from tradefix_radio.contracts.market import Score100, Unit

#: Accepted pitch spellings. Both sharps and flats are allowed because genre
#: convention differs (producers write F# minor, not Gb minor) and the key string
#: ends up in the generation prompt, where idiomatic spelling reads better.
_PITCHES = (
    "C", "C#", "Db", "D", "D#", "Eb", "E", "F",
    "F#", "Gb", "G", "G#", "Ab", "A", "A#", "Bb", "B",
)
_MODES = ("major", "minor", "dorian", "mixolydian", "lydian", "phrygian", "harmonic minor")


class BlueprintMarketContextV1(Contract):
    """The market conditions that produced this blueprint (§8 ``market``)."""

    #: Which market this track was planned against.
    #:
    #: Recorded on the blueprint, not derived at render time, because a track generated
    #: under XAUUSD may still be sitting in the queue after the station has moved to
    #: BTCUSD. Without this the §46 detail page and the queue would attribute it to
    #: whichever market happened to be active when someone looked.
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=32)
    regime: MarketRegime
    direction: MarketDirection
    energy: Score100
    trend_strength: Score100
    volatility: Score100
    confidence: Unit
    session: str = Field(min_length=1, max_length=48)


class CompositionSpecV1(Contract):
    """Musical direction (§8 ``composition``).

    Intensity fields are unit-interval *directions to the generator*, not
    measurements. They become part of the text prompt and, where a provider
    supports it, explicit conditioning. They are separate from ``energy`` because
    a track can be high-energy and sparse (minimal techno) or low-energy and busy
    (jazzhop), and collapsing them would flatten the station's range.
    """

    genre: str = Field(min_length=1, max_length=64)
    secondary_genre: str | None = Field(default=None, max_length=64)
    bpm: int = Field(ge=40, le=220)
    key: str = Field(min_length=1, max_length=32)
    duration_seconds: int = Field(ge=30, le=600)

    energy: Unit
    rhythm_density: Unit
    bass_intensity: Unit
    drum_intensity: Unit
    melodic_complexity: Unit

    #: Section labels in order, e.g. ``["intro", "verse", "hook", ...]``.
    #: Config-driven vocabulary, so not an enum.
    structure: tuple[str, ...] = Field(default_factory=tuple)
    #: Free-form instrumentation hints fed to the prompt.
    instrumentation: tuple[str, ...] = Field(default_factory=tuple)
    #: Emotional tone words (§1 "emotional tone").
    mood: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        """Reject malformed keys at the contract boundary.

        Worth validating because a nonsense key silently degrades every prompt
        and would be nearly invisible in a week-long run — it would just sound
        slightly worse.
        """
        # Split on the FIRST space only: some modes are two words
        # ("harmonic minor"), and splitting on all whitespace would reject them.
        pitch, separator, raw_mode = value.strip().partition(" ")
        if not separator or not raw_mode:
            raise ValueError(f"key must be '<pitch> <mode>', got {value!r}")
        mode = " ".join(raw_mode.lower().split())
        if pitch not in _PITCHES:
            raise ValueError(f"unknown pitch {pitch!r}; expected one of {_PITCHES}")
        if mode not in _MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {_MODES}")
        return f"{pitch} {mode}"

    @model_validator(mode="after")
    def _distinct_genres(self) -> CompositionSpecV1:
        if self.secondary_genre is not None and self.secondary_genre == self.genre:
            raise ValueError("secondary_genre must differ from genre")
        return self


class VocalSpecV1(Contract):
    """Vocal direction (§8 ``vocal``)."""

    enabled: bool
    style: VocalStyle
    density: Unit

    @model_validator(mode="after")
    def _coherent(self) -> VocalSpecV1:
        if self.enabled and self.style is VocalStyle.NONE:
            raise ValueError("vocal.enabled is True but style is NONE")
        if not self.enabled:
            if self.style is not VocalStyle.NONE:
                raise ValueError("vocal.style must be NONE when vocals are disabled")
            if self.density != 0.0:
                raise ValueError("vocal.density must be 0 when vocals are disabled")
        return self


class LyricsSpecV1(Contract):
    """Lyrical direction (§8 ``lyrics``).

    This is the *brief* for the lyric engine, not the lyrics themselves — those
    are :class:`~tradefix_radio.contracts.lyrics.LyricsV1`. Separating them is
    what lets the §17 validators reject a lyric and request a rewrite without
    invalidating the whole musical blueprint.
    """

    enabled: bool
    primary_topic: str | None = Field(default=None, max_length=96)
    secondary_topic: str | None = Field(default=None, max_length=96)
    #: 0, 1 or 2 natural mentions (§16). Spamming the brand is forbidden.
    tradefix_mentions: int = Field(ge=0, le=2)
    educational_intensity: Unit
    #: One of the §15 formats. Config-driven vocabulary.
    format: str | None = Field(default=None, max_length=64)
    #: Narrator viewpoint, e.g. ``first_person``, ``observer``, ``mentor``.
    perspective: str | None = Field(default=None, max_length=48)

    @model_validator(mode="after")
    def _coherent(self) -> LyricsSpecV1:
        if not self.enabled:
            if self.primary_topic or self.secondary_topic:
                raise ValueError("topics must be unset when lyrics are disabled")
            if self.tradefix_mentions:
                raise ValueError("tradefix_mentions must be 0 when lyrics are disabled")
            if self.educational_intensity != 0.0:
                raise ValueError("educational_intensity must be 0 when lyrics are disabled")
        elif not self.primary_topic:
            raise ValueError("primary_topic is required when lyrics are enabled")
        if (
            self.primary_topic is not None
            and self.secondary_topic is not None
            and self.primary_topic == self.secondary_topic
        ):
            raise ValueError("secondary_topic must differ from primary_topic")
        return self


class NoveltySpecV1(Contract):
    """Originality target for this candidate (§8 ``novelty``).

    A *target*, not a guarantee. Phrasing matters: §21 and §86 forbid claiming
    that any score proves originality.
    """

    target: Unit


class MusicBlueprintV1(Contract):
    """Complete creative specification for one track (§8).

    Note what is absent: no model name, no checkpoint path, no sampler settings.
    Those belong to :class:`~tradefix_radio.contracts.generation.GenerationRequestV1`,
    which wraps a blueprint with provider specifics. Keeping them out is what
    stops "ACE-Step-specific details leaking throughout the core architecture"
    (§18).
    """

    track_id: str = Field(min_length=1, max_length=64)
    created_at_iso: str = Field(min_length=1, max_length=40)

    market: BlueprintMarketContextV1
    composition: CompositionSpecV1
    vocal: VocalSpecV1
    lyrics: LyricsSpecV1
    novelty: NoveltySpecV1

    #: Fictional station persona (§100). Never a real artist.
    persona_id: str | None = Field(default=None, max_length=48)
    #: Original title (§99); assigned by the director, not the model.
    title: str = Field(min_length=1, max_length=120)

    #: §94 scheduling priority and §95 creative temperature at decision time.
    priority: GenerationPriority = GenerationPriority.NORMAL
    creative_temperature: Unit = 0.5

    #: Reproducibility seed. Metadata only — §23 is explicit that seeds are not
    #: proof of uniqueness.
    seed: int = Field(ge=0, le=2**63 - 1)

    #: Why the director chose this, for the §48/§47 explainability panes.
    rationale: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _vocal_lyric_coherence(self) -> MusicBlueprintV1:
        """Lyrics without vocals is incoherent; catch it at the boundary.

        An instrumental blueprint carrying a lyric topic would produce a track
        whose stored metadata lies about its own content, which then poisons the
        §12 topic-repetition horizons.
        """
        if self.lyrics.enabled and not self.vocal.enabled:
            raise ValueError("lyrics.enabled requires vocal.enabled")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_instrumental(self) -> bool:
        return not self.vocal.enabled

    def signature_payload(self) -> dict[str, Any]:
        """The creative identity of this blueprint, excluding incidentals.

        Excluded on purpose: ``track_id``, ``created_at_iso``, ``seed``,
        ``title``, ``rationale``, ``priority``, ``creative_temperature``, and the
        market context. Two tracks made minutes apart from the same market
        conditions with the same genre/BPM/key/topic *are* the same creative
        decision even though all of those differ — and §11's "same blueprint:
        never repeat" is about the decision, not the paperwork.
        """
        composition = self.composition
        return {
            "genre": composition.genre,
            "secondary_genre": composition.secondary_genre,
            "bpm": composition.bpm,
            "key": composition.key,
            "duration_bucket": composition.duration_seconds // 15,
            "structure": list(composition.structure),
            "instrumentation": sorted(composition.instrumentation),
            "mood": sorted(composition.mood),
            "energy_bucket": round(composition.energy * 10),
            "rhythm_bucket": round(composition.rhythm_density * 10),
            "bass_bucket": round(composition.bass_intensity * 10),
            "drum_bucket": round(composition.drum_intensity * 10),
            "melodic_bucket": round(composition.melodic_complexity * 10),
            "vocal_enabled": self.vocal.enabled,
            "vocal_style": self.vocal.style.value,
            "vocal_density_bucket": round(self.vocal.density * 10),
            "lyrics_enabled": self.lyrics.enabled,
            "primary_topic": self.lyrics.primary_topic,
            "secondary_topic": self.lyrics.secondary_topic,
            "lyric_format": self.lyrics.format,
            "persona_id": self.persona_id,
        }

    def signature(self) -> str:
        """Stable hash of :meth:`signature_payload`.

        ``sort_keys`` plus the bucketing above make this reproducible across
        processes and Python versions, which matters because the §11 uniqueness
        check runs in the worker while the §48 page reads it from the API.
        """
        payload = json.dumps(self.signature_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Convenient alias for the current blueprint version used across the codebase.
MusicBlueprint = MusicBlueprintV1

__all__ = [
    "BlueprintMarketContextV1",
    "CompositionSpecV1",
    "LyricsSpecV1",
    "MusicBlueprint",
    "MusicBlueprintV1",
    "NoveltySpecV1",
    "VocalSpecV1",
]
