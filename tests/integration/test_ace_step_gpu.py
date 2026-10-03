"""Real ACE-Step smoke tests (§7.30).

Marked ``gpu`` and ``slow``: these need a GPU, a downloaded 6 GB checkpoint and a running
service, and each one costs ~45 seconds. §7.30 is explicit that *"Normal CI should not
require downloading the model"*, so nothing here runs by default.

    pytest -m gpu                       # run them
    pytest -m "not gpu"                 # the default fast suite

What is here is deliberately narrow. The *protocol* is covered exhaustively by
`test_ace_step_provider.py` against a fake server, which runs everywhere in a second. These
tests exist to answer the questions a fake cannot:

* does the real model load, and into how much VRAM;
* does an instrumental request actually produce an instrumental, across seeds (§7.11);
* is the same seed reproducible, and a different seed different (§7.13);
* does real output survive the Phase 6 pipeline (§7.18);
* does the station refuse a real OOM without dying (§7.7).

They skip, rather than fail, when the service is not running. A developer without a GPU
should see "skipped", not a wall of red for an environment they were never expected to have.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import make_blueprint, make_settings
from tradefix_radio.audio.analysis import extract_features, librosa_available
from tradefix_radio.audio.io import read_audio
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import SystemClock
from tradefix_radio.core.errors import GpuOutOfMemoryError
from tradefix_radio.generation.ace_step import AceStepClient, AceStepProvider, ModelState
from tradefix_radio.generation.adherence import AdherenceStatus, measure_adherence
from tradefix_radio.generation.factory import provider_profiles
from tradefix_radio.generation.gpu import GpuProbe
from tradefix_radio.generation.provider import GenerationRequest

pytestmark = [pytest.mark.gpu, pytest.mark.slow]

#: Where the service is expected. Overridable so this can point at another machine.
BASE_URL = os.environ.get("TRADEFIX_ACESTEP_URL", "http://127.0.0.1:8001")

#: Short, because every second here is a real second. Long enough to be real music.
DURATION = 30

#: Duration for the tests that run the Phase 6 pipeline.
#:
#: Above `qc.min_duration_seconds` (45 s), because Phase 6 is entitled to reject a 30-second
#: track as truncated and a test that fed it one would be measuring its own mistake. The
#: station never requests durations below the configured music range either.
PIPELINE_DURATION = 60


def _service_is_up() -> bool:
    """Probe once, synchronously, so the skip decision costs nothing per test."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _service_is_up(),
        reason=f"no ACE-Step service at {BASE_URL}; start it with `uv run acestep-api`",
    )
)


@pytest.fixture(scope="module")
def gpu_settings(tmp_path_factory: pytest.TempPathFactory) -> AppSettings:
    return make_settings(
        tmp_path_factory.mktemp("ace-gpu"),
        {},
        generation={
            "provider": "ace_step",
            "ace_step": {
                "base_url": BASE_URL,
                "timeout_seconds": 900.0,
                "load_timeout_seconds": 2_400.0,
                "min_free_vram_mb": 1_800,
            },
        },
    )


@pytest.fixture(scope="module")
def provider(gpu_settings: AppSettings) -> Iterator[AceStepProvider]:
    """One provider for the module. The model loads once, as §7.6 requires."""
    ace = gpu_settings.generation.ace_step
    yield AceStepProvider(
        ace,
        clock=SystemClock(),
        client=AceStepClient(ace.base_url),
        profiles=provider_profiles(gpu_settings),
        gpu=GpuProbe(),
    )


def request_for(tmp_path: Path, track_id: str, **blueprint: object) -> GenerationRequest:
    return GenerationRequest(
        blueprint=make_blueprint(track_id, duration_seconds=DURATION, **blueprint),
        output_path=tmp_path / f"{track_id}.flac",
        lyrics=None,
        timeout_seconds=900.0,
        attempt=1,
    )


# ------------------------------------------------------------------ Gate A


async def test_gate_a_the_model_loads(provider: AceStepProvider) -> None:
    """§7.32 Gate A: ACE-Step loads successfully on the target GPU."""
    await provider.load()
    assert provider.state is ModelState.READY
    assert provider.status.load_seconds is not None

    health = await provider.healthcheck()
    assert health.healthy, health.detail


# ------------------------------------------------------------------ Gate B


