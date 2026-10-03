# visual/textures/

| Directory | Holds |
|---|---|
`source/` | Uncompressed source art, 300 dpi at 1.5x output size |
`compressed/` | BC7/ASTC atlases consumed by the renderer |
`screens/` | Bezel, glass and reflection overlays for the seven live screen surfaces |

Budget, sized against the **2 487 MiB of free VRAM** measured with ACE-Step resident — not
against the card's 12 GB nameplate:

| Set | Budget |
|---|---|
Character atlases | 120 MiB |
Environment, per resident camera stack | 90 MiB |
Screen surfaces | 40 MiB |

Music generation outranks the picture. If free VRAM approaches the `GPUManager` floor the
renderer sheds quality and evicts non-active camera stacks, rather than letting a generation
fail. That ordering is an architectural commitment (`ADR_VISUAL_RUNTIME.md` §4), not a tuning
preference.
