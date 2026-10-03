"""Measure what the visual runtime costs — on the side that can be measured here.

This script deliberately reports two different KINDS of number and keeps them apart:

  MEASURED   the Python side: director tick cost, command volume, socket bandwidth.
             Real wall-clock numbers from running the real code on this machine.

  DERIVED    the browser side: draw calls, uniform updates and vertices per frame,
             counted from the scene definition by mirroring `app.js`'s draw loop.
             This is a workload budget, NOT a GPU measurement. It tells you how much
             work the renderer asks for; it cannot tell you how long the GPU takes.

The GPU numbers the brief asks for (GPU %, VRAM, frame time at 30 vs 60 fps) require a
browser with a GPU process, and nothing here substitutes for them. See `V8_REPORT.md` §D.

Usage:
    python scripts/visual/measure_runtime_cost.py [--minutes 10]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

from tradefix_radio.core.clock import UTC, VirtualClock
from tradefix_radio.visual.camera import CAMERA_METADATA
from tradefix_radio.visual.director import BehaviorDirector
from tradefix_radio.visual.renderer import (
    TARGET_HEIGHT,
    TARGET_WIDTH,
    action_command,
    camera_command,
    encode,
    gaze_command,
)
from tradefix_radio.visual.scene import frame_workload
from tradefix_radio.visual.simulate import SCENARIOS

TICK_HZ = 20.0  # the service's own tick rate (0.05 s), so the cost is the real cost


# -------------------------------------------------------------- MEASURED: the Python


def python_cost(minutes: float, scenario: str, seed: int) -> dict[str, object]:
    """Run the real director at the service's real tick rate and time it.

    The commands are built with the same three helpers `VisualRuntime._tick` uses, so the
    bandwidth figure is the bandwidth the socket would actually carry.
    """
    clock = VirtualClock(datetime(2026, 10, 3, 21, 0, tzinfo=UTC))
    director = BehaviorDirector(clock=clock, rng=random.Random(seed))
    curve = SCENARIOS[scenario]

    ticks = int(minutes * 60 * TICK_HZ)
    step = 1.0 / TICK_HZ
    durations: list[float] = []
    command_bytes = 0
    command_count = 0
    sequence = 0

    for index in range(ticks):
        clock.advance_sync(step)
        state = curve.state(clock.now())

        started = time.perf_counter()
        output = director.tick(state)
        durations.append((time.perf_counter() - started) * 1000.0)

        commands = [action_command(action) for action in output.actions]
        commands.extend(gaze_command(shift) for shift in output.gaze_shifts)
        if output.camera_cut is not None:
            sequence += 1
            commands.append(
                camera_command(
                    output.camera_cut.camera_id,
                    sequence=sequence,
                    transition=output.camera_cut.transition,
                    parallax=CAMERA_METADATA[output.camera_cut.camera_id].parallax_amplitude,
                )
            )
        if commands:
            command_bytes += len(encode(commands).encode("utf-8"))
            command_count += len(commands)

        if index % 12000 == 0:
            print(f"  {index / TICK_HZ / 60:5.1f} min", end="\r")

    durations.sort()
    wall = sum(durations) / 1000.0
    simulated = ticks * step
    return {
        "scenario": scenario,
        "simulated_seconds": simulated,
        "ticks": ticks,
        "tick_ms_mean": round(statistics.fmean(durations), 4),
        "tick_ms_p50": round(durations[len(durations) // 2], 4),
        "tick_ms_p95": round(durations[int(len(durations) * 0.95)], 4),
        "tick_ms_max": round(durations[-1], 4),
        "cpu_seconds_total": round(wall, 2),
        "cpu_fraction_of_one_core": round(wall / simulated, 5),
        "commands_total": command_count,
        "commands_per_minute": round(command_count / (simulated / 60), 1),
        "socket_bytes_per_second": round(command_bytes / simulated, 1),
        "socket_kb_per_hour": round(command_bytes / simulated * 3600 / 1024, 1),
    }


# --------------------------------------------------------------------------- report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--scenario", default="breakout", choices=sorted(SCENARIOS))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    print("DERIVED — renderer workload per frame (not a GPU measurement)")
    frame = frame_workload()
    print(f"  target                  {TARGET_WIDTH}x{TARGET_HEIGHT}")
    print(f"  draw calls / frame      {frame['draw_calls_per_frame']}")
    for name, count in frame["breakdown"].items():  # type: ignore[union-attr]
        print(f"      {name:<20} {count}")
    print(f"  uniform updates / frame {frame['uniform_updates_per_frame']}")
    print(f"  vertices / frame        {frame['vertices_per_frame']}")
    print(f"  textures                {frame['textures']}")
    print(f"  shader programs         {frame['shader_programs']}")
    per_second = frame["draw_calls_per_second"]
    print(f"  draw calls / s @ 30     {per_second['30']}")  # type: ignore[index]
    print(f"  draw calls / s @ 60     {per_second['60']}")  # type: ignore[index]

    print(f"\nMEASURED — Python director, {args.minutes:g} simulated minutes")
    cost = python_cost(args.minutes, args.scenario, args.seed)
    print(f"  tick mean               {cost['tick_ms_mean']} ms")
    print(f"  tick p95                {cost['tick_ms_p95']} ms")
    print(f"  tick max                {cost['tick_ms_max']} ms")
    print(f"  CPU of one core         {float(cost['cpu_fraction_of_one_core']) * 100:.3f} %")
    print(f"  commands / minute       {cost['commands_per_minute']}")
    print(f"  socket bandwidth        {cost['socket_bytes_per_second']} B/s "
          f"({cost['socket_kb_per_hour']} KiB/h)")

    if args.json:
        args.json.write_text(
            json.dumps({"derived_frame": frame, "measured_python": cost}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
