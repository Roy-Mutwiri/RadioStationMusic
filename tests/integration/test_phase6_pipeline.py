"""End-to-end post-production (§6.21).

The claim under test is the one §6 opens with: **a track that fails this pipeline must NEVER
become READY for live radio.** That is a statement about the whole chain, not about any
component, so it has to be tested through the real station with the real pipeline attached —
every component passing individually would still permit a station that ignores their verdict.

The second claim, §6.14's, is just as important and easier to lose: *generator success is not
a playable track*. A rejected track must leave the queue so the scheduler can replan, must
reach a terminal state in the database, and must leave behind the evidence that explains it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tests.audio_fixtures import musical, silence
from tests.conftest import make_blueprint
from tradefix_radio.audio.analysis import librosa_available
from tradefix_radio.audio.io import write_audio
from tradefix_radio.audio.mastering import ffmpeg_available
from tradefix_radio.config.schema import AppSettings
from tradefix_radio.core.clock import UTC, SystemClock
from tradefix_radio.core.state_machine import TrackState
from tradefix_radio.persistence.database import Database
from tradefix_radio.persistence.models import Track
from tradefix_radio.persistence.repositories import (
    OriginalityRepository,
    TrackRepository,
)
from tradefix_radio.postprocess.pipeline import (
    IllegalPipelineTransition,
    PipelineStage,
    PostProductionPipeline,
    RejectionReason,
)

pytestmark = [
    pytest.mark.skipif(not librosa_available(), reason="librosa is required"),
    pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is required"),
]

FIXED_NOW = __import__("datetime").datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


@pytest.fixture
def pipeline(settings: AppSettings, tmp_path: Path) -> PostProductionPipeline:
    return PostProductionPipeline(
        settings, clock=SystemClock(), master_dir=tmp_path / "mastered"
    )


def _source(tmp_path: Path, buffer, name: str) -> Path:
    path = tmp_path / name
    write_audio(path, buffer)
    return path


# ------------------------------------------------------------- the happy path


async def test_a_clean_track_reaches_approved_with_a_master(
    pipeline: PostProductionPipeline, tmp_path: Path
) -> None:
    source = _source(tmp_path, musical(seconds=50.0, seed=1), "clean.wav")
    outcome = await pipeline.process(
        track_id="TF-CLEAN",
        source=source,
        blueprint=make_blueprint("TF-CLEAN", duration_seconds=50),
        library=[],
        now=FIXED_NOW,
    )
    assert outcome.approved, outcome.detail
    assert outcome.stage is PipelineStage.APPROVED
    assert outcome.master_path is not None
    assert outcome.master_path.is_file()
    # Every stage left evidence behind, which is what the Originality page reads.
    assert outcome.raw_qc is not None
    assert outcome.final_qc is not None
    assert outcome.features is not None
    assert outcome.canonical_hash
    assert outcome.fingerprint is not None
    assert outcome.mastering is not None
    assert outcome.similarity is not None
    assert set(outcome.timings) >= {"decode", "features", "qc", "mastering", "final_qc"}


async def test_the_master_is_a_separate_file_from_the_source(
    pipeline: PostProductionPipeline, tmp_path: Path
) -> None:
    """The raw render survives, so a rejection can be inspected and a master re-made."""
    source = _source(tmp_path, musical(seconds=50.0, seed=2), "raw.wav")
    outcome = await pipeline.process(
        track_id="TF-M", source=source, blueprint=make_blueprint("TF-M", duration_seconds=50),
        library=[], now=FIXED_NOW,
    )
    assert outcome.master_path != source
    assert source.is_file()


# ---------------------------------------------------------------- rejections


async def test_broken_audio_is_rejected_before_anything_expensive_runs(
    pipeline: PostProductionPipeline, tmp_path: Path
) -> None:
    """QC first, and a QC failure stops the chain.

    Ordering is a correctness property, not an optimisation: mastering a silent file would
    produce a failure with a confusing reason, and fingerprinting it would put a meaningless
    entry in the library that every later candidate is compared against.
    """
    source = _source(tmp_path, silence(seconds=50.0), "silent.wav")
    outcome = await pipeline.process(
        track_id="TF-DEAD", source=source, blueprint=make_blueprint("TF-DEAD", duration_seconds=50),
        library=[], now=FIXED_NOW,
    )
    assert not outcome.approved
    assert outcome.stage is PipelineStage.QC_REJECTED
    assert outcome.rejection_reason is RejectionReason.QC_DEFECT
    assert outcome.mastering is None
    assert outcome.master_path is None
    assert "mastering" not in outcome.timings


async def test_an_exact_duplicate_is_rejected_and_names_the_original(
    pipeline: PostProductionPipeline, tmp_path: Path
) -> None:
    source = _source(tmp_path, musical(seconds=50.0, seed=3), "dupe.wav")

    async def owner(_hash: str) -> str:
        return "TF-ORIGINAL"

    outcome = await pipeline.process(
        track_id="TF-COPY", source=source,
        blueprint=make_blueprint("TF-COPY", duration_seconds=50),
        library=[], duplicate_hash_owner_lookup=owner, now=FIXED_NOW,
    )
    assert not outcome.approved
    assert outcome.stage is PipelineStage.ORIGINALITY_REJECTED
    assert outcome.rejection_reason is RejectionReason.EXACT_DUPLICATE
    assert outcome.novelty_score == 0.0
    assert "TF-ORIGINAL" in outcome.detail


async def test_an_unreadable_file_is_quarantined_not_crashed(
    pipeline: PostProductionPipeline, tmp_path: Path
) -> None:
    """A corrupt render is a track problem, not a station problem."""
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"this is not audio")
    outcome = await pipeline.process(
        track_id="TF-BROKEN", source=broken, blueprint=None, library=[], now=FIXED_NOW
    )
    assert not outcome.approved
    assert outcome.stage is PipelineStage.QUARANTINED
    assert outcome.detail


# ------------------------------------------------------- the state machine


def test_illegal_stage_transitions_fail_explicitly(
    pipeline: PostProductionPipeline,
) -> None:
    """§6.13: illegal transitions must fail explicitly rather than being tolerated.

    Tolerating one would mean a track could appear APPROVED without the stages in between
    having run, which is precisely the condition the whole phase exists to prevent — and it
    would be invisible, because the end state would look correct.
    """
    with pytest.raises(IllegalPipelineTransition):
        pipeline._advance(PipelineStage.QC_PENDING, PipelineStage.APPROVED)
    with pytest.raises(IllegalPipelineTransition):
        pipeline._advance(PipelineStage.QC_REJECTED, PipelineStage.MASTERING)
    # The legal step still works, so the test is about the rule and not about raising always.
    assert pipeline._advance(PipelineStage.QC_PENDING, PipelineStage.QC_PASSED) is (
        PipelineStage.QC_PASSED
    )


# --------------------------------------------- the station boundary (§6.14)


async def test_a_rejected_track_never_becomes_ready(
    settings: AppSettings, tmp_path: Path, database: Database
) -> None:
    """§6's central claim, asserted through the real station.

    The station is driven directly rather than through a full run: what is being tested is
    the *decision*, and a full run would make the test a soak. The soak that proves the
    station keeps broadcasting through rejections is separate, and runs in `tradefix soak`.
    """
    from tradefix_radio.generation.manager import GenerationOutcome
    from tradefix_radio.generation.provider import GenerationResult

    station = _station_with_pipeline(settings, tmp_path, database)
    pipeline = PostProductionPipeline(
        settings, clock=SystemClock(), master_dir=tmp_path / "mastered"
    )
    station._post_production = pipeline

    blueprint = make_blueprint("TF-BAD", duration_seconds=50)
    await _plan(station, database, blueprint)

    source = _source(tmp_path, silence(seconds=50.0), "TF-BAD.wav")
    outcome = GenerationOutcome(
        job_id="job-1",
        track_id="TF-BAD",
        state="completed",
        result=GenerationResult(
            track_id="TF-BAD",
            audio_path=source,
            duration_seconds=50.0,
            sample_rate=44_100,
            channels=2,
            generation_seconds=1.0,
            model_identifier="mock",
            provider_name="mock",
        ),
        attempt=1,
        wall_seconds=1.0,
    )

    await station._accept_generated(outcome)

    # Never READY, in the queue or in the database.
    assert station._queue.get("TF-BAD") is None, "a rejected track kept its queue slot"
    async with database.session() as session:
        track = await session.get(Track, "TF-BAD")
        assert track is not None
        assert track.state in {TrackState.REJECTED.value, TrackState.QUARANTINED.value}
    assert station.stats.post_production_rejected == 1
    assert station.stats.tracks_ready == 0

    # And the evidence survives, which is what makes the rejection reviewable.
    async with database.session() as session:
        evidence = await OriginalityRepository(session).evidence_for("TF-BAD")
    assert evidence.qc_results, "a rejection recorded no QC evidence"
    assert any(result.failed_count > 0 for result in evidence.qc_results)


async def test_a_clean_track_does_reach_ready_through_the_station(
    settings: AppSettings, tmp_path: Path, database: Database
) -> None:
    """The paired positive. Without it, rejecting everything would pass the test above."""
    from tradefix_radio.generation.manager import GenerationOutcome
    from tradefix_radio.generation.provider import GenerationResult

    station = _station_with_pipeline(settings, tmp_path, database)
    station._post_production = PostProductionPipeline(
        settings, clock=SystemClock(), master_dir=tmp_path / "mastered"
    )

    blueprint = make_blueprint("TF-GOOD", duration_seconds=50)
    await _plan(station, database, blueprint)

    source = _source(tmp_path, musical(seconds=50.0, seed=9), "TF-GOOD.wav")
    outcome = GenerationOutcome(
        job_id="job-2",
        track_id="TF-GOOD",
        state="completed",
        result=GenerationResult(
            track_id="TF-GOOD",
            audio_path=source,
            duration_seconds=50.0,
            sample_rate=44_100,
            channels=2,
            generation_seconds=1.0,
            model_identifier="mock",
            provider_name="mock",
        ),
        attempt=1,
        wall_seconds=1.0,
    )

    await station._accept_generated(outcome)

    entry = station._queue.get("TF-GOOD")
    assert entry is not None
    assert entry.audio_path is not None
    # The queue points at the *master*, not the raw render: playing the unmastered file would
    # make every loudness guarantee in §6.9 cosmetic.
    assert "mastered" in entry.audio_path
    async with database.session() as session:
        track = await session.get(Track, "TF-GOOD")
        assert track is not None
        assert track.state == TrackState.READY.value
    assert station.stats.tracks_ready == 1
    assert station.stats.post_production_rejected == 0


async def _plan(station, database: Database, blueprint) -> None:
    """Put a planned track into the queue and the database, as the scheduler would.

    Both sides, because the station's acceptance path touches both and a test that set up
    only one would pass for the wrong reason.
    """
    from tradefix_radio.radio.queue import QueueEntry

    station._queue.append(
        QueueEntry(
            track_id=blueprint.track_id,
            blueprint=blueprint,
            duration_seconds=float(blueprint.composition.duration_seconds),
            inserted_at=FIXED_NOW,
        )
    )
    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint,
            now=FIXED_NOW,
            provider="mock",
            model_identifier="mock-1",
            state=TrackState.PLANNED,
        )


def _station_with_pipeline(
    settings: AppSettings, tmp_path: Path, database: Database
):
    """A station with every subsystem real except the parts these tests do not touch."""
    from tradefix_radio.audio.sinks import NullSink
    from tradefix_radio.config.loader import CONFIG_DIR
    from tradefix_radio.director.library import load_content_library
    from tradefix_radio.director.music_director import MusicDirector
    from tradefix_radio.director.selection import WeightedSelector
    from tradefix_radio.generation.manager import (
        DatabaseJobUnitOfWork,
        GenerationManager,
    )
    from tradefix_radio.generation.mock import MockMusicProvider
    from tradefix_radio.radio.station import RadioStation
    from tradefix_radio.radio.station_ids import StationIdLibrary, default_library
    from tradefix_radio.runtime.coordinator import RuntimeCoordinator

    clock = SystemClock()
    provider = MockMusicProvider(settings.generation.mock, clock=clock, seed=3)
    return RadioStation(
        settings,
        database=database,
        coordinator=RuntimeCoordinator(clock=clock),
        director=MusicDirector(
            settings,
            load_content_library(config_dir=CONFIG_DIR),
            selector=WeightedSelector(random.Random(3)),
        ),
        generation=GenerationManager(
            provider=provider,
            settings=settings.generation,
            unit_of_work=DatabaseJobUnitOfWork(database.session),
            clock=clock,
        ),
        sink=NullSink(sample_rate=16_000, channels=1, clock=clock, realtime=False),
        station_ids=StationIdLibrary(
            default_library(tmp_path / "station_ids"), rng=random.Random(3)
        ),
        clock=clock,
        audio_dir=tmp_path / "audio",
    )


# ------------------------------------------------- defect injection (§6.23)


async def test_the_injector_produces_exactly_what_it_claims(tmp_path: Path) -> None:
    """The soak's evidence rests on this, so it is checked directly.

    If the injector silently produced fewer defects than its rate implies, the soak would
    report a high rejection rate against a low injection count and look *better* than the
    truth. The arithmetic has to be verifiable.
    """
    from tradefix_radio.generation.defects import DefectInjectingProvider, DefectKind

    class _Stub:
        """Writes a distinct clean file per call and nothing else."""

        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, request, *, on_progress=None):
            from tradefix_radio.generation.provider import GenerationResult

            self.calls += 1
            write_audio(request.output_path, musical(seconds=3.0, seed=self.calls))
            return GenerationResult(
                track_id=request.blueprint.track_id,
                audio_path=request.output_path,
                duration_seconds=3.0,
                sample_rate=44_100,
                channels=2,
                generation_seconds=0.0,
                model_identifier="stub",
                provider_name="stub",
            )

        async def healthcheck(self):
            from tradefix_radio.generation.provider import ProviderHealth

            return ProviderHealth.ok()

        async def cancel(self, track_id: str) -> bool:
            return False

        def describe(self):
            from tradefix_radio.generation.provider import ProviderDescription

            return ProviderDescription(
                name="stub", model_identifier="stub", version="1", supports_lyrics=False
            )

    from tradefix_radio.generation.provider import GenerationRequest

    injector = DefectInjectingProvider(
        _Stub(), invalid_rate=0.2, duplicate_rate=0.1, seed=4242
    )
    total = 200
    for index in range(total):
        blueprint = make_blueprint(f"TF-{index:04d}", duration_seconds=30)
        await injector.generate(
            GenerationRequest(
                blueprint=blueprint,
                output_path=tmp_path / f"{index:04d}.wav",
                lyrics=None,
                timeout_seconds=30,
                attempt=1,
            )
        )

    stats = injector.stats
    assert stats.total == total
    corrupted = len(stats.corrupted_track_ids)
    duplicated = len(stats.duplicate_track_ids)
    # Binomial sampling, so the rates are approached rather than hit exactly. The tolerance
    # is wide enough not to flake at n=200 and narrow enough that a broken injector — one
    # that injected nothing, or everything — fails.
    assert 0.13 <= corrupted / total <= 0.27, f"{corrupted}/{total} corrupted"
    assert 0.05 <= duplicated / total <= 0.16, f"{duplicated}/{total} duplicated"
    # Every kind actually occurs, or a defect class is going untested in the soak.
    assert {DefectKind.SILENCE.value, DefectKind.DROPOUT.value} <= set(stats.by_kind)


async def test_injected_defects_are_the_ones_qc_rejects(
    pipeline: PostProductionPipeline, tmp_path: Path, settings: AppSettings
) -> None:
    """Each injected defect maps to a failing check, named.

    The soak proves nothing broken aired; this proves the rejections happened for the right
    reason rather than because something incidental tripped first.
    """
    from tradefix_radio.audio.analysis import extract_features
    from tradefix_radio.audio.io import read_audio
    from tradefix_radio.audio.qc import QcStage, run_audio_qc
    from tradefix_radio.generation.defects import DefectInjectingProvider, DefectKind

    expected = {
        DefectKind.SILENCE: "non_empty",
        DefectKind.CLIPPING: "clipping",
        DefectKind.TRUNCATION: "duration",
        DefectKind.DROPOUT: "internal_silence",
    }
    injector = DefectInjectingProvider(object(), seed=1)  # type: ignore[arg-type]

    for kind, check_name in expected.items():
        path = tmp_path / f"{kind.value}.wav"
        write_audio(path, musical(seconds=50.0, seed=5))
        injector._corrupt(path, kind)
        features = extract_features(read_audio(path))
        result = run_audio_qc(
            track_id=kind.value,
            features=features,
            settings=settings.qc,
            stage=QcStage.RAW,
            blueprint=make_blueprint("TF-X", duration_seconds=50),
        )
        failed = {check.name for check in result.failures}
        assert check_name in failed, f"{kind.value} did not fail {check_name}; failed {failed}"


# ----------------------------------------------- the evidence round trip (§6.12)


async def test_the_library_round_trips_everything_comparison_needs(
    settings: AppSettings, tmp_path: Path, database: Database
) -> None:
    """What is written must come back in the form the engine compares.

    The regression this guards is subtle and was real: `load_library` stored the perceptual
    fingerprint and the blueprint fields, then returned ``None`` for both. The engine's
    weight renormalisation then redistributed their share across the components that *were*
    present — including the timbre comparison, which saturates on material from a single
    generator — and clean tracks began failing as near-duplicates of each other. Nothing
    errored; the library was simply quieter than it looked.
    """
    from tradefix_radio.audio.analysis import extract_features
    from tradefix_radio.audio.fingerprint import canonical_sha256, default_provider
    from tradefix_radio.audio.io import read_audio
    from tradefix_radio.core.clock import UTC

    blueprint = make_blueprint("TF-LIB", duration_seconds=50, genre="deep_house", bpm=124)
    source = _source(tmp_path, musical(seconds=50.0, seed=21), "lib.wav")
    buffer = read_audio(source)
    features = extract_features(buffer)
    provider = default_provider()
    fingerprint = provider.compute(source, buffer, features)
    now = __import__("datetime").datetime(2026, 10, 3, 10, 0, tzinfo=UTC)

    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=now, provider="mock", model_identifier="mock-1"
        )
        await OriginalityRepository(session).record_fingerprint(
            "TF-LIB",
            features=features,
            canonical_hash=canonical_sha256(buffer),
            file_sha256="deadbeef",
            fingerprint=fingerprint,
            blueprint_signature=blueprint.signature(),
            lyric_hash=None,
            embedding_version=2,
            computed_at=now,
        )

    async with database.session() as session:
        library = await OriginalityRepository(session).load_library()

    assert len(library) == 1
    entry = library[0]
    assert entry.track_id == "TF-LIB"
    assert entry.canonical_hash == canonical_sha256(buffer)
    assert len(entry.embedding) == len(features.embedding())
    assert entry.chroma_mean == features.chroma_mean
    assert entry.tempo == features.tempo

    # The two that were silently dropped.
    assert entry.fingerprint is not None, "the stored fingerprint was not rehydrated"
    assert entry.fingerprint.provider == provider.name
    assert entry.blueprint is not None, "the blueprint summary was not rehydrated"
    assert entry.blueprint.genre == "deep_house"
    assert entry.blueprint.bpm == 124
    # Composition energy, not market energy — different scales, different meanings.
    assert entry.blueprint.energy == pytest.approx(blueprint.composition.energy)
    assert entry.created_at is not None


async def test_an_exact_hash_lookup_spans_the_whole_table(
    settings: AppSettings, tmp_path: Path, database: Database
) -> None:
    """§6.3 stays exhaustive even though the similarity window is bounded.

    `load_library` is deliberately limited — an unbounded read would stall inside the
    generation deadline at scale. The hash lookup is a separate indexed point query with no
    such bound, so a byte-identical copy of an ancient track is still caught.
    """
    from tradefix_radio.audio.analysis import extract_features
    from tradefix_radio.audio.fingerprint import canonical_sha256
    from tradefix_radio.audio.io import read_audio
    from tradefix_radio.core.clock import UTC

    now = __import__("datetime").datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    blueprint = make_blueprint("TF-OLD", duration_seconds=50)
    buffer = read_audio(_source(tmp_path, musical(seconds=50.0, seed=22), "old.wav"))
    digest = canonical_sha256(buffer)

    async with database.session() as session:
        await TrackRepository(session).create(
            blueprint, now=now, provider="mock", model_identifier="mock-1"
        )
        await OriginalityRepository(session).record_fingerprint(
            "TF-OLD",
            features=extract_features(buffer),
            canonical_hash=digest,
            file_sha256="x",
            fingerprint=None,
            blueprint_signature=blueprint.signature(),
            lyric_hash=None,
            embedding_version=2,
            computed_at=now,
        )

    async with database.session() as session:
        repository = OriginalityRepository(session)
        assert await repository.canonical_hash_owner(digest) == "TF-OLD"
        assert await repository.canonical_hash_owner("not-a-real-hash") is None
        # Even with the comparison window closed to nothing.
        assert await repository.load_library(limit=0) == []
        assert await repository.canonical_hash_owner(digest) == "TF-OLD"
