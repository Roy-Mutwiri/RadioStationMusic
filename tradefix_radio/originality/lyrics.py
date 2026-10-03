"""Lyric originality (§6.8).

Independent of the audio comparison on purpose. The same words over different music is a
repeat a listener notices immediately, and an audio comparison will not see it at all — two
renders of the same lyric in different genres are acoustically unrelated.

Everything here is lexical: hashes, shingles, token overlap. No embedding model, because the
brief rules out a cloud dependency and no suitable local model is installed; the interface is
shaped so one can be added without changing the callers.

What this cannot do
-------------------
It compares a candidate against **this station's own lyrics**. It will catch the station
repeating itself, and it will not detect text copied from outside the corpus — §17 says plainly
that copyrighted lyrics are not detectable from text, and §86 forbids claiming otherwise. The
validators that *prevent* such content are Phase 3's, and they work by constraining what the
composer may produce rather than by recognising what it did.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "LyricFingerprint",
    "LyricSimilarity",
    "compare_lyrics",
    "fingerprint_lyrics",
]

#: Length of the word n-grams used for overlap.
#:
#: Five. Shorter and ordinary English collides — "and I am back on the" appears everywhere.
#: Longer and a single changed word hides a reused line, which is the thing being looked for.
SHINGLE_SIZE: Final = 5

#: Hook detection window, in lines. A hook is a short repeated unit, not a verse.
_HOOK_LINES: Final = 2

#: Internal repetition above which a lyric is flagged as padded rather than structured.
#:
#: 0.72. Choruses repeat by design and a high figure is normal; this sits above what a
#: conventional verse-chorus structure produces and below a lyric that is one line forty times.
MAX_INTERNAL_REPETITION: Final = 0.72

_WORD = re.compile(r"[a-z0-9']+")


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace.

    Section markers like ``[Chorus]`` are removed: they are structure, not content, and
    leaving them in makes two different lyrics with the same arrangement look similar.
    """
    without_sections = re.sub(r"\[[^\]]*\]", " ", text.lower())
    return " ".join(_WORD.findall(without_sections))


def _tokens(text: str) -> list[str]:
    return _normalise(text).split()


