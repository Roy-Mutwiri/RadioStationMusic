"""The ACE-Step provider against a real HTTP server (§7.19, §7.30).

The server is fake; the HTTP is not. §7.30 asks for "fake-server/provider protocol tests",
and the reason to run them over a socket rather than a patched method is that the failures
worth catching live in the protocol: envelope handling, how a 500 surfaces, what a hang does
to the timeout, whether a download path works. A stubbed client would assert that the
provider calls a method, which was never in question.

None of this needs a GPU or a model, so it runs in normal CI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fake_ace_step import FakeAceStep
from tests.conftest import make_blueprint, make_settings
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import SystemClock
from tradefix_radio.core.errors import (
    GenerationCancelledError,
    GenerationError,
    GenerationTimeoutError,
    GpuOutOfMemoryError,
    ProviderUnavailableError,
)
from tradefix_radio.generation.ace_step import AceStepProvider, ModelState
from tradefix_radio.generation.ace_step.client import AceStepClient
from tradefix_radio.generation.gpu import GpuProbe
from tradefix_radio.generation.provider import GenerationRequest

#: A probe that reports no GPU, so the VRAM pre-flight is skipped unless a test wants it.
NO_GPU = GpuProbe(enabled=False)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[FakeAceStep]:
    with FakeAceStep(audio_dir=tmp_path / "ace-out") as fake:
        yield fake


def settings_for(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str], **overrides: object
) -> AppSettings:
    ace: dict[str, object] = {
        "base_url": server.base_url,
        "timeout_seconds": 20.0,
        "load_timeout_seconds": 20.0,
        # Off by default: these tests are about the protocol, and the pre-flight needs a
        # real card to mean anything. The OOM tests turn it back on explicitly.
        "min_free_vram_mb": 0,
    }
    ace.update(overrides)
    return make_settings(tmp_path, clean_environ, generation={"ace_step": ace})


def provider_for(
    server: FakeAceStep,
    tmp_path: Path,
    clean_environ: dict[str, str],
    *,
    gpu: GpuProbe | None = None,
    **overrides: object,
) -> AceStepProvider:
    settings = settings_for(server, tmp_path, clean_environ, **overrides)
    return AceStepProvider(
        settings.generation.ace_step,
        clock=SystemClock(),
        client=AceStepClient(server.base_url),
        gpu=gpu or NO_GPU,
    )


def request_for(tmp_path: Path, track_id: str = "TF-001", **blueprint: object):
    bp = make_blueprint(track_id, duration_seconds=30, **blueprint)
    return GenerationRequest(
        blueprint=bp,
        output_path=tmp_path / f"{track_id}.wav",
        lyrics=None,
        timeout_seconds=20.0,
        attempt=1,
    )


# ------------------------------------------------------------- happy path


async def test_a_generation_produces_a_verified_file(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path))

    assert result.audio_path.is_file()
    assert result.duration_seconds > 0
    assert result.provider_name == "ace_step"
    assert result.generation_seconds > 0
    # The duration reported is the file's, measured — not the request echoed back.
    assert "duration_deviation_seconds" in result.detail
    assert provider.state is ModelState.READY


async def test_the_prompt_is_stored_with_the_result(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.9: store the final provider prompt for reproducibility."""
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path, genre="uk_drill", bpm=142))

    prompt = result.detail["prompt"]
    assert isinstance(prompt, dict)
    assert "UK drill" in str(prompt["caption"])
    assert prompt["bpm"] == 142
    assert prompt["seed"] == 1234
    assert prompt["profile"] == "balanced"


async def test_the_station_chooses_the_seed_not_the_server(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§23's registry can only prevent reuse if the seed is ours.

    `use_random_seed` must be false in every submission, or the seed registry is recording
    numbers that had no effect on the audio.
    """
    provider = provider_for(server, tmp_path, clean_environ)
    await provider.generate(request_for(tmp_path, seed=777))

    assert len(server.submissions) == 1
    submitted = server.submissions[0]
    assert submitted["seed"] == 777
    assert submitted["use_random_seed"] is False


async def test_an_instrumental_request_uses_the_marker_the_server_reads(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.11, pinned to the mechanism that actually works.

    The REST ``GenerateMusicRequest`` has **no** ``instrumental`` field — verified against
    the running server's OpenAPI schema — and pydantic ignores unknown keys, so an earlier
    version's ``instrumental: True`` was a silent no-op that read like enforcement. The
    marker is the real signal, and this test fails if anyone reintroduces the decoration.
    """
    provider = provider_for(server, tmp_path, clean_environ)
    await provider.generate(request_for(tmp_path, instrumental=True))

    submitted = server.submissions[0]
    assert submitted["lyrics"] == "[Instrumental]"
    assert "instrumental" not in submitted, (
        "the REST API has no such field; sending it implies a guarantee it does not give"
    )


async def test_polling_waits_for_a_slow_generation(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.pending_polls = 2
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path))
    assert result.audio_path.is_file()


