# visual/cameras/

Camera definitions and overlay geometry, derived from the blockout.

| File | Role |
|---|---|
`cameras.json` | The seven compositions, generated from `environment/TRADE_FIX_OFFICE_01.blockout.json` |
`overlay_zones.json` | Overlay-safe rects and per-camera protected regions (`CAMERA_PLAN.md` §7) |
`director.json` | Hold distributions per band, per-shot caps, veto rules, transition shares |

## The constraint that shapes all seven

The character's eye is at z1295. The monitors' top bezel is at z1298. They are level within
3 mm, so a front camera at eye level sees a monitor where his face should be. Every south-side
camera is therefore elevated and angled down, and each one's sight line is checked against the
bezel plane at y2350. `scripts/visual/validate_blockout.py` recomputes those clearances and
fails if a recorded value is more than 1 mm out.

Protected overlay regions are enforced by the renderer, not remembered by the operator.
`CAM_4` suppresses all overlay; `CAM_3` protects the entire screen area.
