"""Lyrics: subject selection, composition and safety validation (§13–§17).

Three separable concerns, kept separate on purpose:

:mod:`.director`   **what** to talk about — subject, format, perspective, brand
                   frequency, educational payload. Owns topic history (§13), so the
                   generator cannot default to writing about discipline every time.
:mod:`.composer`   **the words**, built from the topic graph's accurate, hedged
                   teaching points.
:mod:`.validators` **whether the words are acceptable** (§17).

The split means a §17 rejection triggers a rewrite of the text without discarding the
musical decision. Regenerating audio is expensive; rewriting words is not.
"""

from tradefix_radio.lyrics.composer import ComposedLyric, LyricComposer
from tradefix_radio.lyrics.director import LyricPlan, LyricsDirector
from tradefix_radio.lyrics.validators import (
    LyricValidator,
    ValidationContext,
    ValidatorConfig,
)

__all__ = [
    "ComposedLyric",
    "LyricComposer",
    "LyricPlan",
    "LyricValidator",
    "LyricsDirector",
    "ValidationContext",
    "ValidatorConfig",
]
