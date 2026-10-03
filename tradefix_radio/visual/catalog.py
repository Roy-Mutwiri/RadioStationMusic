"""The action catalogue and the interaction chains.

Every behaviour TF_TRADER_01 can perform, as data. This module is the single place
tuning lives (§71: no magic numbers scattered through logic) and the single place a
behaviour is defined — the director contains no action names at all.

Two things are deliberately absent:

**No regime names.** Market influence is expressed only as a :class:`BandBiasV1`, six
multipliers against the intensity bands. A fifteenth market regime is a one-line change
in the bridge and touches nothing here, which is the seam ADR-11 draws.

**No constants in the scheduler.** Durations, cooldowns and blends are ranges, sampled
per execution. A fixed cooldown is a visible period, and over an eight-hour stream a
visible period is the thing viewers notice first.

Chain steps
-----------
Object interactions are **transactional**, not sequences of independent random actions.
`COFFEE_DRINK` is one unit: once the hand closes on the mug, every remaining step runs.
Its steps exist as specs so the renderer has something concrete to perform and the debug
timeline has something to print, but they carry ``chain_only=True`` and the scheduler
refuses to pick them on their own — a `sip` scheduled without a preceding `pick_cup` is
a mug teleporting to his mouth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tradefix_radio.visual.contracts import (
    ActionCategory,
    ActionSpecV1,
    BandBiasV1,
    CharacterState,
    GazeTarget,
    InteractionLock,
    Interruptibility,
)

CS = CharacterState
AC = ActionCategory
IL = InteractionLock
IN = Interruptibility
GT = GazeTarget

# ============================================================ tunable constants
#
# Centralised so a behaviour tuning pass is one file and one diff. The values are the
# V1 motion library's, with the brief's worked examples reproduced exactly where it
# gave them (blink 2-8 s, double blink 25-120 s, coffee 8-30 min, headphones 4-20 min,
# posture reset 5-25 min, note writing 2-15 min).

#: Jitter applied to every sampled amplitude, so the same action is never the same size
#: twice. Narrow on purpose: wider reads as inconsistent rather than as alive.
AMPLITUDE_JITTER: Final = (0.88, 1.12)

#: Group cooldowns, as sampled **ranges** in seconds, shared across every action with
#: the same anti-repeat key. Without these the scheduler can satisfy four separate
#: headphone cooldowns in sequence and produce a man fiddling with his headphones for a
#: minute.
#:
#: Ranges rather than a fixed value times a jitter factor, and the headphone row is why.
#: It was 150 s, which never bound at all — every headphone action already carried a
#: 240 s per-action floor — so the group rule did nothing and a quiet half-hour produced
#: 18 headphone adjustments per hour against the brief's "4-20 minutes". These are now
#: the brief's intervals directly, and they are the binding constraint.
GROUP_COOLDOWN_SECONDS: Final[dict[str, tuple[float, float]]] = {
    "headphones": (240.0, 1200.0),
    "fatigue": (300.0, 1500.0),
    "coffee": (480.0, 1800.0),
    "note": (120.0, 900.0),
    "posture": (420.0, 1800.0),
    "posture_minor": (210.0, 720.0),
    "lean": (60.0, 180.0),
    "smirk": (900.0, 3600.0),
}

#: The stable working states body maintenance may occur in.
#:
#: Deliberately **not** POSTURE_RESET alone. Tying the family to a single short-dwell
#: state is what starved it: the state is entered rarely and exits in 3-10 s, often before
#: the next action consideration fires. A man shifts in his chair while reading a chart,
#: not only during a dedicated interlude.
_POSTURE_STATES: Final = (
    CS.IDLE_FOCUS,
    CS.WAITING,
    CS.ANALYZING,
    CS.POSTURE_RESET,
)

#: Interval between *major* posture resets, sampled. The brief: several per hour, not
#: dozens. A median near 10 minutes gives roughly 5-6 an hour across the family.
POSTURE_MAINTENANCE_INTERVAL_SECONDS: Final = (420.0, 1_020.0)

#: Minor members (elbow, hand rest) are admissible on a shorter gate — they are small
#: adjustments rather than events, and holding them to the major interval makes the hands
#: look glued down between resets.
POSTURE_MINOR_INTERVAL_SECONDS: Final = (210.0, 540.0)

#: Hard ceilings on a category's share of any rolling hour. The brief's "same category
#: domination" rule, as arithmetic.
CATEGORY_HOUR_SHARE_CEILING: Final[dict[ActionCategory, float]] = {
    AC.MICRO: 0.62,
    AC.WORK: 0.48,
    AC.POSTURE: 0.06,
    AC.HEADPHONES: 0.04,
    AC.CAFFEINE: 0.08,
    AC.FATIGUE: 0.04,
    AC.REACTION: 0.06,
    AC.MUSIC: 0.12,
}


def _spec(
    action_id: str,
    category: ActionCategory,
    duration_ms: tuple[int, int],
    cooldown_s: tuple[float, float],
    *,
    blend: tuple[int, int] = (150, 220),
    weight: float = 1.0,
    locks: tuple[InteractionLock, ...] = (),
    interrupt: Interruptibility = IN.ALWAYS,
    bias: BandBiasV1 | None = None,
    music_bias: float = 0.0,
    states: tuple[CharacterState, ...] = (),
    anchor: str | None = None,
    gaze: GazeTarget | None = None,
    cameras: tuple[str, ...] = (),
    cost: float = 0.1,
    tags: tuple[str, ...] = (),
    key: str | None = None,
    chain_only: bool = False,
    min_fatigue: float = 0.0,
    max_hour_share: float | None = None,
) -> ActionSpecV1:
    """Terse constructor. The catalogue is data and should read as a table."""
    return ActionSpecV1(
        action_id=action_id,
        category=category,
        duration_ms=duration_ms,
        blend_in_ms=blend[0],
        blend_out_ms=blend[1],
        interruptibility=interrupt,
        interaction_locks=locks,
        cooldown_range_seconds=cooldown_s,
        weight=weight,
        market_bias=bias or BandBiasV1(),
        music_bias=music_bias,
        allowed_character_states=states,
        required_anchor=anchor,
        gaze_target=gaze,
        camera_affinity=cameras,
        energy_cost=cost,
        tags=tags,
        anti_repeat_key=key or action_id,
        chain_only=chain_only,
        min_fatigue_phase=min_fatigue,
        max_hour_share=max_hour_share,
    )


# ============================================================ MICRO
#
# Small, frequent, interruptible. The texture layer, and the majority of all events.

_MICRO: Final = (
    # Blinks are scheduled by the dedicated generator in `gaze.py`, not by the action
    # scheduler, because their interval distribution is log-normal and their suppression
    # is driven by concentration. They appear here so they have specs the renderer can
    # perform and the timeline can print.
    _spec("blink", AC.MICRO, (110, 150), (2.0, 8.0), blend=(0, 0), weight=0.0,
          key="blink", tags=("reflex",), cost=0.0),
    _spec("double_blink", AC.MICRO, (380, 510), (25.0, 120.0), blend=(0, 0), weight=0.0,
          key="blink", tags=("reflex",), cost=0.0),
    _spec("slow_blink", AC.MICRO, (280, 380), (45.0, 240.0), blend=(0, 0), weight=0.0,
          key="blink", tags=("reflex", "fatigue_coupled"), cost=0.0),

    _spec("eye_left", AC.MICRO, (600, 1400), (4.0, 14.0), blend=(90, 140),
          bias=BandBiasV1.ramp(0.6, 1.7), gaze=GT.MONITOR_LEFT, key="eye_dart"),
    _spec("eye_right", AC.MICRO, (600, 1400), (4.0, 14.0), blend=(90, 140),
          bias=BandBiasV1.ramp(0.6, 1.7), gaze=GT.MONITOR_RIGHT, key="eye_dart"),
    _spec("eye_down", AC.MICRO, (500, 1100), (8.0, 26.0), blend=(90, 140), weight=0.7,
          bias=BandBiasV1(b0=0.8, b1=1.0, b2=1.0, b3=1.0, b4=0.9, b5=0.8),
          gaze=GT.KEYBOARD, key="eye_dart"),
    _spec("eye_main_monitor", AC.MICRO, (300, 600), (2.0, 6.0), blend=(80, 120),
          weight=1.6, gaze=GT.MONITOR_MAIN, key="eye_recentre"),

    _spec("micro_brow_raise", AC.MICRO, (400, 900), (15.0, 60.0), blend=(120, 200),
          weight=0.6, bias=BandBiasV1.ramp(0.4, 1.5), key="brow"),
    _spec("micro_frown", AC.MICRO, (500, 1200), (20.0, 75.0), blend=(150, 240),
          weight=0.5, bias=BandBiasV1.ramp(0.3, 1.6), key="brow"),
    _spec("small_head_tilt", AC.MICRO, (900, 2200), (20.0, 70.0), blend=(250, 380),
          weight=0.9, locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN, key="head_idle"),
    _spec("small_head_turn", AC.MICRO, (700, 1800), (18.0, 65.0), blend=(220, 340),
          weight=0.9, locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN, key="head_idle"),
    # Keyed to itself, not "posture". Sharing the key with `posture_reset` meant
    # this action — which fires every 35-130 s — armed the 5-25 minute group cooldown
    # and starved the major reset completely: zero occurrences across two simulated
    # hours. A frequent member and a rare member of the same family need separate keys.
    _spec("shoulder_shift", AC.MICRO, (800, 1700), (35.0, 130.0), blend=(260, 360),
          weight=0.8, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          key="shoulder_shift", cost=0.2),
    _spec("finger_tap", AC.MICRO, (500, 1600), (12.0, 50.0), blend=(90, 150),
          weight=0.7, bias=BandBiasV1(b0=0.5, b1=0.9, b2=1.0, b3=1.1, b4=1.2, b5=1.1),
          key="finger"),
    _spec("hand_reposition", AC.MICRO, (600, 1300), (25.0, 85.0), blend=(160, 240),
          weight=0.7, key="hand_idle"),
)

# ============================================================ WORK
#
# The bulk of visible activity. Most hold a lock; most override gaze.

_WORK: Final = (
    _spec("mouse_move", AC.WORK, (700, 2200), (6.0, 30.0), blend=(160, 220), weight=1.4,
          locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.3, 1.9), anchor="ANCHOR_MOUSE", gaze=GT.MONITOR_MAIN,
          cameras=("CAM_6", "CAM_7"), cost=0.4, key="mouse"),
    _spec("mouse_click", AC.WORK, (180, 320), (4.0, 22.0), blend=(60, 90), weight=1.1,
          locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.2, 1.8), anchor="ANCHOR_MOUSE", cost=0.2, key="mouse"),
    _spec("mouse_double_click", AC.WORK, (320, 480), (25.0, 120.0), blend=(60, 90),
          weight=0.5, locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.1, 1.4), anchor="ANCHOR_MOUSE", cost=0.2, key="mouse"),
    _spec("mouse_scroll", AC.WORK, (600, 1800), (10.0, 45.0), blend=(110, 170),
          weight=0.9, locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.2, 1.4), anchor="ANCHOR_MOUSE", gaze=GT.MONITOR_MAIN,
          cost=0.3, key="mouse"),

    _spec("typing_short", AC.WORK, (1000, 4000), (10.0, 60.0), blend=(140, 200),
          weight=1.5, locks=(IL.BOTH_HANDS, IL.KEYBOARD), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.2, 1.8), anchor="ANCHOR_KEYBOARD_HOME_R",
          gaze=GT.MONITOR_MAIN, cameras=("CAM_6",), cost=0.6, key="typing"),
    _spec("typing_medium", AC.WORK, (4000, 7500), (45.0, 200.0), blend=(160, 240),
          weight=0.9, locks=(IL.BOTH_HANDS, IL.KEYBOARD), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.2, 1.6), anchor="ANCHOR_KEYBOARD_HOME_R",
          gaze=GT.MONITOR_MAIN, cameras=("CAM_6",), cost=0.9, key="typing"),
    _spec("typing_long", AC.WORK, (7500, 14000), (90.0, 420.0), blend=(180, 280),
          weight=0.6, locks=(IL.BOTH_HANDS, IL.KEYBOARD), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=0.1, b1=0.5, b2=1.0, b3=1.3, b4=1.4, b5=1.2),
          anchor="ANCHOR_KEYBOARD_HOME_R", gaze=GT.MONITOR_MAIN, cameras=("CAM_6",),
          cost=1.4, key="typing"),
    _spec("hotkey", AC.WORK, (250, 500), (30.0, 150.0), blend=(70, 110), weight=0.6,
          locks=(IL.LEFT_HAND, IL.KEYBOARD), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.1, 1.9), anchor="ANCHOR_KEYBOARD_HOME_L",
          cost=0.2, key="typing"),

    _spec("chart_pan", AC.WORK, (1400, 3200), (60.0, 260.0), blend=(200, 280),
          weight=0.6, locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=None, b1=0.5, b2=1.0, b3=1.3, b4=1.5, b5=1.4),
          anchor="ANCHOR_MOUSE", gaze=GT.MONITOR_MAIN, cost=0.5, key="chart"),
    _spec("chart_inspect", AC.WORK, (2500, 7000), (25.0, 110.0), blend=(300, 400),
          weight=1.4, locks=(IL.HEAD,), interrupt=IN.ALWAYS,
          bias=BandBiasV1(b0=0.3, b1=0.9, b2=1.1, b3=1.5, b4=1.7, b5=1.6),
          gaze=GT.MONITOR_MAIN, cameras=("CAM_3", "CAM_4"), cost=0.5, key="chart"),
    _spec("monitor_left_glance", AC.WORK, (700, 1500), (15.0, 70.0), blend=(180, 250),
          weight=1.1, locks=(IL.HEAD,), bias=BandBiasV1.ramp(0.4, 1.8),
          gaze=GT.MONITOR_LEFT, cost=0.2, key="monitor_switch"),
    _spec("monitor_right_glance", AC.WORK, (700, 1500), (15.0, 70.0), blend=(180, 250),
          weight=1.1, locks=(IL.HEAD,), bias=BandBiasV1.ramp(0.4, 1.8),
          gaze=GT.MONITOR_RIGHT, cost=0.2, key="monitor_switch"),

    _spec("lean_forward", AC.WORK, (1200, 2600), (30.0, 140.0), blend=(380, 520),
          weight=1.0, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1.ramp(0.2, 2.1), gaze=GT.MONITOR_MAIN,
          cameras=("CAM_1", "CAM_7"), cost=0.5, key="lean"),
    _spec("lean_back", AC.WORK, (1400, 3000), (45.0, 200.0), blend=(420, 600),
          weight=0.9, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=1.0, b1=1.2, b2=1.0, b3=0.9, b4=0.8, b5=0.9),
          cost=0.4, key="lean"),
    _spec("hand_to_chin", AC.WORK, (3000, 9000), (120.0, 480.0), blend=(420, 560),
          weight=0.7, locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=0.6, b1=1.2, b2=1.1, b3=1.0, b4=0.7, b5=0.5),
          gaze=GT.MONITOR_MAIN, cameras=("CAM_4",), cost=0.3, key="hand_face"),
    _spec("hand_to_mouth", AC.WORK, (2500, 7000), (150.0, 540.0), blend=(400, 540),
          weight=0.5, locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=0.5, b1=1.0, b2=1.1, b3=1.1, b4=0.8, b5=0.6),
          gaze=GT.MONITOR_MAIN, cameras=("CAM_4",), cost=0.3, key="hand_face"),

    # The user-facing note intents. Both are chain entry points — see `_CHAINS`.
    _spec("note_write_short", AC.WORK, (2500, 5000), (120.0, 480.0), blend=(350, 480),
          weight=0.8, locks=(IL.RIGHT_HAND, IL.PEN), interrupt=IN.NEVER,
          bias=BandBiasV1(b0=0.4, b1=1.2, b2=1.1, b3=1.1, b4=1.0, b5=0.8),
          anchor="ANCHOR_NOTEBOOK", gaze=GT.NOTEBOOK, cameras=("CAM_6",),
          cost=0.7, key="note"),
    _spec("note_write_long", AC.WORK, (5000, 11000), (300.0, 900.0), blend=(350, 480),
          weight=0.4, locks=(IL.RIGHT_HAND, IL.PEN), interrupt=IN.NEVER,
          bias=BandBiasV1(b0=0.4, b1=1.3, b2=1.0, b3=0.9, b4=0.6, b5=0.4),
          anchor="ANCHOR_NOTEBOOK", gaze=GT.NOTEBOOK, cameras=("CAM_6",),
          cost=1.1, key="note"),
    _spec("watch_check", AC.WORK, (1100, 2000), (300.0, 1200.0), blend=(250, 340),
          weight=0.4, locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=1.2, b1=1.1, b2=1.0, b3=0.9, b4=0.7, b5=0.6),
          cameras=("CAM_2",), cost=0.2, key="watch"),
)

# ============================================================ HEADPHONES

_HEADPHONES: Final = (
    _spec("adjust_left", AC.HEADPHONES, (1300, 2400), (240.0, 1200.0), blend=(280, 380),
          weight=0.6, locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          anchor="ANCHOR_HP_CUP_L", cost=0.3, key="headphones", max_hour_share=0.02),
    _spec("adjust_right", AC.HEADPHONES, (1300, 2400), (240.0, 1200.0), blend=(280, 380),
          weight=0.5, locks=(IL.RIGHT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          anchor="ANCHOR_HP_CUP_R", cost=0.3, key="headphones", max_hour_share=0.02),
    _spec("press_earcup", AC.HEADPHONES, (900, 1600), (300.0, 1500.0), blend=(220, 300),
          weight=0.4, locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN,
          anchor="ANCHOR_HP_CUP_L", cost=0.2, key="headphones", max_hour_share=0.015),
    _spec("settle_headphones", AC.HEADPHONES, (1100, 1900), (420.0, 1800.0),
          blend=(260, 360), weight=0.5, locks=(IL.BOTH_HANDS,), interrupt=IN.AFTER_BLEND_IN,
          anchor="ANCHOR_HP_BAND", cost=0.3, key="headphones", max_hour_share=0.015),
)

# ============================================================ CAFFEINE
#
# Every step except `coffee_reset` is chain-only. The chain is the unit.

_CAFFEINE: Final = (
    _spec("reach_cup", AC.CAFFEINE, (600, 1000), (0.0, 0.0), blend=(220, 160),
          locks=(IL.LEFT_HAND,), interrupt=IN.AFTER_BLEND_IN, anchor="ANCHOR_MUG_BODY",
          gaze=GT.COFFEE, chain_only=True, cost=0.2, key="coffee"),
    _spec("pick_cup", AC.CAFFEINE, (400, 700), (0.0, 0.0), blend=(120, 100),
          locks=(IL.LEFT_HAND, IL.COFFEE), interrupt=IN.NEVER, anchor="ANCHOR_MUG_BODY",
          gaze=GT.COFFEE, chain_only=True, cost=0.2, key="coffee"),
    _spec("sip", AC.CAFFEINE, (1200, 2400), (0.0, 0.0), blend=(180, 180),
          locks=(IL.LEFT_HAND, IL.COFFEE), interrupt=IN.NEVER, anchor="ANCHOR_MUG_LIP",
          chain_only=True, cost=0.3, key="coffee", cameras=("CAM_4", "CAM_6")),
    _spec("hold_cup", AC.CAFFEINE, (800, 3000), (0.0, 0.0), blend=(140, 140),
          locks=(IL.LEFT_HAND, IL.COFFEE), interrupt=IN.NEVER, anchor="ANCHOR_MUG_BODY",
          gaze=GT.MONITOR_MAIN, chain_only=True, cost=0.2, key="coffee"),
    _spec("place_cup", AC.CAFFEINE, (500, 900), (0.0, 0.0), blend=(140, 220),
          locks=(IL.LEFT_HAND, IL.COFFEE), interrupt=IN.NEVER, anchor="ANCHOR_MUG_RING",
          gaze=GT.COFFEE, chain_only=True, cost=0.2, key="coffee"),
    # The trip to the coffee zone. Entry point, not chain-only.
    _spec("coffee_reset", AC.CAFFEINE, (18000, 34000), (2100.0, 5400.0),
          blend=(600, 800), weight=0.3, locks=(IL.BOTH_HANDS, IL.UPPER_BODY),
          interrupt=IN.NEVER, bias=BandBiasV1(b0=1.3, b1=1.2, b2=1.0, b3=0.8, b4=0.4, b5=0.2),
          states=(CS.CAFFEINE_BREAK,), cameras=("CAM_5",), cost=2.0, key="coffee",
          max_hour_share=0.04),
)

# ============================================================ FATIGUE
#
# Used sparingly. The brief's constraint is the specification: intense, not exhausted.
# Every one is gated by the fatigue phase and chains `refocus` on exit, which is what
# makes the family read as discipline rather than as decline.

_FATIGUE: Final = (
    _spec("neck_stretch", AC.FATIGUE, (2000, 3600), (600.0, 1800.0), blend=(420, 560),
          weight=0.4, locks=(IL.HEAD, IL.UPPER_BODY), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=1.2, b1=1.1, b2=1.0, b3=0.8, b4=0.5, b5=0.3),
          cost=0.4, key="fatigue", min_fatigue=0.35, max_hour_share=0.015),
    _spec("deep_exhale", AC.FATIGUE, (1600, 2800), (420.0, 1500.0), blend=(350, 480),
          weight=0.5, bias=BandBiasV1(b0=1.2, b1=1.1, b2=1.0, b3=0.9, b4=0.6, b5=0.4),
          cost=0.2, key="fatigue", min_fatigue=0.35, max_hour_share=0.015),
    _spec("brief_head_down", AC.FATIGUE, (1500, 2800), (1200.0, 3600.0), blend=(380, 500),
          weight=0.2, locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN,
          bias=BandBiasV1(b0=1.1, b1=1.0, b2=0.9, b3=0.6, b4=0.3, b5=0.15),
          gaze=GT.NOTEBOOK, cost=0.3, key="fatigue", min_fatigue=0.55, max_hour_share=0.01),
    # Chained after every fatigue action. This is the mechanism that keeps the family
    # reading as "tired for two seconds, then back to work".
    _spec("refocus", AC.FATIGUE, (500, 900), (0.0, 0.0), blend=(120, 180),
          interrupt=IN.NEVER, gaze=GT.MONITOR_MAIN, chain_only=True, cost=0.1,
          key="fatigue"),
)

# ============================================================ POSTURE
#
# Long-horizon body maintenance. A family of seven, not one animation.
#
# The V6 soak found the single `posture_reset` action firing **zero** times across two
# simulated hours, and the cause was structural rather than a weight that needed raising:
# it existed only inside the short-dwell POSTURE_RESET state, it sat in the FATIGUE
# category behind a fatigue ramp a quiet market never reached, and it shared an
# anti-repeat key with a micro movement that fires every ninety seconds.
#
# Three things changed. This is its own category, so no fatigue gate applies. Every
# member is admissible from the stable working states, so it does not depend on a state
# being entered. And eligibility is governed by a dedicated maintenance gate in the
# director — time since the last major posture change, no object chain, no reaction —
# which is what "long-horizon" actually means.
#
# Target: a few per hour. Not dozens, and not zero.

_POSTURE: Final = (
    _spec("chair_reposition", AC.POSTURE, (1800, 3400), (900.0, 3600.0), blend=(480, 620),
          weight=1.0, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, cameras=("CAM_2", "CAM_5"), cost=0.5,
          key="posture", tags=("body_maintenance", "major"), max_hour_share=0.02),
    _spec("spine_straighten", AC.POSTURE, (1600, 3000), (900.0, 3600.0), blend=(460, 600),
          weight=1.1, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, cameras=("CAM_1", "CAM_2", "CAM_7"), cost=0.4,
          key="posture", tags=("body_maintenance", "major"), max_hour_share=0.02),
    _spec("posture_lean_back", AC.POSTURE, (2400, 4800), (900.0, 3600.0), blend=(520, 680),
          weight=0.9, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, gaze=GT.MIDDLE_DISTANCE,
          cameras=("CAM_1", "CAM_2", "CAM_7"), cost=0.4,
          key="posture", tags=("body_maintenance", "major"), max_hour_share=0.02),
    _spec("shoulder_roll", AC.POSTURE, (1600, 2900), (900.0, 3600.0), blend=(360, 480),
          weight=1.0, locks=(IL.UPPER_BODY,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, cameras=("CAM_1", "CAM_2"), cost=0.3,
          key="posture", tags=("body_maintenance", "major"), max_hour_share=0.02),
    _spec("neck_reset", AC.POSTURE, (1400, 2600), (900.0, 3600.0), blend=(400, 540),
          weight=1.0, locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, cameras=("CAM_2", "CAM_4"), cost=0.3,
          key="posture", tags=("body_maintenance", "major"), max_hour_share=0.02),
    # The two minor members. Shorter, less disruptive, and admissible more often — a
    # family where every member is a major event reads as a man who cannot settle.
    _spec("elbow_reposition", AC.POSTURE, (900, 1800), (420.0, 1500.0), blend=(280, 380),
          weight=0.8, locks=(IL.BOTH_HANDS,), interrupt=IN.AFTER_BLEND_IN,
          states=_POSTURE_STATES, anchor="ANCHOR_KEYBOARD_HOME_R", cameras=("CAM_6",),
          cost=0.2, key="posture_minor", tags=("body_maintenance", "minor"),
          max_hour_share=0.025),
    _spec("hand_rest_reset", AC.POSTURE, (700, 1500), (420.0, 1500.0), blend=(240, 340),
          weight=0.8, interrupt=IN.AFTER_BLEND_IN, states=_POSTURE_STATES,
          cameras=("CAM_6",), cost=0.15, key="posture_minor",
          tags=("body_maintenance", "minor"), max_hour_share=0.025),
)

# ============================================================ REACTION
#
# Triggered by salience, not by the scheduler's clock. Composed two or three at a time.

_REACTION: Final = (
    _spec("quick_chart_glance", AC.REACTION, (1400, 2800), (0.0, 0.0), blend=(160, 240),
          weight=1.4, locks=(IL.HEAD,), interrupt=IN.NEVER, gaze=GT.MONITOR_MAIN,
          states=(CS.MARKET_REACTION,), cost=0.3, key="react_open",
          tags=("reaction_open",), cameras=("CAM_3", "CAM_4")),
    _spec("lean_forward_reaction", AC.REACTION, (900, 1700), (0.0, 0.0), blend=(180, 300),
          weight=1.5, locks=(IL.UPPER_BODY,), interrupt=IN.NEVER, gaze=GT.MONITOR_MAIN,
          states=(CS.MARKET_REACTION,), cost=0.4, key="react_open",
          tags=("reaction_open",), cameras=("CAM_1", "CAM_7")),
    _spec("rapid_mouse", AC.REACTION, (500, 1200), (0.0, 0.0), blend=(90, 140),
          weight=1.1, locks=(IL.RIGHT_HAND, IL.MOUSE), interrupt=IN.NEVER,
          anchor="ANCHOR_MOUSE", states=(CS.MARKET_REACTION,), cost=0.4,
          key="react_act", tags=("reaction_act",)),
    _spec("quick_note", AC.REACTION, (2500, 6000), (0.0, 0.0), blend=(300, 420),
          weight=0.7, locks=(IL.RIGHT_HAND, IL.PEN), interrupt=IN.NEVER,
          anchor="ANCHOR_NOTEBOOK", gaze=GT.NOTEBOOK, states=(CS.MARKET_REACTION,),
          cost=0.7, key="react_act", tags=("reaction_act",)),
    _spec("small_nod", AC.REACTION, (700, 1300), (0.0, 0.0), blend=(150, 220),
          weight=1.0, locks=(IL.HEAD,), interrupt=IN.NEVER,
          states=(CS.MARKET_REACTION,), cost=0.2, key="react_close",
          tags=("reaction_close",)),
    # Directionally gated: forbidden on an adverse move, as a hard filter rather than a
    # weight. The character smirking at a loss would be the single most
    # character-breaking frame the system could produce.
    _spec("subtle_smirk", AC.REACTION, (900, 1800), (0.0, 0.0), blend=(220, 320),
          weight=0.35, interrupt=IN.NEVER, states=(CS.MARKET_REACTION,), cost=0.1,
          key="smirk", tags=("reaction_close", "favourable_only"), cameras=("CAM_4",),
          max_hour_share=0.004),
    _spec("controlled_exhale", AC.REACTION, (1200, 2200), (0.0, 0.0), blend=(250, 350),
          weight=0.6, interrupt=IN.NEVER, states=(CS.MARKET_REACTION,), cost=0.2,
          key="react_close", tags=("reaction_close",)),
)

# ============================================================ MUSIC
#
# All three are ceilings, not targets. He is trading, not dancing.

_MUSIC: Final = (
    _spec("micro_head_nod", AC.MUSIC, (1200, 5000), (25.0, 160.0), blend=(200, 260),
          weight=0.8, locks=(IL.HEAD,), music_bias=1.2, cost=0.1, key="music_nod",
          tags=("beat_locked",), max_hour_share=0.06),
    _spec("finger_rhythm", AC.MUSIC, (1500, 6000), (20.0, 130.0), blend=(140, 200),
          weight=0.7, music_bias=1.0, cost=0.1, key="music_finger",
          tags=("beat_locked",), max_hour_share=0.05),
    _spec("small_shoulder_rhythm", AC.MUSIC, (2500, 8000), (60.0, 300.0), blend=(280, 360),
          weight=0.4, locks=(IL.UPPER_BODY,), music_bias=0.7, cost=0.1,
          key="music_shoulder", tags=("beat_locked",), max_hour_share=0.03),
)

# ============================================================ chain internals
#
# Steps that exist only inside a chain and are not part of the behaviour-intent
# vocabulary. They are specs so the renderer has something to perform; the scheduler
# cannot reach them.

_CHAIN_STEPS: Final = (
    _spec("note_gaze_down", AC.WORK, (400, 800), (0.0, 0.0), blend=(180, 140),
          locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN, gaze=GT.NOTEBOOK,
          chain_only=True, cost=0.1, key="note"),
    _spec("reach_pen", AC.WORK, (500, 900), (0.0, 0.0), blend=(200, 140),
          locks=(IL.RIGHT_HAND,), interrupt=IN.AFTER_BLEND_IN, anchor="ANCHOR_PEN",
          gaze=GT.NOTEBOOK, chain_only=True, cost=0.2, key="note"),
    _spec("acquire_pen", AC.WORK, (250, 450), (0.0, 0.0), blend=(110, 90),
          locks=(IL.RIGHT_HAND, IL.PEN), interrupt=IN.NEVER, anchor="ANCHOR_PEN",
          chain_only=True, cost=0.1, key="note"),
    _spec("return_pen", AC.WORK, (450, 800), (0.0, 0.0), blend=(140, 200),
          locks=(IL.RIGHT_HAND, IL.PEN), interrupt=IN.NEVER, anchor="ANCHOR_PEN",
          chain_only=True, cost=0.1, key="note"),
    _spec("note_gaze_up", AC.WORK, (400, 800), (0.0, 0.0), blend=(180, 220),
          locks=(IL.HEAD,), interrupt=IN.AFTER_BLEND_IN, gaze=GT.MONITOR_MAIN,
          chain_only=True, cost=0.1, key="note"),
    _spec("breath_after_sip", AC.CAFFEINE, (900, 1700), (0.0, 0.0), blend=(200, 280),
          interrupt=IN.NEVER, chain_only=True, cost=0.1, key="coffee"),
)


# ============================================================ chains


@dataclass(frozen=True, slots=True)
class ActionChain:
    """A transactional interaction.

    The brief's requirement, and the reason it is a requirement: *"Do not fake them as
    independent random actions."* Six independent actions that happen to occur in the
    right order will eventually occur in the wrong one, and the wrong one is a mug
    lifted to a mouth that is already holding a pen.

    ``commit_step`` is the heart of it. Steps before it may still be abandoned —
    reaching toward the mug and changing your mind is a real thing people do. From that
    step onward the chain is committed and **nothing** stops it: not the scheduler, not
    a market reaction, not an operator. That is what makes a floating mug impossible
    rather than merely unlikely.
    """

    chain_id: str
    entry_action: str
    steps: tuple[str, ...]
    #: Index from which the chain can no longer be abandoned.
    commit_step: int
    #: Locks the chain holds across its whole run, beyond any individual step's.
    held_locks: tuple[InteractionLock, ...]
    description: str

    def __post_init__(self) -> None:
        if not 0 <= self.commit_step < len(self.steps):
            raise ValueError(f"{self.chain_id}: commit_step {self.commit_step} out of range")

    @property
    def length(self) -> int:
        return len(self.steps)

    def is_committed_at(self, step: int) -> bool:
        return step >= self.commit_step

    def required_locks(self) -> frozenset[InteractionLock]:
        """Every lock the chain will need, across all its steps.

        Claimed in full at step 0 and held to completion, which is what "transactional"
        has to mean. The first implementation claimed only step 0's own locks: the
        NOTE_WRITE chain opens with `note_gaze_down` (HEAD) and acquires the pen two
        steps later (RIGHT_HAND, PEN), so admissibility was checked against HEAD alone
        and the chain could start while the right hand was on the mouse. It raised
        `LockConflict` mid-chain within two simulated hours — which is the designed
        failure, but the fix belongs here.
        """
        locks: set[InteractionLock] = set(self.held_locks)
        for step in self.steps:
            locks |= CATALOG[step].effective_locks
        locks.discard(InteractionLock.NONE)
        return frozenset(locks)


#: The three transactional interactions.
#:
#: `COFFEE_DRINK` follows the brief's sequence exactly: reach, contact, lock, lift, sip,
#: lower, place, release, return. `hold_cup` sits between sip and place because a sip
#: that goes straight back to the desk reads as mechanical — people hold the mug for a
#: moment, looking at a screen, before putting it down.
CHAINS: Final[dict[str, ActionChain]] = {
    "COFFEE_DRINK": ActionChain(
        chain_id="COFFEE_DRINK",
        entry_action="reach_cup",
        steps=("reach_cup", "pick_cup", "sip", "hold_cup", "place_cup", "breath_after_sip"),
        commit_step=1,  # once the hand closes on the mug, it finishes
        held_locks=(IL.LEFT_HAND, IL.COFFEE),
        description="reach, grip, lift, sip, hold, lower, place, release",
    ),
    "NOTE_WRITE": ActionChain(
        chain_id="NOTE_WRITE",
        entry_action="note_gaze_down",
        steps=(
            "note_gaze_down", "reach_pen", "acquire_pen", "note_write_short",
            "return_pen", "note_gaze_up",
        ),
        commit_step=2,  # once the pen is pinched, it finishes
        held_locks=(IL.RIGHT_HAND, IL.PEN),
        description="gaze notebook, reach pen, acquire, write, return pen, gaze monitors",
    ),
    "NOTE_WRITE_LONG": ActionChain(
        chain_id="NOTE_WRITE_LONG",
        entry_action="note_gaze_down",
        steps=(
            "note_gaze_down", "reach_pen", "acquire_pen", "note_write_long",
            "return_pen", "note_gaze_up",
        ),
        commit_step=2,
        held_locks=(IL.RIGHT_HAND, IL.PEN),
        description="as NOTE_WRITE, with a longer writing beat",
    ),
}

#: Fatigue actions chain a refocus on exit. Not a full chain — a mandatory tail.
FATIGUE_TAIL: Final = "refocus"

#: Actions that, when selected, actually start a chain. Maps intent to chain.
#:
#: This indirection is what lets the scheduler reason about "a coffee sip" as one
#: weighted candidate while the renderer receives six ordered steps.
CHAIN_ENTRY: Final[dict[str, str]] = {
    "note_write_short": "NOTE_WRITE",
    "note_write_long": "NOTE_WRITE_LONG",
    "quick_note": "NOTE_WRITE",
}

#: The coffee chain has no top-level intent of its own in the brief's vocabulary — the
#: steps *are* the vocabulary. So it is entered by a synthetic candidate the scheduler
#: scores like any other, weighted and cooled down as `coffee_drink`.
COFFEE_INTENT: Final = "coffee_drink"


# ============================================================ assembly


def _build_catalog() -> dict[str, ActionSpecV1]:
    specs: dict[str, ActionSpecV1] = {}
    groups = (
        _MICRO, _WORK, _POSTURE, _HEADPHONES, _CAFFEINE, _FATIGUE, _REACTION,
        _MUSIC, _CHAIN_STEPS,
    )
    for group in groups:
        for spec in group:
            if spec.action_id in specs:
                raise ValueError(f"duplicate action id {spec.action_id!r} in the catalogue")
            specs[spec.action_id] = spec

    # The synthetic coffee intent. Carries the brief's worked example verbatim —
    # 8-30 minutes — and the duration of the chain it expands to.
    specs[COFFEE_INTENT] = _spec(
        COFFEE_INTENT, AC.CAFFEINE, (4000, 8000), (480.0, 1800.0), blend=(400, 520),
        weight=0.9, locks=(IL.LEFT_HAND, IL.COFFEE), interrupt=IN.NEVER,
        bias=BandBiasV1(b0=1.3, b1=1.2, b2=1.0, b3=0.9, b4=0.6, b5=0.4),
        anchor="ANCHOR_MUG_BODY", gaze=GT.COFFEE, cameras=("CAM_6", "CAM_4"),
        cost=0.6, key="coffee", max_hour_share=0.08,
    )
    return specs


#: The catalogue, by action id.
CATALOG: Final[dict[str, ActionSpecV1]] = _build_catalog()

#: Actions the scheduler may pick directly: everything not chain-only, with a weight.
SCHEDULABLE: Final[tuple[str, ...]] = tuple(
    action_id
    for action_id, spec in CATALOG.items()
    if not spec.chain_only and spec.weight > 0.0
)

#: Reaction actions by role, for composing a reaction from opener/action/resolution.
REACTION_OPENERS: Final[tuple[str, ...]] = tuple(
    a for a, s in CATALOG.items() if "reaction_open" in s.tags
)
REACTION_ACTIONS: Final[tuple[str, ...]] = tuple(
    a for a, s in CATALOG.items() if "reaction_act" in s.tags
)
REACTION_CLOSERS: Final[tuple[str, ...]] = tuple(
    a for a, s in CATALOG.items() if "reaction_close" in s.tags
)


def spec(action_id: str) -> ActionSpecV1:
    """Look up a spec, failing loudly on an unknown id."""
    try:
        return CATALOG[action_id]
    except KeyError:
        raise KeyError(
            f"unknown action {action_id!r}; the catalogue defines {len(CATALOG)} actions"
        ) from None


def chain_for(action_id: str) -> ActionChain | None:
    """The chain this action expands to, if any."""
    if action_id == COFFEE_INTENT:
        return CHAINS["COFFEE_DRINK"]
    chain_id = CHAIN_ENTRY.get(action_id)
    return CHAINS[chain_id] if chain_id else None


def group_cooldown_range(action_id: str) -> tuple[float, float] | None:
    """Group cooldown range for this action's anti-repeat key, or ``None``."""
    return GROUP_COOLDOWN_SECONDS.get(spec(action_id).anti_repeat_key)


