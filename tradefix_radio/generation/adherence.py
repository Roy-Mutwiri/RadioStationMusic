"""Did the model produce what the blueprint asked for? (§7.16)

§7.16 draws the line this module is built around:

> *Not every artistic property is objectively measurable. Do not fake genre-confidence
> numbers without a real classifier. Create an adherence report containing only properties
> we can measure honestly.*

So there is no genre score here, and no mood score, and no "musicality" score. There is no
genre classifier in this project, and inventing a number that looks like one would be worse
than reporting nothing — a 0.82 genre-adherence figure on the detail page would be believed.

What **is** measurable, against the features Phase 6 already extracts:

================  =====================================================================
tempo             blueprint BPM against the measured tempo, allowing octave errors
duration          requested seconds against the file's actual length
vocal presence    instrumental requested, instrumental delivered (a weak but real proxy)
energy            intended energy against measured loudness and spectral activity
key               requested key against Krumhansl–Schmuckler estimation, when confident
================  =====================================================================

Each check reports its own status, value and threshold, in the shape §6.1 established — and
an unmeasurable property reports ``UNMEASURED`` with the reason, not a pass.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.audio.analysis import AudioFeatures
    from tradefix_radio.contracts.music import MusicBlueprintV1

__all__ = [
    "AdherenceCheck",
    "AdherenceReport",
    "AdherenceStatus",
    "measure_adherence",
]


class AdherenceStatus(str, enum.Enum):
    """How well one property matched.

    ``UNMEASURED`` is a first-class outcome rather than an absence. A property we cannot
    measure must be visibly unmeasured — collapsing it into ``MATCH`` would quietly claim
    the model got something right that nobody checked.
    """

    MATCH = "match"
    CLOSE = "close"
    DRIFT = "drift"
    UNMEASURED = "unmeasured"


#: Tempo within this many BPM counts as a match.
#:
#: 3 BPM. Beat trackers disagree with each other by about this much on real music, so a
#: tighter bound would report the estimator's noise as the model's failure.
_TEMPO_MATCH_BPM: Final = 3.0
#: Beyond this, the model produced a different tempo rather than a near miss.
_TEMPO_CLOSE_BPM: Final = 10.0

#: Duration tolerance as a fraction of what was requested.
#:
#: §7.12: *"If ACE-Step duration is approximate, do not fake exact compliance."* These are
#: generous because the model's duration is a request, not a contract — the measured
#: distribution in the Phase 7 report is what says whether they are right.
_DURATION_MATCH_RATIO: Final = 0.05
_DURATION_CLOSE_RATIO: Final = 0.15

#: Key estimation below this confidence is not worth comparing against.
#:
#: Krumhansl–Schmuckler on a dense mix is frequently uncertain, and a low-confidence
#: disagreement says more about the estimator than about the track.
_KEY_CONFIDENCE_FLOOR: Final = 0.55

#: Loudness band, in LUFS, that each energy quintile is expected to land in.
#:
#: Derived from the mastering target band rather than invented: these are raw-render
#: expectations, so they are wide. The point is to catch a blueprint asking for 0.95 energy
#: and getting something measurably limp, not to grade the mix.
_ENERGY_LOUDNESS_BANDS: Final[tuple[tuple[float, float, float], ...]] = (
    # (energy ceiling, min LUFS, max LUFS)
    (0.25, -45.0, -18.0),
    (0.50, -32.0, -14.0),
    (0.75, -26.0, -10.0),
    (1.01, -22.0, -5.0),
)


@dataclass(frozen=True)
class AdherenceCheck:
    """One measurable property, in the §6.1 shape."""

    name: str
    status: AdherenceStatus
    requested: float | str | None
    measured: float | str | None
    detail: str
    unit: str = ""

    @property
    def matched(self) -> bool:
        return self.status in (AdherenceStatus.MATCH, AdherenceStatus.CLOSE)


@dataclass(frozen=True)
class AdherenceReport:
    """What the model delivered against what was asked.

    There is deliberately no single "adherence score". Averaging a tempo match with a
    duration drift produces a number that means nothing and hides which one went wrong —
    the same reasoning that keeps Phase 6's similarity components separate.
    """

    track_id: str
    checks: tuple[AdherenceCheck, ...] = field(default_factory=tuple)

    @property
    def measured_checks(self) -> tuple[AdherenceCheck, ...]:
        return tuple(c for c in self.checks if c.status is not AdherenceStatus.UNMEASURED)

    @property
    def drifted(self) -> tuple[AdherenceCheck, ...]:
        return tuple(c for c in self.checks if c.status is AdherenceStatus.DRIFT)

    def as_metadata(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "requested": c.requested,
                    "measured": c.measured,
                    "unit": c.unit,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
            "measured_count": len(self.measured_checks),
            "drift_count": len(self.drifted),
        }

    def summary(self) -> str:
        measured = len(self.measured_checks)
        drifted = len(self.drifted)
        if measured == 0:
            return "nothing could be measured"
        if drifted == 0:
            return f"{measured} measurable propert(ies) matched"
        names = ", ".join(c.name for c in self.drifted)
        return f"{drifted} of {measured} drifted: {names}"


def measure_adherence(
    blueprint: MusicBlueprintV1, features: AudioFeatures
) -> AdherenceReport:
    """Compare the blueprint against the measured audio. Honest properties only."""
    composition = blueprint.composition
    checks: list[AdherenceCheck] = [
        _tempo(composition.bpm, features),
        _duration(float(composition.duration_seconds), features),
        _vocal(blueprint, features),
        _energy(float(composition.energy), features),
        _key(composition.key, features),
    ]
    return AdherenceReport(track_id=blueprint.track_id, checks=tuple(checks))


def _tempo(requested_bpm: int, features: AudioFeatures) -> AdherenceCheck:
    """Tempo, allowing octave errors.

    Half and double time count: a beat tracker reporting 70 for a 140 BPM track is a known
    and extremely common estimator behaviour, not the model ignoring the request. Counting
    it as drift would fill the report with false positives and make the real ones invisible.
    """
    if features.tempo is None or features.tempo <= 0:
        return AdherenceCheck(
            name="tempo",
            status=AdherenceStatus.UNMEASURED,
            requested=requested_bpm,
            measured=None,
            unit="BPM",
            detail="no tempo could be estimated from the audio",
        )

    measured = float(features.tempo)
    candidates = (measured, measured * 2.0, measured / 2.0)
    best = min(candidates, key=lambda value: abs(value - requested_bpm))
    difference = abs(best - requested_bpm)
    octave = "" if abs(best - measured) < 0.01 else f" (octave-corrected from {measured:.1f})"

    if difference <= _TEMPO_MATCH_BPM:
        status, detail = AdherenceStatus.MATCH, f"{best:.1f} BPM{octave}"
    elif difference <= _TEMPO_CLOSE_BPM:
        status, detail = (
            AdherenceStatus.CLOSE,
            f"{best:.1f} BPM{octave}, {difference:.1f} off",
        )
    else:
        status, detail = (
            AdherenceStatus.DRIFT,
            f"{best:.1f} BPM{octave}, {difference:.1f} off the requested {requested_bpm}",
        )
    return AdherenceCheck(
        name="tempo",
        status=status,
        requested=requested_bpm,
        measured=round(measured, 1),
        unit="BPM",
        detail=detail,
    )


def _duration(requested: float, features: AudioFeatures) -> AdherenceCheck:
    measured = features.duration_seconds
    if requested <= 0:
        return AdherenceCheck(
            name="duration",
            status=AdherenceStatus.UNMEASURED,
            requested=None,
            measured=round(measured, 1),
            unit="s",
            detail="no duration was requested",
        )
    ratio = abs(measured - requested) / requested
    if ratio <= _DURATION_MATCH_RATIO:
        status = AdherenceStatus.MATCH
    elif ratio <= _DURATION_CLOSE_RATIO:
        status = AdherenceStatus.CLOSE
    else:
        status = AdherenceStatus.DRIFT
    return AdherenceCheck(
        name="duration",
        status=status,
        requested=round(requested, 1),
        measured=round(measured, 1),
        unit="s",
        detail=f"{measured:.1f}s against {requested:.1f}s requested ({ratio:+.1%})",
    )


def _vocal(blueprint: MusicBlueprintV1, features: AudioFeatures) -> AdherenceCheck:
    """Instrumental requested, instrumental delivered — a weak but real proxy.

    There is no vocal detector here. What there is: vocals occupy 300 Hz–4 kHz densely and
    push the spectral centroid up, so an "instrumental" track whose centroid sits where a
    vocal would is *worth flagging*. That is a hint, and it is labelled as one — the status
    is CLOSE rather than MATCH even when it agrees, because agreement is not proof.

    A vocal blueprint is reported UNMEASURED outright: the absence of a vocal-shaped
    spectrum does not establish the absence of vocals, and claiming otherwise would be the
    fabricated measurement §7.16 forbids.
    """
    wanted_instrumental = not blueprint.vocal.enabled
    if not wanted_instrumental:
        return AdherenceCheck(
            name="vocal_presence",
            status=AdherenceStatus.UNMEASURED,
            requested="vocal",
            measured=None,
            detail=(
                "no vocal detector is installed; whether vocals are present cannot be "
                "measured, only listened for (§7.15)"
            ),
        )

    centroid = features.spectral_centroid
    # Above this, the energy distribution looks like something is singing over the bed.
    suspicious = centroid > 2_600.0
    return AdherenceCheck(
        name="vocal_presence",
        status=AdherenceStatus.DRIFT if suspicious else AdherenceStatus.CLOSE,
        requested="instrumental",
        measured=round(centroid, 0),
        unit="Hz centroid",
        detail=(
            f"spectral centroid {centroid:.0f} Hz is high for an instrumental; worth a "
            "listen (this is a proxy, not a vocal detector)"
            if suspicious
            else f"spectral centroid {centroid:.0f} Hz is consistent with an instrumental"
        ),
    )


def _energy(requested: float, features: AudioFeatures) -> AdherenceCheck:
    """Intended energy against measured loudness.

    Loudness is the one component of "energy" that can be measured without a model. It is
    not the whole of it — a dense, busy, quiet track has high energy in every sense except
    this one — so the bands are wide and the check is for gross mismatch only.
    """
    if features.integrated_lufs is None:
        return AdherenceCheck(
            name="energy",
            status=AdherenceStatus.UNMEASURED,
            requested=round(requested, 2),
            measured=None,
            unit="LUFS",
            detail="no loudness meter was available",
        )

    loudness = features.integrated_lufs
    low, high = next(
        (lo, hi) for ceiling, lo, hi in _ENERGY_LOUDNESS_BANDS if requested < ceiling
    )
    inside = low <= loudness <= high
    return AdherenceCheck(
        name="energy",
        status=AdherenceStatus.CLOSE if inside else AdherenceStatus.DRIFT,
        requested=round(requested, 2),
        measured=round(loudness, 1),
        unit="LUFS",
        detail=(
            f"{loudness:.1f} LUFS, inside the {low:.0f}…{high:.0f} band for energy "
            f"{requested:.2f}"
            if inside
            else f"{loudness:.1f} LUFS, outside the {low:.0f}…{high:.0f} band expected for "
            f"energy {requested:.2f}"
        ),
    )


def _key(requested: str, features: AudioFeatures) -> AdherenceCheck:
    """Requested key against estimation, only when the estimate is confident."""
    estimated = features.musical_key
    confidence = features.key_confidence
    if estimated is None or confidence is None:
        return AdherenceCheck(
            name="key",
            status=AdherenceStatus.UNMEASURED,
            requested=requested,
            measured=None,
            detail="no key could be estimated",
        )
    if confidence < _KEY_CONFIDENCE_FLOOR:
        return AdherenceCheck(
            name="key",
            status=AdherenceStatus.UNMEASURED,
            requested=requested,
            measured=estimated,
            detail=(
                f"key estimated as {estimated} but only at {confidence:.2f} confidence; "
                f"below the {_KEY_CONFIDENCE_FLOOR} floor this says more about the "
                "estimator than the track"
            ),
        )
    match = estimated.strip().lower() == requested.strip().lower()
    return AdherenceCheck(
        name="key",
        status=AdherenceStatus.MATCH if match else AdherenceStatus.DRIFT,
        requested=requested,
        measured=estimated,
        detail=(
            f"{estimated} at {confidence:.2f} confidence"
            if match
            else f"{estimated} at {confidence:.2f} confidence, not the requested {requested}"
        ),
    )
