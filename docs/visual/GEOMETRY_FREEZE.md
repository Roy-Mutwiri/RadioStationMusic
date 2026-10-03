# V1 geometry freeze

The V1 blockout is **frozen**. This document says what that means operationally, what
the enforcement mechanism is, and records every change made since the freeze.

---

## 1. What is frozen

Twelve things, all defined in `visual/environment/TRADE_FIX_OFFICE_01.blockout.json`:

| Frozen | Where |
|---|---|
Character proportions | `character.key_heights`, `character.standing_height`, `head_unit` |
Chair position | `chair.centre`, `chair.seat_pan_z` |
Desk dimensions | `desk.extent`, `desk.surface_z` |
Monitor dimensions | `monitors.panels[].panel` |
Monitor heights | `monitors.common.centre_z`, `monitors.derived` |
Reachable work zone | `anchor_rules.seated_reach_mm`, `reserved_volumes` |
Interaction anchors | `anchors[]` — all twelve |
Gaze targets | `gaze_targets[]` — all fourteen |
Camera transforms | `cameras[]` — all seven |
Office zones | `zones`, `room`, `branding`, `lighting` |
Visual-state contract | `tradefix_radio/visual/contracts.py::VisualStateV1` |
Action contract | `tradefix_radio/visual/contracts.py::ActionSpecV1`, `CharacterActionV1` |

**Nothing above may move casually during art production.** The point of a freeze is not
that the numbers are perfect — several of them were wrong and were corrected during V1 —
but that after this point a change costs a review rather than an edit.

## 2. The enforcement mechanism

A declaration nobody checks is a comment. Two things make this one real.

### `tests/unit/test_visual_geometry_freeze.py`

Seventeen tests that **re-derive** every geometric claim the blockout records and fail if
any drifts. It runs in the ordinary test suite, not as an opt-in check.

It is not ceremonial. Its first run rejected:

| Finding | Consequence had it shipped |
|---|---|
Monitor centres at z1250 put the top bezel at z1418 — above his chin at z1215 | Every front camera would have had his face hidden behind a monitor. The hero composition did not exist |
Three anchors beyond the 830 mm seated reach | He could not reach his own notebook, pen or stream pad |
`GAZE_WINDOW` at yaw 180° | The window is behind him; a 3 % share would have been 180° torso turns |
`PANEL_W2` at y3300, yaw −95° | Behind his shoulder line |
`GAZE_KEYBOARD` at pitch −64° | Looking almost straight down at keys he touch-types |
Default gaze pitch recorded as −6.8° | Arithmetic error; the geometry gives −14.2° |
Front bezel clearances overstated by 20–26 mm | Recorded figures did not match the geometry |
CAM_4's distance off by 4.2 mm, CAM_6's frame excluding two anchors | — |

### `scripts/visual/validate_blockout.py`

The same checks with a readable table, for interactive use. The suite and the script must
agree; `test_script_and_suite_agree` asserts they read the same file.

```
python scripts/visual/validate_blockout.py
tradefix visual doctor
```

`tradefix visual doctor` additionally cross-checks the action catalogue, the camera
metadata and the asset manifest against the blockout — so a motion that names a
nonexistent anchor, or a plate that names a nonexistent camera, fails at the CLI rather
than at runtime.

## 3. The change procedure

Any geometry change after the freeze requires, in writing, in §4 of this document:

1. **Reason** — what is wrong, and why a change is the right fix rather than an
   accommodation elsewhere.
2. **Affected cameras** — every composition whose framing or clearance moves.
3. **Affected anchors** — every anchor whose reach or position moves.
4. **Validator rerun** — the suite and the script, both passing, pasted or referenced.

A change that cannot name its affected cameras has not been thought through: the seven
compositions all resolve against the same geometry, and moving a monitor by 40 mm moves
three bezel clearances.

---

## 4. Change log

### CR-001 — Gaze participation model replaces flat angular limits

**Date:** 2026-10-03, at the start of V6.

**Reason.** The V1 validator used flat limits — yaw ±100°, pitch −45°..+30° — and
rejected three targets a real trader plainly looks at: his notebook (yaw −75°, pitch
−37°), his keyboard (pitch −59°), and out of the window (yaw −162°). The V6 brief lists
`KEYBOARD` and `WINDOW` in its gaze vocabulary, which forced the question, and the answer
is that the flat limit was the wrong model. **The angle was never the problem; an angle
reached with the wrong body parts is.**

