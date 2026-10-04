# World Marker Frozen Diagnostic Report

**Generated:** 2026-10-04
**Test Mode:** world_marker (frozen frame, no RAF loop)
**Result:** ✅ **ALL PASS**

---

## Server Configuration

| Parameter | Value |
|-----------|-------|
| AUTHORITATIVE VISUAL SERVER | localhost:8766 |
| Server PID | 39800 |
| BUILD ID | 2026-10-04-frozen-diagnostic |
| App.js Version | 2026-10-04-three-markers-visible |
| served_build_matches_disk | YES |

---

## Diagnostic Results

| Check | Result |
|-------|--------|
| normal RAF active during diagnostic | NO (stopped) |
| clear_calls_after_marker | 0 |
| preserveDrawingBuffer | true |

---

## Marker Bounding Boxes

| Marker | Color | Pixels Found | Bounding Box | Min Required | Result |
|--------|-------|--------------|--------------|--------------|--------|
| **HEAD** | YELLOW (255,255,0) | 252,473 | 510 × 501 px | 100×100 | **PASS** ✅ |
| **DESK** | CYAN (0,255,255) | 27,639 | 149 × 191 px | 100×100 | **PASS** ✅ |
| **MONITOR** | RED (255,0,0) | 27,637 | 149 × 191 px | 100×100 | **PASS** ✅ |

**Total Marker Pixels:** 307,749

---

## Visibility Analysis

| Check | Result |
|-------|--------|
| WebGL framebuffer has colored pixels | YES (307,749 marker pixels) |
| gl.readPixels() captures content | YES |
| canvas.toDataURL() captures content | YES |
| GPU Readback Mirror visible | YES (19,444 pixels in scaled mirror) |
| allPass | **TRUE** ✅ |

---

## Evidence Files

| File | Description |
|------|-------------|
| `canvas_todataurl.png` | **PROOF** - Shows YELLOW (center), CYAN (left), RED (right) markers |
| `frozen_diagnostic_screenshot.png` | Playwright screenshot with diagnostic panel |

---

## Fixes Applied

1. **preserveDrawingBuffer: true** - Added to all WebGL context creations to retain framebuffer content for screenshots
2. **Marker positioning** - Offset X coordinates to prevent marker occlusion:
   - HEAD: center (X=3200)
   - DESK: left (X=2400)
   - MONITOR: right (X=4000)

---

## Conclusion

**The world_marker rendering is VERIFIED WORKING.**

✅ HEAD marker: 510×501 pixels (5x minimum)
✅ DESK marker: 149×191 pixels (1.5x minimum)
✅ MONITOR marker: 149×191 pixels (1.5x minimum)
✅ No post-draw clears detected (clear_calls_after_marker = 0)
✅ preserveDrawingBuffer enabled
✅ canvas.toDataURL() proves content renders correctly
✅ **allPass = true**

---

## Recommended Next Steps

1. ✅ **World markers confirmed working** - COMPLETE
2. → Proceed to `room_clip` test to verify scene geometry
3. → Proceed to `room` test for full scene rendering
4. → Enable normal RAF loop for animation testing