async def test_audio_is_downloaded_when_it_is_not_a_local_path(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """The deployment where the service does not share our filesystem."""
    server.behaviour.serve_local_path = False
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path))
    assert result.audio_path.is_file()
    assert result.audio_path.stat().st_size > 0


async def test_no_partial_file_is_left_behind_on_success(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§18: never return a path to a file that was not finished.

    The fetch stages to `.part` and renames, so a crash mid-download cannot leave something
    that looks like a finished track.
    """
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path))
    assert not result.audio_path.with_suffix(result.audio_path.suffix + ".part").exists()


# --------------------------------------------------------------- lifecycle


async def test_the_model_loads_once_not_per_track(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.6: *"Do not reload the entire model for every track."*"""
    provider = provider_for(server, tmp_path, clean_environ)
    await provider.generate(request_for(tmp_path, "TF-A"))
    await provider.generate(request_for(tmp_path, "TF-B"))
    await provider.generate(request_for(tmp_path, "TF-C"))

    assert len(server.init_calls) == 1, f"loaded {len(server.init_calls)} times"
    assert len(server.submissions) == 3


async def test_the_lifecycle_reaches_ready_and_reports_load_time(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    provider = provider_for(server, tmp_path, clean_environ)
    assert provider.state is ModelState.UNAVAILABLE
    await provider.load()
    assert provider.state is ModelState.READY
    assert provider.status.load_seconds is not None


async def test_a_dead_service_fails_the_load_and_records_why(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.unhealthy = True
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(ProviderUnavailableError):
        await provider.load()
    assert provider.state is ModelState.FAILED
    assert provider.status.last_error


async def test_healthcheck_never_raises_on_a_dead_service(
    tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§18: a healthcheck that throws cannot be used to decide whether to retry."""
    settings = make_settings(
        tmp_path,
        clean_environ,
        # Nothing is listening on this port.
        generation={"ace_step": {"base_url": "http://127.0.0.1:1", "min_free_vram_mb": 0}},
    )
    provider = AceStepProvider(
        settings.generation.ace_step, clock=SystemClock(), gpu=NO_GPU
    )
    health = await provider.healthcheck()
    assert not health.healthy
    assert "unreachable" in health.detail


# ------------------------------------------------------- failures (§7.19)


async def test_a_server_side_failure_becomes_a_generation_error(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.fail_with = "diffusion step failed"
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(GenerationError) as caught:
        await provider.generate(request_for(tmp_path))
    assert "diffusion step failed" in str(caught.value)


async def test_a_cuda_oom_is_classified_as_oom_not_a_generic_failure(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.7: catch OOM *explicitly*.

    The distinction drives different handling — retry at a cheaper profile versus treat as a
    model fault — so collapsing it into `GenerationError` would lose the one piece of
    information the recovery ladder needs.
    """
    server.behaviour.fail_with = "CUDA out of memory. Tried to allocate 2.00 GiB"
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(GpuOutOfMemoryError):
        await provider.generate(request_for(tmp_path))
    assert provider.status.oom_events == 1


async def test_an_unrelated_memory_word_is_not_mistaken_for_oom(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """The paired negative, so the matcher cannot be widened into uselessness.

    Treating anything mentioning memory as an OOM would send unrelated faults down the
    recovery ladder and mask them behind a retry.
    """
    server.behaviour.fail_with = "failed to memoize the tokenizer vocabulary"
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(GenerationError) as caught:
        await provider.generate(request_for(tmp_path))
    assert not isinstance(caught.value, GpuOutOfMemoryError)


async def test_a_hung_generation_times_out(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.hang = True
    provider = provider_for(server, tmp_path, clean_environ, timeout_seconds=2.0)
    request = request_for(tmp_path)
    with pytest.raises(GenerationTimeoutError):
        await provider.generate(request)
    # The abandoned task is remembered: the GPU is still working on it.
    assert provider.abandoned_tasks


async def test_a_rejected_submission_is_a_provider_problem(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.reject_submission = True
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(ProviderUnavailableError):
        await provider.generate(request_for(tmp_path))


async def test_a_zero_byte_file_fails_generation_not_qc(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.19 lists this explicitly.

    Failing here rather than letting Phase 6 catch it attributes the fault to *generation*,
    which is the difference between retrying the job and quarantining the track.
    """
    server.behaviour.zero_byte = True
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(GenerationError, match="zero-byte"):
        await provider.generate(request_for(tmp_path))


async def test_an_undecodable_file_fails_generation(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    server.behaviour.corrupt = True
    provider = provider_for(server, tmp_path, clean_environ)
    with pytest.raises(GenerationError, match="undecodable"):
        await provider.generate(request_for(tmp_path))


async def test_silent_audio_is_a_valid_generation_and_phase_6_rejects_it(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """The boundary between "generation failed" and "the music is bad".

    A silent file is perfectly well-formed audio. The provider must *not* reject it — that
    judgement belongs to QC, and a provider that pre-empted it would make the pipeline's
    verdict depend on which provider produced the track, which §7.18 forbids.
    """
    from tradefix_radio.audio.analysis import extract_features, librosa_available
    from tradefix_radio.audio.io import read_audio
    from tradefix_radio.audio.qc import QcStage, QcStatus, run_audio_qc

    server.behaviour.silent_audio = True
    provider = provider_for(server, tmp_path, clean_environ)
    result = await provider.generate(request_for(tmp_path))
    assert result.audio_path.is_file()

    if not librosa_available():
        pytest.skip("librosa is required to run QC")
    settings = settings_for(server, tmp_path, clean_environ)
    qc = run_audio_qc(
        track_id="TF-001",
        features=extract_features(read_audio(result.audio_path)),
        settings=settings.qc.model_copy(update={"min_duration_seconds": 1.0}),
        stage=QcStage.RAW,
    )
    assert qc.status is QcStatus.FAIL


async def test_insufficient_vram_refuses_before_allocating(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.7's pre-flight. Cheaper than a real OOM and the same outcome for the manager."""

    class StarvedGpu(GpuProbe):
        def __init__(self) -> None:
            super().__init__(enabled=False)

        async def free_mb(self) -> float:
            return 128.0

    provider = provider_for(
        server, tmp_path, clean_environ, gpu=StarvedGpu(), min_free_vram_mb=5_000
    )
    with pytest.raises(GpuOutOfMemoryError, match="below the"):
        await provider.generate(request_for(tmp_path))
    # Nothing was submitted: the point is to refuse before touching the GPU.
    assert server.submissions == []


async def test_an_absent_gpu_does_not_block_generation(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """A probe returning ``None`` means "unknown", not "zero free"."""
    provider = provider_for(server, tmp_path, clean_environ, min_free_vram_mb=5_000)
    result = await provider.generate(request_for(tmp_path))
    assert result.audio_path.is_file()


# ------------------------------------------------------------- cancellation


async def test_cancel_reports_honestly_that_the_gpu_keeps_working(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """The documented API has no cancellation endpoint, so `cancel` cannot mean "stopped".

    Claiming otherwise would let the manager start a second generation into a GPU still busy
    with the first, turning a cancellation into an OOM. The provider stops waiting and says
    exactly that.
    """
    import asyncio

    server.behaviour.hang = True
    provider = provider_for(server, tmp_path, clean_environ, timeout_seconds=30.0)
    request = request_for(tmp_path)

    task = asyncio.create_task(provider.generate(request))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if provider.status.current_track_id == request.track_id:
            break
    assert await provider.cancel(request.track_id) is True

    with pytest.raises(GenerationCancelledError, match="continues"):
        await task
    assert provider.abandoned_tasks


async def test_cancelling_an_idle_track_reports_nothing_was_running(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    provider = provider_for(server, tmp_path, clean_environ)
    assert await provider.cancel("TF-NOT-RUNNING") is False


# ------------------------------------------------------------------ status


async def test_status_reports_real_numbers_or_none(
    server: FakeAceStep, tmp_path: Path, clean_environ: dict[str, str]
) -> None:
    """§7.24, and §86 underneath it: a p95 of zero with no samples reads as "instant"."""
    provider = provider_for(server, tmp_path, clean_environ)
    before = provider.status.as_payload()
    assert before["latency_p50_seconds"] is None
    assert before["loaded"] is False

    await provider.generate(request_for(tmp_path))

    after = provider.status.as_payload()
    assert after["loaded"] is True
    assert after["generations"] == 1
    assert isinstance(after["latency_p50_seconds"], float)
    assert after["last_success_at"]
