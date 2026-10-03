"""Render the canonical reference drawings from the authoritative blockout.

Two outputs:

  visual/references/office/TRADE_FIX_OFFICE_01_plan.svg
  visual/references/character/TF_TRADER_01_proportions.svg

Generated rather than drawn, for the reason ADR-13 gives: the blockout is the single
source of spatial truth, and a hand-drawn plan is a second source that can disagree with
it. Re-run after any change to the blockout.

    python scripts/visual/render_reference_svg.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLOCKOUT = ROOT / "visual" / "environment" / "TRADE_FIX_OFFICE_01.blockout.json"

# Brand palette — frontend/tailwind.config.js is the project's written art direction.
INK_950, INK_900, INK_800 = "#07080a", "#0c0e12", "#151922"
INK_700, INK_600, INK_500 = "#232935", "#2f3744", "#444d5d"
INK_400, INK_300, INK_100 = "#6b7383", "#9aa1ad", "#e6e8ec"
GOLD, GOLD_DIM, GOLD_LIGHT = "#c9a227", "#8a6f1c", "#e7cd74"
BLUE, GREEN, RED = "#4a7fc2", "#4f9d69", "#c2504a"

FONT = "Inter, 'Segoe UI', system-ui, sans-serif"
MONO = "'JetBrains Mono', Consolas, ui-monospace, monospace"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, w: int, h: int, title: str) -> None:
        self.w, self.h, self.title = w, h, title
        self.parts: list[str] = []

    def add(self, s: str) -> None:
        self.parts.append("  " + s)

    def rect(self, x, y, w, h, fill="none", stroke=INK_500, sw=2, dash=None, rx=0, op=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op != 1.0 else ""
        self.add(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{o}/>'
        )

    def line(self, x1, y1, x2, y2, stroke=INK_500, sw=2, dash=None, op=1.0, cap="butt"):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op != 1.0 else ""
        self.add(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}"{d}{o}/>'
        )

    def circle(self, cx, cy, r, fill="none", stroke=INK_500, sw=2, dash=None, op=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op != 1.0 else ""
        self.add(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d}{o}/>'
        )

    def poly(self, pts, fill="none", stroke=INK_500, sw=2, op=1.0):
        p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        self.add(f'<polygon points="{p}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{op}"/>')

    def text(self, x, y, s, size=34, fill=INK_300, anchor="start", family=FONT,
             weight="400", ls=0.0, op=1.0, rot=None):
        t = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot is not None else ""
        self.add(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" letter-spacing="{ls}" fill="{fill}" '
            f'text-anchor="{anchor}" opacity="{op}"{t}>{esc(s)}</text>'
        )

    def render(self) -> str:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'width="{self.w}" height="{self.h}" role="img">\n'
            f"  <title>{esc(self.title)}</title>\n"
            f'  <rect width="{self.w}" height="{self.h}" fill="{INK_950}"/>\n'
        )
        return head + "\n".join(self.parts) + "\n</svg>\n"


# ----------------------------------------------------------------- floor plan


def render_plan(b: dict) -> str:
    rm = b["room"]["size"]
    RX, RY = rm["x"], rm["y"]
    M, TOP = 620, 760          # margins
    W, H = RX + 2 * M, RY + TOP + M + 520

    def f(x, y):
        """Room (mm, +Y north) -> SVG (+y down)."""
        return M + x, TOP + (RY - y)

    s = Svg(W, H, "TRADE_FIX_OFFICE_01 — floor plan and camera layout")

    s.text(M, 150, "TRADE_FIX_OFFICE_01", 86, INK_100, weight="600", ls=3)
    s.text(M, 225, "Floor plan and camera layout · millimetres · origin at the floor of the south-west corner",
           34, INK_400)
    s.text(M, 283, "GENERATED FROM TRADE_FIX_OFFICE_01.blockout.json — do not edit by hand",
           28, GOLD_DIM, family=MONO, ls=1)

    # --- compass
    cx, cy = W - M - 150, 215
    s.circle(cx, cy, 92, stroke=INK_700, sw=2)
    s.line(cx, cy + 62, cx, cy - 62, INK_500, 3)
    s.poly([(cx, cy - 78), (cx - 15, cy - 50), (cx + 15, cy - 50)], fill=GOLD, stroke="none")
    s.text(cx, cy - 98, "N", 34, GOLD, anchor="middle", weight="600")
    s.text(cx, cy + 128, "+Y", 24, INK_500, anchor="middle", family=MONO)
    s.text(cx + 112, cy + 10, "+X", 24, INK_500, anchor="middle", family=MONO)
    s.line(cx - 62, cy, cx + 62, cy, INK_600, 2)

    # --- room shell
    x0, y0 = f(0, RY)
    s.rect(x0, y0, RX, RY, fill=INK_900, stroke=INK_600, sw=5)

    # --- grid, 1 m
    for gx in range(1000, RX, 1000):
        a, bb = f(gx, 0), f(gx, RY)
        s.line(a[0], a[1], bb[0], bb[1], INK_800, 1.5)
    for gy in range(1000, RY, 1000):
        a, bb = f(0, gy), f(RX, gy)
        s.line(a[0], a[1], bb[0], bb[1], INK_800, 1.5)

    # --- window wall (north)
    g = b["zones"]["E_window"]["glazing"]
    a, bb = f(g["x"][0], RY), f(g["x"][1], RY)
    s.line(a[0], a[1], bb[0], bb[1], BLUE, 16)
    s.line(a[0], a[1] - 22, bb[0], bb[1] - 22, BLUE, 5, op=0.45)
    s.line(a[0], a[1] - 46, bb[0], bb[1] - 46, BLUE, 3, op=0.25)
    for mx in b["zones"]["E_window"]["mullions_x"]:
        p = f(mx, RY)
        s.line(p[0], p[1] - 34, p[0], p[1] + 34, INK_600, 7)
    mid = f((g["x"][0] + g["x"][1]) / 2, RY)
    s.text(mid[0], mid[1] - 86, "ZONE E — WINDOW / NIGHT CITY", 36, BLUE, anchor="middle", ls=2, weight="600")
    s.text(mid[0], mid[1] - 46 + 78, "glazing x 800–5600 · sill z400 · head z2700 · mullions x2400, x4000",
           27, INK_400, anchor="middle")

    # --- west: shelving + statement
    d = b["zones"]["D_bookshelf"]["extent"]
    p = f(d["x"][1], d["y"][1])
    s.rect(p[0] - 400, p[1], 400, d["y"][1] - d["y"][0], fill=INK_800, stroke=INK_600, sw=3)
    lab = f(200, (d["y"][0] + d["y"][1]) / 2)
    s.text(lab[0], lab[1], "ZONE D — BOOKSHELF", 32, INK_300, anchor="middle", ls=2,
           weight="600", rot=-90)
    st = b["branding"][0]["position"]
    a, bb = f(st["x"], st["y"][0]), f(st["x"], st["y"][1])
    s.line(a[0], a[1], bb[0], bb[1], GOLD, 11)
    s.text(a[0] + 62, (a[1] + bb[1]) / 2 + 12, "TF_GOLD_STATEMENT", 27, GOLD, family=MONO)

    # --- east: coffee zone
    cab = b["zones"]["C_coffee"]["objects"][0]["extent"]
    p = f(cab["x"][0], cab["y"][1])
    s.rect(p[0], p[1], cab["x"][1] - cab["x"][0], cab["y"][1] - cab["y"][0],
           fill=INK_800, stroke=INK_600, sw=3)
    lab = f(6000, 3600)
    s.text(lab[0], lab[1], "ZONE C", 32, INK_300, anchor="middle", ls=2, weight="600")
    s.text(lab[0], lab[1] + 46, "COFFEE / RESET", 27, INK_400, anchor="middle")
    for oid, ox, oy in (("espresso", 6000, 2850), ("mug shelf", 6220, 3000)):
        p = f(ox, oy)
        s.circle(p[0], p[1], 26, fill=INK_700, stroke=INK_500, sw=2)
    pz = f(6150, 1500)
    s.circle(pz[0], pz[1], 78, fill="none", stroke=GREEN, sw=3, op=0.5)
    s.text(pz[0], pz[1] + 128, "plant", 26, INK_500, anchor="middle")
    dr = b["zones"]["C_coffee"]["objects"][5]["extent"]
    a, bb = f(dr["x"], dr["y"][0]), f(dr["x"], dr["y"][1])
    s.line(a[0], a[1], bb[0], bb[1], INK_400, 11)
    s.text(a[0] - 36, (a[1] + bb[1]) / 2, "door", 26, INK_500, anchor="end")

    # --- south wall mark
    p = f(3200, 0)
    s.line(p[0] - 300, p[1], p[0] + 300, p[1], INK_700, 9)
    s.text(p[0], p[1] + 56, "TF_MARK_SOUTH  ·  south wall is the camera side", 28, INK_500, anchor="middle")

    # --- desk
    de = b["desk"]["extent"]
    p = f(de["x"][0], de["y"][1])
    dw, dh = de["x"][1] - de["x"][0], de["y"][1] - de["y"][0]
    s.rect(p[0], p[1], dw, dh, fill=INK_800, stroke=INK_400, sw=4)
    s.text(p[0] + 18, p[1] + dh - 20, "ZONE A — TRADING DESK   surface z735", 30, INK_300, weight="600")

    # --- desk mat + reserved envelope
    mt = b["desk"]["mat"]["extent"]
    p = f(mt["x"][0], mt["y"][1])
    s.rect(p[0], p[1], mt["x"][1] - mt["x"][0], mt["y"][1] - mt["y"][0],
           fill=INK_700, stroke=INK_600, sw=2, op=0.8)
    rv = b["reserved_volumes"][0]["extent"]
    p = f(rv["x"][0], rv["y"][1])
    s.rect(p[0], p[1], rv["x"][1] - rv["x"][0], rv["y"][1] - rv["y"][0],
           fill="none", stroke=GOLD_DIM, sw=3, dash="14 10")

    # --- monitors
    for m in b["monitors"]["panels"]:
        c = m["centre"]
        if m.get("mounted") == "west wall":
            p = f(c["x"], c["y"])
            s.rect(p[0], p[1] - m["panel"]["w"] / 2, 40, m["panel"]["w"],
                   fill=BLUE, stroke=INK_100, sw=2, op=0.5)
            s.text(p[0] + 62, p[1] + 10, m["id"], 26, BLUE, family=MONO)
            continue
        yaw = math.radians(m.get("yaw_degrees", 0))
        half = m["panel"]["w"] / 2
        # screen face in plan: a line through the centre, rotated by yaw about Z
        dx, dy = half * math.cos(yaw), half * math.sin(yaw)
        a, bb = f(c["x"] - dx, c["y"] - dy), f(c["x"] + dx, c["y"] + dy)
        s.line(a[0], a[1], bb[0], bb[1], BLUE, 17)
        p = f(c["x"], c["y"])
        s.text(p[0], p[1] + 52, m["id"], 26, BLUE, anchor="middle", family=MONO)
    p = f(3200, 2350)
    s.text(p[0], p[1] - 34, "ZONE B — CHART ARRAY   centre z1130 · top bezel z1298",
           29, BLUE, anchor="middle", weight="600")

    # --- desk objects
    for o in b["desk_objects"]:
        pos = o["position"]
        p = f(pos["x"], pos["y"])
        reach = o.get("reachable", True)
        col = INK_300 if reach else INK_600
        if o["id"] == "KEYBOARD":
            s.rect(p[0] - 160, p[1] - 60, 320, 120, fill=INK_700, stroke=INK_300, sw=3)
        elif o["id"] in ("MOUSE", "MUG"):
            s.circle(p[0], p[1], 34, fill=INK_700, stroke=GOLD if o["id"] == "MUG" else INK_300, sw=3)
        elif o["id"] == "NOTEBOOK":
            s.rect(p[0] - 74, p[1] - 105, 148, 210, fill=INK_700, stroke=INK_300, sw=3)
        else:
            s.circle(p[0], p[1], 22, fill=INK_800, stroke=col, sw=2)
        if o["id"] in ("ON_AIR_LED",):
            continue
        name = o["id"].lower()
        off = {"PEN": -34, "NOTEBOOK": -128, "KEYBOARD": 92, "MOUSE": -54,
               "MUG": -54, "STREAM_PAD": -42}.get(o["id"], 44)
        s.text(p[0], p[1] + off, name, 24, col, anchor="middle", family=MONO)

    # --- reach envelope
    sh = b["character"]["seated_position"]
    reach = b["anchor_rules"]["seated_reach_mm"]
    p = f(sh["x"], sh["y"])
    s.circle(p[0], p[1], reach, stroke=GOLD, sw=3, dash="20 14", op=0.65)
    s.text(p[0] + reach * 0.70, p[1] - reach * 0.70,
           f"seated reach {reach} mm", 27, GOLD, family=MONO)

    # --- character
    s.circle(p[0], p[1], 115, fill=INK_700, stroke=GOLD_LIGHT, sw=4)
    s.poly([(p[0], p[1] + 165), (p[0] - 46, p[1] + 100), (p[0] + 46, p[1] + 100)],
           fill=GOLD_LIGHT, stroke="none")
    s.text(p[0], p[1] + 12, "TF", 40, INK_950, anchor="middle", weight="600", family=MONO)
    s.text(p[0], p[1] + 235, "TF_TRADER_01  ·  faces south (−Y)", 28, GOLD_LIGHT, anchor="middle")
    ch = b["chair"]["centre"]
    pc = f(ch["x"], ch["y"])
    s.rect(pc[0] - 240, pc[1] - 110, 480, 320, fill="none", stroke=INK_600, sw=3, rx=30)

    # --- cameras + FOV wedges
    for c in b["cameras"]:
        cp, ct = c["position"], c["target"]
        P, T = f(cp["x"], cp["y"]), f(ct["x"], ct["y"])
        ang = math.atan2(T[1] - P[1], T[0] - P[0])
        half = math.radians(c["hfov_degrees"] / 2)
        L = math.dist(P, T) * 1.30
        w1 = (P[0] + L * math.cos(ang - half), P[1] + L * math.sin(ang - half))
        w2 = (P[0] + L * math.cos(ang + half), P[1] + L * math.sin(ang + half))
        s.poly([P, w1, w2], fill=GOLD, stroke="none", op=0.085)
        s.line(P[0], P[1], w1[0], w1[1], GOLD, 2, op=0.45)
        s.line(P[0], P[1], w2[0], w2[1], GOLD, 2, op=0.45)
        s.line(P[0], P[1], T[0], T[1], GOLD_LIGHT, 2, dash="12 10", op=0.6)
        s.circle(P[0], P[1], 46, fill=INK_950, stroke=GOLD, sw=4)
        s.text(P[0], P[1] + 13, c["id"].replace("CAM_", ""), 36, GOLD,
               anchor="middle", weight="600", family=MONO)
        lx = P[0] + (70 if cp["x"] < 5000 else -70)
        s.text(lx, P[1] - 62, f'{c["name"]}  {c["focal_mm_35eq"]}mm',
               26, GOLD_LIGHT, anchor="start" if cp["x"] < 5000 else "end")

    # --- scale bar
    sb_x, sb_y = M, H - 350
    s.line(sb_x, sb_y, sb_x + 2000, sb_y, INK_300, 4)
    for i in range(3):
        s.line(sb_x + i * 1000, sb_y - 16, sb_x + i * 1000, sb_y + 16, INK_300, 4)
    s.text(sb_x, sb_y - 34, "0", 26, INK_400, anchor="middle", family=MONO)
    s.text(sb_x + 1000, sb_y - 34, "1 m", 26, INK_400, anchor="middle", family=MONO)
    s.text(sb_x + 2000, sb_y - 34, "2 m", 26, INK_400, anchor="middle", family=MONO)

    # --- legend
    lx, ly = M + 2500, H - 400
    items = [
        (BLUE, "live screen surface / glazing"),
        (GOLD, "camera, field of view, reach envelope"),
        (GOLD_DIM, "reserved hand working envelope"),
        (INK_300, "interactive object (has an anchor)"),
        (INK_600, "decor — beyond reach, no anchor"),
    ]
    for i, (col, lab) in enumerate(items):
        yy = ly + i * 52
        s.line(lx, yy, lx + 56, yy, col, 7)
        s.text(lx + 76, yy + 11, lab, 28, INK_400)

    s.text(W - M, H - 120, f'room {RX} × {RY} × {b["room"]["size"]["z"]} mm   ·   '
                           f'desk z735   ·   eye z1295   ·   bezel z1298',
           28, INK_500, anchor="end", family=MONO)
    return s.render()


# -------------------------------------------------------- character proportions


def render_proportions(b: dict) -> str:
    ch = b["character"]
    kh, HU = ch["key_heights"], ch["head_unit"]
    STAND = ch["standing_height"]
    W, H = 3800, 3000
    s = Svg(W, H, "TF_TRADER_01 — proportions, seated geometry and face construction")

    s.text(140, 150, "TF_TRADER_01", 86, INK_100, weight="600", ls=3)
    s.text(140, 225, "Construction sheet · proportions, seated geometry, face landmarks · millimetres",
           34, INK_400)
    s.text(140, 283, "GENERATED — figures from TRADE_FIX_OFFICE_01.blockout.json and CHARACTER_BIBLE.md",
           28, GOLD_DIM, family=MONO, ls=1)

    FLOOR = 2560
    SC = 1.0

    def zy(z):
        return FLOOR - z * SC

    # ============================== PANEL A — standing
    AX = 420
    s.text(AX, 400, "A · STANDING", 38, GOLD, weight="600", ls=2)
    s.text(AX, 448, f"{STAND} mm · {ch['head_units']} head units of {HU} mm", 27, INK_400)

    s.line(AX - 300, zy(0), AX + 540, zy(0), INK_500, 4)
    s.text(AX - 300, zy(0) + 44, "floor  z0", 26, INK_500, family=MONO)

    # head-unit ladder
    for i in range(8):
        z = STAND - i * HU
        if z < 0:
            z = 0
        y = zy(z)
        s.line(AX - 260, y, AX - 200, y, INK_700, 2)
        if i <= 7:
            s.text(AX - 272, y + 9, f"{i}", 24, INK_600, anchor="end", family=MONO)
    s.line(AX - 230, zy(STAND), AX - 230, zy(0), INK_700, 2, dash="8 8")
    s.text(AX - 330, zy(STAND / 2), "head units", 24, INK_600, anchor="middle", rot=-90)

    stand_marks = [
        (STAND, "crown", GOLD_LIGHT), (1710, "eye", GOLD), (1500, "shoulder", INK_300),
        (1140, "elbow", INK_300), (880, "wrist", INK_300),
    ]
    # schematic figure
    hx = AX + 150
    s.circle(hx, zy(STAND - HU / 2), HU / 2, fill=INK_800, stroke=GOLD_LIGHT, sw=3)
    s.line(hx, zy(STAND - HU), hx, zy(850), INK_500, 14)          # torso
    s.line(hx - 200, zy(1500), hx + 200, zy(1500), INK_500, 10)   # shoulders
    s.line(hx - 200, zy(1500), hx - 215, zy(1140), INK_500, 8)
    s.line(hx - 215, zy(1140), hx - 200, zy(880), INK_500, 8)
    s.line(hx + 200, zy(1500), hx + 215, zy(1140), INK_500, 8)
    s.line(hx + 215, zy(1140), hx + 200, zy(880), INK_500, 8)
    s.line(hx, zy(850), hx - 85, zy(0), INK_500, 10)
    s.line(hx, zy(850), hx + 85, zy(0), INK_500, 10)

    for z, lab, col in stand_marks:
        y = zy(z)
        s.line(AX - 180, y, AX + 480, y, col, 1.5, dash="10 8", op=0.5)
        s.text(AX + 492, y + 9, f"{lab} {z}", 26, col, family=MONO)

    # ============================== PANEL B — seated against the desk
    BX = 1360
    s.text(BX, 400, "B · SEATED — THE RIG TARGET", 38, GOLD, weight="600", ls=2)
    s.text(BX, 448, "verified against TRADE_FIX_OFFICE_01 · elbow sits 35 mm below the desk", 27, INK_400)

    s.line(BX - 120, zy(0), BX + 1500, zy(0), INK_500, 4)

    DESK_Z = b["desk"]["surface_z"]
    # desk slab (seen from the side; character faces -Y, i.e. to the left here)
    s.rect(BX + 120, zy(DESK_Z), 820, 40, fill=INK_800, stroke=INK_400, sw=3)
    s.text(BX + 130, zy(DESK_Z) - 18, f"desk surface z{DESK_Z}", 26, INK_300, family=MONO)

    # monitor
    mon = b["monitors"]
    top, bot = mon["derived"]["top_bezel_z"], mon["derived"]["bottom_bezel_z"]
    s.rect(BX + 700, zy(top), 44, top - bot, fill=BLUE, stroke=INK_100, sw=2, op=0.45)
    s.text(BX + 766, zy(top) + 16, f"top bezel z{top}", 26, BLUE, family=MONO)
    s.text(BX + 766, zy(bot) - 6, f"bottom z{bot}", 26, BLUE, family=MONO)
    s.text(BX + 766, zy(mon["common"]["centre_z"]) + 10,
           f"centre z{mon['common']['centre_z']}", 26, BLUE, family=MONO)

    # chair + figure
    SEAT = kh["seat_pan"]
    s.rect(BX + 20, zy(SEAT), 380, 36, fill=INK_800, stroke=INK_500, sw=3)
    s.rect(BX + 20, zy(1180), 36, 1180 - SEAT, fill=INK_800, stroke=INK_500, sw=3)
    bx = BX + 300
    s.line(bx - 20, zy(SEAT + 60), bx + 24, zy(kh["shoulder"]), INK_500, 16)  # 5 deg lean
    s.circle(bx + 48, zy(kh["chin"] + HU / 2), HU / 2, fill=INK_800, stroke=GOLD_LIGHT, sw=3)
    s.line(bx + 24, zy(kh["shoulder"]), bx + 170, zy(kh["elbow"]), INK_500, 10)   # upper arm
    s.line(bx + 170, zy(kh["elbow"]), bx + 440, zy(DESK_Z + 22), INK_500, 9)      # forearm on desk
    s.line(bx + 440, zy(DESK_Z + 22), bx + 560, zy(DESK_Z + 30), INK_300, 8)      # hand
    s.line(bx - 20, zy(SEAT + 40), bx + 330, zy(kh["thigh_top"] - 40), INK_500, 12)
    s.line(bx + 330, zy(kh["thigh_top"] - 40), bx + 360, zy(0), INK_500, 10)

    seat_marks = [
        (kh["crown"], "crown", GOLD_LIGHT), (kh["eye"], "EYE", GOLD),
        (kh["chin"], "chin", INK_300), (kh["shoulder"], "shoulder", INK_300),
        (DESK_Z, "desk", INK_300),
        (kh["elbow"], "ELBOW", GOLD), (kh["thigh_top"], "thigh top", INK_400),
        (SEAT, "seat pan", INK_400),
    ]
    for z, lab, col in seat_marks:
        y = zy(z)
        strong = lab.isupper()
        s.line(BX - 100, y, BX + 1480, y, col, 2.5 if strong else 1.5,
               dash="10 8", op=0.75 if strong else 0.4)
        s.text(BX - 112, y + 9, f"{lab} {z}", 27 if strong else 25, col,
               anchor="end", family=MONO, weight="600" if strong else "400")

    # the two load-bearing relationships
    ey, ty = zy(kh["eye"]), zy(top)
    s.line(BX + 1400, ey, BX + 1400, ty, GOLD, 3)
    s.text(BX + 1416, (ey + ty) / 2 + 9, f"eye − bezel = {kh['eye'] - top:+d} mm", 26, GOLD, family=MONO)
    el, dk = zy(kh["elbow"]), zy(DESK_Z)
    s.line(BX + 1270, el, BX + 1270, dk, GOLD, 3)
    s.text(BX + 1286, (el + dk) / 2 + 9, f"desk − elbow = {DESK_Z - kh['elbow']:+d} mm",
           26, GOLD, family=MONO)

    gp = b["gaze_rules"]["default_pitch_degrees"]
    s.text(BX, 2760, f"Default gaze pitch {gp}° — eye z{kh['eye']} to screen centre "
                     f"z{mon['common']['centre_z']} over 650 mm.", 28, INK_300)
    s.text(BX, 2808, "Forearms rest on the surface from mid-forearm forward. An elbow above the "
                     "desk makes every typing", 27, INK_400)
    s.text(BX, 2852, "and mouse motion read as hovering — the first thing to check when a desk "
                     "motion looks wrong.", 27, INK_400)

    # ============================== PANEL C — face construction
    CX, CY, F = 2960, 620, 3.4
    s.text(CX - 400, 400, "C · FACE CONSTRUCTION", 38, GOLD, weight="600", ls=2)
    s.text(CX - 400, 448, f"head {HU} mm tall · drawn {F}× · tolerances in CHARACTER_BIBLE §3",
           27, INK_400)

    HH, HW = HU * F, 155 * F
    s.rect(CX - HW / 2, CY, HW, HH, fill=INK_900, stroke=INK_600, sw=3)
    s.line(CX, CY, CX, CY + HH, INK_700, 1.5, dash="10 8")

    land = [
        (0.470, "eye line", GOLD, True),
        (0.660, "nose base", INK_300, False),
        (0.775, "mouth line", GOLD, True),
    ]
    for frac, lab, col, strong in land:
        y = CY + HH * frac
        s.line(CX - HW / 2 - 70, y, CX + HW / 2 + 70, y, col, 2.5 if strong else 1.5,
               dash="10 8", op=0.8 if strong else 0.45)
        s.text(CX + HW / 2 + 82, y + 9, f"{lab}  {frac:.3f} HU", 25, col, family=MONO)

    # eyes — the aperture ratio is the identity lock
    ew, eh = 0.185 * HH, 0.185 * HH * 0.34
    icd = 0.210 * HH
    for sgn in (-1, 1):
        ecx = CX + sgn * (icd / 2 + ew / 2)
        ecy = CY + HH * 0.470
        s.add(f'<ellipse cx="{ecx:.1f}" cy="{ecy:.1f}" rx="{ew/2:.1f}" ry="{eh/2:.1f}" '
              f'fill="{INK_800}" stroke="{GOLD}" stroke-width="3"/>')
        s.circle(ecx, ecy, eh * 0.46, fill="none", stroke=INK_400, sw=2)
        by = ecy - 0.055 * HH - eh / 2
        s.line(ecx - ew * 0.55, by + 8, ecx + ew * 0.55, by - 4, INK_300, 5)
    s.line(CX - icd / 2, CY + HH * 0.470, CX + icd / 2, CY + HH * 0.470, GOLD_LIGHT, 3)
    s.text(CX, CY + HH * 0.470 - 26, "0.210 HU", 23, GOLD_LIGHT, anchor="middle", family=MONO)

    # zygomatic / gonial
    zw = HW
    gw = HW * 0.84
    yz = CY + HH * 0.50
    yg = CY + HH * 0.86
    s.line(CX - zw / 2, yz, CX + zw / 2, yz, BLUE, 3, op=0.7)
    s.line(CX - gw / 2, yg, CX + gw / 2, yg, BLUE, 3, op=0.7)
    s.text(CX - zw / 2 - 16, yz + 9, "bizygomatic 1.00", 24, BLUE, anchor="end", family=MONO)
    s.text(CX - gw / 2 - 16, yg + 9, "bigonial 0.84", 24, BLUE, anchor="end", family=MONO)

    # mouth, nose
    mw = 0.215 * HH
    s.line(CX - mw / 2, CY + HH * 0.775, CX + mw / 2, CY + HH * 0.775, INK_300, 5)
    nw = 0.165 * HH
    s.line(CX - nw / 2, CY + HH * 0.660, CX + nw / 2, CY + HH * 0.660, INK_400, 4)

    s.text(CX - HW / 2, CY + HH + 90, "THE IDENTITY LOCK", 30, GOLD, weight="600", ls=2)
    for i, ln in enumerate([
        "eye aperture height ÷ width = 0.34 ± 0.02",
        "above ~0.42 he stops reading as 37 years old,",
        "no matter what the rest of the face does.",
        "",
        "inner canthus separation  0.210 ± 0.008 HU",
        "bizygomatic : bigonial    1.00 : 0.84 ± 0.02",
        "brow above upper lid      0.055 ± 0.006 HU",
        "outer canthal tilt        2–4° downward",
    ]):
        s.text(CX - HW / 2, CY + HH + 148 + i * 44, ln, 26,
               GOLD_LIGHT if i == 0 else INK_400, family=MONO)

    # panel dividers
    for x in (1180, 2500):
        s.line(x, 360, x, H - 180, INK_800, 2)

    s.text(140, H - 90, "Head is 1/7.6 of standing height. Seated figures must agree with the "
                        "blockout; scripts/visual/validate_blockout.py checks it.",
           27, INK_500)
    return s.render()


def main() -> None:
    b = json.loads(BLOCKOUT.read_text(encoding="utf-8"))
    out = [
        (ROOT / "visual" / "references" / "office" / "TRADE_FIX_OFFICE_01_plan.svg", render_plan(b)),
        (ROOT / "visual" / "references" / "character" / "TF_TRADER_01_proportions.svg", render_proportions(b)),
    ]
    for path, body in out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}  ({len(body):,} bytes)")


if __name__ == "__main__":
    main()
