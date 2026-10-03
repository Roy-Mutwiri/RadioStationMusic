# Office bible — TRADE_FIX_OFFICE_01

The canonical definition of the environment. Numeric geometry lives in
`visual/environment/TRADE_FIX_OFFICE_01.blockout.json`, which is **authoritative** — this
document explains and justifies it, the JSON decides it. Where a painted plate disagrees with
the blockout, the plate is wrong.

Drawings: `visual/references/office/TRADE_FIX_OFFICE_01_plan.svg` (floor plan with camera
positions) and `..._elevations.svg` (the four walls).

---

## 1. What the room has to say

> High-end but lived-in. Intense. Caffeine-heavy. Deep into the trenches. Disciplined rather
> than luxurious for luxury's sake.

The distinction the whole room turns on: **this is not a wealth display, it is a workspace that
has been used hard and kept in order.** Wealth signals — marble, chrome, a car key on the desk,
an expensive watch *displayed* — are forbidden. Discipline signals — a notebook with real marks
in it, books that have been opened, four screens arranged deliberately rather than
decoratively, one mug and not five — are the point.

Two reference failures to steer away from by name:

- **The crypto-influencer office.** RGB, neon, glass, a visible price ticker as decor, slogans
  on every surface. Ruled out by the palette rule in §2 and the branding budget in §8.
- **The sterile corporate desk.** Nothing out of place, no coffee, no notebook, no wear. It
  contradicts *lived-in* and makes the character's presence unexplained.

The room should read as though he has sat down at it a thousand times.

---

## 2. Palette — authoritative

Taken from `frontend/tailwind.config.js`, which is not a theme file but the project's **written
art direction**. Its own words: *"Gold is an accent, not a theme. It marks exactly one thing per
screen. The moment a second element claims it, it stops meaning anything."* The office obeys
that literally.

| Role | Values | Where |
|---|---|---|
| Deepest black | `#07080a` | Shadow, south wall, monitor bezels, floor in shadow |
| Panel black | `#0c0e12` | Desk surface, cabinetry, chair |
| Raised surface | `#151922` – `#1b202b` | Desk edge, shelf faces, lit cabinet fronts |
| Mid structure | `#232935` – `#2f3744` | Walls in amber falloff, shelf interiors |
| Material detail | `#444d5d` – `#6b7383` | Monitor arms, hardware, cable, mic |
| Light text / paper | `#9aa1ad` – `#e6e8ec` | Notebook paper, book pages, screen text |
| **Gold accent** | `#8a6f1c`, `#c9a227`, `#d9b949`, `#e7cd74`, `#f0e0a6` | See the budget below |
| Warm amber light | `#c9962f` tinted toward `#e7cd74` at the source | Lamp pools, shelf practicals |
| Cool exterior | `#4a7fc2` desaturated toward `#232935` at distance | City, sky, window fill |
| Monitor cyan | `#4a7fc2` – `#9aa1ad` | Screen glow and spill |
| Market up / down | `#4f9d69` / `#c2504a` | **Chart content only.** Never architecture, never props |

### The gold budget

Gold is metered, not decorated with. **Total gold must occupy under 2 % of frame area in every
camera**, and it may appear on at most **four** objects in any single frame:

1. The headphone pivot rings and cup marks (counted as one object).
2. The watch hands and indices.
3. The mug's Trade Fix mark.
4. One architectural element — the wall statement in `TF_GOLD_STATEMENT`, or a single shelf
   edge inlay, **never both in the same frame**.

The pendant, when visible on a forward lean, replaces slot 4 rather than adding to it.

**Market colours are chart-only, and this is a hard rule.** Green and red on a wall, a plant
pot or an LED would make the room itself appear to express a market opinion, and the station's
honesty rules forbid implying a market claim the data does not support. Green and red appear
only inside screen content, where they mean what they mean on a chart.

---

## 3. Shell and coordinate system

Right-handed, millimetres. **Origin at the floor of the south-west corner.** +X east, +Y north,
+Z up. Every camera, anchor and gaze target in every other document resolves against this.

| | |
|---|---|
| Footprint | 6 400 (X) × 4 800 (Y) |
| Ceiling | 3 000 |
| Floor | Wide dark oak, `#0c0e12`, matte with a long low sheen that catches the lamp pools |
| Ceiling | Flat dark plaster, `#07080a`. Unlit and effectively invisible except in CAM 5 |

