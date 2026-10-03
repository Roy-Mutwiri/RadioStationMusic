"""Behaviour energy, music influence, and the fatigue rhythm.

Everything that turns *conditions* into *intensity*. Three deliberate properties:

**Market drives through one scalar, not through labels.** The brief is explicit: *"Do
NOT directly select animations based on market labels."* So the market's whole influence
is :func:`behavior_energy` — one number in [0, 1] — plus the intensity band used for
per-action bias. No action name appears here and no regime name appears below the bridge.

**The intensity span is narrow.** Action rate runs 0.55x to 1.60x from dormant to peak.
That is a factor of 2.9, and it is deliberately small. The naive mapping is "more
volatility, more movements per minute", and taken far it produces a twitching man. Most
of the visible difference comes from *which* actions win — B5 biases mouse work,
monitor comparison and leaning forward while suppressing sustained reading and coffee —
and from blends 28 % shorter, which makes every motion land more sharply. A man moving
1.6x as often with crisper motions reads as markedly more alert. A man moving 4x as
often reads as panicking, and the brief's instruction for extreme volatility is
*"increase movement frequency slightly, but still preserve realism."*

**Music is much weaker than the market, by construction.** Not by tuning: music can
only reach three actions and one rhythm policy, and its ceilings are clamps rather than
targets. There is no value of ``music_energy`` that makes him dance, because the
amplitude limit is applied after every multiplier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from tradefix_radio.visual.contracts import (
    IntensityBand,
    RhythmPolicyV1,
    VisualStateV1,
)

# ============================================================ band effects


@dataclass(frozen=True, slots=True)
class BandProfile:
    """How one intensity band changes the shape of behaviour."""

    band: IntensityBand
    #: Multiplier on action rate. The full span is 0.55-1.60 — see the module docstring.
    rate: float
    #: Multiplier on blend durations. Below 1.0 makes motions land more sharply.
    blend_scale: float
    #: Salience a market move must reach to fire a reaction.
    reaction_threshold: float
    #: Target share of time spent in IDLE_FOCUS.
    idle_share: float
    #: Multiplier on fatigue action weight. High alertness suppresses fatigue.
    fatigue_scale: float
    #: Multiplier on gaze dwell. Breakouts shorten holds; quiet lengthens them.
    dwell_scale: float


_PROFILES: Final[dict[IntensityBand, BandProfile]] = {
    IntensityBand.B0_DORMANT: BandProfile(
        IntensityBand.B0_DORMANT, 0.55, 1.30, 1.01, 0.70, 1.2, 1.45
    ),
    IntensityBand.B1_QUIET: BandProfile(
        IntensityBand.B1_QUIET, 0.75, 1.15, 0.80, 0.58, 1.1, 1.30
    ),
    IntensityBand.B2_STEADY: BandProfile(
        IntensityBand.B2_STEADY, 1.00, 1.00, 0.65, 0.45, 1.0, 1.00
    ),
    IntensityBand.B3_FOCUSED: BandProfile(
        IntensityBand.B3_FOCUSED, 1.20, 0.90, 0.50, 0.34, 0.8, 0.85
    ),
    IntensityBand.B4_ALERT: BandProfile(
        IntensityBand.B4_ALERT, 1.45, 0.80, 0.38, 0.24, 0.5, 0.70
    ),
    IntensityBand.B5_PEAK: BandProfile(
        IntensityBand.B5_PEAK, 1.60, 0.72, 0.30, 0.20, 0.3, 0.62
    ),
}

#: `B0_DORMANT`'s threshold is above 1.0, which is how reactions are disabled without a
#: special case: salience is a unit value and can never reach it. A reaction with no
#: market event behind it is the visual equivalent of a fabricated price.
assert _PROFILES[IntensityBand.B0_DORMANT].reaction_threshold > 1.0


def profile_for(band: IntensityBand) -> BandProfile:
    return _PROFILES[band]


# ============================================================ behaviour energy

#: Weights for the behaviour-energy mix. Market dominates; music contributes a tenth.
_W_MARKET_ENERGY: Final = 0.46
_W_VELOCITY: Final = 0.18
_W_BAND: Final = 0.26
_W_MUSIC: Final = 0.10

#: Energy velocity, in points per minute, that counts as fully fast. Above this the
#: term saturates — a violent move is a violent move and twice as violent is not twice
#: as much reason to move your hands.
_VELOCITY_SATURATION: Final = 8.0

#: How strongly recent activity damps energy. Without this the director ratchets: a busy
#: minute raises energy, which raises the rate, which makes the next minute busier.
_ACTIVITY_DAMPING: Final = 0.22

#: Ceiling on behaviour energy. Reserved headroom, deliberately unreachable: he is
#: experienced, and a man at 100 % is a man with nothing held back.
_ENERGY_CEILING: Final = 0.92
_ENERGY_FLOOR: Final = 0.08


def behavior_energy(
    state: VisualStateV1 | None = None,
    *,
    band: IntensityBand | None = None,
    market_energy: float | None = None,
    energy_velocity: float | None = None,
    music_energy: float | None = None,
    recent_activity: float = 0.0,
) -> float:
    """Derive the single behavioural drive scalar, in [0.08, 0.92].

    Takes either a :class:`VisualStateV1` or the loose parts, because the bridge
    computes this *before* the state exists — the state carries the result.

    ``recent_activity`` is the rolling sum of ``energy_cost`` over the last minute,
    normalised. It is subtracted, and that negative feedback is the whole reason the
    system does not ratchet: a man who has just typed for eleven seconds is less likely
    to immediately type again, independently of every cooldown, which is what makes
    activity come in bouts rather than at a constant drip.

    Absent market data contributes the neutral 0.5 rather than 0.0. A missing feed must
    not read as a calm market — the brief forbids replacing unavailable values with fake
    zeros, and an energy of zero is exactly that mistake with a behavioural consequence.
    """
    if state is not None:
        band = band or state.intensity_band
        market_energy = state.market_energy if market_energy is None else market_energy
        energy_velocity = (
            state.market_energy_velocity if energy_velocity is None else energy_velocity
        )
        music_energy = state.music_energy if music_energy is None else music_energy
    if band is None:
        raise ValueError("behavior_energy needs either a state or an explicit band")

    energy_term = 0.5 if market_energy is None else market_energy / 100.0
    velocity_term = (
        0.5
        if energy_velocity is None
        else 0.5 + 0.5 * math.tanh(energy_velocity / _VELOCITY_SATURATION)
    )
    band_term = band.rank / 5.0
    music_term = 0.4 if music_energy is None else music_energy

    raw = (
        _W_MARKET_ENERGY * energy_term
        + _W_VELOCITY * velocity_term
        + _W_BAND * band_term
        + _W_MUSIC * music_term
    )
    damped = raw - _ACTIVITY_DAMPING * min(1.0, max(0.0, recent_activity))
    return max(_ENERGY_FLOOR, min(_ENERGY_CEILING, damped))


#: How far energy modulates the band's base rate, as a fraction either side.
#:
#: +-18 % rather than +-15 %, and the reason is a property test. For adjacent band rates
#: to *overlap* — so that a classification flip alone cannot change the action rate —
#: the ratio between neighbouring base rates must stay under (1+m)/(1-m). At 15 % that
#: admits 1.353, and B1/B0 is 0.75/0.55 = 1.364: the dormant-to-quiet boundary had a
#: 0.8 % discontinuity that energy could not bridge. 18 % admits 1.439 and every
#: boundary overlaps.
RATE_ENERGY_SPAN: Final = 0.18


def action_rate_multiplier(band: IntensityBand, energy: float) -> float:
    """Combine the band's coarse rate with the continuous energy.

    The band sets the bracket; energy modulates inside it. Continuous rather than
    stepped so behaviour does not visibly change gear as a classification flips — which
    it does, several times an hour, at band boundaries.
    """
    base = profile_for(band).rate
    clamped = max(0.0, min(1.0, energy))
    return base * (1.0 - RATE_ENERGY_SPAN + 2.0 * RATE_ENERGY_SPAN * clamped)


def interval_scale(band: IntensityBand, energy: float) -> float:
    """Multiplier on the gap between actions. The reciprocal of the rate."""
    return 1.0 / max(0.1, action_rate_multiplier(band, energy))


# ============================================================ music

#: Ceilings. Clamps applied after every multiplier, so no setting can exceed them.
MAX_NOD_DEGREES: Final = 1.1
MAX_MUSIC_SHARE: Final = 0.12
MAX_CONSECUTIVE_BEATS: Final = 8
NOD_GAP_SECONDS: Final = 25.0

#: Above this BPM a nod lands on every second beat. At 174 BPM a per-beat nod is
#: physically wrong for a seated man and reads as a glitch rather than as rhythm.
SUBDIVISION_BPM: Final = 160.0
#: Below this, beats 1 and 3 only.
SLOW_BPM: Final = 85.0


def rhythm_policy(state: VisualStateV1, *, reactivity: float = 1.0) -> RhythmPolicyV1:
    """Rhythmic policy for the renderer.

    ADR-11's split: Python sets policy, the renderer keeps phase. A per-beat round trip
    is impossible at 174 BPM, and unnecessary — what the renderer needs is a BPM, a
    phase anchor, a subdivision and a probability, and it can decide which beats land.

    ``reactivity`` is the operator's music-reactivity slider. It scales probability and
    **cannot raise a ceiling**: a slider that could produce a dancing trader would make
    every constraint in this module advisory.
    """
    bpm = state.music_bpm
    if bpm is None or not state.broadcasting:
        return RhythmPolicyV1(bpm=None, nod_probability=0.0)

    energy = state.music_energy if state.music_energy is not None else 0.4
    # 0.2 at silence-adjacent energy, 1.4 at full. Then clamped to a unit probability.
    probability = min(1.0, (0.2 + 1.2 * energy) * max(0.0, min(1.5, reactivity)))

    # Both ends of the tempo range land on every second beat, for opposite reasons:
    # above 160 BPM a per-beat nod is physically wrong for a seated man, and below
    # 85 BPM the nod belongs on beats 1 and 3.
    fast_or_slow = bpm >= SUBDIVISION_BPM or bpm <= SLOW_BPM
    subdivision = 2 if fast_or_slow else 1

    return RhythmPolicyV1(
        bpm=float(bpm),
        downbeat_phase=state.track_progress,
        beat_subdivision=subdivision,
        nod_probability=probability,
        max_nod_degrees=MAX_NOD_DEGREES,
        max_consecutive_beats=MAX_CONSECUTIVE_BEATS,
        mandatory_gap_seconds=NOD_GAP_SECONDS,
    )


def music_weight_factor(
    music_bias: float, state: VisualStateV1, *, reactivity: float = 1.0
) -> float:
    """How music scales one action's weight.

    Returns 1.0 for the 63 actions with ``music_bias == 0.0``, which is all of them
    except the three MUSIC entries. That is the structural reason music is weaker than
    the market rather than a tuning claim: it simply cannot reach anything else.
    """
    if music_bias <= 0.0:
        return 1.0
    if not state.music_present:
        # Nothing playing. The rhythmic actions are not merely unlikely, they are
        # meaningless — a man tapping to silence.
        return 0.0
    energy = state.music_energy if state.music_energy is not None else 0.4
    scaled = 1.0 + music_bias * (energy - 0.4) * max(0.0, min(1.5, reactivity))
    return max(0.0, min(2.2, scaled))


# ============================================================ fatigue rhythm
#
# MOVED. The one-directional accumulator that lived here pinned at its ceiling after
# about sixteen hours, and the fix was structural rather than a better constant — see
# `tradefix_radio/visual/rhythm.py`, which models focus, fatigue and recovery as a
# cycle whose phases set rates rather than values.


__all__ = [
    "MAX_CONSECUTIVE_BEATS",
    "MAX_MUSIC_SHARE",
    "MAX_NOD_DEGREES",
    "NOD_GAP_SECONDS",
    "RATE_ENERGY_SPAN",
    "SLOW_BPM",
    "SUBDIVISION_BPM",
    "BandProfile",
    "action_rate_multiplier",
    "behavior_energy",
    "interval_scale",
    "music_weight_factor",
    "profile_for",
    "rhythm_policy",
]
