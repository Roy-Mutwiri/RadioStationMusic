"""Blueprint → ACE-Step generation spec (§7.9).

MusicDirector decides *musical intent*: uk_trap at 148 BPM, energy 0.88, rap vocal, about
liquidity. ACE-Step wants a caption string, a lyric block with structural tags, and a handful
of typed parameters. The translation between those two is a property of **this model**, not of
the station, so it lives here.

§7.9 is explicit about why: *"Do not place ACE-Step prompt syntax inside MusicDirector."* The
director already survived two provider changes without edits — the mock reads the intensity
fields numerically, this reads them as adjectives — and it stays that way only if every piece
of model-specific vocabulary is on this side of the boundary.

Determinism
-----------
The builder is a pure function of the blueprint. The same blueprint produces a
byte-identical caption, every time, with no RNG anywhere. That matters for §7.13's seed work:
if the caption drifted between runs, "same seed, same input" could not be tested at all,
because the input would not be the same.

The built prompt is returned as part of the spec and stored with the track (§7.9's
*"Store the final provider prompt with generation metadata for reproducibility/debugging"*),
so a track in the library can always be traced back to the exact text that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.contracts.lyrics import LyricsV1
    from tradefix_radio.contracts.music import MusicBlueprintV1

__all__ = [
    "CAPTION_MAX_CHARS",
    "AceStepPromptBuilder",
    "GenerationSpec",
]

#: ACE-Step documents ``caption`` as max 512 characters.
#:
#: Enforced here rather than discovered at the server: a caption truncated by the model is a
#: silent change to what was asked for, and the half-sentence that survives may invert the
#: meaning of the clause it was cut from.
CAPTION_MAX_CHARS: Final = 512

#: Energy bands, as adjectives the model understands.
#:
#: Bands rather than a number because "energy 0.73" means nothing to a text-conditioned
#: model, and because the director's energy is already a continuous value whose *audible*
#: consequence is categorical: a listener hears "driving" or "laid back", not 0.73.
_ENERGY_WORDS: Final[tuple[tuple[float, str], ...]] = (
    (0.15, "very calm, sparse, ambient"),
    (0.30, "calm, relaxed, laid back"),
    (0.50, "steady, mid-tempo groove"),
    (0.70, "energetic, driving"),
    (0.85, "high energy, punchy, intense"),
    (1.01, "explosive, maximum intensity, aggressive"),
)

#: Density/intensity adjectives, applied only when the value is far enough from the middle to
#: be worth saying. A caption that describes every axis as "moderate" has described nothing.
_INTENSITY_WORDS: Final[tuple[tuple[str, str, str], ...]] = (
    # (field, low phrase, high phrase)
    ("rhythm_density", "sparse rhythm", "busy intricate rhythm"),
    ("bass_intensity", "light bass", "heavy sub bass"),
    ("drum_intensity", "soft drums", "hard hitting drums"),
    ("melodic_complexity", "simple melody", "complex layered melody"),
)

#: How far from 0.5 an intensity must sit before it earns a word in the caption.
_INTENSITY_DEADBAND: Final = 0.18

#: Genre identifiers are snake_case internally; the model reads English.
_GENRE_WORDS: Final[dict[str, str]] = {
    "uk_drill": "UK drill",
    "uk_trap": "UK trap",
    "uk_garage": "UK garage",
    "deep_house": "deep house",
    "afro_house": "afro house",
    "jersey_club": "jersey club",
    "drum_and_bass": "drum and bass",
    "dnb": "drum and bass",
    "lofi": "lo-fi hip hop",
    "rnb": "R&B",
    "amapiano": "amapiano",
    "trap_soul": "trap soul",
    "boom_bap": "boom bap",
    "future_garage": "future garage",
    "liquid_dnb": "liquid drum and bass",
    "phonk": "phonk",
}

#: Vocal styles, likewise.
_VOCAL_WORDS: Final[dict[str, str]] = {
    "rap": "rap vocal",
    "sung": "sung vocal",
    "melodic_rap": "melodic rap vocal",
    "spoken": "spoken word vocal",
    "chant": "chanted vocal",
    "harmony": "layered vocal harmonies",
    "none": "",
}

#: Section names the station uses, mapped to ACE-Step's structural tags.
#:
#: ACE-Step reads bracketed tags in the lyric block. Our structure vocabulary is the
#: director's, so the mapping is explicit — an unmapped section becomes a title-cased tag
#: rather than being dropped, because a missing section changes the song's shape.
_SECTION_TAGS: Final[dict[str, str]] = {
    "intro": "[Intro]",
    "verse": "[Verse]",
    "hook": "[Chorus]",
    "chorus": "[Chorus]",
    "pre_hook": "[Pre-Chorus]",
    "prechorus": "[Pre-Chorus]",
    "bridge": "[Bridge]",
    "breakdown": "[Break]",
    "drop": "[Drop]",
    "outro": "[Outro]",
    "instrumental": "[Instrumental]",
}

#: What ACE-Step expects in the lyric field when there are to be no vocals.
INSTRUMENTAL_MARKER: Final = "[Instrumental]"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class GenerationSpec:
    """Everything the provider sends, plus what it needs to record afterwards.

    Separate from the provider so it can be built and asserted on without a server, a GPU or
    a model — which is what makes §7.30's prompt-builder unit tests possible at all.
    """

    caption: str
    lyrics: str
    instrumental: bool
    seed: int
    duration_seconds: float
    bpm: int
    key_scale: str
    #: Set from the active profile, not from the blueprint.
    inference_steps: int
    guidance_scale: float
    #: Which §7.8 profile produced the step/guidance pair.
    profile: str
    #: ``True`` when the lyric text was altered on the way to the model (§7.10).
    lyrics_modified: bool = False
    #: Why, when it was. Empty when the lyrics were passed through untouched.
    lyric_notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_metadata(self) -> dict[str, object]:
        """The record persisted with the track (§7.9, §7.26)."""
        return {
            "caption": self.caption,
            # The lyric field as submitted, marker and all. Omitted until B3 on the grounds
            # that the lyric was already in the `lyrics` table — which was true only for
            # what the station *composed*, not for what the model was *given*. The gap
            # between those two is the only place a silent downgrade can hide.
            "lyrics": self.lyrics,
            "instrumental": self.instrumental,
            "seed": self.seed,
            "duration_seconds": self.duration_seconds,
            "bpm": self.bpm,
            "key_scale": self.key_scale,
            "inference_steps": self.inference_steps,
            "guidance_scale": self.guidance_scale,
            "profile": self.profile,
            "lyrics_modified": self.lyrics_modified,
            "lyric_notes": list(self.lyric_notes),
            "warnings": list(self.warnings),
        }


class AceStepPromptBuilder:
    """Turns a blueprint into an ACE-Step generation spec.

    Stateless and deterministic. Constructed with nothing, because everything it needs comes
    from the blueprint and the profile passed to :meth:`build`.
    """

    def build(
        self,
        blueprint: MusicBlueprintV1,
        *,
        lyrics: LyricsV1 | None,
        inference_steps: int,
        guidance_scale: float,
        profile: str,
        seed: int | None = None,
    ) -> GenerationSpec:
        """Translate one blueprint. Pure: no clock, no RNG, no I/O."""
        composition = blueprint.composition
        warnings: list[str] = []

        caption, caption_warnings = self._caption(blueprint)
        warnings.extend(caption_warnings)

        instrumental = not blueprint.vocal.enabled or not blueprint.lyrics.enabled
        lyric_text, modified, notes = self._lyrics(blueprint, lyrics, instrumental)
        if not instrumental and lyrics is None:
            # A vocal blueprint with no composed lyrics. ACE-Step would invent its own, which
            # §7.10 forbids doing silently — and inventing lyrics for a *trading* station is
            # exactly where §14's "never imply a guaranteed outcome" gets violated by a model
            # that has no idea what it is singing about. Instrumental is the safe realisation.
            instrumental = True
            lyric_text = INSTRUMENTAL_MARKER
            reason = (
                "the blueprint asked for vocals but no lyrics were composed; generated "
                "instrumental rather than letting the model invent its own words"
            )
            warnings.append(reason)
            # Also a lyric *note*, and `modified` set — this is the §7.10 case, not a
            # caption nicety. Recorded only as a warning before, which left the station's
            # own record saying the lyric had been passed through unchanged while the
            # track went out wordless. A downgrade nobody can query for is a silent one.
            notes.append(reason)
            modified = True

        return GenerationSpec(
            caption=caption,
            lyrics=lyric_text,
            instrumental=instrumental,
            seed=int(blueprint.seed if seed is None else seed),
            duration_seconds=float(composition.duration_seconds),
            bpm=int(composition.bpm),
            key_scale=self._key(composition.key),
            inference_steps=inference_steps,
            guidance_scale=guidance_scale,
            profile=profile,
            lyrics_modified=modified,
            lyric_notes=tuple(notes),
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------- caption

    def _caption(self, blueprint: MusicBlueprintV1) -> tuple[str, list[str]]:
        composition = blueprint.composition
        parts: list[str] = []

        parts.append(_genre_phrase(composition.genre))
        if composition.secondary_genre and composition.secondary_genre != composition.genre:
            parts.append(f"with {_genre_phrase(composition.secondary_genre)} influence")

        parts.append(f"{composition.bpm} BPM")
        parts.append(composition.key)
        parts.append(_energy_phrase(composition.energy))

        for name, low, high in _INTENSITY_WORDS:
            value = float(getattr(composition, name))
            if value <= 0.5 - _INTENSITY_DEADBAND:
                parts.append(low)
            elif value >= 0.5 + _INTENSITY_DEADBAND:
                parts.append(high)

        if composition.mood:
            parts.append(", ".join(composition.mood))
        if composition.instrumentation:
            parts.append(", ".join(composition.instrumentation))

        if blueprint.vocal.enabled:
            vocal = _VOCAL_WORDS.get(blueprint.vocal.style.value, blueprint.vocal.style.value)
            if vocal:
                parts.append(vocal)
        else:
            parts.append("instrumental, no vocals")

        caption = _WHITESPACE.sub(" ", ", ".join(part for part in parts if part)).strip()

        warnings: list[str] = []
        if len(caption) > CAPTION_MAX_CHARS:
            # Trimmed on a separator so the caption stays a list of complete clauses. Cutting
            # mid-phrase can leave "heavy sub" or "no" dangling, which reads to the model as
            # something other than what was meant.
            caption, dropped = _trim_to_clauses(caption, CAPTION_MAX_CHARS)
            warnings.append(
                f"caption exceeded {CAPTION_MAX_CHARS} characters; dropped {dropped} "
                "trailing descriptor(s)"
            )
        return caption, warnings

    # -------------------------------------------------------------- lyrics

    def _lyrics(
        self,
        blueprint: MusicBlueprintV1,
        lyrics: LyricsV1 | None,
        instrumental: bool,
    ) -> tuple[str, bool, list[str]]:
        """The lyric block, and an honest record of whether it was altered (§7.10).

        §7.10: *"Do not allow the provider to silently replace full lyrics."* The station
        composed these words, Phase 3 validated them against §14 and §17, and anything this
        method changes has to be reported — so the stored `provider_lyrics` can be diffed
        against `requested_lyrics` later.
        """
        if instrumental or lyrics is None:
            return INSTRUMENTAL_MARKER, False, []

        text = lyrics.text.strip()
        if not text:
            return INSTRUMENTAL_MARKER, True, ["the composed lyric was empty"]

        notes: list[str] = []
        if _has_section_tags(text):
            # Already structured. Pass through untouched — this is the case §7.10 cares most
            # about, and "untouched" has to mean untouched.
            return text, False, notes

        tagged = self._apply_structure(text, blueprint)
        notes.append(
            "added ACE-Step section tags derived from the blueprint structure; "
            "the words themselves are unchanged"
        )
        return tagged, True, notes

    def _apply_structure(self, text: str, blueprint: MusicBlueprintV1) -> str:
        """Insert structural tags deterministically from the blueprint's own structure.

        §7.10: *"If ACE-Step imposes structural tags … create them deterministically from our
        lyric structure."* Blank-line-separated stanzas are mapped onto the blueprint's
        section list in order. No guessing at which stanza is a chorus — the blueprint already
        said what the shape is, and inventing a different one here would mean the audio no
        longer matches the plan the director recorded.
        """
        stanzas = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        if not stanzas:
            return INSTRUMENTAL_MARKER

        sections = list(blueprint.composition.structure) or ["verse"]
        # Sections that carry no words are skipped rather than consuming a stanza: an [Intro]
        # tag followed by the first verse's words would label the verse as an intro.
        singable = [name for name in sections if name not in {"intro", "outro", "instrumental"}]
        if not singable:
            singable = ["verse"]

        out: list[str] = []
        for index, stanza in enumerate(stanzas):
            section = singable[index % len(singable)]
            out.append(f"{_section_tag(section)}\n{stanza}")
        return "\n\n".join(out)

    # ----------------------------------------------------------------- key

    def _key(self, key: str) -> str:
        """Our ``"F# minor"`` into ACE-Step's ``keyscale`` spelling.

        ACE-Step's documented examples are ``"C Major"`` and ``"Am"``. The long form is used
        because it is unambiguous: ``"Am"`` and ``"A"`` differ by one character and mean
        entirely different keys, and a typo in a short form is not detectable.
        """
        parts = key.split()
        if len(parts) != 2:
            return key
        tonic, mode = parts
        return f"{tonic} {'Major' if mode.lower().startswith('maj') else 'Minor'}"


# ------------------------------------------------------------------ helpers


def _genre_phrase(genre: str) -> str:
    return _GENRE_WORDS.get(genre, genre.replace("_", " "))


def _energy_phrase(energy: float) -> str:
    for ceiling, phrase in _ENERGY_WORDS:
        if energy < ceiling:
            return phrase
    return _ENERGY_WORDS[-1][1]


def _section_tag(section: str) -> str:
    return _SECTION_TAGS.get(section, f"[{section.replace('_', ' ').title()}]")


def _has_section_tags(text: str) -> bool:
    return bool(re.search(r"^\s*\[[^\]]+\]\s*$", text, flags=re.MULTILINE))


def _trim_to_clauses(caption: str, limit: int) -> tuple[str, int]:
    """Drop whole comma-separated clauses from the end until the caption fits."""
    clauses = [clause.strip() for clause in caption.split(",")]
    dropped = 0
    while clauses and len(", ".join(clauses)) > limit:
        clauses.pop()
        dropped += 1
    if not clauses:
        # Nothing survived whole-clause trimming, which means one clause is itself over the
        # limit. A hard cut is the only option left, and it is reported as a drop.
        return caption[:limit].rstrip(), dropped
    return ", ".join(clauses), dropped