def validate_catalog(known_anchors: frozenset[str], known_gaze: frozenset[str]) -> list[str]:
    """Cross-check the catalogue against frozen geometry.

    Returns problems rather than raising, so a caller can report all of them at once —
    a startup that names one broken anchor per run is a slow way to fix six.
    """
    problems: list[str] = []
    for action_id, action in CATALOG.items():
        if action.required_anchor and action.required_anchor not in known_anchors:
            problems.append(f"{action_id}: unknown anchor {action.required_anchor!r}")
        if action.gaze_target and action.gaze_target.value not in known_gaze:
            problems.append(f"{action_id}: unknown gaze target {action.gaze_target.value!r}")
    for chain in CHAINS.values():
        for step in chain.steps:
            if step not in CATALOG:
                problems.append(f"chain {chain.chain_id}: unknown step {step!r}")
        if chain.entry_action != chain.steps[0]:
            problems.append(
                f"chain {chain.chain_id}: entry {chain.entry_action!r} is not its first step"
            )
    for intent, chain_id in CHAIN_ENTRY.items():
        if intent not in CATALOG:
            problems.append(f"chain entry {intent!r} is not in the catalogue")
        if chain_id not in CHAINS:
            problems.append(f"chain entry {intent!r} maps to unknown chain {chain_id!r}")
    if not REACTION_OPENERS:
        problems.append("no reaction openers are tagged; a reaction cannot be composed")
    return problems


