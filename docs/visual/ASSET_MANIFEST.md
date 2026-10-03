# Art asset contract

What the painted plates must provide, declared **before** any of them exist.

**Generated file:** `visual/assets/manifest.json` — produced from
`tradefix_radio/visual/assets.py`. Hand edits are overwritten.
**Regenerate:** `tradefix visual manifest --write`
**Inspect:** `tradefix visual manifest` · **Validate:** `tradefix visual doctor`

---

## 1. Why this exists now

The tenth acceptance criterion is that final artwork can later be inserted without
redesigning the runtime. That is only achievable if the runtime was built against a
declared contract rather than against whatever the first delivered PSD happens to look
like.

So the contract comes first, and the runtime is built against it. Twenty-two assets are
declared; none is delivered; every one of them has its dimensions, layer order, depths,
masks, pivot, camera applicability and acceptance gate fixed.

## 2. The three rules that are easy to get wrong

**Depth comes from geometry, never from the eye.** Every environment layer carries a
depth in millimetres read off `TRADE_FIX_OFFICE_01.blockout.json`, and parallax is
*derived* from it:

```python
parallax = camera_amplitude × (2248 / depth_mm)
```

Inverse-depth against a reference plane at the character's distance. The brief is
explicit — *"Do not invent arbitrary parallax amounts by eye later"* — and the reason is
that eyeballed parallax is the single most common way a 2.5D scene reads as a stack of
cards rather than as a room. Two layers at the same real distance always move together,
because they have to.

**Masks are specified before painting.** A flattened image cannot animate. Discovering
that after nine character plates have been painted is expensive and entirely avoidable,
so the mask lists are part of the contract rather than a note for later.

**Lighting is delivered unbaked.** Five separate layers. Baking them makes the lighting
modulation in `OFFICE_BIBLE.md` §5 unimplementable, and that modulation is how the room
responds to market energy at all.

## 3. Output and oversampling

| | |
|---|---|
Canonical output | **1920 × 1080** |
Alternate, supported | 2560 × 1440 |
Plate oversample | **1.5×** → environment plates at 2880 × 1620 |
Target frame rate | **30 fps**, configurable |

Oversampling exists so a slow push-in has real pixels to move into rather than upscaling
into softness. 1.5× covers the 4 % maximum push plus the alternate output.

30 fps is the default because **the frame-rate decision is deferred to a measurement**,
not assumed: the brief is explicit that a stable 30 may beat an unstable 60, and the
measurement needs the placeholder runtime to exist first.

## 4. The twenty-two assets

### One master character model

`TF_TRADER_01_master` — 4096 × 4096, pivot (0.5, 0.92) at the seat so a lean rotates
about the right place.

ADR-13: **one** master layered model. Per-camera work is a projected view of the *same*
layer tree, never a new face.

Seventeen separable regions, which is the **minimum** that achieves believable motion
rather than everything split — each extra layer costs atlas space and a seam that can
show:

```
hair_rear → torso → arm_L(upper, fore, hand) → arm_R(upper, fore, hand)
          → neck → head → brows → eyes → eyelids → mouth_jaw
          → hair_front → headphones → pendant
```

Acceptance: layer names match `CHARACTER_BIBLE.md` §4 exactly — the exporter fails the
build on a mismatch — and seated geometry agrees with the blockout: elbow z700, eye
z1295, forearms in contact with a z735 surface.

### Nine character references

| # | Asset | Size | Cameras |
|---|---|---|---|
1 | `char_ref_01_front_portrait` | 2048² | CAM_1, CAM_4 |
2 | `char_ref_02_three_quarter_left` | 2048² | CAM_7 |
3 | `char_ref_03_three_quarter_right` | 2048² | CAM_1 |
4 | `char_ref_04_profile_left` | 2048² | CAM_2 |
5 | `char_ref_05_profile_right` | 2048² | — |
6 | `char_ref_06_full_body_front` | 2048 × 3072 | CAM_5 |
7 | `char_ref_07_full_body_back` | 2048 × 3072 | CAM_3 |
8 | `char_ref_08_seated_trading` | 2048² | CAM_1, CAM_7 |
9 | `char_ref_09_seated_side` | 2048² | CAM_2, CAM_6 |

**Identity target, frozen** (`CHARACTER_BIBLE.md` §2): white male, visual age 35–40,
short textured brown to dark-blond hair, short mature beard or heavy stubble, hazel to
brown eyes, realistic adult facial proportions, black Trade Fix clothing, black and gold
headphones, black watch on the **left** wrist, restrained gold pendant.

Forbidden: teenager appearance, a different haircut, a changed beard, a changed nose,
jaw or eye shape.

### Seven environment plates

| # | Asset | Camera | Role |
|---|---|---|---|
1 | `env_plate_01_hero_front` | CAM_1 | **The colour and value reference.** All others graded against it |
2 | `env_plate_02_side_desk` | CAM_2 | The plate `TF-HP-01` is judged on |
3 | `env_plate_03_over_shoulder` | CAM_3 | The only readable chart content |
4 | `env_plate_04_wide_office` | CAM_5 | **The spatial coherence check** |
5 | `env_plate_05_desk_detail` | CAM_6 | The anchor-accuracy plate |
6 | `env_plate_06_coffee_zone` | CAM_5 variant | Zone C as a destination |
7 | `env_plate_07_window_city` | CAM_2 variant | Three city depth planes |

All seven depict **one geometrically consistent room**, composed against the V1 blockout.
If any plate disagrees with the CAM_5 wide plate about where something is, that plate is
wrong.

## 5. The hero layer stack

