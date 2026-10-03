# visual/materials/

Shaders and composite definitions shared across camera stacks.

| File | Role |
|---|---|
`composite.json` | Layer blend modes and the eight-layer compositing order (`MOTION_LIBRARY.md` §8) |
`lighting.json` | The four light layers, their temperatures, and their modulation ranges |
`shaders/` | GLSL for deformation skinning, parallax offset, light compositing, screen surfaces |

Lighting is composited, never baked. The modulation table in `OFFICE_BIBLE.md` §5 is the full
permitted range — ±8 % on monitor spill from market energy, ±5 % inverse on the warm key, ±3 %
from music, and a slow warm drift after 02:00 local.

**No flashing, no beat-synchronised brightness pulse, no hue rotation, no RGB fixture, no light
that strobes or sweeps.** A 24/7 stream with a pulsing room is unwatchable within an hour.