#: Every member of the body-maintenance family, by tier.
POSTURE_MAJOR: Final[tuple[str, ...]] = tuple(
    action.action_id for action in _POSTURE if "major" in action.tags
)
POSTURE_MINOR: Final[tuple[str, ...]] = tuple(
    action.action_id for action in _POSTURE if "minor" in action.tags
)


def is_body_maintenance(action_id: str) -> bool:
    return "body_maintenance" in spec(action_id).tags


def is_major_posture(action_id: str) -> bool:
    return "major" in spec(action_id).tags


__all__ = [
    "AMPLITUDE_JITTER",
    "CATALOG",
    "CATEGORY_HOUR_SHARE_CEILING",
    "CHAINS",
    "CHAIN_ENTRY",
    "COFFEE_INTENT",
    "FATIGUE_TAIL",
    "GROUP_COOLDOWN_SECONDS",
    "POSTURE_MAINTENANCE_INTERVAL_SECONDS",
    "POSTURE_MAJOR",
    "POSTURE_MINOR",
    "POSTURE_MINOR_INTERVAL_SECONDS",
    "REACTION_ACTIONS",
    "REACTION_CLOSERS",
    "REACTION_OPENERS",
    "SCHEDULABLE",
    "ActionChain",
    "chain_for",
    "group_cooldown_range",
    "is_body_maintenance",
    "is_major_posture",
    "spec",
    "validate_catalog",
]