Only `CAM_1`'s stack is enumerated. The other six derive from the same room and the same
depth rule, and enumerating them before the hero plate is accepted would be guessing at
compositions nobody has seen.

Depths are **computed** from the blockout — the camera sits at y 900, so each layer's
depth is the distance from it to the plane the layer depicts.

| # | Layer | Depth | Parallax @ 0.008 | Notes |
|---|---|---|---|---|
L00 | far city | 400 000 mm | 0.00004 | haze and sky; effectively static |
L01 | mid city | 150 000 mm | 0.00012 | towers, lit crowns, aircraft light |
L02 | near city | 25 000 mm | 0.00072 | building masses, window grids, traffic |
L03 | window glass | 3 900 mm | 0.0046 | glazing, reflections, mullions |
L04 | rear wall | 3 900 mm | 0.0046 | north wall returns |
L05 | shelves | 1 700 mm | 0.0106 | west shelving at the frame edge |
L06 | plants rear | 600 mm | 0.0300 | east plant |
L07 | chair rear | 2 430 mm | 0.0074 | `chair_occlusion` |
L08 | **character** | 2 100 mm | 0.0086 | the master layer group, deformable |
L09 | monitor screens | 1 450 mm | 0.0124 | live surfaces |
L10 | monitor bodies | 1 450 mm | 0.0124 | `monitor_foreground` |
L11 | desk props | 1 800 mm | 0.0100 | notebook, pen, phone, laptop, mic |
L12 | keyboard, mouse | 1 840 mm | 0.0098 | `keyboard_occlusion` |
L13 | mug | 1 720 mm | 0.0105 | `mug_occlusion` |
L14 | desk front | 1 300 mm | 0.0138 | `desk_foreground` |
L15 | coffee steam | 1 700 mm | 0.0106 | runtime composite, mug-temperature driven |
L16 | atmosphere | 1 500 mm | 0.0120 | runtime composite; dust in the amber pools |

Two of the seventeen are **runtime composites** rather than paint: steam has to track mug
temperature, and the atmospheric pass has to modulate with lighting.

## 6. Masks

### Character — 19 channels

```
eyes · eyelids · brows · mouth_jaw
hand_left · hand_right · forearm_left · forearm_right
upper_arm_left · upper_arm_right · torso · neck · head
hair_front · hair_rear
headphones_band · headphones_cup_left · headphones_cup_right
pendant
```

`eyes` must separate sclera, iris, pupil and specular so gaze can aim. `eyelids` must
close upper and lower independently.

### Environment — 6 channels

```
mug_occlusion        the mug in front of the hand
keyboard_occlusion   fingers behind key tops
monitor_foreground   screens and bezels in front of the character
desk_foreground      the near desk edge
chair_occlusion      chair arms and back over the torso
plant_foreground
```

These are what let the character pass *behind* things. Without them he is a sticker on a
photograph.

### Lighting — 5 unbaked layers

```
warm_key · cool_fill · monitor_spill · gold_accent · occlusion_ao
```

## 7. Camera parallax amplitudes

From `camera.py`'s metadata, and the multiplier in the formula in §2:

| Camera | Amplitude | Push-in | Note |
|---|---|---|---|
`CAM_1` | 0.008 | yes | hero front |
`CAM_2` | 0.006 | no | side profile |
`CAM_3` | 0.004 | no | over-shoulder |
`CAM_4` | 0.003 | yes | face close-up; shallowest |
`CAM_5` | 0.010 | no | wide office; most parallax |
`CAM_6` | **0.002** | **no** | hands; a push-in amplifies contact errors |
`CAM_7` | 0.007 | yes | three-quarter cinematic |

## 8. Painting order is an order

1. **`char_ref_01_front_portrait` alone.** Iterate until `CHARACTER_BIBLE.md` §5 passes
   in full. Nothing else proceeds.
2. Views 2 and 3 (three-quarters), with view 1 supplied as an image reference. Judged
   **against view 1**, not against the spec in isolation: the question is *"is this the
   same man"*, not *"is this a good face"*.
3. Views 4 and 5 (profiles). Identity drifts most here, so they follow the easier
   three-quarters.
4. View 6 (standing), then 8 (seated). Proportion before pose.
5. Views 7 and 9.
6. `TF_TRADER_01_master`.
7. `env_plate_01_hero_front`, then `env_plate_04_wide_office`.

**If a later view cannot be made to match view 1, view 1 is not re-opened.** The later
view is repainted. That is the whole mechanism by which one man appears in nine drawings.

## 9. Dropping art in

When a plate arrives:

1. Put it in `visual/references/` (reference) or `visual/environment/stacks/CAM_n/`
   (plate).
2. Set its `version` in `assets.py` from `0.0.0`.
3. `tradefix visual manifest --write`
4. `tradefix visual doctor` — cross-checks dimensions, layers, masks, cameras and anchors
   against frozen geometry.

**No behaviour change should be necessary.** The director knows about `anchor` ids and
`GazeTarget` names, both of which resolve through the blockout. It has never known
anything about pixels.

## 10. Current state

**Twenty-two assets declared. Zero delivered.**

```
$ tradefix visual manifest
  art asset contract v1.0.0
  22 assets, 22 outstanding
  ...
  manifest agrees with frozen V1 geometry
```

The blocker is unchanged from V1: there is no image generator on this machine, and the
plates need a paint capability that does not exist here. `INITIAL_VISUAL_AUDIT.md` §6
sets out the three routes and recommends a local diffusion install with an identity
adapter, run offline between station sessions.

**What is not blocked:** everything in `BEHAVIOR_DIRECTOR.md` and `STATE_BRIDGE.md` is
built, tested and running against this contract.
