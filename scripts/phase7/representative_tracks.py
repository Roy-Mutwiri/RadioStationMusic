"""§7.14 — the seven representative generations, measured end to end.

Each track goes blueprint -> AceStepProvider -> Phase 6 pipeline, and everything §7.14 asks
to record is recorded: prompt, seed, generation time, duration, VRAM peak, QC, novelty,
master.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/.Music")

from tests.conftest import make_blueprint
from tradefix_radio.audio.analysis import extract_features, warm_up
from tradefix_radio.config.loader import load_settings
from tradefix_radio.core.clock import SystemClock
from tradefix_radio.generation.ace_step import AceStepClient, AceStepProvider
from tradefix_radio.generation.adherence import measure_adherence
from tradefix_radio.generation.factory import provider_profiles
from tradefix_radio.generation.gpu import GpuProbe
from tradefix_radio.generation.provider import GenerationRequest
from tradefix_radio.postprocess.pipeline import PostProductionPipeline

OUT = Path("D:/.Music/artifacts/phase7")
DURATION = 60

# §7.14's seven. Instrumental where the blueprint says so; the two vocal cases carry real
# composed lyrics so §7.10's pass-through is exercised against the real model.
CASES = [
    dict(name="1-quiet-lofi-instrumental", genre="lofi", bpm=72, key="C major",
         energy=0.12, instrumental=True),
    dict(name="2-deep-house-instrumental", genre="deep_house", bpm=122, key="A minor",
         energy=0.55, instrumental=True),
    dict(name="3-high-energy-trap-instrumental", genre="uk_trap", bpm=148, key="F# minor",
         energy=0.92, instrumental=True),
    dict(name="4-rap-tradefix-lyrics", genre="uk_drill", bpm=142, key="G minor",
         energy=0.85, instrumental=False, lyrics="brand"),
    dict(name="5-educational-trading-rap", genre="boom_bap", bpm=92, key="D minor",
         energy=0.6, instrumental=False, lyrics="educational"),
    dict(name="6-high-volatility-dnb", genre="dnb", bpm=174, key="E minor",
         energy=0.95, instrumental=True),
    dict(name="7-lower-energy-rnb", genre="rnb", bpm=88, key="B minor",
         energy=0.35, instrumental=True),
]

BRAND_LYRIC = """[Verse]
Charts don't promise, they only show the shape
Levels where the liquidity will break
I mark the zone and then I let it come
Patience is a discipline not everyone has done

[Chorus]
Trade Fix Radio through the London session light
Structure over feeling, hold the line tonight"""

EDUCATIONAL_LYRIC = """[Verse]
A higher high and then a higher low
That is the definition of a trend, you know
Risk defined before the entry's ever made
Position sized so one loss never breaks the trade

[Chorus]
Learn the structure, read it slow
Nothing here is certain, that's the only thing I know"""


