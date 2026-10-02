"""MockMusicProvider and the §18 interface (§18, §62, milestone 4.1).

§62 requires the mock to produce **real** audio, quickly, because the whole station is built
and proven against it before any model exists. The tests therefore check what a *provider*
must guarantee, not what a synthesiser must sound like — that is `test_synthesis.py`'s job.

The injectable failure and defect rates matter more than they look. §66's chaos tests and §24's
QC suite both need input that is genuinely broken rather than hand-built, and a defect rate that
silently did nothing would make both of them tests of nothing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from tradefix_radio.audio.io import read_audio, read_info
from tradefix_radio.config.schema import MockProviderSettings, QualityControlSettings
from tradefix_radio.contracts.music import MusicBlueprintV1
from tradefix_radio.core.clock import VirtualClock
from tradefix_radio.core.errors import (
    GenerationCancelledError,
    GenerationError,
    GenerationTimeoutError,
)
from tradefix_radio.generation import (
    GenerationRequest,
    MockMusicProvider,
    MusicGenerationProvider,
)
from tradefix_radio.generation.mock import DEFECT_KINDS, MODEL_IDENTIFIER, PROVIDER_NAME
from tests.conftest import make_blueprint as _make_blueprint

#: Shortest duration the §8 contract allows. Rendering is real work — ~13 ms of CPU per second
#: of audio — so the 215-second default fixture turned the bulk loops below into minutes. The
#: tests that care about duration say so explicitly.
SHORT_SECONDS = 30


def make_blueprint(track_id: str = "TF-00001", **overrides: object) -> MusicBlueprintV1:
    """``tests.conftest.make_blueprint`` with a short default duration."""
    overrides.setdefault("duration_seconds", SHORT_SECONDS)
    return _make_blueprint(track_id, **overrides)  # type: ignore[arg-type]


#: Sample rate for tests about provider *behaviour* rather than audio quality.
#:
#: At 44.1 kHz a 30-second stereo float file is 10.6 MB, and the bulk loops below write about
#: 170 of them — nearly two gigabytes of disk traffic, which dominated the run. 16 kHz keeps
#: every property these tests measure (duration, silence, clipping, DC offset, determinism)
#: while costing a third as much. Tests that measure the audio itself ask for 44.1 kHz.
BEHAVIOUR_RATE = 16_000


def provider(**overrides: object) -> MockMusicProvider:
    settings = MockProviderSettings(
        **{"latency_seconds": 0.0, "sample_rate": BEHAVIOUR_RATE, **overrides}  # type: ignore[arg-type]
    )
    return MockMusicProvider(settings)


def request_for(
    tmp_path: Path,
    blueprint: MusicBlueprintV1 | None = None,
    *,
    suffix: str = ".wav",
    **overrides: object,
) -> GenerationRequest:
    plan = blueprint or make_blueprint(track_id="TF-00001")
    return GenerationRequest(
        blueprint=plan,
        output_path=tmp_path / f"{plan.track_id}{suffix}",
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- the interface


def test_the_mock_satisfies_the_provider_protocol() -> None:
    """§18's seam. If this fails, Phase 7's ACE-Step adapter has nothing to conform to."""
    assert isinstance(provider(), MusicGenerationProvider)


def test_the_description_identifies_the_model() -> None:
    """Persisted per track (§37) and shown on the §46 detail page."""
    description = provider().describe()
    assert description.name == PROVIDER_NAME
    assert description.model_identifier == MODEL_IDENTIFIER
    assert not description.is_realtime_costly


def test_the_mock_does_not_claim_vocal_support_it_does_not_have() -> None:
    """The synthesiser has no voice, and saying otherwise would make the §19 contract a lie.

    Vocal blueprints still render — as the instrumental arrangement — so the queue keeps
    moving; what must not happen is the director being told vocals are available.
    """
    assert provider().describe().supports_vocals is False


async def test_the_mock_is_always_healthy_and_says_why() -> None:
    health = await provider().healthcheck()
    assert health.healthy
    assert health.detail


# ---------------------------------------------------------------- generation


async def test_generation_writes_real_audio_of_the_requested_duration(tmp_path: Path) -> None:
    blueprint = make_blueprint(track_id="TF-00001")
    result = await provider().generate(request_for(tmp_path, blueprint))

    assert result.audio_path.is_file()
    assert result.track_id == blueprint.track_id
    assert result.duration_seconds == pytest.approx(
        blueprint.composition.duration_seconds, abs=0.1
    )
    assert result.channels == 2
    assert result.generation_seconds > 0.0
    assert result.model_identifier == MODEL_IDENTIFIER

    audio = read_audio(result.audio_path)
    assert audio.peak() > 0.1, "the provider wrote silence"
    assert not np.isnan(audio.samples).any()


async def test_the_reported_duration_is_measured_not_echoed(tmp_path: Path) -> None:
    """§24's duration-deviation check is structurally incapable of failing otherwise.

    A provider that returned the requested duration would make the deviation always zero, so
    the check would pass for a truncated file. Here the measured duration must match the file
    on disk, which is what the defect tests below then exploit.
    """
    result = await provider().generate(request_for(tmp_path))
    assert result.duration_seconds == pytest.approx(
        read_info(result.audio_path).duration_seconds, abs=1e-3
    )


@pytest.mark.parametrize("suffix", [".wav", ".flac"])
async def test_both_output_containers_work(tmp_path: Path, suffix: str) -> None:
    result = await provider().generate(request_for(tmp_path, suffix=suffix))
    assert read_info(result.audio_path).duration_seconds > 0


async def test_generation_creates_missing_directories(tmp_path: Path) -> None:
    blueprint = make_blueprint(track_id="TF-00001")
    nested = tmp_path / "2026" / "10" / "02"
    result = await provider().generate(
        GenerationRequest(blueprint=blueprint, output_path=nested / "track.flac")
    )
    assert result.audio_path.is_file()


async def test_progress_is_reported_during_generation_not_only_at_the_end(
    tmp_path: Path,
) -> None:
    """§41's queue panel shows per-track progress, so it has to arrive while work happens."""
    seen: list[float] = []
    await provider().generate(request_for(tmp_path), on_progress=seen.append)
    assert seen[0] == 0.0
    assert seen[-1] == 1.0
    assert len(seen) >= 3, "progress jumped straight from nothing to done"
    assert seen == sorted(seen), "progress went backwards"


