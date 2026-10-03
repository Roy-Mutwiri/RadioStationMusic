"""The renderer contract: what crosses the wire, in both directions.

```
BehaviorDirector (Python)
      |  VisualCommandV1  @ event rate, ~20 Hz ceiling
      v
WebGL2 / Canvas renderer (OBS browser source)
      |  RendererTelemetryV1  @ 1 Hz
      v
BehaviorDirector
```

This module owns the serialisation and nothing else. It does not render, and it does not
decide — it is the boundary, and keeping it thin is what lets the placeholder renderer
and the eventual painted one consume exactly the same stream.

Why the protocol is commands rather than state
----------------------------------------------
A state-sync protocol ("here is the full pose, 20 times a second") would be simpler to
write and would make every frame depend on the last message arriving. A command protocol
("start `coffee_sip`, 5 240 ms, blend 400/520") lets the renderer interpolate locally,
survive a dropped message, and keep rhythmic phase without a round trip — which ADR-11
requires, because per-beat round trips are impossible at 174 BPM.

The cost is that the renderer must be resyncable, so :func:`resync_commands` exists and
is sent whenever `last_command_sequence` falls too far behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from tradefix_radio.visual.contracts import (
    CharacterActionV1,
    CharacterState,
    GazeShiftV1,
    GazeTarget,
    RendererTelemetryV1,
    RhythmPolicyV1,
    VisualStateV1,
)

#: Protocol version. The renderer refuses a mismatch rather than guessing.
PROTOCOL_VERSION: Final = 1

#: Output the renderer targets, and the frame cap. 30 is the default because the
#: frame-rate decision is deferred to a measurement rather than assumed — the brief is
#: explicit that a stable 30 may beat an unstable 60.
TARGET_WIDTH: Final = 1920
TARGET_HEIGHT: Final = 1080
TARGET_FPS: Final = 30

#: Quality profiles. Each names what it sheds; the renderer reports which is active.
QUALITY_PROFILES: Final[dict[str, dict[str, Any]]] = {
    "low": {
        "resident_camera_stacks": 1,
        "monitor_surface_scale": 0.5,
        "light_layers": "per_layer",
        "parallax_layers": 6,
        "target_fps": 30,
    },
    "balanced": {
        "resident_camera_stacks": 2,
        "monitor_surface_scale": 0.75,
        "light_layers": "per_layer",
        "parallax_layers": 12,
        "target_fps": 30,
    },
    "high": {
        "resident_camera_stacks": 3,
        "monitor_surface_scale": 1.0,
        "light_layers": "per_pixel",
        "parallax_layers": 17,
        "target_fps": 60,
    },
}

#: Telemetry silence after which the renderer is considered gone. The director keeps
#: running regardless — that is the whole point of ADR-11's split.
TELEMETRY_TIMEOUT_SECONDS: Final = 10.0

#: Command-sequence lag that triggers a full resync.
RESYNC_LAG_THRESHOLD: Final = 40

#: Sustained fraction of target fps below which quality is shed, and for how long.
SHED_FPS_RATIO: Final = 0.80
SHED_WINDOW_SECONDS: Final = 30.0


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def action_command(action: CharacterActionV1) -> dict[str, Any]:
    """`START` — begin one action.

    Durations and blends are already sampled and amplitude already jittered, so the
    renderer performs rather than decides. That asymmetry is deliberate: every choice
    lives in Python where it is testable.
    """
    return {
        "v": PROTOCOL_VERSION,
        "kind": "START",
        "seq": action.sequence,
        "at": _iso(action.started_at),
        "action_id": action.action_id,
        "category": action.category.value,
        "duration_ms": action.duration_ms,
        "blend_in_ms": action.blend_in_ms,
        "blend_out_ms": action.blend_out_ms,
        "interruptibility": action.interruptibility.value,
        "locks": [lock.value for lock in action.locks],
        "amplitude": action.amplitude,
        "anchor": action.anchor,
        "gaze_target": action.gaze_target.value if action.gaze_target else None,
        "character_state": action.character_state.value,
        "chain_id": action.chain_id,
        "chain_step": action.chain_step,
    }


def gaze_command(shift: GazeShiftV1) -> dict[str, Any]:
    """`GAZE` — move the eyes, and the head with them.

    ``head_contribution`` and ``transit_ms`` are carried rather than derived in the
    renderer so that "eyes lead, head follows" is one decision made in one place.
    """
    return {
        "v": PROTOCOL_VERSION,
        "kind": "GAZE",
        "seq": shift.sequence,
        "at": _iso(shift.started_at),
        "target": shift.target.value,
        "from": shift.from_target.value if shift.from_target else None,
        "transit_ms": shift.transit_ms,
        "dwell_ms": shift.dwell_ms,
        "participation": shift.participation.value,
        "angle_degrees": shift.angle_degrees,
        "head_contribution": shift.head_contribution,
        "forces_blink": shift.forces_blink,
    }


def rhythm_command(policy: RhythmPolicyV1, *, sequence: int) -> dict[str, Any]:
    """`SET` — rhythmic policy.

    The ceilings travel with the policy because the renderer **enforces** them rather
    than merely receiving them. A dropped message must not be able to make a dancing
    trader possible, so the limits are restated on every update.
    """
    return {
        "v": PROTOCOL_VERSION,
        "kind": "SET",
        "seq": sequence,
        "bpm": policy.bpm,
        "downbeat_phase": policy.downbeat_phase,
        "beat_subdivision": policy.beat_subdivision,
        "nod_probability": policy.nod_probability,
        "max_nod_degrees": policy.max_nod_degrees,
        "max_consecutive_beats": policy.max_consecutive_beats,
        "mandatory_gap_seconds": policy.mandatory_gap_seconds,
    }


def screen_command(state: VisualStateV1, *, sequence: int) -> dict[str, Any]:
    """`SCREEN` — what the monitors show.

    Carries the honesty flags explicitly rather than letting the renderer infer them.
    ``feed_trustworthy`` false means charts **stop advancing** and show a marker; a chart
    drawing invented candles on a public stream is a fabricated price with extra steps.
    """
    return {
        "v": PROTOCOL_VERSION,
        "kind": "SCREEN",
        "seq": sequence,
        "active_symbol": state.active_symbol,
        "regime": state.market_regime,
        "band": state.intensity_band.value,
        "energy": state.market_energy,
        "energy_velocity": state.market_energy_velocity,
        "direction": state.market_direction.value if state.market_direction else None,
        "session": state.session,
        "station_mode": state.station_mode.value,
        "emergency_tier": state.emergency_tier,
        "now_playing": {
            "bpm": state.music_bpm,
            "genre": state.music_genre,
            "energy": state.music_energy,
            "progress": state.track_progress,
            "transition": state.transition_state.value,
        },
        "honesty": {
            "feed_trustworthy": state.feed_trust.permits_reactions,
            "market_reactive": state.market_reactive,
            "charts_advance": state.market_reactive,
            "stale_marker": None if state.market_reactive else "NO FEED",
            "price_displayable": False,
            "rule": (
                "No fabricated market activity in any state. No account, position, "
                "balance or order data on any surface, ever."
            ),
        },
    }


def camera_command(
    camera_id: str, *, sequence: int, transition: str, parallax: float
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "kind": "CAMERA",
        "seq": sequence,
        "camera_id": camera_id,
        "transition": transition,
        "parallax_amplitude": parallax,
    }


def resync_commands(
    *,
    sequence: int,
    character_state: CharacterState,
    gaze: GazeTarget,
    camera_id: str,
    running: tuple[str, ...],
    fatigue_phase: float,
    policy: RhythmPolicyV1,
) -> list[dict[str, Any]]:
    """Everything a freshly-attached or lagging renderer needs.

    Sent on connect and whenever `last_command_sequence` falls :data:`RESYNC_LAG_THRESHOLD`
    behind. This is what makes a CEF crash a non-event: the director's history, cooldowns
    and fatigue phase all survive, so the reload resumes rather than restarts.
    """
    return [
        {
            "v": PROTOCOL_VERSION,
            "kind": "RESYNC",
            "seq": sequence,
            "character_state": character_state.value,
            "gaze_target": gaze.value,
            "camera_id": camera_id,
            "running_actions": list(running),
            "fatigue_phase": round(fatigue_phase, 4),
            "target": {"width": TARGET_WIDTH, "height": TARGET_HEIGHT, "fps": TARGET_FPS},
        },
        rhythm_command(policy, sequence=sequence),
    ]


@dataclass
class QualityController:
    """Sheds quality when the renderer cannot keep up, or when VRAM gets tight.

    **Music generation outranks the picture.** That ordering is an architectural
    commitment (ADR-10 §4), not a tuning preference, and this is the mechanism: the
    renderer drops profile and evicts camera stacks rather than letting a generation
    fail for want of VRAM.
    """

    profile: str = "balanced"
    #: Measured GL memory ceiling, MiB. From the ADR's 400 MiB budget with headroom.
    gl_memory_ceiling_mb: float = 420.0
    _below_target_since: float | None = None
    _last_telemetry_monotonic: float | None = None
    history: list[tuple[float, float]] = field(default_factory=list)

    @property
    def renderer_present(self) -> bool:
        return self._last_telemetry_monotonic is not None

    def renderer_alive(self, now_monotonic: float) -> bool:
        last = self._last_telemetry_monotonic
        return last is not None and now_monotonic - last <= TELEMETRY_TIMEOUT_SECONDS

    def observe(self, telemetry: RendererTelemetryV1, now_monotonic: float) -> str | None:
        """Record telemetry. Returns a new profile if one should be applied.

        Uses ``fps_p05`` rather than a mean, deliberately: a 24/7 stream's problem is
        never average frame rate, it is the periodic hitch a mean makes invisible.
        """
        self._last_telemetry_monotonic = now_monotonic
        self.history.append((now_monotonic, telemetry.fps_p05))
        if len(self.history) > 600:
            del self.history[:300]

        target = float(QUALITY_PROFILES[self.profile]["target_fps"])
        starving = telemetry.fps_p05 < target * SHED_FPS_RATIO
        tight = (
            telemetry.gl_memory_mb is not None
            and telemetry.gl_memory_mb > self.gl_memory_ceiling_mb
        )

        if tight:
            return self._step_down("gl memory above ceiling")
        if starving:
            if self._below_target_since is None:
                self._below_target_since = now_monotonic
            elif now_monotonic - self._below_target_since >= SHED_WINDOW_SECONDS:
                self._below_target_since = None
                return self._step_down("fps_p05 below target")
        else:
            self._below_target_since = None
        return None

    def _step_down(self, reason: str) -> str | None:
        order = ("high", "balanced", "low")
        current = order.index(self.profile)
        if current >= len(order) - 1:
            return None
        self.profile = order[current + 1]
        self._shed_reason = reason
        return self.profile

    def needs_resync(self, telemetry: RendererTelemetryV1, current_sequence: int) -> bool:
        return current_sequence - telemetry.last_command_sequence > RESYNC_LAG_THRESHOLD


def encode(commands: list[dict[str, Any]]) -> str:
    """One frame of commands, as a JSON array. Separators are compact on purpose."""
    return json.dumps(commands, separators=(",", ":"))


def decode_telemetry(payload: str | dict[str, Any]) -> RendererTelemetryV1:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    return RendererTelemetryV1.model_validate(raw)


__all__ = [
    "PROTOCOL_VERSION",
    "QUALITY_PROFILES",
    "RESYNC_LAG_THRESHOLD",
    "SHED_FPS_RATIO",
    "SHED_WINDOW_SECONDS",
    "TARGET_FPS",
    "TARGET_HEIGHT",
    "TARGET_WIDTH",
    "TELEMETRY_TIMEOUT_SECONDS",
    "QualityController",
    "action_command",
    "camera_command",
    "decode_telemetry",
    "encode",
    "gaze_command",
    "resync_commands",
    "rhythm_command",
    "screen_command",
]
