"""Validate TRADE_FIX_OFFICE_01.blockout.json against its own stated rules."""
import json
import math
import sys
from itertools import pairwise
from pathlib import Path

P = Path(r"D:\.Music\visual\environment\TRADE_FIX_OFFICE_01.blockout.json")
b = json.loads(P.read_text(encoding="utf-8"))

fails, warns = [], []

room = b["room"]["size"]
eye = (3200.0, 3000.0, 1295.0)
shoulder = (3200.0, 3000.0, 1080.0)
bez = b["validation"]["bezel_plane"]
BY, BZ = float(bez["y"]), float(bez["z"])
FACE_EXEMPT = {"CAM_6"}          # desk-surface shot; target below the bezel by design
# Participation tiers, keyed by id. CR-001 replaced the flat yaw/pitch limits with these;
# this script still read `gaze_rules.yaw_limit_degrees` and so crashed on import from that
# point on. It had been dead ever since — the freeze tests carried the enforcement, which
# is why nothing noticed. Check 4 below is rewritten against the tier model.
TIERS = {t["id"]: t for t in b["gaze_rules"]["participation_model"]["tiers"]}

# --- 1. derived monitor heights
cz = b["monitors"]["common"]["centre_z"]
h = b["monitors"]["panels"][0]["panel"]["h"]
top, bot = cz + h / 2, cz - h / 2
d = b["monitors"]["derived"]
if top != d["top_bezel_z"]:
    fails.append(f"top_bezel_z says {d['top_bezel_z']}, geometry gives {top}")
if bot != d["bottom_bezel_z"]:
    fails.append(f"bottom_bezel_z says {d['bottom_bezel_z']}, geometry gives {bot}")
print(f"[1] monitor top bezel = {top}  bottom = {bot}  eye = {eye[2]}  (delta eye/top = {eye[2]-top:+.0f})")

# --- 2. bezel clearance for every south-side camera
print("\n[2] bezel clearance for south-side cameras (must be > 0):")
for c in b["cameras"]:
    p, t = c["position"], c["target"]
    if p["y"] >= 2200:
        print(f"    {c['id']}: not south of the desk, skipped")
        continue
    if c["id"] in FACE_EXEMPT:
        print(f"    {c['id']}: desk-surface shot, exempt by rule")
        continue
    # ray from camera to the EYE, evaluated at the bezel plane y
    y0, z0 = float(p["y"]), float(p["z"])
    y1, z1 = eye[1], eye[2]
    if not (min(y0, y1) < BY < max(y0, y1)):
        print(f"    {c['id']}: bezel plane not between camera and eye, skipped")
        continue
    u = (BY - y0) / (y1 - y0)
    z = z0 + u * (z1 - z0)
    clear = z - BZ
    claimed = b["validation"]["computed_clearances_mm"].get(c["id"])
    ok = "OK " if clear > 0 else "FAIL"
    print(f"    {c['id']}: ray z at bezel = {z:7.1f}  clearance = {clear:+7.1f} mm  recorded {claimed}  [{ok}]")
    if clear <= 0:
        fails.append(f"{c['id']} sight line to the eye does not clear the bezel ({clear:+.1f} mm)")
    if isinstance(claimed, (int, float)) and abs(clear - claimed) > 1.0:
        fails.append(f"{c['id']} records clearance {claimed} but computed {clear:.1f}")

# --- 3. anchor reach from the seated shoulder
REACH = b["anchor_rules"]["seated_reach_mm"]
assert REACH == 365 + 270 + 195, "recorded reach disagrees with the limb lengths"
print(f"\n[3] anchor reach from seated shoulder (limit {REACH} mm):")
for a in b["anchors"]:
    pos = a["position"]
    dist = math.dist(shoulder, (pos["x"], pos["y"], pos["z"]))
    ok = "OK " if dist <= REACH else "FAIL"
    print(f"    {a['id']:<28} {dist:7.1f} mm  [{ok}]")
    if dist > REACH:
        fails.append(f"{a['id']} at {dist:.0f} mm exceeds reach {REACH} mm")

# --- 4. every gaze target's angle fits the participation it declares
#
# The check is on the DECLARATION, not on the angle. A large angle is fine when the right
# body parts are declared to reach it; what is not fine is a target claiming "eyes only"
# for an angle that needs a shoulder turn. That is the whole point of CR-001.
print("\n[4] gaze target angles against their declared participation tier:")
for g in b["gaze_targets"]:
    pos = g["position"]
    if not isinstance(pos, dict):
        print(f"    {g['id']:<32} dynamic, skipped")
        continue
    dx, dy, dz = pos["x"] - eye[0], pos["y"] - eye[1], pos["z"] - eye[2]
    # neutral facing is -Y; yaw measured off that axis
    yaw = math.degrees(math.atan2(dx, -dy))
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))

    declared = g.get("participation")
    if declared is None:
        print(f"    {g['id']:<32} yaw {yaw:+7.1f}  pitch {pitch:+6.1f}  [NO TIER]")
        fails.append(f"{g['id']} declares no participation tier")
        continue
    tier = TIERS.get(declared)
    if tier is None:
        print(f"    {g['id']:<32} yaw {yaw:+7.1f}  pitch {pitch:+6.1f}  [UNKNOWN {declared}]")
        fails.append(f"{g['id']} declares unknown tier {declared!r}")
        continue

    fits = abs(yaw) <= tier["max_abs_yaw"] and abs(pitch) <= tier["max_abs_pitch"]
    ok = "OK " if fits else "FAIL"
    print(
        f"    {g['id']:<32} yaw {yaw:+7.1f}  pitch {pitch:+6.1f}  "
        f"{declared:<18} (<={tier['max_abs_yaw']}, <={tier['max_abs_pitch']}) [{ok}]"
    )
    if not fits:
        fails.append(
            f"{g['id']} declares {declared!r} (yaw <={tier['max_abs_yaw']}, "
            f"pitch <={tier['max_abs_pitch']}) but sits at yaw {yaw:+.1f}, "
            f"pitch {pitch:+.1f} — declare a wider tier or move the target"
        )