def with_composition(blueprint: MusicBlueprintV1, **fields: object) -> MusicBlueprintV1:
    """A blueprint with its composition spec adjusted.

    ``make_blueprint``'s ``energy`` is the *market* energy on a 0-100 scale, while the
    synthesiser reads ``composition.energy`` on a 0-1 scale. Setting the wrong one is an easy
    mistake that produces a test which passes while measuring nothing.
    """
    return blueprint.model_copy(
        update={"composition": blueprint.composition.model_copy(update=fields)}
    )


async def test_the_audio_reflects_the_blueprint(tmp_path: Path) -> None:
    """§1 end to end: a quiet blueprint and a violent one must not produce the same file."""
    base = make_blueprint(track_id="TF-00001")
    quiet = with_composition(
        base, bpm=72, energy=0.05, rhythm_density=0.15, drum_intensity=0.15
    )
    violent = with_composition(
        base.model_copy(update={"track_id": "TF-00002"}),
        bpm=174, energy=0.95, rhythm_density=0.95, drum_intensity=0.95,
    )
    engine = provider()
    first = read_audio((await engine.generate(request_for(tmp_path, quiet))).audio_path)
    second = read_audio((await engine.generate(request_for(tmp_path, violent))).audio_path)
    shared = min(first.frames, second.frames)
    assert not np.array_equal(first.samples[:shared], second.samples[:shared])
    # Deliberately *not* compared by level: the synthesiser normalises every render to a fixed
    # peak, so RMS lands within 0.01 dB either way and would make this test pass on any pair.
    # Whether the difference is the *musically right* one is measured spectrally in
    # test_synthesis.py, which is where that belongs.


