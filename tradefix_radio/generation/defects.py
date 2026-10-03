"""A provider that deliberately produces bad output (§6.23).

Post-production is only proven by what it rejects. A soak in which every generated track is
clean exercises the happy path and tells you nothing about the station's behaviour when the
generator misbehaves — which is the behaviour that matters, because a real model *will*
misbehave and the failure mode is dead air.

So this wraps any provider and corrupts a configured fraction of its output: silence, heavy
clipping, a truncated render, a dropout in the middle. Separately it re-emits a configured
fraction as **byte-identical copies** of tracks already produced, which is the duplicate case
§6.3 has to catch.

Why this lives in the package rather than in `tests/`
-----------------------------------------------------
`tradefix soak` is a shipped command and the injection is one of its flags, so the code has to
be importable from the CLI. It is also the honest place for it: the mock provider next door is
production code for the same reason. Nothing constructs this unless a flag asks for it, and
the station cannot enable it on its own.
"""

from __future__ import annotations

import enum
import random
import shutil
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

import numpy as np
import structlog

from tradefix_radio.audio.io import read_audio, write_audio
from tradefix_radio.audio.pcm import AudioBuffer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from tradefix_radio.generation.provider import (
        GenerationRequest,
        GenerationResult,
        MusicGenerationProvider,
        ProgressCallback,
        ProviderDescription,
        ProviderHealth,
    )

_log = structlog.get_logger(__name__)

__all__ = ["DefectInjectingProvider", "DefectKind", "InjectionStats"]


class DefectKind(str, enum.Enum):
    """What was done to a track. Recorded so the soak report can break rejections down."""

    NONE = "none"
    SILENCE = "silence"
    CLIPPING = "clipping"
    TRUNCATION = "truncation"
    DROPOUT = "dropout"
    DUPLICATE = "duplicate"


#: The defects an "invalid" render is drawn from, and their relative weights.
#:
#: Weighted towards silence and dropouts because those are what a real generator produces when
#: it fails — a model that runs out of context or hits a sampling collapse emits nothing, not
#: distortion. Clipping is rarer and truncation rarer still, which the weights reflect.
_DEFECT_WEIGHTS: Final[dict[DefectKind, float]] = {
    DefectKind.SILENCE: 0.40,
    DefectKind.DROPOUT: 0.30,
    DefectKind.CLIPPING: 0.20,
    DefectKind.TRUNCATION: 0.10,
}


@dataclass
class InjectionStats:
    """What the injector actually did, for the soak report."""

    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    #: Track ids whose audio was deliberately broken, so the report can check each was caught.
    corrupted_track_ids: list[str] = field(default_factory=list)
    duplicate_track_ids: list[str] = field(default_factory=list)

    def record(self, kind: DefectKind, track_id: str) -> None:
        self.total += 1
        self.by_kind[kind.value] = self.by_kind.get(kind.value, 0) + 1
        if kind is DefectKind.DUPLICATE:
            self.duplicate_track_ids.append(track_id)
        elif kind is not DefectKind.NONE:
            self.corrupted_track_ids.append(track_id)

    @property
    def injected(self) -> int:
        """Tracks that were deliberately made unusable — corrupted or duplicated."""
        return len(self.corrupted_track_ids) + len(self.duplicate_track_ids)


