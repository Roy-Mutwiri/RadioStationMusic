# ART FILES REQUIRED — proof mode

**No character or office artwork exists in this repository.** A repository-wide image
search found only two SVG *diagrams* generated from the blockout
(`visual/references/character/TF_TRADER_01_proportions.svg`,
`visual/references/office/TRADE_FIX_OFFICE_01_plan.svg`), the numeric blockout, and the
asset manifest with all 22 plates marked `OUTSTANDING`. Nothing depicts the trader or the
office.

So the live demo currently renders **procedural proof art** — shapes drawn from the frozen
V1 geometry in the Trade Fix palette, labelled `TEMP_PROOF · PROCEDURAL`. It is
recognisable as a trader at a desk in a night office and every movement is visible, but it
is **not** the approved concept art and does not claim to be. Drop the files below in and
the renderer uses them instead, automatically, on reload.

---

## Where to put them

```
visual/assets/source/character/
visual/assets/source/environment/
```

Both directories exist and are empty. Filenames must match exactly — the loader does not
guess, and `tradefix visual assets validate` will tell you what is wrong before you look
at the browser.

## Character — `visual/assets/source/character/`

The locked identity (`CHARACTER_BIBLE.md`): white male 35–40, short textured brown /
dark-blond hair, short mature beard, hazel-brown eyes, serious mature face, black Trade Fix
hoodie over a black shirt, black-and-gold headphones, black watch on the left wrist, small
restrained gold pendant, black trousers, black sneakers.

| Filename | Size | Alpha | What it must contain |
|---|---|---|---|
| `character_seated_hero.png` | 2048×2048 | **required** | The whole seated figure, CAM_1 front view. Used alone if the segmented layers below are absent. |
| `character_head.png` | 1024×1024 | **required** | Skull, face, ears, beard. **No hair, no eyes, no headphones** — those move separately. |
| `character_torso.png` | 1536×1536 | **required** | Hoodie torso, shoulders, chest, pendant. No arms. |
| `character_arm_left.png` | 1024×1024 | **required** | Upper arm **and** forearm, screen-left. Pivot at the shoulder. |
| `character_arm_right.png` | 1024×1024 | **required** | Same, screen-right. |
| `character_hand_left.png` | 512×512 | **required** | Open hand, neutral. Watch visible on this wrist. |
| `character_hand_right.png` | 512×512 | **required** | Open hand, neutral. |
| `character_eyes.png` | 512×256 | **required** | Both eyes open: sclera, hazel-brown iris, pupil, catchlight. |
| `character_eyelids.png` | 512×256 | **required** | Both lids **closed**, matching the eye geometry. Scaled vertically to blink. |

Optional, improves the result:

| Filename | Size | Alpha | What it adds |
|---|---|---|---|
| `character_hair.png` | 1024×1024 | required | Hair as a separate layer so the head can turn under it. |
| `character_headphones.png` | 1024×512 | required | Band and both cups, with the gold accent. |

## Environment — `visual/assets/source/environment/`

| Filename | Size | Alpha | What it must contain |
|---|---|---|---|
| `office_hero.png` | 3840×2160 | no | The CAM_1 office behind the character: rear wall, shelves, window opening, warm practical lighting. **No desk front, no character.** |
| `desk_foreground.png` | 3840×2160 | **required** | The desk surface and near edge, in front of the character's lower body. |
| `monitor_layer.png` | 2048×1024 | **required** | Monitor bezels and stands only — **not** the screen content. Screens are drawn live. |
| `city_skyline.png` | 4096×1536 | **required** | The night skyline seen through the window. Alpha above the roofline. |

Props, drawn procedurally until supplied:

| Filename | Size | Alpha |
|---|---|---|
| `coffee_mug.png` | 512×512 | required |
| `notebook.png` | 512×512 | required |
| `pen.png` | 256×256 | required |

---

## Rules the loader enforces

Run `tradefix visual assets validate` after copying files in. It checks:

- the filename is one this list declares
- the pixel dimensions match exactly
- an alpha channel exists where the table says **required**
- no file is a placeholder (a fully opaque or fully transparent image is rejected)
- `*_pivot.json` metadata, if present, carries a `[x, y]` array in 0–1 — **not** `{x, y}`

That last one is not pedantry. A single geometry vector serialised as `{"x":…,"y":…}`
instead of `[x, y]` took the renderer down completely last week: it reached an array
destructuring as `object is not iterable` on the first draw call of every frame. Vectors
are arrays here, everywhere.

## What happens with partial art

Proof mode composes per layer. Supply `office_hero.png` alone and you get the painted
office behind the procedural trader. Supply the nine character layers and the trader is
painted and still fully animated. Anything missing stays procedural, and the HUD's **ART**
panel says exactly how many of each group are present.

A flat plate painted for one camera cannot serve the others. When an imported plate is
live and the selected camera has no plate of its own, that camera shows
**ART VIEW NOT YET AVAILABLE** rather than stretching the wrong image across it.