async def test_the_same_blueprint_generates_identical_audio(tmp_path: Path) -> None:
    """Determinism from the blueprint's own seed — §64 replays depend on it."""
    blueprint = make_blueprint(track_id="TF-00001")
    first = await provider().generate(
        GenerationRequest(blueprint=blueprint, output_path=tmp_path / "a.wav")
    )
    second = await provider().generate(
        GenerationRequest(blueprint=blueprint, output_path=tmp_path / "b.wav")
    )
    assert np.array_equal(
        read_audio(first.audio_path).samples, read_audio(second.audio_path).samples
    )


async def test_output_passes_the_shipped_qc_thresholds(tmp_path: Path) -> None:
    """Milestone 4.1: "passes the Phase-6 QC rules"."""
    qc = QualityControlSettings()
    blueprint = make_blueprint(duration_seconds=int(qc.min_duration_seconds) + 15)
    audio = read_audio(
        (await provider().generate(request_for(tmp_path, blueprint))).audio_path
    )
    assert audio.duration_seconds >= qc.min_duration_seconds
    assert audio.silence_ratio() <= qc.max_silence_ratio
    assert audio.clipped_sample_ratio() <= qc.max_clipped_sample_ratio
    assert audio.dc_offset() <= qc.max_dc_offset


# ---------------------------------------------------------------- latency (§93)


async def test_latency_is_slept_on_the_injected_clock(tmp_path: Path) -> None:
    """§64 runs seven simulated days in minutes, which only works if nothing sleeps for real.

    The latency still has to *exist*, because §93's capacity ratio is measured from it — a
    provider with no latency at all would make the capacity predictor untestable.
    """
    clock = VirtualClock()
    engine = MockMusicProvider(
        MockProviderSettings(latency_seconds=30.0, sample_rate=BEHAVIOUR_RATE), clock=clock
    )
    task = asyncio.create_task(engine.generate(request_for(tmp_path)))
    await asyncio.sleep(0)
    assert not task.done()
    await clock.run_for(31.0)
    result = await task
    assert result.audio_path.is_file()
    assert clock.monotonic() >= 30.0


async def test_latency_beyond_the_deadline_is_a_timeout_not_late_work(
    tmp_path: Path,
) -> None:
    """Returning late audio would let the scheduler believe a deadline was met."""
    engine = MockMusicProvider(MockProviderSettings(latency_seconds=120.0, sample_rate=BEHAVIOUR_RATE))
    with pytest.raises(GenerationTimeoutError, match="exceeds the"):
        await engine.generate(request_for(tmp_path, timeout_seconds=10.0))


# ---------------------------------------------------------------- cancellation (§28)


async def test_cancellation_stops_an_in_flight_job(tmp_path: Path) -> None:
    """§28's replan and an operator skip both need this."""
    clock = VirtualClock()
    engine = MockMusicProvider(MockProviderSettings(latency_seconds=10.0, sample_rate=BEHAVIOUR_RATE), clock=clock)
    blueprint = make_blueprint(track_id="TF-00001")
    task = asyncio.create_task(
        engine.generate(
            GenerationRequest(blueprint=blueprint, output_path=tmp_path / "a.wav")
        )
    )
    await asyncio.sleep(0)
    assert await engine.cancel(blueprint.track_id) is True
    await clock.run_for(11.0)
    with pytest.raises(GenerationCancelledError):
        await task


async def test_cancelling_an_unknown_track_is_not_an_error() -> None:
    """The job may have finished between the decision to cancel and the call."""
    assert await provider().cancel("TF-99999") is False