So each target now declares a **participation tier** — `eyes`, `head`, `shoulders`,
`head_pitch_strong`, `torso`, `chair_swivel` — and the validator checks the *declaration*
against the measured geometry rather than rejecting the geometry.

**Changes:**

- Added `gaze_rules.participation_model` with six tiers and their yaw/pitch envelopes.
- Removed `gaze_rules.yaw_limit_degrees` and `pitch_limits_degrees`.
- Every gaze target now carries a `participation` field.
- Reinstated `GAZE_KEYBOARD` at the keyboard's **far edge** (3 200, 2 680, 757) rather
  than its centre, tier `head_pitch_strong`, share 0.008. It is a hotkey hunt, not
  typing.
- Reinstated `GAZE_WINDOW` (2 600, 4 800, 1 600), tier `chair_swivel`, share 0.004,
  rationed to `look_away_from_monitors`.
- Rebalanced non-screen shares so the total is 1.000 and the screen subset is 0.890.
- Removed the `GazeTarget.MONITOR_FAR_LEFT` enum member: it and `MONITOR_LOWER` both
  resolved to `MON_4`, which silently doubled that panel's gaze share because two
  members each drew their own base share from one physical surface. `MONITOR_LOWER`
  keeps the mapping. `test_gaze_mapping_is_injective` now asserts the mapping is 1:1.

**Affected cameras:** none. No camera position, target, lens or clearance changed.

**Affected anchors:** none. No anchor position changed.

**Validator rerun:** `tests/unit/test_visual_geometry_freeze.py` — 17 passed.
`scripts/visual/validate_blockout.py` — ALL CHECKS PASSED.

### CR-002 — CAM_4 distance correction and CAM_6 reframing

**Date:** 2026-10-03, at the start of V6.

**Reason.** Two findings from tightening the freeze suite.

`CAM_4`'s recorded `distance_mm` was 1 267 against a geometric 1 262.8 — a hand-arithmetic
slip of 4.2 mm, which propagated into its recorded frame size.

`CAM_6`'s frame spanned x 2 431–3 869 and excluded the stream pad at x 3 900. CAM_6 is the
contact-verification shot; an anchor outside its frame cannot have its contact judged
from the only camera close enough to judge it.

**Changes:**

- `CAM_4`: distance 1 267 → 1 263; frame 536 × 302 → 535 × 301; z-range
  [1 179, 1 481] → [1 180, 1 480].
- `CAM_6`: position (3 981, 1 691, 1 259) → (4 068, 1 634, 1 288); target x 3 150 → 3 190;
  distance 1 399 → 1 479; frame 1 438 × 809 → 1 520 × 855; x-range
  [2 431, 3 869] → [2 430, 3 950].

**Affected cameras:** `CAM_4`, `CAM_6`. Both remain inside the room. `CAM_4`'s bezel
clearance is unchanged at +104 mm (the sight line to the eye did not move; only the
recorded distance was wrong). `CAM_6` is exempt from the clearance rule as a
desk-surface shot.

**Affected anchors:** none moved. `CAM_6` now frames **all six** anchored desk objects:
pen and notebook (2 480), mouse (2 720), keyboard (3 200), mug (3 760), stream pad (3 900).

**Validator rerun:** 17 passed, including the new
`test_cam_6_frames_every_interactive_prop` and
`test_camera_frame_widths_match_their_lenses`.

---

## 5. What is *not* frozen

Deliberately left open, because these are tuning rather than geometry:

- **Action catalogue values** — durations, cooldowns, weights, band biases. They live in
  `tradefix_radio/visual/catalog.py` and are expected to be tuned against soak results.
  Several already have been; see `V6_REPORT.md` §4.
- **Band profiles** — rate, blend scale, reaction threshold, idle share, dwell scale.
- **Camera scheduling metadata** — hold times, affinities, share caps. The camera
  *transforms* are frozen; when the camera director chooses between them is not.
- **The asset manifest's per-plate layer stacks** beyond the hero camera. Only `CAM_1`'s
  stack is enumerated; the other six derive from the same room and the same depth rule,
  and enumerating them before the hero plate is accepted would be guessing at
  compositions nobody has seen.