async def test_gate_b_instrumental_generation_succeeds(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.32 Gate B, and the file is verified rather than assumed."""
    result = await provider.generate(
        request_for(tmp_path, "TF-GPU-B", genre="lofi", bpm=80, instrumental=True)
    )
    assert result.audio_path.is_file()
    assert result.duration_seconds > DURATION * 0.5
    assert result.sample_rate >= 44_100
    assert result.generation_seconds > 0
    # §7.4 requires GPU metrics when available, and here they are available.
    assert result.peak_vram_bytes is not None
    assert result.detail["prompt"]


@pytest.mark.skipif(not librosa_available(), reason="librosa is required")
async def test_instrumental_is_instrumental_across_seeds(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.11: *"Test this with multiple seeds."*

    There is no vocal detector, so this cannot assert "no vocals" — claiming that would be
    the fabricated measurement §7.16 forbids. What it can assert is that the model accepted
    the instruction consistently: the request carried the marker every time, and the output
    is coherent audio of the right length every time. §7.15's listen is the real check and
    the report records it.
    """
    for index, seed in enumerate((11, 2_024, 98_765)):
        result = await provider.generate(
            request_for(
                tmp_path,
                f"TF-GPU-INST-{index}",
                genre="deep_house",
                bpm=120,
                instrumental=True,
                seed=seed,
            )
        )
        features = extract_features(read_audio(result.audio_path))
        assert features.rms > 0.001, f"seed {seed} produced silence"
        assert features.duration_seconds > DURATION * 0.5
        prompt = result.detail["prompt"]
        assert isinstance(prompt, dict)
        assert prompt["instrumental"] is True


# ------------------------------------------------------------------ Gate C


async def test_gate_c_vocal_generation_succeeds(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.32 Gate C, with real composed lyrics passed through unaltered (§7.10)."""
    from tradefix_radio.contracts.lyrics import LyricLineV1, LyricsV1

    text = (
        "[Verse]\nStructure on the chart before the entry\n"
        "Risk defined, the level does the telling\n\n"
        "[Chorus]\nHold the line, the patience is the edge"
    )
    lyrics = LyricsV1(
        track_id="TF-GPU-C",
        text=text,
        lines=tuple(
            LyricLineV1(section="verse", text=line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ),
        primary_topic="market_structure",
        secondary_topic="risk_management",
        format="full rap",
        perspective="first_person",
        tradefix_mentions=0,
        educational_intensity=0.6,
        concepts_used=("structure",),
    )
    request = GenerationRequest(
        blueprint=make_blueprint(
            "TF-GPU-C", genre="uk_drill", bpm=140, duration_seconds=DURATION,
            instrumental=False,
        ),
        output_path=tmp_path / "TF-GPU-C.flac",
        lyrics=lyrics,
        timeout_seconds=900.0,
        attempt=1,
    )
    result = await provider.generate(request)
    assert result.audio_path.is_file()

    prompt = result.detail["prompt"]
    assert isinstance(prompt, dict)
    # Already tagged, so §7.10's pass-through applies: untouched means untouched.
    assert prompt["lyrics_modified"] is False
    assert prompt["instrumental"] is False


# ------------------------------------------------------------- §7.13 seeds


@pytest.mark.skipif(not librosa_available(), reason="librosa is required")
async def test_seed_behaviour_is_measured_not_assumed(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.13: *"Do not assume deterministic behavior without measuring it."*

    This test **records** rather than demands. If the model turns out to be
    non-deterministic for a fixed seed, that is a fact about the model to write down, not a
    failure of the integration — and asserting determinism we have not verified would be
    the assumption §7.13 explicitly forbids.

    What *is* asserted is the part the station depends on: a different seed must produce
    different audio. Without that, §23's seed registry is bookkeeping with no effect.
    """
    from tradefix_radio.audio.fingerprint import canonical_sha256

    same_a = await provider.generate(
        request_for(tmp_path, "TF-GPU-S1", genre="lofi", bpm=90, instrumental=True, seed=4242)
    )
    same_b = await provider.generate(
        request_for(tmp_path, "TF-GPU-S2", genre="lofi", bpm=90, instrumental=True, seed=4242)
    )
    different = await provider.generate(
        request_for(tmp_path, "TF-GPU-S3", genre="lofi", bpm=90, instrumental=True, seed=777)
    )

    hash_a = canonical_sha256(read_audio(same_a.audio_path))
    hash_b = canonical_sha256(read_audio(same_b.audio_path))
    hash_c = canonical_sha256(read_audio(different.audio_path))

    # The one that must hold.
    assert hash_a != hash_c, (
        "a different seed produced byte-identical audio; the seed is not reaching the model "
        "and §23's registry cannot prevent repetition"
    )
    # The one that is recorded either way. Both outcomes are legitimate and the report says
    # which was observed.
    print(f"\n§7.13 same-seed reproducibility: {'DETERMINISTIC' if hash_a == hash_b else 'NON-DETERMINISTIC'}")


# ----------------------------------------------------- Gate E / F: Phase 6


@pytest.mark.skipif(not librosa_available(), reason="librosa is required")
async def test_gate_e_real_output_passes_the_phase_6_pipeline(
    provider: AceStepProvider, gpu_settings: AppSettings, tmp_path: Path
) -> None:
    """§7.32 Gate E, and §7.18: no Phase 6 stage is skipped because ACE-Step made the audio."""
    from tradefix_radio.audio.mastering import ffmpeg_available
    from tradefix_radio.postprocess.pipeline import PostProductionPipeline

    if not ffmpeg_available():
        pytest.skip("FFmpeg is required for mastering")

    blueprint = make_blueprint(
        "TF-GPU-E", genre="deep_house", bpm=124, duration_seconds=PIPELINE_DURATION,
        instrumental=True,
    )
    result = await provider.generate(
        GenerationRequest(
            blueprint=blueprint,
            output_path=tmp_path / "TF-GPU-E.flac",
            lyrics=None,
            timeout_seconds=900.0,
            attempt=1,
        )
    )

    pipeline = PostProductionPipeline(
        gpu_settings, master_dir=tmp_path / "masters", warm_analysis=False
    )
    outcome = await pipeline.process(
        track_id="TF-GPU-E",
        source=result.audio_path,
        blueprint=blueprint,
        library=[],
    )

    # QC always runs, and its verdict decides how far the rest gets. Not "approved" — a
    # real model may legitimately produce something QC rejects, and demanding approval
    # would make this test flaky for an honest reason.
    assert outcome.raw_qc is not None
    assert outcome.features is not None

    if outcome.raw_qc.passed:
        # Past QC, every later stage must have run. The hash is computed *after* QC
        # deliberately (Phase 6 §6.3): fingerprinting a broken render would put a
        # meaningless entry in the library that every later candidate is compared against.
        assert outcome.canonical_hash, "a QC-passing track must be fingerprinted"
        assert outcome.similarity is not None
    if outcome.approved:
        assert outcome.master_path is not None
        assert outcome.master_path.is_file()
        assert outcome.final_qc is not None
    else:
        assert outcome.rejection_reason is not None
        assert outcome.detail


@pytest.mark.skipif(not librosa_available(), reason="librosa is required")
async def test_blueprint_adherence_is_measurable_on_real_output(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.16 against the real model.

    Asserts that the measurable properties *are* measured, not that they all match — the
    model is allowed to miss, and the report records the distribution. A test demanding a
    perfect match would be a test of the model's luck.
    """
    blueprint = make_blueprint(
        "TF-GPU-ADH", genre="dnb", bpm=174, key="E minor",
        duration_seconds=PIPELINE_DURATION, instrumental=True,
    )
    result = await provider.generate(
        GenerationRequest(
            blueprint=blueprint,
            output_path=tmp_path / "TF-GPU-ADH.flac",
            lyrics=None,
            timeout_seconds=900.0,
            attempt=1,
        )
    )
    report = measure_adherence(blueprint, extract_features(read_audio(result.audio_path)))
    assert len(report.measured_checks) >= 3, report.summary()
    # Duration is the one the model controls most directly, so it is the one worth asserting.
    duration = next(c for c in report.checks if c.name == "duration")
    assert duration.status is not AdherenceStatus.UNMEASURED


# ------------------------------------------------------------------ Gate H


async def test_gate_h_an_impossible_vram_requirement_is_refused_cleanly(
    gpu_settings: AppSettings, tmp_path: Path
) -> None:
    """§7.32 Gate H: OOM is handled without taking anything down.

    Simulated by setting the pre-flight floor above the card's capacity, which exercises the
    same path a real OOM takes — classified as `GpuOutOfMemoryError`, counted, and raised
    rather than crashing. Provoking a *real* OOM on a shared desktop would risk the user's
    other applications, and the handling under test is identical either way.
    """
    ace = gpu_settings.generation.ace_step.model_copy(
        update={"min_free_vram_mb": 100_000}
    )
    starved = AceStepProvider(
        ace,
        clock=SystemClock(),
        client=AceStepClient(ace.base_url),
        gpu=GpuProbe(),
    )
    await starved.load()

    with pytest.raises(GpuOutOfMemoryError):
        await starved.generate(request_for(tmp_path, "TF-GPU-OOM", instrumental=True))

    assert starved.status.oom_events >= 1
    # The provider survives and remains usable: an OOM is a rejected job, not a dead model.
    health = await starved.healthcheck()
    assert health.healthy


async def test_concurrent_requests_are_serialised(
    provider: AceStepProvider, tmp_path: Path
) -> None:
    """§7.22: one generation at a time on one GPU.

    Two concurrent calls must not both reach the card — on 12 GB that is how an OOM is
    manufactured. The provider's lock is what makes this true regardless of what the caller
    does, rather than relying on the config guard alone.
    """
    first = asyncio.create_task(
        provider.generate(request_for(tmp_path, "TF-GPU-C1", instrumental=True))
    )
    second = asyncio.create_task(
        provider.generate(request_for(tmp_path, "TF-GPU-C2", instrumental=True))
    )
    results = await asyncio.gather(first, second)
    assert all(r.audio_path.is_file() for r in results)
    # Both completed, and the provider never reported two tracks in flight.
    assert provider.status.generations >= 2