| Wall | Plane | Role |
|---|---|---|
| **North** | Y = 4 800 | **Zone E** — the window wall and city |
| **West** | X = 0 | **Zone D** — bookshelf / discipline wall, and the two mounted chart panels |
| **East** | X = 6 400 | **Zone C** — coffee / reset zone, door |
| **South** | Y = 0 | The camera side. Dark acoustic felt, `#07080a`. Almost never in frame |

### Orientation, and why it is this way round

**The character sits at X = 3 200, Y = 3 000, facing south (−Y).** Four things follow, and all
four are the reason for the choice:

1. **The window is behind him.** Every front-facing camera gets the city skyline as his
   backdrop and the window as a cool rim on his shoulders and hair. This is the composition
   that makes the hero shots premium, and it comes free from the layout rather than from
   lighting tricks.
2. **The monitors sit between him and the front cameras.** Unavoidable at any desk, and turned
   into an asset: the array reads as a dark ledge across the lower frame with cyan glow rising
   onto his face. Front cameras shoot *over* the top bezel line at Z = 1 298.
3. **His right hand is to the west**, so the mouse, the right-hand lamp and the notebook are
   all on the west half of the desk, and the mic and stream pad are on the east half. Derived,
   not chosen: facing −Y with +Z up puts his right at −X.
4. **His left wrist — and therefore the watch — faces east**, which is why CAM 2 is an
   east-side camera. The side profile is the only shot where the watch reads.

---

## 4. Zones

### A — Trading desk

| | |
|---|---|
| Extent | X 1 400 → 5 000 (3 600 wide), Y 2 200 → 3 000 (800 deep) |
| Surface | **Z = 735** |
| Material | Black ash veneer `#0c0e12`, matte, 40 mm apron with a `#1b202b` edge |
| Legs | Blackened steel, inset 200 mm |
| Desk mat | Black felt, 1 050 × 400, X 2 600 → 3 650, Y 2 500 → 2 900 |
| Chair | Black mesh task chair, seat pan **Z = 460**, centre X 3 200, Y 3 250 |

Desk at 735 against a seated elbow at 700 (`CHARACTER_BIBLE.md` §7) means his forearms rest on
the surface from mid-forearm forward with a slight rise to the wrists. That 35 mm relationship
is what makes typing and mouse work read as contact rather than hover, and it is the first
thing to check if a desk motion looks wrong.

**Deliberately kept clear:** X 2 600 → 3 650 between Y 2 500 and 2 900 is the hand working
envelope. Nothing may be placed in it except the keyboard, mouse, mat and the mug's landing
ring. The brief's warning about clutter that makes movements impossible is enforced as a
reserved volume, not as taste.

**The 830 mm reach rule, which decides most of the layout below.** A seated man's functional
reach from the shoulder at (3 200, 3 000, 1 080) is upper arm 365 + forearm 270 + hand 195 =
**830 mm**. The desk is 3 600 mm wide, so its outer thirds are physically unreachable from the
chair. Every object he touches must therefore sit inside that radius, and everything outside it
is decor carrying no interaction anchor. This is not a guideline — the blockout validator fails
on any anchor beyond it, and the first run of that check rejected the notebook, the pen and the
stream pad, all of which had been placed for composition rather than for reach.

On-desk objects, west to east. **Reach** is the distance from the seated shoulder.

| Object | Centre (X, Y) | Reach | Note |
|---|---|---|---|
| Desk lamp, articulated | 1 600, 2 880 | — | **Primary warm key.** Head at Z 1 180, aimed west-down into the notebook |
| Trading books, two stacked | 1 550, 2 350 | *decor* | Spines dark, edges worn. Titles illegible by design. Sits under `MON_5` |
| Pen | 2 480, 2 890 | 802 mm | Matte black, on the notebook's near gutter |
| Notebook, open | 2 480, 2 810 | 818 mm | A5, grid, visible pen marks, one dog-eared corner |
| **Mouse** | 2 720, 2 560 | 726 mm | Matte black, low profile, on the mat |
| **Keyboard** | 3 200, 2 740 | 432 mm | 75 % layout, black, unlit or faint white |
| **Mug** | 3 760, 2 620 | 760 mm | Matte black, gold Trade Fix mark. Steam when hot — see §7 |
| Stream pad | 3 900, 2 860 | 783 mm | 6 keys, dim white icons. **No RGB** |
| Phone, face down | 4 050, 2 420 | *decor* | Black. Face down is a discipline signal — he is not meant to pick it up |
| Mic on a low boom | 4 620, 2 950 | *decor* | Dynamic broadcast mic, black, arm entering from the east. Never adjusted on air |
| Laptop, closed | 4 760, 2 500 | *decor* | Black, lid shut, pushed back beside the `MON_4` stand |

