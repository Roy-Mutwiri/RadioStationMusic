# visual/environment/

Geometry and painted layer stacks for TRADE_FIX_OFFICE_01.

| File | Role |
|---|---|
`TRADE_FIX_OFFICE_01.blockout.json` | **AUTHORITATIVE.** The single source of spatial truth. Validate with `scripts/visual/validate_blockout.py` |
`TRADE_FIX_OFFICE_01.blend` | Low-poly blockout built from the JSON. Offline tool only — never runs while the station is live (ADR-13) |
`stacks/CAM_<n>/` | One depth-assigned painted layer stack per camera composition |
`underlays/` | Perspective renders from the blockout, used as painting guides |

## Why a blockout at all

A 2.5D pipeline has no shared scene graph, so "all seven cameras frame the same room" is an
authoring discipline rather than a guarantee. The blockout is the mechanism that makes it one:
plates are painted over renders from it, and a disputed plate can be re-rendered and compared.

The blockout is never the thing that is wrong.

## Layer stacks

Flat images cannot parallax, and without parallax the slow push-in and breathing drift in
`CAMERA_PLAN.md` §4 have nothing to work with. Every stack therefore ships as **depth-assigned
layers**, at 300 dpi and 1.5x final output size so a push-in has real pixels to move into.

Only the active camera's stack is resident; neighbours are pre-warmed and the rest evicted, per
the VRAM budget in `ADR_VISUAL_RUNTIME.md` §4.