class DefectInjectingProvider:
    """Wraps a provider and spoils a fraction of its output.

    Deterministic for a given seed, because a soak that injects a different pattern on every
    run cannot be compared against the previous one — and "the rejection rate changed" would
    be indistinguishable from "the injection changed".
    """

    def __init__(
        self,
        inner: MusicGenerationProvider,
        *,
        invalid_rate: float = 0.20,
        duplicate_rate: float = 0.10,
        seed: int = 2026,
    ) -> None:
        if not 0.0 <= invalid_rate <= 1.0 or not 0.0 <= duplicate_rate <= 1.0:
            raise ValueError("rates must be between 0 and 1")
        if invalid_rate + duplicate_rate > 1.0:
            raise ValueError("invalid_rate + duplicate_rate cannot exceed 1")
        self._inner = inner
        self._invalid_rate = invalid_rate
        self._duplicate_rate = duplicate_rate
        self._rng = random.Random(seed)  # noqa: S311 - test instrumentation, not security
        self._produced: list[Path] = []
        self.stats = InjectionStats()

    # The `MusicGenerationProvider` protocol, delegated rather than inherited so this wraps
    # any implementation — including the real ACE-Step provider when Phase 7 lands.
    async def healthcheck(self) -> ProviderHealth:
        return await self._inner.healthcheck()

    async def cancel(self, track_id: str) -> bool:
        return await self._inner.cancel(track_id)

    def describe(self) -> ProviderDescription:
        """The inner provider's identity, with the injection declared in the name.

        Declared rather than hidden: this name is written into every track row the run
        produces, so a database full of deliberately broken tracks can never be mistaken
        later for a record of the real provider misbehaving.
        """
        inner = self._inner.describe()
        return replace(inner, name=f"{inner.name}+defects")

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        result = await self._inner.generate(request, on_progress=on_progress)
        # `total` is incremented by `record`, which every path below calls exactly once.
        # Counting it here as well double-counted every track and would have made the soak
        # report an injection rate half of the real one — flattering, and wrong.
        kind = self._choose()

        if kind is DefectKind.DUPLICATE and self._produced:
            source = self._rng.choice(self._produced)
            # A byte-for-byte copy. This is the §6.3 case: not similar audio, the *same*
            # audio, which the canonical hash must catch before anything expensive runs.
            shutil.copyfile(source, result.audio_path)
            self.stats.record(kind, request.blueprint.track_id)
            _log.info(
                "defects.injected",
                track_id=request.blueprint.track_id,
                kind=kind.value,
                copied_from=source.name,
            )
            return result

        if kind is not DefectKind.NONE:
            self._corrupt(result.audio_path, kind)
            self.stats.record(kind, request.blueprint.track_id)
            _log.info(
                "defects.injected", track_id=request.blueprint.track_id, kind=kind.value
            )
            return result

        # Only clean output joins the pool duplicates are drawn from. Copying a corrupted
        # track would make one injected defect count as two, and the report's arithmetic
        # would stop adding up.
        self._produced.append(result.audio_path)
        self.stats.record(DefectKind.NONE, request.blueprint.track_id)
        return result

    def _choose(self) -> DefectKind:
        roll = self._rng.random()
        if roll < self._duplicate_rate:
            return DefectKind.DUPLICATE
        if roll < self._duplicate_rate + self._invalid_rate:
            kinds = list(_DEFECT_WEIGHTS)
            return self._rng.choices(kinds, weights=[_DEFECT_WEIGHTS[k] for k in kinds])[0]
        return DefectKind.NONE

    def _corrupt(self, path: Path, kind: DefectKind) -> None:
        """Rewrite the file in place with the named defect."""
        buffer = read_audio(path)
        samples = np.array(buffer.samples, copy=True)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        rate = buffer.sample_rate

        if kind is DefectKind.SILENCE:
            samples[:] = 0.0
        elif kind is DefectKind.CLIPPING:
            # Drive it well past full scale and clamp: the signature of a generator whose
            # output layer saturated.
            samples = np.clip(samples * 12.0, -1.0, 1.0)
        elif kind is DefectKind.TRUNCATION:
            # A tenth of the intended length. The duration check is what catches this, and it
            # matters because a truncated track that *played* would be audible as a cut-off.
            samples = samples[: max(1, samples.shape[0] // 10)]
        elif kind is DefectKind.DROPOUT:
            start = samples.shape[0] // 3
            end = min(samples.shape[0], start + int(rate * 10.0))
            samples[start:end, :] = 0.0

        write_audio(path, AudioBuffer(samples.astype(np.float32), rate))