### B — Main chart wall

Five desk monitors on a single black crossbar at Y = 2 350, plus two wall-mounted panels on the
west wall. Together these are the "chart wall" the brief names — a wall of charts when read
from over his shoulder (CAM 3) and a dark glowing ledge when read from the front.

All desk screens are centred at **Z = 1 130** and tilted back 8°, facing north toward him.

| ID | Role | Size | Centre X | Panel |
|---|---|---|---|---|
| `MON_1` | **Active market chart** — the primary | 27″ | 3 200 | 598 × 336 |
| `MON_2` | Secondary timeframe of the same symbol | 27″ | 2 500 | 598 × 336 |
| `MON_3` | Watchlist and market statistics | 27″ | 3 900 | 598 × 336 |
| `MON_4` | **Trade Fix Radio system / stream activity** | 24″ | 4 560, yawed 22° toward him | 531 × 299 |
| `MON_5` | News and session information *(optional)* | 24″ | 1 840, yawed −22° | 531 × 299 |
| `PANEL_W1` | Long-horizon chart | 32″ wall-mounted, west wall, Y 1 900, Z 1 700 | 708 × 398 |
| `PANEL_W2` | Session / correlation board | 32″ wall-mounted, west wall, Y 2 600, Z 1 700 | 708 × 398 |

Both wall panels sit **forward of his shoulder line**, which is why `PANEL_W2` is at Y 2 600 and
not further north. At Y 3 300 it would have been *behind* him — he faces −Y from Y 3 000 — and
reading it would have needed a 95° turn. Caught by the gaze-range check in the blockout
validator.

**The screen height is set by his eye line, and it constrains every front camera.** Eye at
Z = 1 295 with panel centres at 1 130 puts the top bezel at **Z = 1 298** — level with his eyes,
which is correct ergonomics and gives a **14.2° downward gaze** to the centre of the primary
chart: −165 mm over 650 mm horizontal, inside the 15–20° ergonomic band. That is the reason the
gaze system carries a negative default pitch (`MOTION_LIBRARY.md` §7).

It is also the reason the screens are at this height and not higher. At a more conventional
1 250 mm centre the top bezel would sit at 1 418 — 123 mm **above** his eyes and above his chin
at 1 215 — and every front camera at eye level would have his face hidden behind a monitor.
Front cameras must clear the 1 298 line, which is why CAM 1, CAM 4 and CAM 7 are all elevated
and angled slightly down (`CAMERA_PLAN.md` §3). In CAM 1 the bezel line lands at 30 % of frame
height, crossing just above his shoulders: the glow-ledge composition.

Screen content rules:

- **Every screen is live**, driven by the state frame. No painted screenshots. A frozen chart on
  a 24/7 stream is noticed within minutes.
- `MON_1` and `MON_2` **follow the active symbol.** When the router moves XAUUSD → BTCUSD both
  retitle and redraw. `MON_3`'s watchlist reorders. `MON_4` shows the switch as an event.
  `MON_5` updates the session line.
- **Screens never fabricate.** When the feed is `STALE` or `DISCONNECTED`, charts show their
  last known data explicitly marked stale and stop advancing. This is the same rule the station
  already enforces in `MarketStateV1`, where a price is *unrepresentable* unless the feed is
  live — and the honest visual equivalent is a chart that visibly stops rather than one that
  keeps drawing invented candles. See `STATE_BRIDGE.md` §8.
- **No green or red outside chart marks and P&L figures.** §2's rule.
- Screens are the room's brightest element and set its colour temperature. Their glow is a
  real light source in the composite, not a texture.

### C — Coffee / reset zone

East wall, X 5 600 → 6 400. The room's second pole: he goes here, or reaches toward it, and
comes back. Its existence is what makes `CAFFEINE_BREAK` legible as a destination.

| Object | Position | Note |
|---|---|---|
| Low cabinet | X 5 700 → 6 350, Y 2 600 → 3 100, top Z 900 | Matte black, `#151922` front |
| Espresso machine | X 6 000, Y 2 850, base Z 900 | Black and steel. One small warm power LED |
| Mug shelf, two spares | X 6 220, Y 3 000, Z 1 320 | Both black. Only the desk mug carries the gold mark |
| Warm practical, under-shelf | X 6 100, Y 2 950, Z 1 300 | **Secondary amber source.** Pools on the cabinet top |
| Tall plant — *Zamioculcas* | X 6 150, Y 1 500, base Z 0 | Dark glossy leaves, 1 400 tall, matte black pot |
| Door | X 6 400, Y 600 → 1 500 | Closed, flush, dark. Visible only in CAM 5 |