async def test_a_cancelled_job_leaves_no_output_file(tmp_path: Path) -> None:
    """A partial file that exists would make the queue believe the track is ready."""
    clock = VirtualClock()
    engine = MockMusicProvider(MockProviderSettings(latency_seconds=5.0, sample_rate=BEHAVIOUR_RATE), clock=clock)
    blueprint = make_blueprint(track_id="TF-00001")
    path = tmp_path / "a.wav"
    task = asyncio.create_task(
        engine.generate(GenerationRequest(blueprint=blueprint, output_path=path))
    )
    await asyncio.sleep(0)
    await engine.cancel(blueprint.track_id)
    await clock.run_for(6.0)
    with pytest.raises(GenerationCancelledError):
        await task
    assert not path.exists()


async def test_cancellation_state_does_not_leak_to_the_next_job(tmp_path: Path) -> None:
    """A cancelled track id must not poison a retry of the same track."""
    clock = VirtualClock()
    engine = MockMusicProvider(MockProviderSettings(latency_seconds=1.0, sample_rate=BEHAVIOUR_RATE), clock=clock)
    blueprint = make_blueprint(track_id="TF-00001")

    task = asyncio.create_task(
        engine.generate(
            GenerationRequest(blueprint=blueprint, output_path=tmp_path / "a.wav")
        )
    )
    await asyncio.sleep(0)
    await engine.cancel(blueprint.track_id)
    await clock.run_for(2.0)
    with pytest.raises(GenerationCancelledError):
        await task

    # The retry has to be driven too. Awaiting it directly would wait forever: the provider
    # sleeps its latency on the virtual clock, and nothing else is advancing it.
    retry_task = asyncio.create_task(
        engine.generate(
            GenerationRequest(
                blueprint=blueprint, output_path=tmp_path / "b.wav", attempt=2
            )
        )
    )
    await asyncio.sleep(0)
    await clock.run_for(2.0)
    retry = await retry_task
    assert retry.audio_path.is_file()


# ---------------------------------------------------------------- chaos (§66)


async def test_a_failure_rate_of_one_always_fails(tmp_path: Path) -> None:
    engine = MockMusicProvider(
        MockProviderSettings(
            latency_seconds=0.0, failure_rate=1.0, sample_rate=BEHAVIOUR_RATE
        )
    )
    with pytest.raises(GenerationError, match="injected failure"):
        await engine.generate(request_for(tmp_path))


async def test_a_failure_rate_of_zero_never_fails(tmp_path: Path) -> None:
    """The default must be quiet, or every other test becomes flaky."""
    engine = provider()
    for index in range(20):
        blueprint = make_blueprint(track_id=f"TF-{index:05d}")
        await engine.generate(request_for(tmp_path, blueprint))


async def test_a_partial_failure_rate_produces_both_outcomes(tmp_path: Path) -> None:
    """§66 needs *intermittent* failure; one that always or never fires tests nothing."""
    engine = MockMusicProvider(
        MockProviderSettings(
            latency_seconds=0.0, failure_rate=0.5, sample_rate=BEHAVIOUR_RATE
        ),
        seed=3,
    )
    failures = 0
    for index in range(24):
        blueprint = make_blueprint(track_id=f"TF-{index:05d}")
        try:
            await engine.generate(request_for(tmp_path, blueprint))
        except GenerationError:
            failures += 1
    assert 3 < failures < 21, f"{failures}/24 failed"


async def test_failure_injection_does_not_depend_on_the_blueprint(tmp_path: Path) -> None:
    """Chaos must be independent of content, or an injected defect looks like a bad genre.

    The provider keeps a chaos RNG separate from the per-track render seed precisely so that
    *whether* a job fails cannot change when a blueprint does. Two runs at the same provider
    seed over different blueprints must fail on the same job numbers.
    """

    async def outcomes(seed: int, *, vary: bool) -> list[bool]:
        engine = MockMusicProvider(
            MockProviderSettings(
                latency_seconds=0.0, failure_rate=0.4, sample_rate=BEHAVIOUR_RATE
            ),
            seed=seed,
        )
        failed: list[bool] = []
        for index in range(12):
            blueprint = make_blueprint(
                track_id=f"TF-{index:05d}",
                bpm=90 + (index * 7 if vary else 0),
                key="C major" if vary else "A minor",
                duration_seconds=45 + (index if vary else 0),
            )
            try:
                await engine.generate(
                    request_for(tmp_path, blueprint, suffix=f"-{vary}.wav")
                )
                failed.append(False)
            except GenerationError:
                failed.append(True)
        return failed

    plain = await outcomes(5, vary=False)
    varied = await outcomes(5, vary=True)
    assert any(plain), "the failure rate never fired, so the comparison proves nothing"
    assert plain == varied