def _shingles(tokens: list[str], size: int = SHINGLE_SIZE) -> frozenset[str]:
    if len(tokens) < size:
        return frozenset({" ".join(tokens)}) if tokens else frozenset()
    return frozenset(
        " ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        normalised = _normalise(raw)
        # Three words or more. Shorter "lines" are ad-libs and section labels, and counting
        # them makes every lyric with a "yeah" look like every other.
        if len(normalised.split()) >= 3:
            out.append(normalised)
    return out


@dataclass(frozen=True)
class LyricFingerprint:
    """What is stored for one lyric, and what comparison runs against."""

    track_id: str
    content_hash: str
    #: One per line, so a single reused line is findable without comparing whole texts.
    line_hashes: frozenset[str]
    shingles: frozenset[str]
    hook_hashes: frozenset[str]
    word_count: int
    unique_word_ratio: float
    internal_repetition: float
    tradefix_mentions: int
    #: Reserved for a local embedding model. The interface exists; nothing fills it yet.
    embedding: tuple[float, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.word_count == 0


def fingerprint_lyrics(track_id: str, text: str, *, tradefix_mentions: int = 0) -> LyricFingerprint:
    """Reduce a lyric to the forms comparison needs."""
    tokens = _tokens(text)
    lines = _lines(text)

    # A hook is a short repeated unit. Hashing consecutive line pairs catches a chorus
    # couplet reused across tracks, which a whole-text comparison dilutes to nothing.
    hooks = {
        _hash(" / ".join(lines[index : index + _HOOK_LINES]))
        for index in range(max(0, len(lines) - _HOOK_LINES + 1))
    }

    unique_ratio = len(set(tokens)) / len(tokens) if tokens else 0.0
    repeated_lines = len(lines) - len(set(lines))
    internal_repetition = repeated_lines / len(lines) if lines else 0.0

    return LyricFingerprint(
        track_id=track_id,
        content_hash=_hash(_normalise(text)),
        line_hashes=frozenset(_hash(line) for line in lines),
        shingles=_shingles(tokens),
        hook_hashes=frozenset(hooks),
        word_count=len(tokens),
        unique_word_ratio=unique_ratio,
        internal_repetition=internal_repetition,
        tradefix_mentions=tradefix_mentions,
    )


@dataclass(frozen=True)
class LyricSimilarity:
    """How alike two lyrics are, and in what way.

    The components are kept separate because they mean different things to an operator: a
    high shingle score with no shared lines is paraphrase, while one shared hook with low
    overall overlap is a chorus lifted into a new verse.
    """

    score: float
    exact_match: bool
    shared_line_ratio: float
    shingle_jaccard: float
    shared_hook: bool
    detail: str

    @property
    def is_duplicate(self) -> bool:
        return self.exact_match


def compare_lyrics(candidate: LyricFingerprint, existing: LyricFingerprint) -> LyricSimilarity:
    """Compare two lyric fingerprints. Score is 0–1, higher meaning more alike.

    The score is the **maximum** of its components rather than a weighted blend. A lyric that
    shares one whole hook and nothing else is a repeat a listener will notice, and averaging
    that against a low overall overlap would hide it — which is the failure mode a blended
    score has in exactly the cases that matter.
    """
    if candidate.is_empty or existing.is_empty:
        return LyricSimilarity(
            score=0.0,
            exact_match=False,
            shared_line_ratio=0.0,
            shingle_jaccard=0.0,
            shared_hook=False,
            detail="one of the tracks is instrumental",
        )

    if candidate.content_hash == existing.content_hash:
        return LyricSimilarity(
            score=1.0,
            exact_match=True,
            shared_line_ratio=1.0,
            shingle_jaccard=1.0,
            shared_hook=True,
            detail="the lyrics are identical",
        )

    shared_lines = candidate.line_hashes & existing.line_hashes
    line_ratio = (
        len(shared_lines) / min(len(candidate.line_hashes), len(existing.line_hashes))
        if candidate.line_hashes and existing.line_hashes
        else 0.0
    )

    union = candidate.shingles | existing.shingles
    jaccard = len(candidate.shingles & existing.shingles) / len(union) if union else 0.0

    shared_hook = bool(candidate.hook_hashes & existing.hook_hashes)
    # A shared hook scores high but not 1.0 — it is a strong signal of reuse, not proof the
    # lyric is the same one.
    hook_component = 0.8 if shared_hook else 0.0

    score = max(line_ratio, jaccard, hook_component)

    if line_ratio >= 0.5:
        detail = f"{len(shared_lines)} lines appear in both lyrics"
    elif shared_hook:
        detail = "both lyrics share a hook"
    elif jaccard >= 0.3:
        detail = f"{jaccard:.0%} of five-word phrases overlap"
    else:
        detail = "little textual overlap"

    return LyricSimilarity(
        score=min(1.0, score),
        exact_match=False,
        shared_line_ratio=line_ratio,
        shingle_jaccard=jaccard,
        shared_hook=shared_hook,
        detail=detail,
    )


def internal_repetition_warning(fingerprint: LyricFingerprint) -> str | None:
    """Flag a lyric that repeats itself excessively (§6.8).

    Separate from cross-track comparison: a lyric can be unlike everything in the library and
    still be one line forty times, which is a quality defect rather than an originality one.
    """
    if fingerprint.is_empty:
        return None
    if fingerprint.internal_repetition > MAX_INTERNAL_REPETITION:
        return (
            f"{fingerprint.internal_repetition:.0%} of lines are repeats of other lines in the "
            "same lyric"
        )
    if fingerprint.word_count >= 40 and fingerprint.unique_word_ratio < 0.25:
        return (
            f"only {fingerprint.unique_word_ratio:.0%} of words are distinct across "
            f"{fingerprint.word_count} words"
        )
    return None