### D — Bookshelf / discipline wall

West wall, X 0 → 400, Y 900 → 4 200. Open blackened-steel shelving, five shelves at Z = 500,
900, 1 300, 1 700 and 2 100, with the two mounted chart panels from §B set into the run.

| Shelf | Contents |
|---|---|
| 500 | Box files, dark. Three ring binders |
| 900 | Trading and market-structure books stood upright, 14–18 of them. Spines dark, worn, **no legible titles** |
| 1 300 | **Warm practical** at Y 2 400 washing down over the books — the third amber source. Two books laid flat. A small trailing plant (*Epipremnum*) at Y 3 600 |
| 1 700 | Interrupted by `PANEL_W1` / `PANEL_W2`. One closed notebook, one small black clock with a **moving second hand** |
| 2 100 | Mostly empty. `TF_GOLD_STATEMENT` sits above the run at Z 2 400 |

Deliberately unfilled shelf space. A wall packed to every edge reads as decoration; a wall with
gaps reads as one someone actually takes things off.

### E — Window and city

North wall, Y = 4 800. Glazing from X 800 → 5 600, sill Z 400, head Z 2 700. Three bays with
two slim blackened mullions at X 2 400 and X 4 000.

The mullions matter: they break the city into panels, give the parallax something to read
against, and divide the backdrop so it cannot become a flat glowing rectangle behind his head.

Beyond the glass, three depth planes:

| Plane | Distance | Content |
|---|---|---|
| Near | 15–40 m | Two building masses, mostly dark, window grids in warm white and cool white. Some windows dark, a few amber. **Slow, sparse change** |
| Mid | 80–250 m | Tower silhouettes with lit crowns and a scatter of occupied floors. One aircraft warning light, slow red pulse |
| Far | 400 m+ | Low haze band, `#232935` to `#4a7fc2`, the city's glow against the sky. Slow cloud drift |

Between them at the near plane, one elevated road: **traffic as slow-moving warm and cool
points of light**, never resolved into vehicles.

Night at all times. The sky is a deep desaturated blue-black, `#07080a` at the top graduating
to `#232935` near the horizon. **Never a sunrise, never a sunset, never daylight.** The station
runs around the clock and the room's answer to that is a night that does not resolve — the one
place the brief's hours-without-end quality is carried by the set rather than by the character.

**The city must not compete.** Combined window luminance stays below the screens and below the
amber pools. No single light outside is brighter than the desk lamp. It is a backdrop that
rewards a look, not a view that pulls the eye off him.

### F — Stream / microphone zone

Not a separate corner but the desk's east end: the mic boom at X 4 620, the stream pad at
X 4 300, the closed laptop at X 4 700, and `MON_4` yawed toward him showing station activity.
Reading it as part of the desk rather than as a studio is intentional — he is a trader who
happens to be broadcasting, not a broadcaster with a chart open.

A single small gold `ON AIR` indicator sits on the mic arm. It is the only element in the room
driven by station state rather than market state, and it does not count against the gold budget
because it is under 4 mm in frame.

---

## 5. Lighting

Four layers, each with a distinct colour temperature, direction and job. Composited at runtime
from `60_lighting` rather than baked into plates (`CHARACTER_BIBLE.md` §4, rule 3) so they can
modulate.

| Layer | Source | Temp | Direction | Job |
|---|---|---|---|---|
| **Warm key** | Desk lamp (X 1 600), shelf practical (Z 1 300), coffee practical | ~2 700 K | West and above, falling | The room's warmth, and the pools that make the floor and desk read as material. Strongest single source |
| **Cool fill** | The window | ~7 500 K, low intensity | North, behind him, broad | **Rim on his shoulders, hair and jaw edge.** Wraps to both shoulder edges; in cropped portrait views it reads as a right-side rim |
| **Monitor spill** | `MON_1`–`MON_5`, `PANEL_W1/2` | ~6 500 K cyan | South, below eye line, upward | Fills the jaw, cheekbones and the underside of the brow. The light that says he is looking at something |
| **Gold accent** | Statement wall, mug mark, headphone rings, watch | ~2 400 K, tiny | Local | Specular glints only. Never illuminates anything |