def lyrics_for(track_id: str, kind: str):
    from tradefix_radio.contracts.lyrics import LyricLineV1, LyricsV1

    text = BRAND_LYRIC if kind == "brand" else EDUCATIONAL_LYRIC
    return LyricsV1(
        track_id=track_id,
        text=text,
        lines=tuple(
            LyricLineV1(section="verse", text=line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ),
        primary_topic="liquidity_breakout" if kind == "brand" else "market_structure",
        secondary_topic="risk_management",
        format="full rap",
        perspective="first_person",
        tradefix_mentions=1 if kind == "brand" else 0,
        educational_intensity=0.4 if kind == "brand" else 0.85,
        concepts_used=("liquidity",) if kind == "brand" else ("trend", "risk"),
    )


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings(
        env_file=None,
        environ={},
        overrides={
            "generation": {
                "provider": "ace_step",
                "ace_step": {
                    "base_url": "http://127.0.0.1:8001",
                    "timeout_seconds": 900.0,
                    # Measured: the model resident leaves ~2.8 GB free on this desktop.
                    # The pre-flight is calibrated after this run; disabled here so the
                    # measurement is not blocked by a guess.
                    "min_free_vram_mb": 0,
                },
            }
        },
    )
    ace = settings.generation.ace_step
    provider = AceStepProvider(
        ace,
        clock=SystemClock(),
        client=AceStepClient(ace.base_url),
        profiles=provider_profiles(settings),
        gpu=GpuProbe(),
    )
    warm_up()
    pipeline = PostProductionPipeline(
        settings, master_dir=OUT / "masters", warm_analysis=False
    )

    await provider.load()
    print(f"provider state: {provider.state.value}\n", flush=True)

    records = []
    library: list = []
    for index, case in enumerate(CASES, start=1):
        name = str(case["name"])
        track_id = f"TF-P7-{index:02d}"
        lyric_kind = case.get("lyrics")
        blueprint = make_blueprint(
            track_id,
            genre=str(case["genre"]),
            secondary_genre=None,
            bpm=int(case["bpm"]),
            key=str(case["key"]),
            composition_energy=float(case["energy"]),
            instrumental=bool(case["instrumental"]),
            duration_seconds=DURATION,
            seed=7000 + index * 13,
            title=name,
        )
        lyrics = lyrics_for(track_id, str(lyric_kind)) if lyric_kind else None

        request = GenerationRequest(
            blueprint=blueprint,
            output_path=OUT / "raw" / f"{track_id}.flac",
            lyrics=lyrics,
            timeout_seconds=900.0,
            attempt=1,
        )

        print(f"[{index}/7] {name} ...", flush=True)
        started = time.perf_counter()
        try:
            result = await provider.generate(request)
        except Exception as error:
            print(f"    FAILED: {type(error).__name__}: {error}", flush=True)
            records.append({"case": name, "track_id": track_id, "error": str(error)})
            continue
        wall = time.perf_counter() - started

        features = extract_features(__import__(
            "tradefix_radio.audio.io", fromlist=["read_audio"]
        ).read_audio(result.audio_path))
        adherence = measure_adherence(blueprint, features)

        outcome = await pipeline.process(
            track_id=track_id,
            source=result.audio_path,
            blueprint=blueprint,
            library=library,
            lyric_text=lyrics.text if lyrics else None,
        )
        if outcome.approved and outcome.features is not None:
            from tradefix_radio.originality.similarity import LibraryEntry

            library.append(
                LibraryEntry(
                    track_id=track_id,
                    canonical_hash=outcome.canonical_hash,
                    embedding=tuple(outcome.features.embedding()),
                    chroma_mean=outcome.features.chroma_mean,
                    mfcc_mean=outcome.features.mfcc_mean,
                    tempo=outcome.features.tempo,
                )
            )

        prompt = result.detail.get("prompt", {})
        record = {
            "case": name,
            "track_id": track_id,
            "genre": case["genre"],
            "bpm_requested": case["bpm"],
            "key_requested": case["key"],
            "energy_requested": case["energy"],
            "instrumental": case["instrumental"],
            "seed": prompt.get("seed"),
            "caption": prompt.get("caption"),
            "lyrics_modified": prompt.get("lyrics_modified"),
            "generation_seconds": round(wall, 2),
            "requested_duration": DURATION,
            "audio_duration": round(result.duration_seconds, 2),
            "realtime_factor": round(result.duration_seconds / wall, 2),
            "sample_rate": result.sample_rate,
            "channels": result.channels,
            "peak_vram_mb": (
                round(result.peak_vram_bytes / 1e6, 1) if result.peak_vram_bytes else None
            ),
            "gpu": result.detail.get("gpu"),
            "tempo_measured": round(features.tempo, 1) if features.tempo else None,
            "key_measured": features.musical_key,
            "lufs": round(features.integrated_lufs, 1) if features.integrated_lufs else None,
            "adherence": adherence.summary(),
            "adherence_detail": adherence.as_metadata(),
            "raw_qc": outcome.raw_qc.status.value if outcome.raw_qc else None,
            "raw_qc_failures": (
                [c.name for c in outcome.raw_qc.failures] if outcome.raw_qc else []
            ),
            "novelty": outcome.novelty_score,
            "mastering": (
                outcome.mastering.detail if outcome.mastering else None
            ),
            "master_lufs": (
                outcome.mastering.measured_lufs_after if outcome.mastering else None
            ),
            "peak_constrained": (
                outcome.mastering.peak_constrained if outcome.mastering else None
            ),
            "approved": outcome.approved,
            "stage": outcome.stage.value,
            "detail": outcome.detail,
            "pipeline_timings": {k: round(v, 2) for k, v in outcome.timings.items()},
        }
        records.append(record)
        print(
            f"    {wall:.1f}s gen, {result.duration_seconds:.1f}s audio "
            f"({record['realtime_factor']}x), vram peak {record['peak_vram_mb']} MB, "
            f"qc={record['raw_qc']}, approved={outcome.approved}",
            flush=True,
        )

    (OUT / "generation_report.json").write_text(
        json.dumps(
            {"cases": records, "provider_status": provider.status.as_payload()},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {OUT / 'generation_report.json'}")


asyncio.run(main())