# --- 4b. no camera may carry scheduling metadata. CR-003.
print("\n[4b] cameras carry geometry only:")
for c in b["cameras"]:
    scheduling = [k for k in ("hour_share_cap", "minimum_hold_seconds",
                              "maximum_hold_seconds", "market_affinity") if k in c]
    if scheduling:
        print(f"    {c['id']}: carries {', '.join(scheduling)}  [FAIL]")
        fails.append(
            f"{c['id']} declares scheduling metadata {scheduling}; that lives in "
            "CAMERA_METADATA (GEOMETRY_FREEZE.md §5, CR-003)"
        )
print(f"    {len(b['cameras'])} cameras checked")

# --- 5. default gaze pitch claim
m1 = next(g for g in b["gaze_targets"] if g["id"] == "GAZE_MON_1")["position"]
dx, dy, dz = m1["x"] - eye[0], m1["y"] - eye[1], m1["z"] - eye[2]
pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
claimed = b["gaze_rules"]["default_pitch_degrees"]
print(f"\n[5] default gaze pitch to MON_1 = {pitch:+.2f} deg, claimed {claimed:+.2f}")
if abs(pitch - claimed) > 0.3:
    fails.append(f"default_pitch_degrees claims {claimed} but geometry gives {pitch:+.2f}")

# --- 6. monitor x-extent overlap
print("\n[6] monitor x extents (desk panels must not overlap):")
desk = [p for p in b["monitors"]["panels"] if not p.get("mounted")]
spans = []
for p in desk:
    w = p["panel"]["w"] * math.cos(math.radians(abs(p.get("yaw_degrees", 0))))
    x0, x1 = p["centre"]["x"] - w / 2, p["centre"]["x"] + w / 2
    spans.append((x0, x1, p["id"]))
for x0, x1, i in sorted(spans):
    print(f"    {i:<8} {x0:7.1f} .. {x1:7.1f}")
s = sorted(spans)
for (_a0, a1, ai), (b0, _b1, bi) in pairwise(s):
    if a1 > b0:
        fails.append(f"{ai} and {bi} x-extents overlap ({a1:.0f} > {b0:.0f})")
lo, hi = s[0][0], max(x1 for _, x1, _ in s)
claimed_span = b["monitors"]["array_span_x"]
print(f"    array span = {lo:.0f} .. {hi:.0f}  claimed {claimed_span}")
dk = b["desk"]["extent"]["x"]
if lo < dk[0] or hi > dk[1]:
    fails.append(f"monitor array {lo:.0f}..{hi:.0f} exceeds desk {dk}")

# --- 7. reserved volume intrusion
print("\n[7] reserved hand envelope intrusion:")
rv = b["reserved_volumes"][0]["extent"]
allowed = {"KEYBOARD", "MOUSE", "MUG"}
for o in b["desk_objects"]:
    p = o["position"]
    inside = rv["x"][0] <= p["x"] <= rv["x"][1] and rv["y"][0] <= p["y"] <= rv["y"][1]
    if inside:
        tag = "allowed" if o["id"] in allowed else "INTRUSION"
        print(f"    {o['id']:<14} inside envelope  [{tag}]")
        if o["id"] not in allowed:
            fails.append(f"{o['id']} intrudes into HAND_WORKING_ENVELOPE")
print("    (objects not listed are outside the envelope)")

# --- 8. cameras inside the room
print("\n[8] camera positions inside room bounds:")
for c in b["cameras"]:
    p = c["position"]
    ok = 0 < p["x"] < room["x"] and 0 < p["y"] < room["y"] and 0 < p["z"] < room["z"]
    print(f"    {c['id']}: ({p['x']}, {p['y']}, {p['z']})  [{'OK ' if ok else 'FAIL'}]")
    if not ok:
        fails.append(f"{c['id']} outside the room volume")

# --- 9. gaze share sum
tot = sum(g["base_share"] for g in b["gaze_targets"])
screens = sum(g["base_share"] for g in b["gaze_targets"] if g.get("screen"))
print(f"\n[9] gaze shares sum = {tot:.3f}; screen share = {screens:.3f} (target {b['gaze_rules']['screen_share_target']})")
if abs(tot - 1.0) > 0.01:
    fails.append(f"gaze base_share sums to {tot:.3f}, not 1.0")
if abs(screens - b["gaze_rules"]["screen_share_target"]) > 0.01:
    fails.append(f"screen gaze share {screens:.3f} != target {b['gaze_rules']['screen_share_target']}")

print("\n" + "=" * 62)
if fails:
    print(f"FAILED  ({len(fails)})")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