Three pools of practical light with deep falloff between them, and the darkness between the
pools is as composed as the pools. Shadows are rich and controlled — black but not crushed,
never flat.

### Modulation

Lighting responds, but only just. Target: an attentive viewer notices after twenty minutes; a
casual one never consciously does.

| Driver | Effect | Range |
|---|---|---|
| Market energy | Monitor spill intensity and saturation | ±8 % over 0–100 energy |
| Market energy | Warm key intensity, **inversely** — high energy cools the room | ±5 % |
| Music energy | Monitor spill, a very slow breathing modulation | ±3 %, 8–20 s period |
| Hour of night | Warm key drifts warmer and down after 02:00 local | −6 % intensity, −150 K |
| `EXTREME_VOLATILITY` | Monitor spill gains contrast, not brightness | +12 % contrast |
| Station `BUFFER_LOW` | **Nothing.** Operational state is never dramatised in the set | — |

**Forbidden absolutely:** any flashing, any beat-synchronised brightness pulse, any hue
rotation, any RGB fixture, any light that strobes or sweeps. The brief rules out gamer lighting
by name, and a 24/7 stream with a pulsing room is unwatchable within an hour.

---

## 6. Ambient motion

Continuous background life, independent of the character and of each other. Nothing here is
ever synchronised, and every period is irrational relative to the others so no two elements
ever drift into lockstep.

| Element | Motion | Period | Amplitude |
|---|---|---|---|
| **Coffee steam** | Rising wisp, turbulent, fading at ~250 mm | Continuous while hot | See §7 |
| Plant — ZZ, east | Very slow leaf sway | 11–17 s, randomised | ±1.5° |
| Plant — trailing, west shelf | Slower, smaller | 19–26 s | ±1.0° |
| Monitor chart content | Live candle formation, scrolling price | Driven by the state frame | — |
| Monitor refresh | Faint scanline shimmer | 1/60 s, sub-threshold | Barely perceptible |
| Shelf clock second hand | Tick | 1 s | Discrete |
| Espresso machine LED | Slow warm breathe | 4.3 s | 70–100 % |
| Stream pad icons | Very faint brightness drift | 7–13 s | ±4 % |
| `ON AIR` indicator | Steady while broadcasting | — | — |
| City — near windows | Individual windows change state | 40–400 s, sparse | Discrete |
| City — traffic | Points of light traversing the elevated road | 8–20 s per point | — |
| City — aircraft light | Red pulse | 2.0 s | — |
| City — clouds | Slow drift | 180–400 s to cross | — |
| Dust | Very sparse motes in the amber pools | Continuous | Near-invisible |
| Camera | Slow breathing push or drift on the active camera | 45–90 s | 0.3–0.8 % |

**No cat.** The brief offers one as optional. Declined: it is a strong borrowed signal from the
reference product this project is explicitly building an original alternative to, and a moving
creature in frame competes with the character for the eye. The plants, the steam and the city
carry the ambient-life load without borrowing anything.

---

## 7. The coffee, in detail

The mug is the busiest prop in the room and the most likely to look wrong, so it gets its own
section and its own state machine.

| State | Duration | Steam | Appearance |
|---|---|---|---|
| `FRESH` | 0–4 min after a refill | Strong, visible, turbulent | Full, meniscus near the rim |
| `HOT` | 4–12 min | Moderate | Full |
| `WARM` | 12–25 min | Faint wisp, intermittent | Level drops with each sip |
| `COLD` | 25 min+ | **None** | Low. A faint ring mark inside the rim |
| `EMPTY` | — | None | Visible ring marks. Triggers a bias toward a refill trip |

Rules that keep it believable:

- **Steam tracks temperature, and temperature only.** Steam from a mug that has sat for forty
  minutes is the single most common tell that an animated scene is faked.
- **The level only ever falls**, except across a refill. Each sip removes 8–14 %.
- **The mug has one landing ring** at (3 760, 2 620). It returns there within ±6 mm every time.
  It never ends a sequence anywhere else, and this is a hard constraint, because a mug that
  drifts across the desk over an hour is unrecoverable without a visible snap.
- A refill is a `CAFFEINE_BREAK` trip to Zone C. The brief permits an occasional reach toward
  the coffee machine area, and this is it — on a fixed-camera composition the trip may be
  covered by a camera cut, but the mug state must be consistent on both sides of the cut.
- The mug is lifted with the **left** hand, which keeps the right on the mouse. A right-handed
  lift would mean releasing the mouse every time, which both looks wrong and makes the sip
  non-interruptible for no reason.

---

## 8. Branding budget

