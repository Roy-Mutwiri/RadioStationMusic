"""Camera metadata and state. **A stub, deliberately.**

The camera *director* is a later phase. What exists now is the data it will need and a
state object the rest of the system can read, so that behaviour, telemetry and the
placeholder renderer can all refer to "the active camera" without that meaning anything
yet.

Two things make this worth building now rather than later:

* Several action specs carry ``camera_affinity``, and the narrative bonus that uses it
  needs somewhere to live. Declaring the metadata now keeps the affinity data honest —
  `tests/unit/test_visual_catalog.py` checks every affinity names a real camera.
* The seven transforms are frozen V1 geometry. Loading them through the blockout rather
  than restating them here is what keeps the freeze meaningful.

What this does **not** do: choose cameras, cut, hold, or veto. :meth:`CameraState.hold`
advances a timer and nothing else. The hold distributions and the five cut vetoes are
specified in `docs/visual/CAMERA_PLAN.md` §5 and are not implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from tradefix_radio.visual.contracts import CharacterState, IntensityBand
from tradefix_radio.visual.geometry import Blockout, CameraTransform

#: Transition styles. Hard cut is the default because it is what a real multi-camera
#: production uses, and the only transition that is invisible when it is right.
TRANSITION_CUT: Final = "cut"
TRANSITION_CROSSFADE: Final = "crossfade"
TRANSITION_PUSH_CONTINUE: Final = "push_continue"


@dataclass(frozen=True, slots=True)
class CameraMetadata:
    """Scheduling properties of one composition, from `CAMERA_PLAN.md`.

    ``parallax_amplitude`` is the one field with a runtime consumer today: the
    placeholder renderer uses it to offset layers by depth, which is the only way the
    slow drift reads as a camera rather than as a zoom.
    """

    camera_id: str
    minimum_hold_seconds: float
    maximum_hold_seconds: float
    #: Weight multiplier per intensity band. Breakouts favour the close and
    #: over-shoulder compositions; quiet favours the wide and medium ones.
    market_affinity: dict[IntensityBand, float]
    #: Weight multiplier per character state — the narrative bonus.
    behavior_affinity: dict[CharacterState, float]
    transition: str
    #: Fraction of frame width the layers may travel. Sub-perceptual by design.
    parallax_amplitude: float
    #: Ceiling on this camera's share of any rolling hour.
    hour_share_cap: float
    #: Whether a slow push-in is permitted. Forbidden on the hands shot, where it
    #: amplifies exactly the contact errors that composition is most exposed to.
    allows_push_in: bool


def _affinity(*pairs: tuple[IntensityBand, float]) -> dict[IntensityBand, float]:
    return dict(pairs)


B = IntensityBand
CS = CharacterState

#: The seven compositions' scheduling metadata. Values from `CAMERA_PLAN.md` §3 and §5.
CAMERA_METADATA: Final[dict[str, CameraMetadata]] = {
    "CAM_1": CameraMetadata(
        camera_id="CAM_1",
        minimum_hold_seconds=50.0,
        maximum_hold_seconds=240.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 1.3), (B.B1_QUIET, 1.2), (B.B2_STEADY, 1.1),
            (B.B3_FOCUSED, 1.1), (B.B4_ALERT, 1.1), (B.B5_PEAK, 1.0),
        ),
        behavior_affinity={CS.MARKET_REACTION: 1.4, CS.IDLE_FOCUS: 1.1},
        transition=TRANSITION_CUT,
        parallax_amplitude=0.008,
        # The home shot carries the largest share. Primacy is enforced by the camera
        # director's return-to-master bias; this cap is what stops it becoming the
        # *only* shot.
        hour_share_cap=0.34,
        allows_push_in=True,
    ),
    "CAM_2": CameraMetadata(
        camera_id="CAM_2",
        minimum_hold_seconds=55.0,
        maximum_hold_seconds=200.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 1.2), (B.B1_QUIET, 1.3), (B.B2_STEADY, 1.0),
            (B.B3_FOCUSED, 0.8), (B.B4_ALERT, 0.6), (B.B5_PEAK, 0.5),
        ),
        behavior_affinity={CS.WAITING: 1.3, CS.CAFFEINE_BREAK: 1.2},
        transition=TRANSITION_CROSSFADE,
        parallax_amplitude=0.006,
        hour_share_cap=0.15,
        allows_push_in=False,
    ),
    "CAM_3": CameraMetadata(
        camera_id="CAM_3",
        minimum_hold_seconds=45.0,
        maximum_hold_seconds=150.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 0.4), (B.B1_QUIET, 0.7), (B.B2_STEADY, 1.0),
            (B.B3_FOCUSED, 1.4), (B.B4_ALERT, 1.9), (B.B5_PEAK, 2.1),
        ),
        behavior_affinity={CS.ANALYZING: 2.0, CS.EXECUTING: 1.6},
        transition=TRANSITION_CUT,
        parallax_amplitude=0.004,
        hour_share_cap=0.20,
        allows_push_in=False,
    ),
    "CAM_4": CameraMetadata(
        camera_id="CAM_4",
        minimum_hold_seconds=35.0,
        maximum_hold_seconds=75.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 0.5), (B.B1_QUIET, 0.8), (B.B2_STEADY, 1.0),
            (B.B3_FOCUSED, 1.2), (B.B4_ALERT, 1.5), (B.B5_PEAK, 1.5),
        ),
        behavior_affinity={CS.MARKET_REACTION: 2.5, CS.ANALYZING: 1.2},
        transition=TRANSITION_CUT,
        parallax_amplitude=0.003,
        hour_share_cap=0.08,
        allows_push_in=True,
    ),
    "CAM_5": CameraMetadata(
        camera_id="CAM_5",
        minimum_hold_seconds=70.0,
        maximum_hold_seconds=300.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 1.6), (B.B1_QUIET, 1.4), (B.B2_STEADY, 1.0),
            (B.B3_FOCUSED, 0.7), (B.B4_ALERT, 0.5), (B.B5_PEAK, 0.4),
        ),
        behavior_affinity={CS.CAFFEINE_BREAK: 1.8, CS.WAITING: 1.2},
        transition=TRANSITION_CROSSFADE,
        parallax_amplitude=0.010,
        hour_share_cap=0.18,
        allows_push_in=False,
    ),
    "CAM_6": CameraMetadata(
        camera_id="CAM_6",
        minimum_hold_seconds=30.0,
        maximum_hold_seconds=50.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 0.5), (B.B1_QUIET, 0.9), (B.B2_STEADY, 1.0),
            (B.B3_FOCUSED, 1.2), (B.B4_ALERT, 1.3), (B.B5_PEAK, 1.2),
        ),
        behavior_affinity={CS.EXECUTING: 3.0, CS.NOTE_TAKING: 3.0, CS.CAFFEINE_BREAK: 2.5},
        transition=TRANSITION_CUT,
        parallax_amplitude=0.002,
        hour_share_cap=0.06,
        allows_push_in=False,
    ),
    "CAM_7": CameraMetadata(
        camera_id="CAM_7",
        minimum_hold_seconds=55.0,
        maximum_hold_seconds=300.0,
        market_affinity=_affinity(
            (B.B0_DORMANT, 1.3), (B.B1_QUIET, 1.2), (B.B2_STEADY, 1.1),
            (B.B3_FOCUSED, 1.1), (B.B4_ALERT, 1.0), (B.B5_PEAK, 0.9),
        ),
        behavior_affinity={CS.IDLE_FOCUS: 1.3, CS.MARKET_REACTION: 1.3},
        transition=TRANSITION_CUT,
        parallax_amplitude=0.007,
        # The second hero. Generous, but below CAM_1 — the stream has one home shot, and
        # two cameras with near-equal large shares is what makes a rotation visible.
        hour_share_cap=0.24,
        allows_push_in=True,
    ),
}

#: Human-readable shot names, matching `CAMERA_PLAN.md` §3. Used by the HUD and the demo
#: control panel, so a viewer reads "HANDS / DESK" rather than "CAM_6".
CAMERA_NAMES: Final[dict[str, str]] = {
    "CAM_1": "HERO FRONT",
    "CAM_2": "SIDE PROFILE",
    "CAM_3": "OVER SHOULDER",
    "CAM_4": "FACE CLOSE-UP",
    "CAM_5": "WIDE OFFICE",
    "CAM_6": "HANDS / DESK",
    "CAM_7": "THREE-QUARTER HERO",
}

#: The home shot. What should be on screen when someone arrives for the first time.
#:
#: `CAM_1`, the hero front — the composition every plate is graded against
#: (`CAMERA_PLAN.md` §3) and the one the brief requires to remain primary. The earlier
#: choice of `CAM_7` made the three-quarter the home shot, which measured as `CAM_7`
#: 22.1 % / `CAM_1` 15.9 % of airtime over two hours: a stream with no clear master.
DEFAULT_CAMERA: Final = "CAM_1"


@dataclass(slots=True)
class CameraState:
    """Which composition is live, and for how long. **Stub.**

    Deliberately has no `select` method. Automatic selection, the hold distributions and
    the five cut vetoes belong to the camera director, which is a later phase. Calling
    :meth:`cut_to` from an operator control is the only way the camera changes today.
    """

    camera_id: str = DEFAULT_CAMERA
    since_monotonic: float = 0.0
    auto: bool = False
    transition: str = TRANSITION_CUT
    #: Cameras used in this session, newest last. Feeds the later anti-repeat rule.
    recent: list[str] = field(default_factory=list)

    def hold_seconds(self, now_monotonic: float) -> float:
        return max(0.0, now_monotonic - self.since_monotonic)

    def metadata(self) -> CameraMetadata:
        return CAMERA_METADATA[self.camera_id]

    def transform(self, blockout: Blockout) -> CameraTransform:
        return blockout.camera(self.camera_id)

    def cut_to(
        self, camera_id: str, now_monotonic: float, *, transition: str = TRANSITION_CUT
    ) -> None:
        if camera_id not in CAMERA_METADATA:
            raise KeyError(
                f"unknown camera {camera_id!r}; the seven frozen V1 cameras are "
                f"{', '.join(sorted(CAMERA_METADATA))}"
            )
        if camera_id == self.camera_id:
            return
        self.recent.append(self.camera_id)
        if len(self.recent) > 12:
            del self.recent[:-12]
        self.camera_id = camera_id
        self.since_monotonic = now_monotonic
        self.transition = transition

    def past_minimum_hold(self, now_monotonic: float) -> bool:
        return self.hold_seconds(now_monotonic) >= self.metadata().minimum_hold_seconds

    def past_maximum_hold(self, now_monotonic: float) -> bool:
        return self.hold_seconds(now_monotonic) >= self.metadata().maximum_hold_seconds


def validate_metadata(blockout: Blockout) -> list[str]:
    """Check the metadata against frozen geometry. Returns problems rather than raising."""
    problems: list[str] = []
    for camera_id, meta in CAMERA_METADATA.items():
        if camera_id not in blockout.cameras:
            problems.append(f"{camera_id} has metadata but is not in the blockout")
            continue
        if meta.minimum_hold_seconds >= meta.maximum_hold_seconds:
            problems.append(f"{camera_id}: minimum hold is not below maximum")
        if meta.minimum_hold_seconds < 30.0:
            problems.append(
                f"{camera_id}: minimum hold {meta.minimum_hold_seconds}s is below the "
                "absolute 30s floor; no market condition justifies faster cutting"
            )
        # Share caps are scheduling, not geometry, and `GEOMETRY_FREEZE.md` §5 declares
        # them tunable. A cap recorded in the blockout therefore has to be rejected
        # rather than cross-checked: cross-checking it meant every tuning pass had to
        # edit the frozen artefact, which is how a freeze quietly stops meaning anything.
        # CR-003 removed the one that existed.
        declared = blockout.cameras[camera_id].hour_share_cap
        if declared is not None:
            problems.append(
                f"{camera_id}: the blockout declares hour_share_cap {declared}. Share "
                "caps are scheduling metadata and live in CAMERA_METADATA; the blockout "
                "carries geometry only (GEOMETRY_FREEZE.md §5)"
            )
    for camera_id in blockout.cameras:
        if camera_id not in CAMERA_METADATA:
            problems.append(f"{camera_id} is in the blockout but has no metadata")
    return problems


__all__ = [
    "CAMERA_METADATA",
    "CAMERA_NAMES",
    "DEFAULT_CAMERA",
    "TRANSITION_CROSSFADE",
    "TRANSITION_CUT",
    "TRANSITION_PUSH_CONTINUE",
    "CameraMetadata",
    "CameraState",
    "validate_metadata",
]
