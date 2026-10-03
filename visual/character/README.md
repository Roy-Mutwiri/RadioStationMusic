# visual/character/

The **one** master layered model for TF_TRADER_01, its rig, and its exported atlases.

| File | Role |
|---|---|
`TF_TRADER_01.master.psd` | The single layered source. Layer names MUST match `docs/visual/CHARACTER_BIBLE.md` §4 exactly — the exporter fails the build on a mismatch |
`TF_TRADER_01.rig.json` | Skeleton, joint DoF, per-layer vertex weights, depth-ordering swap thresholds |
`TF_TRADER_01.face.json` | The six expressions and their amplitude caps |
`atlas/` | Packed, compressed texture atlases. One set, shared by all seven cameras |

## Rules

- **One head, painted once.** Every camera uses the same `20_head` layers under a different
  projection. A per-camera head repaint is the exact failure the identity lock exists to stop.
- **`60_lighting` is never painted in.** Monitor spill and amber key are composited at runtime
  so they can modulate with market state. Baking them makes the lighting response in
  `OFFICE_BIBLE.md` §5 unimplementable.
- **Identity is frozen once accepted.** `CHARACTER_BIBLE.md` §10 lists what may drift over
  weeks and what may never change. Nothing in the second list is editable here.
- Seated geometry must agree with the blockout: elbow z700, eye z1295, forearms resting on a
  z735 desk surface from mid-forearm forward.
