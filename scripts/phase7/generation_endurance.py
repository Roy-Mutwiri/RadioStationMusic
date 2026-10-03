"""§7.21 capacity and §7.23 long-run stability, in one pass.

Twenty consecutive real generations, tracking latency, VRAM and process RAM before and
after each. §7.23 asks to look for leaks; a leak is a *trend*, so what matters is the slope
across the run rather than any single reading.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/.Music")

import psutil

from tests.conftest import make_blueprint
from tradefix_radio.config.loader import load_settings
from tradefix_radio.core.clock import SystemClock
from tradefix_radio.generation.ace_step import AceStepClient, AceStepProvider
from tradefix_radio.generation.factory import provider_profiles
from tradefix_radio.generation.gpu import GpuProbe
from tradefix_radio.generation.provider import GenerationRequest

OUT = Path("D:/.Music/artifacts/phase7/endurance")
RUNS = 20
DURATION = 60

GENRES = ["lofi", "deep_house", "uk_trap", "dnb", "rnb", "uk_drill", "amapiano", "boom_bap"]
BPMS = [72, 96, 110, 124, 140, 152, 168, 174]


def acestep_process() -> psutil.Process | None:
    """The ACE-Step server, so its RAM can be watched for growth."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if "acestep" in cmdline.lower() and "python" in (proc.info.get("name") or "").lower():
            return proc
    return None


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
            }
        },
    )
    ace = settings.generation.ace_step
    gpu = GpuProbe()
    provider = AceStepProvider(
        ace,
        clock=SystemClock(),
        client=AceStepClient(ace.base_url),
        profiles=provider_profiles(settings),
        gpu=gpu,
    )

    cold_started = time.perf_counter()
    await provider.load()
    cold_load = time.perf_counter() - cold_started

    server = acestep_process()
    print(f"cold load (already resident): {cold_load:.1f}s")
    print(f"acestep server pid: {server.pid if server else 'not found'}\n", flush=True)

    rows = []
    for index in range(1, RUNS + 1):
        track_id = f"TF-E7-{index:02d}"
        blueprint = make_blueprint(
            track_id,
            genre=GENRES[index % len(GENRES)],
            secondary_genre=None,
            bpm=BPMS[(index * 3) % len(BPMS)],
            composition_energy=0.2 + 0.7 * ((index % 5) / 4.0),
            instrumental=True,
            duration_seconds=DURATION,
            seed=90_000 + index * 37,
        )
        before_gpu = await gpu.sample()
        before_ram = server.memory_info().rss if server else None

        started = time.perf_counter()
        try:
            result = await provider.generate(
                GenerationRequest(
                    blueprint=blueprint,
                    output_path=OUT / f"{track_id}.flac",
                    lyrics=None,
                    timeout_seconds=900.0,
                    attempt=1,
                )
            )
            error = None
            wall = time.perf_counter() - started
            audio = result.duration_seconds
            peak_vram = (
                result.peak_vram_bytes / 1e6 if result.peak_vram_bytes else None
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            wall = time.perf_counter() - started
            audio = 0.0
            peak_vram = None

        after_gpu = await gpu.sample()
        after_ram = server.memory_info().rss if server else None

        row = {
            "run": index,
            "track_id": track_id,
            "genre": blueprint.composition.genre,
            "seconds": round(wall, 2),
            "audio_seconds": round(audio, 2),
            "realtime_factor": round(audio / wall, 3) if wall > 0 else 0.0,
            "vram_used_before_mb": round(before_gpu.used_mb, 0) if before_gpu else None,
            "vram_used_after_mb": round(after_gpu.used_mb, 0) if after_gpu else None,
            "vram_peak_mb": round(peak_vram, 0) if peak_vram else None,
            "server_rss_before_mb": round(before_ram / 1e6, 1) if before_ram else None,
            "server_rss_after_mb": round(after_ram / 1e6, 1) if after_ram else None,
            "error": error,
        }
        rows.append(row)
        print(
            f"[{index:2d}/{RUNS}] {row['genre']:12s} {wall:5.1f}s "
            f"({row['realtime_factor']}x) vram {row['vram_used_after_mb']} MB "
            f"rss {row['server_rss_after_mb']} MB"
            + (f"  ERROR {error}" if error else ""),
            flush=True,
        )

    ok = [r for r in rows if not r["error"]]
    latencies = [r["seconds"] for r in ok]
    factors = [r["realtime_factor"] for r in ok]
    summary = {
        "runs": RUNS,
        "succeeded": len(ok),
        "failed": RUNS - len(ok),
        "cold_load_seconds": round(cold_load, 1),
        "latency_min": round(min(latencies), 1) if latencies else None,
        "latency_p50": round(statistics.median(latencies), 1) if latencies else None,
        "latency_p95": (
            round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)], 1)
            if latencies
            else None
        ),
        "latency_max": round(max(latencies), 1) if latencies else None,
        "realtime_p50": round(statistics.median(factors), 2) if factors else None,
        "vram_first": ok[0]["vram_used_after_mb"] if ok else None,
        "vram_last": ok[-1]["vram_used_after_mb"] if ok else None,
        "rss_first": ok[0]["server_rss_after_mb"] if ok else None,
        "rss_last": ok[-1]["server_rss_after_mb"] if ok else None,
    }
    if summary["rss_first"] and summary["rss_last"]:
        summary["rss_growth_mb"] = round(summary["rss_last"] - summary["rss_first"], 1)
        summary["rss_growth_per_track_mb"] = round(
            summary["rss_growth_mb"] / max(1, len(ok)), 2
        )
    if summary["vram_first"] and summary["vram_last"]:
        summary["vram_growth_mb"] = round(summary["vram_last"] - summary["vram_first"], 1)

    # Latency trend: a model that slows as it runs is leaking something.
    if len(latencies) >= 6:
        half = len(latencies) // 2
        summary["latency_first_half_mean"] = round(statistics.mean(latencies[:half]), 1)
        summary["latency_second_half_mean"] = round(statistics.mean(latencies[half:]), 1)

    (OUT / "endurance.json").write_text(
        json.dumps({"summary": summary, "runs": rows}, indent=2), encoding="utf-8"
    )
    print("\n=== §7.21 / §7.23 summary ===")
    for key, value in summary.items():
        print(f"  {key:30s} {value}")
    print(f"\nwritten to {OUT / 'endurance.json'}")


asyncio.run(main())
