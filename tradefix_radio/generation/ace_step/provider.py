"""The ACE-Step provider (§7.4).

One implementation of `MusicGenerationProvider`, and nothing more. §7's opening instruction
is that ACE-Step must be *"one implementation of the existing generation-provider interface,
not a special case spread through the codebase"* — so everything model-specific is inside
this package, and nothing outside it imports from here except the composition root that
chooses a provider.

What this class owns:

* the §7.6 lifecycle, so the model is loaded once and not per track;
* the §7.7 VRAM pre-flight, measurement and OOM ladder;
* the §7.8 profile in force for a given request;
* translation, via :class:`AceStepPromptBuilder`, which it does not do itself.

What it deliberately does not own: QC, originality, mastering, retries, queueing. Those are
Phase 6's and the GenerationManager's, and a provider that second-guessed them would make the
pipeline's verdict depend on which provider produced the audio — which is precisely what
§7.18 forbids.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from tradefix_radio.core.errors import (
    GenerationCancelledError,
    GenerationError,
    GenerationTimeoutError,
    GpuOutOfMemoryError,
    ProviderUnavailableError,
)
from tradefix_radio.generation.ace_step.client import (
    POLL_INTERVAL_SECONDS,
    AceStepClient,
    AceStepHttpError,
    TaskStatus,
)
from tradefix_radio.generation.ace_step.lifecycle import (
    BUILTIN_PROFILES,
    GenerationProfile,
    ModelState,
    check_transition,
)
from tradefix_radio.generation.ace_step.prompt import AceStepPromptBuilder, GenerationSpec
from tradefix_radio.generation.gpu import GpuProbe, GpuUsage
from tradefix_radio.generation.provider import (
    GenerationResult,
    ProviderDescription,
    ProviderHealth,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradefix_radio.config.schema import AceStepSettings
    from tradefix_radio.core.clock import Clock
    from tradefix_radio.generation.provider import GenerationRequest, ProgressCallback

_log = structlog.get_logger(__name__)

__all__ = ["AceStepProvider", "ProviderStatus"]

#: Substrings that identify an out-of-memory failure in a server-side error string.
#:
#: Matched on text because the failure crosses a process boundary as a message — there is no
#: exception type to catch. Deliberately narrow: treating any error mentioning "memory" as an
#: OOM would send unrelated faults down the §7.7 ladder and mask them.
_OOM_MARKERS: Final = (
    "cuda out of memory",
    "out of memory",
    "outofmemoryerror",
    "cuda_error_out_of_memory",
    "hip out of memory",
)

#: How much of the configured timeout the *submission* call may take.
#:
#: Submission should return a task id promptly. A server that blocks it until the audio is
#: ready is tolerated by giving it the full budget, but the normal case is fast.
_SUBMIT_TIMEOUT_FRACTION: Final = 1.0

#: Audio format requested from the server.
#:
#: FLAC: lossless, so Phase 6 measures the generator's actual output rather than a codec's
#: idea of it, and roughly half the size of WAV on disk. Phase 6's mastering produces the
#: broadcast WAV afterwards.
_AUDIO_FORMAT: Final = "flac"


@dataclass
class ProviderStatus:
    """What §7.24's health endpoint reports.

    A mutable snapshot held by the provider rather than recomputed, because most of it is
    history — last success, last error, observed latencies — which cannot be queried from the
    service.
    """

    state: ModelState = ModelState.UNAVAILABLE
    model: str = ""
    lm_model: str = ""
    provider_version: str = ""
    loaded_at_monotonic: float | None = None
    load_seconds: float | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    generations: int = 0
    failures: int = 0
    oom_events: int = 0
    #: Recent wall-clock generation times, for p50/p95.
    latencies: list[float] = field(default_factory=list)
    #: Most recent GPU reading, so the page can show VRAM without its own probe.
    gpu: dict[str, object] = field(default_factory=dict)
    current_track_id: str | None = None
    current_started_monotonic: float | None = None

    def record_latency(self, seconds: float, *, window: int = 50) -> None:
        self.latencies.append(seconds)
        if len(self.latencies) > window:
            del self.latencies[: len(self.latencies) - window]

    def percentile(self, fraction: float) -> float | None:
        """Nearest-rank percentile of observed generation time, or ``None``.

        ``None`` rather than 0.0 with no samples: a p95 of zero reads as "instant" on the
        Generation page, which is the §86 failure this project keeps guarding against.
        """
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
        return ordered[index]

    def as_payload(self) -> dict[str, object]:
        """§7.24's shape."""
        return {
            "provider": "ace_step",
            "status": self.state.value,
            "model": self.model,
            "lm_model": self.lm_model,
            "version": self.provider_version,
            "loaded": self.state in (ModelState.READY, ModelState.GENERATING),
            "load_seconds": self.load_seconds,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "generations": self.generations,
            "failures": self.failures,
            "oom_events": self.oom_events,
            "latency_p50_seconds": self.percentile(0.5),
            "latency_p95_seconds": self.percentile(0.95),
            "current_track_id": self.current_track_id,
            "gpu": dict(self.gpu),
        }


