"""``tradefix visual`` — behaviour simulation, asset manifest, and self-checks.

Six subcommands. The first four run **without artwork and without the station**:

    tradefix visual simulate    run the behaviour director for simulated hours
    tradefix visual timeline     print a human-readable action timeline
    tradefix visual manifest     show or write the art asset contract
    tradefix visual doctor       validate the catalogue against frozen V1 geometry
    tradefix visual serve        serve the placeholder renderer and push commands
    tradefix visual benchmark    measure what a browser reports rendering it

``simulate`` is the one that matters. It is the whole argument for the director living
in Python: a 24-hour behavioural soak that completes in minutes, is reproducible from a
seed, and prints numbers a human can argue with.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from tradefix_radio.config.schema import AppSettings
from tradefix_radio.visual.assets import AssetManifest, manifest_path, write_manifest
from tradefix_radio.visual.bridge import StationLink, default_station_url
from tradefix_radio.visual.camera import validate_metadata
from tradefix_radio.visual.catalog import (
    CATALOG,
    CHAINS,
    SCHEDULABLE,
    validate_catalog,
)
from tradefix_radio.visual.contracts import ActionCategory
from tradefix_radio.visual.director import TICK_SECONDS
from tradefix_radio.visual.geometry import default_blockout, load_blockout
from tradefix_radio.visual.scene import frame_workload
from tradefix_radio.visual.service import (
    DEFAULT_PORT,
    RUNTIME_DIR,
    VisualRuntime,
    create_app,
    runtime_url,
)
from tradefix_radio.visual.simulate import DURATIONS, SCENARIOS, run_scenario


def register(subparsers: Any) -> None:
    """Add the ``visual`` subcommand tree.

    ``subparsers`` is argparse's private ``_SubParsersAction``, typed as ``Any`` for the
    same reason the other CLI modules do it: mypy and argparse disagree about the name
    of that generic across versions.
    """
    parser = subparsers.add_parser(
        "visual", help="behaviour simulation, asset manifest and visual self-checks"
    )
    inner = parser.add_subparsers(dest="visual_command", required=True)

    simulate = inner.add_parser(
        "simulate", help="run the behaviour director for simulated time"
    )
    simulate.add_argument(
        "--scenario", default="range", choices=sorted(SCENARIOS),
        help="simulated market and music conditions",
    )
    simulate.add_argument(
        "--duration", default="30m",
        help=f"named duration ({', '.join(DURATIONS)}) or a number of seconds",
    )
    simulate.add_argument("--seed", type=int, default=1, help="RNG seed; runs are reproducible")
    simulate.add_argument(
        "--spike-every", type=float, default=0.0, metavar="SECONDS",
        help="inject a salience spike this often, to exercise market reactions",
    )
    simulate.add_argument(
        "--tick", type=float, default=TICK_SECONDS, metavar="SECONDS",
        help=(
            "sampling interval. The director decides on 1.4-4.2 s intervals, so 0.25 "
            "samples them amply at a fifth of the cost — right for a long soak, wrong "
            "for a latency check"
        ),
    )
    simulate.add_argument("--actions", type=int, default=0, metavar="N",
                          help="also print the N most frequent actions")
    simulate.add_argument("--json", action="store_true", help="emit machine-readable output")

    timeline = inner.add_parser(
        "timeline", help="print a human-readable action timeline"
    )
    timeline.add_argument("--scenario", default="range", choices=sorted(SCENARIOS))
    timeline.add_argument("--duration", default="5m")
    timeline.add_argument("--seed", type=int, default=1)
    timeline.add_argument("--lines", type=int, default=60)
    timeline.add_argument("--skip", type=int, default=0)
    timeline.add_argument("--spike-every", type=float, default=0.0, metavar="SECONDS")

    manifest = inner.add_parser("manifest", help="show or write the art asset contract")
    manifest.add_argument(
        "--write", action="store_true",
        help=f"write {manifest_path().name} (generated; hand edits are overwritten)",
    )
    manifest.add_argument("--json", action="store_true")

    doctor = inner.add_parser(
        "doctor", help="validate the catalogue and cameras against frozen V1 geometry"
    )
    doctor.add_argument("--blockout", default=None, help="path to an alternative blockout")

    serve = inner.add_parser(
        "serve", help="run the visual process and the placeholder renderer"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--scenario", default=None, choices=sorted(SCENARIOS),
        help=(
            "drive the director from a simulated scenario instead of the station. "
            "Every simulated frame is badged, so a test mode cannot be streamed by "
            "accident"
        ),
    )
    serve.add_argument("--seed", type=int, default=None, help="RNG seed; makes a run reproducible")
    serve.add_argument(
        "--station", default=None, metavar="URL",
        help=f"station WebSocket (default {default_station_url()})",
    )

    benchmark = inner.add_parser(
        "benchmark",
        help="serve the runtime, then summarise the frame timings a browser reports",
    )
    benchmark.add_argument("--host", default="127.0.0.1")
    benchmark.add_argument("--port", type=int, default=DEFAULT_PORT)
    benchmark.add_argument("--scenario", default="breakout", choices=sorted(SCENARIOS))
    benchmark.add_argument("--seed", type=int, default=7)
    benchmark.add_argument(
        "--fps", type=int, default=30, choices=(30, 60),
        help="frame cap to measure; run it twice, once at each, and compare",
    )
    benchmark.add_argument(
        "--seconds", type=float, default=120.0,
        help="how long to collect once a renderer connects",
    )
    benchmark.add_argument("--json", type=Path, default=None, help="write the summary here")


async def command(args: argparse.Namespace, _settings: AppSettings) -> int:
    """Dispatch. Settings are accepted for CLI symmetry; the visual layer reads none.

    That is not an oversight — the visual layer is a read-only consumer with no station
    configuration of its own, and taking settings it ignores keeps it honest about that.
    """
    if args.visual_command == "serve":
        return await _serve(args)
    if args.visual_command == "benchmark":
        return await _benchmark(args)
    handler = {
        "simulate": _simulate,
        "timeline": _timeline,
        "manifest": _manifest,
        "doctor": _doctor,
    }[args.visual_command]
    return handler(args)


# ------------------------------------------------------------------ simulate


def _resolve_duration(raw: str) -> float | None:
    if raw in DURATIONS:
        return DURATIONS[raw]
    try:
        return float(raw)
    except ValueError:
        return None


def _simulate(args: argparse.Namespace) -> int:
    seconds = _resolve_duration(args.duration)
    if seconds is None:
        print(
            f"  unknown duration {args.duration!r}; use one of "
            f"{', '.join(DURATIONS)} or a number of seconds"
        )
        return 1

    report, _ = run_scenario(
        args.scenario,
        seconds,
        seed=args.seed,
        salience_spike_every_seconds=args.spike_every,
        tick_seconds=args.tick,
    )

    if args.json:
        print(json.dumps(_report_json(report), indent=2))
        return 0 if report.is_structurally_sound else 1

    print(report.summary())
    if args.actions:
        print()
        print(report.top_actions(args.actions))
    if not report.is_structurally_sound:
        print()
        print("  An invariant was broken. That is a bug, not a tuning question.")
        return 1
    if report.has_visible_loop:
        print()
        print("  Repetition thresholds exceeded. Behaviour may read as looped.")
        return 1
    return 0


def _report_json(report: Any) -> dict[str, Any]:
    return {
        "scenario": report.scenario,
        "duration_hours": round(report.hours, 4),
        "seed": report.seed,
        "ticks": report.ticks,
        "actions_total": report.actions_total,
        "deliberate_total": report.deliberate_total,
        "deliberate_per_hour": round(report.deliberate_total / max(1e-9, report.hours), 2),
        "blinks_total": report.blinks_total,
        "gaze_shifts": report.gaze_shifts_total,
        "state_changes": report.state_changes,
        "distinct_actions": len(report.action_counts),
        "category_shares": {
            name: round(count / max(1, sum(report.category_counts.values())), 4)
            for name, count in report.category_counts.items()
        },
        "pacing": {
            "coffee_per_hour": round(report.coffee_per_hour, 3),
            "headphones_per_hour": round(report.headphone_per_hour, 3),
            "posture_reset_per_hour": round(report.posture_reset_per_hour, 3),
            "reactions_per_hour": round(report.reactions / max(1e-9, report.hours), 3),
            "fatigue_phase_final": round(report.fatigue_phase_final, 4),
            "behavior_energy_mean": round(report.behavior_energy_mean, 4),
        },
        "gaze": {
            "screen_share": round(report.gaze_screen_share, 4),
            "camera_share": round(report.camera_gaze_share, 5),
        },
        "blink_interval": {
            "mean": report.blink_interval_mean,
            "min": report.blink_interval_min,
            "max": report.blink_interval_max,
        },
        "repetition": {
            "top_trigram": list(report.top_trigram) if report.top_trigram else None,
            "top_trigram_share": round(report.top_trigram_share, 5),
            "distinct_trigrams": report.distinct_trigrams,
            "trigram_diversity": round(report.trigram_diversity, 4),
            "longest_identical_run": report.longest_identical_run,
            "back_to_back_repeats": report.back_to_back_repeats,
            "has_visible_loop": report.has_visible_loop,
        },
        "rhythm": {
            "min": round(report.fatigue_min, 4),
            "max": round(report.fatigue_max, 4),
            "mean": round(report.fatigue_mean, 4),
            "cycles": report.fatigue_cycles,
            "cycle_durations_hours": report.cycle_durations_hours,
            "turning_points": report.fatigue_turning_points,
            "time_near_floor": round(report.fatigue_time_near_floor, 4),
            "time_near_ceiling": round(report.fatigue_time_near_ceiling, 4),
            "pinned_ceiling": report.fatigue_pinned_ceiling,
            "pinned_floor": report.fatigue_pinned_floor,
            "phase_shares": report.work_phase_shares,
        },
        "posture": {
            "major_total": report.posture_major_total,
            "major_per_hour": round(report.posture_major_total / max(1e-9, report.hours), 3),
            "minor_total": report.posture_minor_total,
            "minor_per_hour": round(report.posture_minor_total / max(1e-9, report.hours), 3),
            "during_chain": report.posture_during_chain,
            "during_reaction": report.posture_during_reaction,
        },
        "camera": {
            "cuts": report.camera.cuts,
            "cuts_per_hour": round(report.camera.cuts_per_hour, 3),
            "hold_seconds": {
                "min": round(report.camera.min_hold, 1),
                "median": round(report.camera.median_hold, 1),
                "mean": round(report.camera.mean_hold, 1),
                "max": round(report.camera.max_hold, 1),
            },
            "airtime_shares": {
                camera_id: round(share, 4)
                for camera_id, share in report.camera.camera_shares.items()
            },
            "cut_counts": dict(report.camera.camera_counts),
            "motivations": dict(report.camera.motivation_counts),
            "transitions": dict(report.camera.transition_counts),
            "vetoes": dict(report.camera.veto_counts),
            "action_visibility": round(report.camera.action_visibility, 4),
            "state_alignment": round(report.camera.state_alignment, 4),
            "top_sequence_share": round(report.camera.top_sequence_share, 5),
            "alternations": report.camera.alternations,
            "unsafe_cuts": report.camera.unsafe_cuts,
            "cam6_without_interaction": report.camera.cam6_without_interaction,
            "safe": report.camera.is_safe,
        },
        "invariants": {
            "lock_violations": report.lock_violations,
            "overlap_violations": report.overlap_violations,
            "illegal_transitions": report.illegal_transitions,
            "unreachable_anchors": report.unreachable_anchors,
            "unknown_gaze_targets": report.unknown_gaze_targets,
            "cooldown_violations": report.cooldown_violations,
            "unsafe_cuts": report.camera.unsafe_cuts,
            "structurally_sound": report.is_structurally_sound,
        },
    }


# ------------------------------------------------------------------ timeline


def _timeline(args: argparse.Namespace) -> int:
    seconds = _resolve_duration(args.duration)
    if seconds is None:
        print(f"  unknown duration {args.duration!r}")
        return 1
    _, simulator = run_scenario(
        args.scenario,
        seconds,
        seed=args.seed,
        collect_timeline=True,
        salience_spike_every_seconds=args.spike_every,
    )
    print(f"  {args.scenario}, seed {args.seed} — {len(simulator.timeline)} events collected")
    print()
    print(simulator.format_timeline(limit=args.lines, skip=args.skip))
    return 0


# ------------------------------------------------------------------ manifest


def _manifest(args: argparse.Namespace) -> int:
    manifest = AssetManifest.build()
    problems = manifest.validate()
    if args.write:
        written = write_manifest()
        print(f"  wrote {written}")
    if args.json:
        print(json.dumps(manifest.to_json(), indent=2))
        return 1 if problems else 0

    print(f"  art asset contract v{manifest.contract_version}")
    print(f"  {len(manifest.assets)} assets, {len(manifest.outstanding)} outstanding")
    print()
    by_type: dict[str, list[Any]] = {}
    for asset in manifest.assets:
        by_type.setdefault(asset.asset_type, []).append(asset)
    for asset_type, assets in by_type.items():
        print(f"  {asset_type}  ({len(assets)})")
        for asset in assets:
            mark = "delivered" if asset.delivered else "OUTSTANDING"
            layers = f"{len(asset.layers)} layers" if asset.layers else "layers TBD"
            print(
                f"    {asset.asset_id:<32} {asset.width}x{asset.height:<6} "
                f"{layers:<14} {mark}"
            )
        print()
    if problems:
        print("  PROBLEMS:")
        for problem in problems:
            print(f"    {problem}")
        return 1
    print("  manifest agrees with frozen V1 geometry")
    return 0


# ------------------------------------------------------------------ doctor


def _doctor(args: argparse.Namespace) -> int:
    from pathlib import Path  # noqa: PLC0415 - only needed on this path

    try:
        blockout = (
            load_blockout(Path(args.blockout)) if args.blockout else default_blockout()
        )
    except Exception as error:  # noqa: BLE001 - the whole point is to report it
        print(f"  blockout FAILED to load: {type(error).__name__}: {error}")
        return 1

    print(f"  blockout            {blockout.blockout_id}")
    print(
        f"  geometry            {len(blockout.anchors)} anchors, "
        f"{len(blockout.gaze)} gaze targets, {len(blockout.cameras)} cameras"
    )
    print(
        f"  key heights         desk z{blockout.desk_surface_z:.0f}, "
        f"eye z{blockout.eye[2]:.0f}, bezel z{blockout.monitor_top_bezel_z:.0f}, "
        f"reach {blockout.seated_reach_mm:.0f} mm"
    )
    print(
        f"  gaze               {blockout.default_gaze_pitch_degrees:.1f} deg default "
        f"pitch, {blockout.min_gaze_transit_ms} ms transit floor, "
        f"{blockout.screen_share_target * 100:.0f} % screen target"
    )
    print()

    counts: dict[str, int] = {}
    for action in CATALOG.values():
        counts[action.category.value] = counts.get(action.category.value, 0) + 1
    print(f"  catalogue           {len(CATALOG)} actions, {len(SCHEDULABLE)} schedulable")
    for category in ActionCategory:
        print(f"    {category.value:<12} {counts.get(category.value, 0)}")
    print(f"  chains              {len(CHAINS)}")
    for chain in CHAINS.values():
        locks = ", ".join(sorted(lock.value for lock in chain.required_locks()))
        print(
            f"    {chain.chain_id:<17} {chain.length} steps, commits at "
            f"{chain.commit_step}, holds [{locks}]"
        )
    print()

    problems = validate_catalog(
        frozenset(blockout.anchors), frozenset(t.value for t in blockout.gaze)
    )
    problems += validate_metadata(blockout)
    problems += AssetManifest.build(blockout).validate(blockout)

    if problems:
        print("  PROBLEMS:")
        for problem in problems:
            print(f"    {problem}")
        return 1
    print("  catalogue, cameras and asset manifest all agree with frozen V1 geometry")
    return 0


# ------------------------------------------------------------------ serve


async def _serve(args: argparse.Namespace) -> int:
    """Run the visual process. Serves the placeholder renderer and pushes commands."""
    import uvicorn  # noqa: PLC0415 - only this path needs a server

    if not RUNTIME_DIR.is_dir():
        print(f"  runtime not found at {RUNTIME_DIR}")
        return 1

    runtime = VisualRuntime(
        seed=args.seed,
        scenario=args.scenario,
        station_url=args.station or default_station_url(),
    )
    # The station link only runs in live mode. In scenario mode the visual layer is
    # entirely self-contained, which is what lets the renderer be verified with no
    # station running at all.
    #
    # Attached to the runtime rather than registered as an `on_event` handler: FastAPI
    # ignores `on_event` entirely when an explicit `lifespan` is passed, so the link
    # would have been created and never started.
    if args.scenario is None:
        runtime.link = StationLink(url=runtime.station_url, bridge=runtime.bridge)

    app = create_app(runtime)

    print(f"  visual runtime      {runtime_url(args.host, args.port)}")
    print(f"  mode                {'simulated: ' + args.scenario if args.scenario else 'live'}")
    if args.scenario is None:
        print(f"  station socket      {runtime.station_url} (read-only)")
    print(f"  OBS browser source  {runtime_url(args.host, args.port)} at 1920x1080")
    print("  debug overlay       add ?debug=1   HUD off: ?hud=0   60 fps: ?fps=60")
    print()

    config = uvicorn.Config(
        app, host=args.host, port=args.port, log_level="info", access_log=False
    )
    await uvicorn.Server(config).serve()
    return 0


# ------------------------------------------------------------------ benchmark


async def _benchmark(args: argparse.Namespace) -> int:
    """Measure the renderer's cost. Needs a browser; this command drives everything else.

    The GPU figures cannot be obtained from Python — no headless process on this machine
    has a GPU context — so this serves the runtime, waits for a browser to attach, and
    reports what the renderer itself measured. Run it once at `--fps 30` and once at
    `--fps 60`, then compare: ADR-10 prefers 30 if the picture holds, because ACE-Step
    owns this GPU and the visual layer is the tenant.
    """
    import uvicorn  # noqa: PLC0415 - only this path needs a server

    runtime = VisualRuntime(seed=args.seed, scenario=args.scenario)
    app = create_app(runtime)
    url = f"{runtime_url(args.host, args.port)}?fps={args.fps}&hud=1"

    config = uvicorn.Config(
        app, host=args.host, port=args.port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())

    print(f"  open this in Chrome or an OBS browser source at 1920x1080:\n\n    {url}\n")
    print(f"  collecting for {args.seconds:g}s once it connects. Alongside it, read")
    print("  GPU and VRAM from Task Manager > Performance > GPU, or OBS > View > Stats —")
    print("  a renderer cannot measure the GPU it is running on.\n")

    try:
        waited = 0.0
        while not runtime.benchmark().get("samples") and waited < 300.0:
            await asyncio.sleep(1.0)
            waited += 1.0
        if not runtime.benchmark().get("samples"):
            print("  no renderer connected within 5 minutes; nothing measured")
            return 1

        print("  renderer attached; collecting")
        await asyncio.sleep(args.seconds)
        summary = runtime.benchmark()
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serving

    summary["fps_cap"] = args.fps
    summary["derived_frame_workload"] = frame_workload()

    print(f"\n  fps cap             {args.fps}")
    print(f"  samples             {summary['samples']}")
    print(f"  frames rendered     {summary['frames_rendered']}")
    for key in ("fps_mean", "fps_p05", "frame_time_p95_ms", "gl_memory_mb"):
        spread = summary[key]
        print(
            f"  {key:<19} min {spread['min']:<9} mean {spread['mean']:<9} "
            f"p95 {spread['p95']:<9} max {spread['max']}"
        )
    print(f"  dropped frames      {summary['dropped_frames']}")
    print(f"  quality profile     {summary['profile']}")

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\n  wrote {args.json}")
    return 0


__all__ = ["command", "register"]
