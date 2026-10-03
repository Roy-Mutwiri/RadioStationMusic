# visual/

Every asset for the Trade Fix Radio visual performance layer. **Nothing generated for this
layer lands in Downloads, on the Desktop, or anywhere else.**

Specification lives in `docs/visual/`. This tree holds the artefacts those documents describe.

| Directory | Holds | Does not hold |
|---|---|---|
`character/` | The TF_TRADER_01 master layered model, its rig, its atlases | Reference art — that is `references/character/` |
`environment/` | The blockout (authoritative geometry), the Blender scene, one layer stack per camera | Painted reference plates |
`animations/` | Authored curves and motion reference clips | The action catalogue (Python, in `tradefix_radio/visual/catalog.py`) or the director |
`cameras/` | Camera definitions and overlay-zone maps derived from the blockout | — |
`materials/` | Shared shaders and composite definitions | Textures |
`textures/` | Source and compressed atlases | Source PSDs for the character — those live with the model |
`references/` | The canonical, frozen reference art and drawings | Work in progress |
`exports/` | Build output consumed by the renderer. **Regenerable; never hand-edited** | Anything that is a source of truth |

## The two authoritative files

**`environment/TRADE_FIX_OFFICE_01.blockout.json`** is the single source of spatial truth.
Room dimensions, every object position, every interaction anchor, every gaze target and all
seven camera positions, targets and fields of view, in millimetres. Where a painted plate, a
rig or an animation disagrees with it, the plate, rig or animation is wrong (ADR-13).

Validate it after any change:

```
python scripts/visual/validate_blockout.py
```

It checks nine rules — camera sight lines clearing the monitor bezel plane, every anchor inside
the 830 mm seated reach, gaze targets inside head range, the reserved hand envelope, monitor
overlap, camera containment, and that the gaze shares sum to 1.0 with the screen subset at its
target. Its first run rejected three anchors and two gaze targets, so it is not ceremonial.

**`character/TF_TRADER_01.master`** is the one layered character model. Per-camera work is a
projected view of the *same* layer tree — never a new face. The layer taxonomy is fixed in
`docs/visual/CHARACTER_BIBLE.md` §4 and the exporter fails on a mismatch.

## Regenerating the drawings

```
python scripts/visual/render_reference_svg.py
```

Emits the floor plan and the character construction sheet **from the blockout**, so the
drawings cannot drift out of agreement with the data. Re-run after editing the blockout.

`references/index.html` is a convenience viewer for the generated drawings — serve this
directory over HTTP and open it. Not a deliverable.

## Status

Phase V1 is complete except the painted art, which is blocked: there is no image generator on
this machine. See `docs/status/VISUAL_PHASE_1_REPORT.md` and `docs/visual/INITIAL_VISUAL_AUDIT.md`
§6.