Restraint is the requirement. The brief offers four statements and asks for one or two.

**Two placements in the whole room:**

| Placement | Content | Treatment |
|---|---|---|
| `TF_GOLD_STATEMENT` — west wall above the shelving, X 60, Y 2 100 → 3 400, Z 2 400 | **`DISCIPLINE · EXECUTION · FREEDOM`** | Brushed-gold lettering, 90 mm cap height, flush-mounted, lit only by falloff from the Z 1 300 practical. Legible, never bright |
| `TF_MARK_SOUTH` — south wall, X 3 200, Z 1 900 | The Trade Fix mark alone, no words | 180 mm, dark-on-dark, `#1b202b` on `#07080a`. Visible only when the amber pool catches it, and in frame **only in CAM 3** |

That is all. Not on the mug *and* the wall *and* the chair *and* the mat — the mug carries one
mark, he carries one on his chest, the headphones carry their pair, the wall carries one
statement. Five marks in a room, four of them under 25 mm.

`TRADE FIX RADIO` as a wordmark appears **only inside `MON_4`'s interface**, where it is part
of the station's own UI and reads as software rather than as signage.

---

## 9. Reference views

Seven environment plates, one per camera, all from the same blockout. Painted **after** the
character's view 1 is accepted and **before** any rigging, because the seated geometry in
`CHARACTER_BIBLE.md` §7 has to be verified against the real desk before the rig is built to it.

| # | Plate | Camera | Must establish |
|---|---|---|---|
| 1 | **Hero front** | CAM 1 | The master composition: monitor ledge low in frame, him centred, city behind. The plate every other one is judged against for colour and value |
| 2 | Side office | CAM 2 | Desk depth, window on the right, watch on the left wrist, headphone band profile |
| 3 | Over-shoulder | CAM 3 | The chart wall lit, `TF_MARK_SOUTH`, the back of the headband, the room receding south |
| 4 | Wide office | CAM 5 | All six zones in one frame. **The spatial coherence check** — if any other plate disagrees with this one, that plate is wrong |
| 5 | Desk detail | CAM 6 | Keyboard, mouse, mat, notebook, mug landing ring, hand envelope. The anchor-accuracy plate |
| 6 | Coffee zone | CAM 5 variant, east | Zone C as a destination: machine, mug shelf, practical, plant |
| 7 | Window / city | CAM 2 variant, north-biased | The three city depth planes, mullion division, traffic road |

Shared constraints for all seven:

- **One room.** Any object visible in two plates is in the same place, at the same scale, in
  both. Re-render from the blockout when in doubt; the blockout is never the thing that is wrong.
- Lighting layers delivered separately and unbaked.
- Night exterior in all seven. No plate implies a time of day other than night.
- Gold under 2 % of frame area, at most four objects (§2).
- 300 dpi at 1.5× final output size, so a slow push-in has real pixels to move into.
- Delivered as a **layer stack with depth assignments**, not a flat image. A flat plate cannot
  parallax, and without parallax the push-in and drift in §6 have nothing to work with.

---

## 10. Acceptance checklist

- [ ] Every object position agrees with `TRADE_FIX_OFFICE_01.blockout.json`
- [ ] Desk surface at Z 735; eye at Z 1 295; monitor centres at Z 1 130; top bezel at Z 1 298
- [ ] Hand working envelope (X 2 600–3 650, Y 2 500–2 900) clear of everything but keyboard, mouse, mat and mug ring
- [ ] Every interactive object within 830 mm of the seated shoulder; everything beyond it is decor with no anchor
- [ ] Mug sits on its landing ring at (3 760, 2 620)
- [ ] `scripts/visual/validate_blockout.py` passes
- [ ] All five desk screens plus both wall panels present and oriented toward X 3 200, Y 3 000
- [ ] Night exterior; three city depth planes; two mullions at X 2 400 and 4 000
- [ ] Window luminance below screen and amber-pool luminance
- [ ] Three warm pools with real falloff between them
- [ ] Gold on at most four objects, under 2 % of frame area
- [ ] Green and red nowhere except inside chart content
- [ ] No RGB, no neon, no flashing, no visible light fixture other than the three practicals
- [ ] Exactly two brand placements, per §8
- [ ] Lived-in: notebook marked, books worn, one mug, shelf gaps — and **clean**, not filthy
- [ ] Lighting layers separate and unbaked
- [ ] Delivered as a depth-assigned layer stack
- [ ] Cross-checked against the CAM 5 wide plate for spatial agreement
