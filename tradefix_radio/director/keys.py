"""Musical key selection.

Keys are chosen with intent rather than uniformly. Mode carries mood — minor and
phrygian read as dark, major and mixolydian as open — so the key is tied to the market's
character the same way genre and BPM are. A uniformly random key would make §11's "same
key: avoid within previous 4 tracks" the *only* thing the key decision expressed, which
wastes a real musical lever.

Spellings are idiomatic rather than canonical: producers write F# minor, not Gb minor, and
the key string ends up in the generation prompt where idiom reads better. The contract
(:class:`~tradefix_radio.contracts.music.CompositionSpecV1`) accepts both.
"""

from __future__ import annotations

from collections.abc import Sequence

from tradefix_radio.contracts.enums import MarketDirection, MarketRegime

#: Pitches, in the spellings producers actually use.
PITCHES: tuple[str, ...] = (
    "C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B",
)

#: Modes grouped by the mood they carry. The grouping is the point: it lets the director
#: ask for "something dark" rather than hard-coding a key per regime.
DARK_MODES: tuple[str, ...] = ("minor", "phrygian", "harmonic minor")
NEUTRAL_MODES: tuple[str, ...] = ("minor", "dorian", "mixolydian")
BRIGHT_MODES: tuple[str, ...] = ("major", "mixolydian", "lydian")

#: Keys that recur in electronic and hip-hop production, so they sound idiomatic rather
#: than arbitrary. Weighted up, never required.
COMMON_KEYS: frozenset[str] = frozenset(
    {
        "F# minor", "A minor", "C# minor", "G minor", "D minor", "E minor",
        "Bb minor", "C minor", "B minor", "F minor",
        "C major", "G major", "F major", "D major", "A major", "Eb major",
    }
)


def all_keys() -> tuple[str, ...]:
    """Every key the contract accepts, as ``"<pitch> <mode>"``."""
    modes = sorted(set(DARK_MODES) | set(NEUTRAL_MODES) | set(BRIGHT_MODES))
    return tuple(f"{pitch} {mode}" for pitch in PITCHES for mode in modes)


def modes_for(
    *, energy: float, direction: MarketDirection, regime: MarketRegime
) -> Sequence[str]:
    """Modes that suit the market's current character.

    Direction leads, because it is the clearest musical signal the market gives: a
    bearish break wants a dark mode and a bullish trend an open one. Energy only breaks
    the tie when direction is neutral — a loud directionless market (a volatility spike)
    reads as tense rather than as either happy or sad, which is what the dark/neutral
    split expresses.
    """
    if direction is MarketDirection.BEARISH:
        return DARK_MODES
    if direction is MarketDirection.BULLISH:
        # A bullish *breakout* is aggressive rather than cheerful, so it keeps the
        # neutral set; a bullish *trend* has room to open up.
        if regime in {MarketRegime.BULLISH_BREAKOUT, MarketRegime.EXTREME_VOLATILITY}:
            return NEUTRAL_MODES
        return BRIGHT_MODES if energy < 70 else NEUTRAL_MODES
    if regime in {MarketRegime.EXTREME_VOLATILITY, MarketRegime.HIGH_VOLATILITY_RANGE}:
        return DARK_MODES
    if energy <= 30:
        # Quiet and directionless: the one place the brighter modes genuinely belong,
        # since lo-fi and jazzhop live there.
        return (*NEUTRAL_MODES, *BRIGHT_MODES)
    return NEUTRAL_MODES


def key_candidates(
    *, energy: float, direction: MarketDirection, regime: MarketRegime
) -> tuple[str, ...]:
    """Keys appropriate to the current market, idiomatic ones first."""
    modes = modes_for(energy=energy, direction=direction, regime=regime)
    candidates = [f"{pitch} {mode}" for pitch in PITCHES for mode in modes]
    # Stable ordering with common keys first, so a low creative temperature concentrates
    # on idiomatic choices rather than on whatever happened to sort first.
    return tuple(
        sorted(candidates, key=lambda key: (key not in COMMON_KEYS, key))
    )


def key_weight(key: str) -> float:
    """Selection weight: idiomatic keys are likelier, unusual ones stay possible."""
    return 1.6 if key in COMMON_KEYS else 1.0


def relative_distance(first: str, second: str) -> int:
    """Semitone distance between two keys' tonics, 0–6.

    Used to prefer a key that is harmonically *distant* from the previous track, which is
    a stronger form of variety than merely "a different key": F# minor followed by G minor
    is a semitone apart and sounds like a mistake rather than a change.
    """
    try:
        first_index = PITCHES.index(first.split()[0])
        second_index = PITCHES.index(second.split()[0])
    except (ValueError, IndexError):
        return 0
    raw = abs(first_index - second_index)
    return min(raw, 12 - raw)


__all__ = [
    "BRIGHT_MODES",
    "COMMON_KEYS",
    "DARK_MODES",
    "NEUTRAL_MODES",
    "PITCHES",
    "all_keys",
    "key_candidates",
    "key_weight",
    "modes_for",
    "relative_distance",
]