# ---------------------------------------------------------------- defects (§24)


async def test_a_defect_rate_of_one_always_produces_broken_audio(tmp_path: Path) -> None:
    engine = MockMusicProvider(
        MockProviderSettings(
            latency_seconds=0.0, defect_rate=1.0, sample_rate=BEHAVIOUR_RATE
        ),
        seed=1,
    )
    result = await engine.generate(request_for(tmp_path))
    assert result.detail.get("defect") in DEFECT_KINDS


async def test_every_defect_kind_is_detectable_by_the_qc_rules(tmp_path: Path) -> None:
    """Each injected defect must break exactly one §24 rule, or the QC suite proves nothing.

    Run until all four kinds have appeared, then assert each one is caught. Without this, a
    "defect" that still passed QC would make the Phase 6 suite a test of hand-built fixtures.
    """
    qc = QualityControlSettings()
    engine = MockMusicProvider(
        MockProviderSettings(
            latency_seconds=0.0, defect_rate=1.0, sample_rate=BEHAVIOUR_RATE
        ),
        seed=2,
    )
    requested = float(make_blueprint(track_id="TF-00000").composition.duration_seconds)
    seen: dict[str, bool] = {}

    for index in range(30):
        blueprint = make_blueprint(track_id=f"TF-{index:05d}")
        result = await engine.generate(request_for(tmp_path, blueprint))
        kind = str(result.detail["defect"])
        if kind in seen:
            continue
        audio = read_audio(result.audio_path)
        if kind == "truncated":
            deviation = abs(audio.duration_seconds - requested) / requested
            seen[kind] = deviation > qc.max_duration_deviation
        elif kind == "silent":
            seen[kind] = audio.silence_ratio() > qc.max_silence_ratio
        elif kind == "clipped":
            seen[kind] = audio.clipped_sample_ratio() > qc.max_clipped_sample_ratio
        elif kind == "dc_offset":
            seen[kind] = audio.dc_offset() > qc.max_dc_offset
        if len(seen) == len(DEFECT_KINDS):
            break

    assert set(seen) == set(DEFECT_KINDS), f"only saw {sorted(seen)} in 30 jobs"
    undetected = [kind for kind, detected in seen.items() if not detected]
    assert not undetected, f"defects QC would not catch: {undetected}"


async def test_a_defect_rate_of_zero_produces_clean_audio(tmp_path: Path) -> None:
    engine = provider()
    for index in range(10):
        blueprint = make_blueprint(track_id=f"TF-{index:05d}")
        result = await engine.generate(request_for(tmp_path, blueprint))
        assert "defect" not in result.detail


async def test_defect_kinds_vary(tmp_path: Path) -> None:
    """All four kinds should appear, not just the first in the tuple."""
    engine = MockMusicProvider(
        MockProviderSettings(
            latency_seconds=0.0, defect_rate=1.0, sample_rate=BEHAVIOUR_RATE
        ),
        seed=4,
    )
    kinds: Counter[str] = Counter()
    for index in range(24):
        blueprint = make_blueprint(track_id=f"TF-{index:05d}")
        result = await engine.generate(request_for(tmp_path, blueprint))
        kinds[str(result.detail["defect"])] += 1
    assert len(kinds) >= 3, f"defect kinds seen: {dict(kinds)}"