class AceStepProvider:
    """`MusicGenerationProvider` backed by a local ACE-Step service."""

    def __init__(
        self,
        settings: AceStepSettings,
        *,
        clock: Clock,
        client: AceStepClient | None = None,
        profiles: dict[str, GenerationProfile] | None = None,
        gpu: GpuProbe | None = None,
        builder: AceStepPromptBuilder | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._client = client or AceStepClient(settings.base_url)
        self._profiles = profiles or dict(BUILTIN_PROFILES)
        self._gpu = gpu or GpuProbe()
        self._builder = builder or AceStepPromptBuilder()

        self._status = ProviderStatus(
            model=settings.dit_model, lm_model=settings.lm_model
        )
        # One generation at a time. §7.22 says not to assume concurrency helps, and the
        # config schema already refuses `max_concurrent_jobs > 1` for this provider; the lock
        # makes that true in this process as well, rather than relying on the caller.
        self._lock = asyncio.Lock()
        self._cancelled: set[str] = set()
        #: Tasks whose generation we stopped waiting for. The GPU is still busy with them,
        #: so the next job must not assume a free card. See `cancel`.
        self._abandoned: set[str] = set()

    # --------------------------------------------------------------- state

    @property
    def status(self) -> ProviderStatus:
        return self._status

    @property
    def state(self) -> ModelState:
        return self._status.state

    def _transition(self, target: ModelState) -> None:
        self._status.state = check_transition(self._status.state, target)
        _log.debug("ace_step.state", state=target.value)

    def describe(self) -> ProviderDescription:
        return ProviderDescription(
            name="ace_step",
            model_identifier=self._settings.dit_model,
            supports_vocals=True,
            is_realtime_costly=True,
            # ACE-Step documents 10–600 s.
            max_duration_seconds=600.0,
        )

    # ---------------------------------------------------------- lifecycle

    async def load(self) -> None:
        """Bring the model up (§7.6). Idempotent when already READY."""
        if self._status.state in (ModelState.READY, ModelState.GENERATING):
            return
        if self._status.state is ModelState.LOADING:
            return

        self._transition(ModelState.LOADING)
        started = time.perf_counter()
        try:
            health = await self._client.health()
            self._status.provider_version = str(
                health.get("version") or health.get("service") or ""
            )
            await self._client.init_model(
                dit_model=self._settings.dit_model,
                lm_model=self._settings.lm_model,
                # Cold load pulls a checkpoint from disk into VRAM and may download it. The
                # generation timeout is far too short for that, so loading gets its own.
                timeout=self._settings.load_timeout_seconds,
            )
        except AceStepHttpError as error:
            self._fail(f"model load failed: {error}")
            raise ProviderUnavailableError(f"ACE-Step load failed: {error}") from error

        self._status.load_seconds = time.perf_counter() - started
        self._status.loaded_at_monotonic = self._clock.monotonic()
        self._transition(ModelState.READY)
        _log.info(
            "ace_step.loaded",
            model=self._settings.dit_model,
            lm_model=self._settings.lm_model,
            seconds=round(self._status.load_seconds, 2),
        )

    async def unload(self) -> None:
        """Release the model (§7.6).

        Best-effort: the documented API has no unload endpoint, so this drops *our* claim on
        the model and records the state. The service keeps its weights resident. Saying so is
        better than a method that pretends to free 5 GB and does not.
        """
        if self._status.state is ModelState.UNAVAILABLE:
            return
        if self._status.state is ModelState.GENERATING:
            raise GenerationError("cannot unload while a generation is in flight")
        self._transition(ModelState.UNLOADING)
        self._transition(ModelState.UNAVAILABLE)
        self._status.loaded_at_monotonic = None
        _log.info(
            "ace_step.unloaded",
            detail="the service retains its weights; no unload endpoint is documented",
        )

    def _fail(self, message: str) -> None:
        self._status.last_error = message[:500]
        self._status.last_error_at = self._clock.now().isoformat()
        self._status.failures += 1
        with contextlib.suppress(Exception):
            self._status.state = check_transition(self._status.state, ModelState.FAILED)

    # -------------------------------------------------------- healthcheck

    async def healthcheck(self) -> ProviderHealth:
        """Never raises (§18)."""
        started = time.perf_counter()
        try:
            payload = await self._client.health()
        except AceStepHttpError as error:
            return ProviderHealth.down(
                f"ACE-Step at {self._settings.base_url} is unreachable: {error}",
                latency_seconds=time.perf_counter() - started,
            )
        except Exception as error:  # noqa: BLE001 - a healthcheck must not propagate
            return ProviderHealth.down(
                f"healthcheck failed: {type(error).__name__}: {error}",
                latency_seconds=time.perf_counter() - started,
            )

        elapsed = time.perf_counter() - started
        status = str(payload.get("status", "")).lower()
        if status and status not in ("ok", "healthy", "ready"):
            return ProviderHealth.down(
                f"ACE-Step reports status {status!r}", latency_seconds=elapsed
            )
        return ProviderHealth.ok(
            f"ACE-Step {self._status.provider_version or 'service'} responding "
            f"({self._status.state.value})",
            latency_seconds=elapsed,
        )

    # --------------------------------------------------------- generation

    async def generate(
        self,
        request: GenerationRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> GenerationResult:
        """Produce one track. Raises on failure; never returns partial audio."""
        async with self._lock:
            return await self._generate_locked(request, on_progress)

    async def _generate_locked(
        self, request: GenerationRequest, on_progress: ProgressCallback | None
    ) -> GenerationResult:
        track_id = request.track_id
        self._cancelled.discard(track_id)

        if self._status.state is not ModelState.READY:
            await self.load()
        if self._status.state is not ModelState.READY:
            raise ProviderUnavailableError(
                f"ACE-Step is {self._status.state.value}, not ready to generate"
            )

        profile = self._profile_for(request)
        spec = self._builder.build(
            request.blueprint,
            lyrics=request.lyrics,
            inference_steps=profile.inference_steps,
            guidance_scale=profile.guidance_scale,
            profile=profile.name,
        )

        await self._preflight_vram(request)

        self._transition(ModelState.GENERATING)
        self._status.current_track_id = track_id
        self._status.current_started_monotonic = self._clock.monotonic()
        _report(on_progress, 0.02)

        started = time.perf_counter()
        try:
            async with self._gpu.measure() as measured:
                result = await self._run_task(request, spec, profile, on_progress)
            usage: GpuUsage = measured.usage
        except BaseException:
            self._status.current_track_id = None
            with contextlib.suppress(Exception):
                self._status.state = check_transition(
                    self._status.state, ModelState.READY
                )
            raise
        finally:
            self._status.current_track_id = None
            self._status.current_started_monotonic = None

        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            self._status.state = check_transition(self._status.state, ModelState.READY)

        self._status.generations += 1
        self._status.record_latency(elapsed)
        self._status.last_success_at = self._clock.now().isoformat()
        self._status.gpu = usage.as_metadata()

        audio_path, measured_duration, sample_rate, channels = result
        _log.info(
            "ace_step.generated",
            track_id=track_id,
            seconds=round(elapsed, 2),
            audio_seconds=round(measured_duration, 2),
            requested_seconds=round(spec.duration_seconds, 1),
            profile=profile.name,
            seed=spec.seed,
            peak_vram_mb=usage.peak_used_mb,
        )
        _report(on_progress, 1.0)

        return GenerationResult(
            track_id=track_id,
            audio_path=audio_path,
            duration_seconds=measured_duration,
            sample_rate=sample_rate,
            channels=channels,
            generation_seconds=elapsed,
            model_identifier=self._settings.dit_model,
            provider_name="ace_step",
            peak_vram_bytes=usage.peak_vram_bytes,
            detail={
                "prompt": spec.as_metadata(),
                # What the station *asked* for, kept beside what the provider was *given*.
                # The spec only knows the latter, so a downgrade from validated lyric to
                # `[Instrumental]` is invisible from the spec alone (§7.10).
                "requested_lyrics": (
                    None if request.lyrics is None else request.lyrics.text
                ),
                "gpu": usage.as_metadata(),
                "lm_model": self._settings.lm_model,
                "requested_duration_seconds": spec.duration_seconds,
                "duration_deviation_seconds": round(
                    measured_duration - spec.duration_seconds, 3
                ),
                "attempt": request.attempt,
            },
        )

    def _profile_for(self, request: GenerationRequest) -> GenerationProfile:
        """The profile this request runs under.

        A request that carries lyrics gets the vocal profile. Diction needs denoising steps
        that an arrangement does not, and at the `balanced` default the words never resolve —
        the model renders a convincing backing track and swallows the vocal. That was the
        cause of "no vocals at all" on real station output whose submission record showed the
        complete validated lyric had been sent.

        Keyed on ``request.lyrics``, not on ``blueprint.vocal.enabled``. A vocal blueprint
        that failed to get validated lyrics is realised as an instrumental, and paying 2.2x
        the GPU time for words that are not in the payload would be pure waste.

        A retry steps *down* in cost. §7.7 allows degrading settings after a failure, and a
        second attempt at the same expensive profile is the attempt most likely to fail the
        same way — particularly after an OOM, where the cheaper profile is also the smaller
        allocation.
        """
        has_lyrics = request.lyrics is not None and bool(request.lyrics.text.strip())
        name = self._settings.vocal_profile if has_lyrics else self._settings.profile
        if request.attempt > 1:
            # `vocal` sits above `quality`: the step-down ladder is ordered by cost, so a
            # retry after a timeout or an OOM gives up diction before it gives up the track.
            ladder = ["vocal", "quality", "balanced", "fast"]
            if name in ladder:
                name = ladder[min(len(ladder) - 1, ladder.index(name) + request.attempt - 1)]
        return self._profiles.get(name) or self._profiles.get("balanced") or (
            BUILTIN_PROFILES["balanced"]
        )

    async def _preflight_vram(self, request: GenerationRequest) -> None:
        """Refuse to start when the card plainly cannot hold it (§7.7).

        Raising `GpuOutOfMemoryError` *before* allocating is the cheap version of the same
        outcome: the manager's existing OOM handling applies, and the GPU never gets into the
        thrash that a real OOM causes. On a host with no GPU the check is skipped rather than
        failed — the probe returning ``None`` means "unknown", not "zero".
        """
        if self._settings.min_free_vram_mb <= 0:
            return
        free = await self._gpu.free_mb()
        if free is None:
            return
        if free < self._settings.min_free_vram_mb:
            self._status.oom_events += 1
            raise GpuOutOfMemoryError(
                f"{free:.0f} MB VRAM free, below the {self._settings.min_free_vram_mb} MB "
                f"required to start {request.track_id}"
            )

    async def _run_task(
        self,
        request: GenerationRequest,
        spec: GenerationSpec,
        profile: GenerationProfile,
        on_progress: ProgressCallback | None,
    ) -> tuple[Path, float, int, int]:
        """Submit, poll, download, verify. Returns (path, duration, rate, channels)."""
        timeout = min(
            request.timeout_seconds,
            self._settings.timeout_seconds * profile.timeout_multiplier,
        )
        payload = _submission_payload(spec, self._settings)

        try:
            task_id = await self._client.release_task(
                payload, timeout=timeout * _SUBMIT_TIMEOUT_FRACTION
            )
        except AceStepHttpError as error:
            self._fail(str(error))
            raise ProviderUnavailableError(f"submission failed: {error}") from error

        deadline = self._clock.monotonic() + timeout
        result = None
        while True:
            if request.track_id in self._cancelled:
                self._abandoned.add(task_id)
                raise GenerationCancelledError(
                    f"{request.track_id} was cancelled; ACE-Step task {task_id} continues "
                    "on the GPU because the API exposes no cancellation"
                )
            if self._clock.monotonic() >= deadline:
                self._abandoned.add(task_id)
                raise GenerationTimeoutError(
                    f"{request.track_id} exceeded {timeout:.0f}s (task {task_id})"
                )

            try:
                result = await self._client.query_result(task_id, timeout=timeout)
            except AceStepHttpError as error:
                # A poll that fails is not a generation that failed: the service may be
                # briefly busy. The deadline above is what ends this, not one bad poll.
                _log.debug("ace_step.poll_failed", task_id=task_id, error=str(error))
                await self._clock.sleep(POLL_INTERVAL_SECONDS)
                continue

            if result.status is TaskStatus.SUCCESS:
                break
            if result.status is TaskStatus.FAILURE:
                self._raise_for_remote_failure(result.error or "unknown error", request)

            _report(on_progress, _elapsed_fraction(self._clock.monotonic(), deadline, timeout))
            await self._clock.sleep(POLL_INTERVAL_SECONDS)

        _report(on_progress, 0.9)
        if not result.file:
            self._fail("task succeeded with no audio file")
            raise GenerationError(
                f"ACE-Step reported success for {request.track_id} but returned no file"
            )
        if result.seed is not None and result.seed != spec.seed:
            # Recorded, not corrected. §7.13 is about measuring what the model does.
            _log.info(
                "ace_step.seed_differs",
                track_id=request.track_id,
                requested=spec.seed,
                used=result.seed,
            )

        await self._fetch(result.file, request.output_path, timeout=timeout)
        return _verify_audio(request.output_path, request.track_id)

    def _raise_for_remote_failure(self, message: str, request: GenerationRequest) -> None:
        """Turn a server-side error string into the right exception type (§7.19)."""
        lowered = message.lower()
        self._fail(message)
        if any(marker in lowered for marker in _OOM_MARKERS):
            self._status.oom_events += 1
            raise GpuOutOfMemoryError(
                f"ACE-Step ran out of VRAM generating {request.track_id}: {message[:300]}"
            )
        if "timeout" in lowered or "timed out" in lowered:
            raise GenerationTimeoutError(
                f"ACE-Step timed out generating {request.track_id}: {message[:300]}"
            )
        if "cancel" in lowered:
            raise GenerationCancelledError(f"{request.track_id}: {message[:300]}")
        raise GenerationError(
            f"ACE-Step failed to generate {request.track_id}: {message[:300]}"
        )

    async def _fetch(self, file_ref: str, destination: Path, *, timeout: float) -> None:
        """Get the rendered audio onto our disk, atomically.

        A local path is copied rather than downloaded when the service shares a filesystem
        with us — which it does in the default single-machine deployment — because pulling
        40 MB through localhost HTTP to land it two directories away is pure waste.

        Written to a temporary name and renamed, so a partially-fetched file can never be
        picked up as a finished track. §18's contract: *"Must not return a result pointing at
        a file it did not finish writing."*
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_suffix(destination.suffix + ".part")

        source = Path(file_ref)
        is_local = not file_ref.startswith(("http://", "https://")) and await asyncio.to_thread(
            source.is_file
        )
        if is_local:
            await asyncio.to_thread(_copy_file, source, staging)
        else:
            payload = await self._client.download(file_ref, timeout=timeout)
            if not payload:
                raise GenerationError(f"ACE-Step returned an empty audio body for {file_ref}")
            await asyncio.to_thread(staging.write_bytes, payload)

        await asyncio.to_thread(_replace, staging, destination)

    # ------------------------------------------------------------- cancel

    async def cancel(self, track_id: str) -> bool:
        """Stop waiting for a track (§7.4).

        Returns ``True`` when a generation was in flight for this track. **The GPU keeps
        working**: the documented API has no cancellation endpoint, so all this can do is
        stop waiting and release the job slot. The log says so explicitly, and the abandoned
        task is remembered so the pre-flight check can see the card is still busy.
        """
        if self._status.current_track_id != track_id and track_id not in self._cancelled:
            return False
        self._cancelled.add(track_id)
        _log.warning(
            "ace_step.cancel_requested",
            track_id=track_id,
            detail=(
                "ACE-Step exposes no cancellation endpoint; the station has stopped waiting "
                "but the GPU continues rendering this track to completion"
            ),
        )
        return True

    @property
    def abandoned_tasks(self) -> frozenset[str]:
        """Tasks still running on the GPU that nothing is waiting for."""
        return frozenset(self._abandoned)


# ------------------------------------------------------------------ helpers


def _submission_payload(spec: GenerationSpec, settings: AceStepSettings) -> dict[str, Any]:
    """The `/release_task` body, built from the spec and settings.

    ``use_random_seed`` is false and an explicit seed is always sent: §23's registry exists to
    stop intentional seed reuse, and it can only do that if the station chooses the seed. A
    server-chosen seed would also make §7.13's determinism test unanswerable.
    """
    payload: dict[str, Any] = {
        "prompt": spec.caption,
        "lyrics": spec.lyrics,
        "model": settings.dit_model,
        "inference_steps": spec.inference_steps,
        "guidance_scale": spec.guidance_scale,
        "seed": spec.seed,
        "use_random_seed": False,
        "batch_size": 1,
        "audio_duration": spec.duration_seconds,
        "bpm": spec.bpm,
        "key_scale": spec.key_scale,
        "audio_format": _AUDIO_FORMAT,
        "task_type": "text2music",
        "thinking": settings.thinking,
    }
    # No ``instrumental`` key, and that is a correction rather than an omission.
    #
    # An earlier version sent ``instrumental: True`` alongside the marker, on the reasoning
    # that belt and braces was safer. Checked against the running server's own schema,
    # ``GenerateMusicRequest`` has no such field and pydantic's default policy is to ignore
    # unknown keys — so the flag did nothing at all, while reading like a second line of
    # defence. A no-op that looks like enforcement is worse than no enforcement, because it
    # stops anyone looking for the real mechanism.
    #
    # The real mechanism is the ``"[Instrumental]"`` lyric marker, which the server's own
    # docstring names: *"Use '[Instrumental]' for instrumental songs."* §7.11 verifies it
    # empirically across seeds rather than trusting either of us.
    return payload


def _verify_audio(path: Path, track_id: str) -> tuple[Path, float, int, int]:
    """Read back what was written. Nothing is trusted until it has been decoded.

    §7.19 lists "invalid output", "zero-byte file" and "truncated file" as cases that must be
    classified, and they are all indistinguishable from success until something opens the
    file. Phase 6 would catch them a moment later, but failing here attributes the fault to
    *generation* rather than to quality — which is the difference between retrying the job
    and quarantining the track.
    """
    from tradefix_radio.audio.io import read_info  # noqa: PLC0415
    from tradefix_radio.core.errors import AudioError  # noqa: PLC0415

    if not path.is_file():
        raise GenerationError(f"ACE-Step produced no file for {track_id} at {path}")
    size = path.stat().st_size
    if size == 0:
        raise GenerationError(f"ACE-Step produced a zero-byte file for {track_id}")

    try:
        info = read_info(path)
    except AudioError as error:
        raise GenerationError(
            f"ACE-Step produced an undecodable file for {track_id}: {error}"
        ) from error

    if info.duration_seconds <= 0:
        raise GenerationError(f"ACE-Step produced an empty-duration file for {track_id}")
    return path, info.duration_seconds, info.sample_rate, info.channels


def _copy_file(source: Path, destination: Path) -> None:
    import shutil  # noqa: PLC0415

    shutil.copyfile(source, destination)


def _replace(staging: Path, destination: Path) -> None:
    staging.replace(destination)


def _report(callback: ProgressCallback | None, value: float) -> None:
    """Progress is advisory; a throwing callback must not fail a generation."""
    if callback is None:
        return
    with contextlib.suppress(Exception):
        callback(max(0.0, min(1.0, value)))


def _elapsed_fraction(now: float, deadline: float, timeout: float) -> float:
    """Elapsed-time progress, capped below completion.

    §7.25: *"Do not display fake percent complete if ACE-Step does not expose trustworthy
    progress."* It does not — `/query_result` reports pending or done, nothing between. This
    is honest about being a *clock*, not a measure of the work: it is capped at 0.8 so it can
    never imply "almost there", and the UI renders this provider's progress as indeterminate.
    """
    if timeout <= 0:
        return 0.05
    remaining = max(0.0, deadline - now)
    return max(0.05, min(0.8, 1.0 - (remaining / timeout)))
