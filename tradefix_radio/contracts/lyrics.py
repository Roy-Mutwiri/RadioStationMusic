"""Lyric contracts (§13, §17).

The lyric *text* is separated from the lyric *brief*
(:class:`~tradefix_radio.contracts.music.LyricsSpecV1`) so that a §17 validation
failure can trigger a rewrite of the words without discarding the musical
decision — which matters for throughput, because regenerating audio is expensive
and rewriting text is not.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import Field, computed_field, field_validator

from tradefix_radio.contracts.base import Contract
from tradefix_radio.contracts.market import Unit

_WORD_RE = re.compile(r"[a-z0-9']+")
#: ACE-Step and similar models take structure tags inline, e.g. ``[verse]``.
_SECTION_TAG_RE = re.compile(r"^\[[a-z0-9 _-]{2,24}\]$", re.IGNORECASE)


class LyricLineV1(Contract):
    """One line, tagged with the section it belongs to."""

    section: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=400)


class LyricsV1(Contract):
    """Generated lyrics plus the metadata the originality engine needs.

    ``normalised_text`` and ``content_hash`` exist because §17 requires duplicate
    detection against previous Trade Fix lyrics, and a raw-text comparison would
    miss trivial variations (case, punctuation, section tags). Normalising once
    here means every consumer compares the same way.
    """

    track_id: str = Field(min_length=1, max_length=64)
    #: The model-facing text, including inline section tags.
    text: str = Field(min_length=1, max_length=20_000)
    lines: tuple[LyricLineV1, ...] = Field(default_factory=tuple)

    primary_topic: str = Field(min_length=1, max_length=96)
    secondary_topic: str | None = Field(default=None, max_length=96)
    format: str = Field(min_length=1, max_length=64)
    perspective: str = Field(min_length=1, max_length=48)

    tradefix_mentions: int = Field(ge=0, le=2)
    educational_intensity: Unit

    #: Concepts actually taught, for the §12 long-horizon topic history. Distinct
    #: from ``primary_topic``: a track about "liquidity sweep" may incidentally
    #: teach "stop placement", and both must count against repetition.
    concepts_used: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lyric text is blank")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def normalised_text(self) -> str:
        """Lowercased, tag-stripped, whitespace-collapsed word stream.

        Deliberately destructive: it throws away everything that is not a word so
        that "Trade Fix!" and "trade fix" hash identically. Used only for
        duplicate detection, never for display or generation.
        """
        stripped = [
            line
            for line in (raw.strip() for raw in self.text.splitlines())
            if line and not _SECTION_TAG_RE.match(line)
        ]
        words = _WORD_RE.findall(" ".join(stripped).lower())
        return " ".join(words)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """SHA-256 of :attr:`normalised_text` — exact-duplicate detection (§17)."""
        return hashlib.sha256(self.normalised_text.encode("utf-8")).hexdigest()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def word_count(self) -> int:
        return len(self.normalised_text.split())

    def shingles(self, size: int = 5) -> frozenset[str]:
        """Word n-grams, for near-duplicate detection via Jaccard similarity.

        Five words is a deliberate choice: short enough that a reworded couplet
        still overlaps, long enough that common trading phrases ("risk to
        reward") do not create false positives across unrelated songs.
        """
        if size < 2:
            raise ValueError("shingle size must be at least 2")
        words = self.normalised_text.split()
        if len(words) < size:
            return frozenset({" ".join(words)} if words else set())
        return frozenset(
            " ".join(words[i : i + size]) for i in range(len(words) - size + 1)
        )


class LyricViolationV1(Contract):
    """One §17 validation failure, with enough detail to explain a rejection."""

    rule: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=400)
    #: The offending fragment, truncated. Shown on the §48 page.
    excerpt: str | None = Field(default=None, max_length=200)
    #: ``True`` when the rule is a hard safety stop rather than a quality nudge.
    fatal: bool = True
    #: For threshold rules, what was measured and what the limit was. §48 requires a
    #: rejection to be explainable, and "lexical diversity 0.18 below the 0.26 minimum"
    #: is actionable where "low diversity" is not.
    measured: float | None = None
    threshold: float | None = None


class LyricValidationResultV1(Contract):
    """Outcome of running every §17 validator over a candidate."""

    track_id: str = Field(min_length=1, max_length=64)
    accepted: bool
    violations: tuple[LyricViolationV1, ...] = Field(default_factory=tuple)
    #: Highest similarity found against stored lyrics, 0–1.
    max_similarity: Unit = 0.0
    closest_track_id: str | None = Field(default=None, max_length=64)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fatal_violations(self) -> tuple[LyricViolationV1, ...]:
        return tuple(v for v in self.violations if v.fatal)


__all__ = [
    "LyricLineV1",
    "LyricValidationResultV1",
    "LyricViolationV1",
    "LyricsV1",
]
