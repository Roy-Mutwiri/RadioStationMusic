"""§7.17 — does the market actually change the music?

This is the phase's critical test and the project's founding claim (§1: "THE MARKET COMPOSES
THE RADIO"). Everything else in Phase 7 is plumbing; this is the one that asks whether the
plumbing carries anything.

Method: drive the **real** MusicDirector with four real market states, let it produce real
blueprints, generate each with the real model, and measure the audio. No hand-written
blueprints — the point is to test the whole chain, and a hand-written blueprint would be me
proving that different inputs give different outputs, which was never in doubt.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, "D:/.Music")

import numpy as np

from tradefix_radio.audio.analysis import extract_features, warm_up
from tradefix_radio.audio.io import read_audio
from tradefix_radio.config.loader import CONFIG_DIR, load_settings
from tradefix_radio.contracts.enums import (
    FeedStatus,
    MarketDirection,
    MarketRegime,
    TradingSession,
)
from tradefix_radio.contracts.market import MarketStateV1
from tradefix_radio.contracts.queue import BufferHealthV1
from tradefix_radio.core.clock import UTC, SystemClock
from tradefix_radio.director.history import ProgrammingHistory
from tradefix_radio.director.library import load_content_library
from tradefix_radio.director.music_director import MusicDirector
from tradefix_radio.director.selection import WeightedSelector
from tradefix_radio.generation.ace_step import AceStepClient, AceStepProvider
from tradefix_radio.generation.factory import provider_profiles
from tradefix_radio.generation.gpu import GpuProbe
from tradefix_radio.generation.provider import GenerationRequest

OUT = Path("D:/.Music/artifacts/phase7/market")
DURATION = 45
PER_REGIME = 2

NOW = __import__("datetime").datetime(2026, 10, 3, 14, 0, tzinfo=UTC)

# Four market conditions, as MarketStateV1 the director actually consumes.
CONDITIONS = [
    ("QUIET", MarketRegime.QUIET, MarketDirection.NEUTRAL, 8.0, 6.0, 10.0, 0.9),
    ("NORMAL_TREND", MarketRegime.BULLISH_TREND, MarketDirection.BULLISH, 45.0, 55.0, 30.0, 0.5),
    ("BREAKOUT", MarketRegime.BULLISH_BREAKOUT, MarketDirection.BULLISH, 82.0, 88.0, 70.0, 0.2),
    ("EXTREME_VOL", MarketRegime.EXTREME_VOLATILITY, MarketDirection.NEUTRAL, 97.0, 40.0, 98.0, 0.1),
]


def market_state(regime, direction, energy, trend, volatility, compression, index):
    return MarketStateV1(
        symbol="XAUUSD",
        timestamp=NOW + timedelta(minutes=index),
        regime=regime,
        direction=direction,
        session=TradingSession.LONDON,
        feed_status=FeedStatus.LIVE,
        energy=energy,
        energy_velocity=0.0,
        volatility=volatility,
        trend_strength=trend,
        momentum=trend * 0.8,
        compression=compression,
        confidence=0.85,
        regime_age_seconds=900.0,
        data_age_seconds=1.0,
        price=2650.0,
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
                    "min_free_vram_mb": 1800,
                },
            },
            "music": {"min_duration_seconds": DURATION, "max_duration_seconds": DURATION + 15},
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
    director = MusicDirector(
        settings,
        load_content_library(config_dir=CONFIG_DIR),
        selector=WeightedSelector(random.Random(31)),
    )
    warm_up()
    await provider.load()

    rows = []
    signatures: set[str] = set()
    index = 0
    for label, regime, direction, energy, trend, vol, compression in CONDITIONS:
        for repeat in range(PER_REGIME):
            index += 1
            state = market_state(regime, direction, energy, trend, vol, compression, index)
            track_id = f"TF-M7-{index:02d}"
            decision = director.create_blueprint(
                track_id=track_id,
                state=state,
                history=ProgrammingHistory([]),
                buffer=BufferHealthV1(
                    minutes_ready=40.0,
                    minutes_in_flight=6.0,
                    minimum_minutes=20.0,
                    target_minutes=45.0,
                    maximum_minutes=90.0,
                    ready_count=10,
                    in_flight_count=2,
                ),
                now=state.timestamp,
                capacity_ratio=1.3,
                used_signatures=frozenset(signatures),
            )
            blueprint = decision.blueprint
            signatures.add(blueprint.signature())
            request = GenerationRequest(
                blueprint=blueprint,
                output_path=OUT / f"{track_id}.flac",
                lyrics=None,
                timeout_seconds=900.0,
                attempt=1,
            )
            print(
                f"[{index}] {label:12s} -> {blueprint.composition.genre:14s} "
                f"{blueprint.composition.bpm:3d} BPM energy {blueprint.composition.energy:.2f} ...",
                flush=True,
            )
            started = time.perf_counter()
            try:
                result = await provider.generate(request)
            except Exception as error:
                print(f"    FAILED {type(error).__name__}: {error}", flush=True)
                continue
            wall = time.perf_counter() - started

            features = extract_features(read_audio(result.audio_path))
            samples = np.asarray(read_audio(result.audio_path).samples, dtype=np.float64)
            mono = samples.mean(axis=1) if samples.ndim == 2 else samples
            # Rhythmic activity: how much the short-term envelope moves. A proxy for
            # "busyness" that needs no model and is comparable across tracks.
            frame = 2048
            usable = (mono.size // frame) * frame
            envelope = np.abs(mono[:usable].reshape(-1, frame)).max(axis=1)
            flux = float(np.abs(np.diff(envelope)).mean()) if envelope.size > 1 else 0.0

            row = {
                "condition": label,
                "track_id": track_id,
                "blueprint_genre": blueprint.composition.genre,
                "blueprint_bpm": blueprint.composition.bpm,
                "blueprint_energy": round(blueprint.composition.energy, 3),
                "market_energy": energy,
                "generation_seconds": round(wall, 1),
                "tempo": round(features.tempo, 1) if features.tempo else None,
                "lufs": round(features.integrated_lufs, 2) if features.integrated_lufs else None,
                "rms": round(features.rms, 4),
                "spectral_centroid": round(features.spectral_centroid, 0),
                "spectral_bandwidth": round(features.spectral_bandwidth, 0),
                "high_energy_ratio": round(features.high_energy_ratio, 5),
                "low_energy_ratio": round(features.low_energy_ratio, 4),
                "crest_factor": round(features.crest_factor, 2),
                "envelope_flux": round(flux, 5),
                "zero_crossing_rate": round(features.zero_crossing_rate, 4),
            }
            rows.append(row)
            print(
                f"    {wall:.0f}s | tempo {row['tempo']} | {row['lufs']} LUFS | "
                f"centroid {row['spectral_centroid']:.0f} Hz | flux {flux:.4f}",
                flush=True,
            )

    (OUT / "market_energy.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )

    print("\n=== §7.17 summary: measured audio per market condition ===")
    header = f"{'condition':14s} {'bp energy':>9s} {'tempo':>6s} {'LUFS':>7s} {'RMS':>7s} {'centroid':>9s} {'flux':>8s}"
    print(header)
    for label, *_ in CONDITIONS:
        group = [r for r in rows if r["condition"] == label]
        if not group:
            continue

        def avg(key):
            values = [r[key] for r in group if r[key] is not None]
            return statistics.mean(values) if values else float("nan")

        print(
            f"{label:14s} {avg('blueprint_energy'):9.2f} {avg('tempo'):6.1f} "
            f"{avg('lufs'):7.2f} {avg('rms'):7.4f} {avg('spectral_centroid'):9.0f} "
            f"{avg('envelope_flux'):8.4f}"
        )
    print(f"\nwritten to {OUT / 'market_energy.json'}")


asyncio.run(main())
