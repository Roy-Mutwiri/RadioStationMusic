"""MockMusicProvider (§62).

§62: the mock provider must produce **real** audio, quickly, so that the whole station can be
built and proven before any AI model exists. Phase 4's entire premise is that the radio runs
24/7 against this provider.

It renders from the blueprint's own numbers through
:mod:`tradefix_radio.audio.synthesis`, which has a consequence worth keeping: **the §1 chain
becomes audible**. A quiet market produces a slow sparse track and a breakout produces a fast
dense one, so a broken energy mapping is something you can hear rather than something you
have to assert.

Three injectable behaviours exist for testing and are **off by default**:

``latency_seconds``  simulated generation time, so §93's capacity maths has something to
                     measure. Slept on the injected clock, so §64's accelerated runs are not
                     limited by it.
``failure_rate``     §66 chaos: a fraction of jobs raise instead of succeeding.
``defect_rate``      §24 QC exercise: a fraction produce deliberately broken audio —
                     too short, silent, clipped or DC-offset — so the QC stage is proven
                     against real defects rather than against hand-built fixtures.

Cancellation is cooperative and checked between render stages, because a synchronous NumPy
render cannot be interrupted mid-array. The granularity is a few hundred milliseconds, which
is fast enough for §28's replan and for an operator skip.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np

from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.pcm import SAMPLE_DTYPE, AudioBuffer
from tradefix_radio.audio.synthesis import Synthesiser, SynthesisSpec
from tradefix_radio.config.schema import MockProviderSettings
from tradefix_radio.core.clock import Clock, SystemClock
from tradefix_radio.core.errors import (
    GenerationCancelledError,
    GenerationError,
    GenerationTimeoutError,
)
from tradefix_radio.generation.provider import (
    GenerationRequest,
    GenerationResult,
    ProgressCallback,
    ProviderDescription,
    ProviderHealth,
)

PROVIDER_NAME = "mock"
MODEL_IDENTIFIER = "tradefix-mock-synth/1"

#: Defect kinds the provider can inject, and what each proves about QC (§24).
DEFECT_KINDS = ("truncated", "silent", "clipped", "dc_offset")


@dataclass(frozen=True)
class _Defect:
    kind: str
    detail: str


class MockMusicProvider:
    """Renders blueprints to real audio with a small synthesiser.

    Satisfies :class:`~tradefix_radio.generation.provider.MusicGenerationProvider`.
    """

    def __init__(
        self,
        settings: MockProviderSettings,
        *,
        clock: Clock | None = None,
        seed: int = 0,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        # Separate from the per-track render seed: this one drives *whether* a job fails,
        # which must not change when a blueprint changes.
        self._chaos = np.random.default_rng(seed)
        self._cancelled: set[str] = set()
        self._in_flight: set[str] = set()

    # -- the interface -----------------------------------------------------

    def describe(self) -> ProviderDescription:
        return ProviderDescription(
            name=PROVIDER_NAME,
            model_identifier=MODEL_IDENTIFIER,
            # The synthesiser has no voice. Vocal blueprints still render — as the
            # instrumental arrangement — and the §8 vocal spec is carried through for
            # Phase 7. Claiming vocal support would make the §19 contract a lie.
            supports_vocals=False,
            is_realtime_costly=False,
            max_duration_seconds=None,
        )

    async def healthcheck(self) -> ProviderHealth:
        """Always healthy — there is nothing to be unhealthy about.

        Reported honestly rather than as a stub: the mock has no model to load, no GPU to
        contend for and no socket to lose. A §34 test that needs an unhealthy provider
        should inject one, not rely on this returning ``False`` by luck.
        """
        return ProviderHealth.ok("mock provider needs no resources")

    async def cancel(self, track_id: str) -> bool:
        if track_id not in self._in_flight:
            return False
        self._cancelled.add(track_id)
        return True

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        track_id = request.track_id
        self._in_flight.add(track_id)
        started = time.perf_counter()
        try:
            return await self._generate(request, started, on_progress)
        finally:
            self._in_flight.discard(track_id)
            self._cancelled.discard(track_id)

    # -- internals ---------------------------------------------------------

    async def _generate(
        self,
        request: GenerationRequest,
        started: float,
        on_progress: ProgressCallback | None,
    ) -> GenerationResult:
        composition = request.blueprint.composition
        self._report(on_progress, 0.0)

        if self._settings.failure_rate > 0 and self._roll() < self._settings.failure_rate:
            raise GenerationError(
                "mock provider injected failure",
                track_id=request.track_id,
                reason="injected_failure_rate",
            )

        defect = self._choose_defect()
        await self._simulate_latency(request)
        self._check_cancelled(request.track_id)
        self._report(on_progress, 0.35)

        spec = SynthesisSpec(
            duration_seconds=float(composition.duration_seconds),
            bpm=composition.bpm,
            key=composition.key,
            energy=composition.energy,
            rhythm_density=composition.rhythm_density,
            bass_intensity=composition.bass_intensity,
            drum_intensity=composition.drum_intensity,
            melodic_complexity=composition.melodic_complexity,
            sample_rate=self._settings.sample_rate,
            channels=2,
            sections=tuple(composition.structure),
            seed=request.blueprint.seed,
        )
        # The render is synchronous NumPy work of a second or two. Run it in a thread so a
        # single-process station (api + worker + playout in one loop, §72 development mode)
        # does not stall its event loop — which would stop the playout engine feeding the
        # sink and cause an underrun during generation.
        #
        # Held on the injected clock for the duration. The wall-clock cost of rendering and
        # writing a file is an artefact of *simulating* a generator, not a cost in the
        # simulated world — ``_simulate_latency`` above is what represents that — so an
        # accelerated clock must not run forward while it happens. Without the hold, a soak
        # measured §93 capacity between 0.57x and 0.88x for a provider configured to be six
        # times faster than playback, and the queue never built past a single track.
        #
        # This is a *leaf*: everything inside is either synchronous or a thread, so the hold
        # never spans an await that itself needs virtual time to advance. Holding around the
        # caller instead deadlocked the clock — see
        # :meth:`~tradefix_radio.radio.station.RadioStation._generation_worker`.
        with self._clock.hold():
            buffer = await asyncio.to_thread(Synthesiser(spec).render)
            self._check_cancelled(request.track_id)
            self._report(on_progress, 0.8)

            if defect is not None:
                buffer = self._apply_defect(buffer, defect)

            write_audio(request.output_path, buffer, subtype=None)
        self._report(on_progress, 1.0)

        return GenerationResult(
            track_id=request.track_id,
            audio_path=request.output_path,
            # Measured from the buffer actually written, not echoed from the request, so
            # §24's duration-deviation check can fail when the "truncated" defect fires.
            duration_seconds=buffer.duration_seconds,
            sample_rate=buffer.sample_rate,
            channels=buffer.channels,
            generation_seconds=time.perf_counter() - started,
            model_identifier=MODEL_IDENTIFIER,
            provider_name=PROVIDER_NAME,
            peak_vram_bytes=None,
            detail={"defect": defect.kind} if defect else {},
        )

    async def _simulate_latency(self, request: GenerationRequest) -> None:
        """Sleep the configured latency, in slices, so cancellation stays responsive.

        Slept on the **injected clock**: under a
        :class:`~tradefix_radio.core.clock.VirtualClock` this costs no wall-clock time, which
        is what lets §64 run seven simulated days in minutes while still exercising the
        capacity maths that the latency exists to feed.
        """
        total = self._settings.latency_seconds
        if total <= 0:
            return
        if total > request.timeout_seconds:
            # Honest ordering: a provider configured to be slower than its deadline should
            # report a timeout rather than silently return late work.
            raise GenerationTimeoutError(
                f"mock latency {total:.1f}s exceeds the {request.timeout_seconds:.1f}s "
                "deadline",
                track_id=request.track_id,
            )
        slice_seconds = min(0.25, total)
        remaining = total
        while remaining > 0:
            await self._clock.sleep(min(slice_seconds, remaining))
            remaining -= slice_seconds
            self._check_cancelled(request.track_id)

    def _check_cancelled(self, track_id: str) -> None:
        if track_id in self._cancelled:
            raise GenerationCancelledError(
                "generation cancelled", track_id=track_id, provider=PROVIDER_NAME
            )

    def _roll(self) -> float:
        return float(self._chaos.random())

    def _choose_defect(self) -> _Defect | None:
        if self._settings.defect_rate <= 0 or self._roll() >= self._settings.defect_rate:
            return None
        kind = str(self._chaos.choice(DEFECT_KINDS))
        return _Defect(kind=kind, detail=f"injected {kind} defect")

    def _apply_defect(self, buffer: AudioBuffer, defect: _Defect) -> AudioBuffer:
        """Break the audio in a specific, QC-detectable way (§24).

        Each kind maps to exactly one §24 rule, so a QC suite that passes this provider's
        defective output has demonstrably exercised that rule rather than a near miss.
        """
        if defect.kind == "truncated":
            # Well under qc.min_duration_seconds and far outside max_duration_deviation.
            return buffer.slice_seconds(0.0, max(1.0, buffer.duration_seconds * 0.15))
        if defect.kind == "silent":
            return AudioBuffer.silence(
                seconds=buffer.duration_seconds,
                sample_rate=buffer.sample_rate,
                channels=buffer.channels,
            )
        if defect.kind == "clipped":
            # Deliberately past full scale so a meaningful fraction of samples clip.
            return AudioBuffer.owning(buffer.samples * SAMPLE_DTYPE(4.0), buffer.sample_rate)
        if defect.kind == "dc_offset":
            offset = np.full_like(buffer.samples, SAMPLE_DTYPE(0.25))
            return AudioBuffer.owning(
                buffer.samples * SAMPLE_DTYPE(0.5) + offset, buffer.sample_rate
            )
        raise AssertionError(f"unknown defect kind {defect.kind!r}")

    @staticmethod
    def _report(callback: ProgressCallback | None, value: float) -> None:
        if callback is not None:
            callback(value)


__all__ = ["DEFECT_KINDS", "MODEL_IDENTIFIER", "PROVIDER_NAME", "MockMusicProvider"]
