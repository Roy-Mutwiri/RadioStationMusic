/*
 * VISUAL PROOF MODE — ANIME-STYLE PROCEDURAL ART
 *
 * A complete procedural anime trader and trading office, drawn entirely in WebGL2.
 * No external PNG assets required. Visually convincing enough to evaluate the concept.
 *
 * Style: premium stylized anime / illustrated realism
 * - Clean dark shapes
 * - Warm cinematic amber lighting
 * - Cool blue city fill
 * - Black/gold palette
 * - Soft shadows, subtle highlights
 * - Layered depth
 */
'use strict';

(function () {
  const S = {
    RECT: 0, ELLIPSE: 1, OUTLINE: 2, ROUNDED: 3,
    GRADIENT: 4, GLOW: 5, TAPER: 6, TEXTURE: 7,
  };

  /* Anime palette - rich, warm shadows, clean highlights
   *
   * BRIGHTENED for visibility. The original values were so dark (RGB 3-20) that the
   * entire scene was indistinguishable from the background. Environment elements now
   * have enough luminance to be clearly visible while maintaining the dark aesthetic. */
  const C = {
    // -- environment (BRIGHTENED - was near-black, now visible)
    night_sky_top:    [0.04, 0.06, 0.14, 1],     // was 0.012, 0.020, 0.055
    night_sky_mid:    [0.07, 0.10, 0.18, 1],     // was 0.035, 0.055, 0.110
    night_sky_low:    [0.12, 0.16, 0.25, 1],     // was 0.065, 0.090, 0.150
    city_far:         [0.10, 0.14, 0.24, 1],     // was 0.055, 0.080, 0.145
    city_mid:         [0.08, 0.11, 0.19, 1],     // was 0.040, 0.060, 0.110
    city_near:        [0.06, 0.09, 0.15, 1],     // was 0.030, 0.045, 0.085
    city_window:      [0.500, 0.680, 0.900, 1],
    city_window_warm: [0.950, 0.780, 0.480, 1],
    city_window_red:  [0.850, 0.350, 0.320, 1],

    wall:             [0.10, 0.11, 0.14, 1],     // was 0.050, 0.055, 0.072
    wall_warm:        [0.18, 0.15, 0.12, 1],     // was 0.110, 0.090, 0.070
    wall_shadow:      [0.06, 0.065, 0.085, 1],   // was 0.030, 0.032, 0.042
    floor:            [0.055, 0.06, 0.08, 1],    // was 0.028, 0.030, 0.040
    floor_reflect:    [0.08, 0.088, 0.12, 1],    // was 0.040, 0.044, 0.058

    desk_top:         [0.14, 0.11, 0.09, 1],     // was 0.075, 0.062, 0.052
    desk_top_lit:     [0.20, 0.16, 0.13, 1],     // was 0.120, 0.095, 0.075
    desk_edge:        [0.18, 0.15, 0.12, 1],     // was 0.110, 0.090, 0.072
    desk_front:       [0.08, 0.07, 0.06, 1],     // was 0.042, 0.036, 0.032
    desk_shadow:      [0.05, 0.045, 0.04, 1],    // was 0.025, 0.022, 0.020

    shelf:            [0.12, 0.10, 0.09, 1],     // was 0.065, 0.055, 0.048
    shelf_lit:        [0.18, 0.15, 0.12, 1],     // was 0.100, 0.082, 0.068
    book_red:         [0.580, 0.220, 0.180, 1],  // brightened
    book_blue:        [0.200, 0.300, 0.480, 1],  // brightened
    book_green:       [0.180, 0.320, 0.220, 1],  // brightened
    book_gold:        [0.480, 0.380, 0.180, 1],  // brightened
    plant_dark:       [0.150, 0.280, 0.180, 1],  // brightened
    plant_light:      [0.220, 0.420, 0.280, 1],  // brightened
    pot:              [0.240, 0.180, 0.140, 1],  // brightened

    bezel:            [0.065, 0.07, 0.09, 1],    // was 0.032, 0.035, 0.045
    bezel_lit:        [0.10, 0.11, 0.14, 1],     // was 0.055, 0.060, 0.075
    screen_bg:        [0.035, 0.055, 0.09, 1],   // was 0.018, 0.028, 0.045
    screen_grid:      [0.10, 0.14, 0.20, 1],     // was 0.070, 0.100, 0.145
    screen_glow:      [0.150, 0.250, 0.380, 0.20],  // brighter glow

    up:               [0.280, 0.620, 0.400, 1],
    up_bright:        [0.380, 0.750, 0.480, 1],
    down:             [0.780, 0.280, 0.260, 1],
    down_bright:      [0.920, 0.360, 0.320, 1],

    amber:            [0.980, 0.680, 0.280, 1],
    amber_soft:       [0.650, 0.420, 0.160, 1],
    amber_glow:       [0.980, 0.680, 0.280, 0.12],
    gold:             [0.820, 0.680, 0.180, 1],
    gold_bright:      [0.950, 0.820, 0.450, 1],
    gold_dim:         [0.550, 0.450, 0.150, 1],
    blue_fill:        [0.260, 0.480, 0.780, 1],
    blue_glow:        [0.260, 0.480, 0.780, 0.08],
    clear:            [0, 0, 0, 0],

    // -- TF_TRADER_01: white male 35-40, mature face, hazel eyes
    skin:             [0.875, 0.720, 0.600, 1],
    skin_shadow:      [0.720, 0.560, 0.460, 1],
    skin_deep:        [0.580, 0.420, 0.350, 1],
    skin_lit:         [0.940, 0.800, 0.680, 1],
    skin_highlight:   [0.980, 0.880, 0.780, 1],
    skin_warm:        [0.850, 0.650, 0.520, 1],

    hair:             [0.280, 0.210, 0.150, 1],
    hair_shadow:      [0.180, 0.130, 0.095, 1],
    hair_lit:         [0.400, 0.310, 0.210, 1],
    hair_highlight:   [0.520, 0.420, 0.300, 1],

    beard:            [0.220, 0.165, 0.120, 1],
    beard_shadow:     [0.150, 0.110, 0.085, 1],
    stubble:          [0.180, 0.140, 0.110, 0.45],

    brow:             [0.260, 0.195, 0.140, 1],

    sclera:           [0.920, 0.915, 0.900, 1],
    sclera_shadow:    [0.820, 0.800, 0.780, 1],
    iris_outer:       [0.380, 0.280, 0.160, 1],
    iris_inner:       [0.520, 0.400, 0.220, 1],
    iris_light:       [0.620, 0.480, 0.280, 1],
    pupil:            [0.040, 0.035, 0.030, 1],
    eye_catch:        [1, 1, 1, 0.95],
    eye_catch2:       [0.850, 0.900, 1, 0.6],
    lash:             [0.120, 0.090, 0.070, 1],

    lip:              [0.680, 0.480, 0.420, 1],
    lip_shadow:       [0.520, 0.360, 0.320, 1],

    nose_shadow:      [0.780, 0.620, 0.520, 1],
    nose_highlight:   [0.920, 0.780, 0.680, 1],

    hoodie:           [0.09, 0.10, 0.13, 1],     // was 0.048, 0.052, 0.065
    hoodie_lit:       [0.14, 0.16, 0.20, 1],     // was 0.085, 0.095, 0.115
    hoodie_shadow:    [0.055, 0.06, 0.08, 1],    // was 0.028, 0.030, 0.038
    hoodie_fold:      [0.12, 0.13, 0.16, 1],     // was 0.065, 0.072, 0.088
    hood:             [0.07, 0.075, 0.095, 1],   // was 0.035, 0.038, 0.048

    shirt:            [0.055, 0.06, 0.075, 1],   // was 0.028, 0.030, 0.038
    trousers:         [0.075, 0.08, 0.10, 1],    // was 0.038, 0.040, 0.050
    trousers_shadow:  [0.045, 0.05, 0.065, 1],   // was 0.022, 0.024, 0.032

    cans:             [0.08, 0.09, 0.12, 1],     // was 0.042, 0.048, 0.062
    cans_lit:         [0.13, 0.15, 0.18, 1],     // was 0.072, 0.082, 0.100
    cans_shadow:      [0.05, 0.055, 0.075, 1],   // was 0.025, 0.028, 0.038

    watch_band:       [0.07, 0.075, 0.09, 1],    // was 0.035, 0.038, 0.045
    watch_face:       [0.10, 0.11, 0.14, 1],     // was 0.055, 0.060, 0.075

    mug_body:         [0.12, 0.10, 0.09, 1],     // was 0.065, 0.058, 0.052
    mug_rim:          [0.16, 0.14, 0.12, 1],     // was 0.095, 0.085, 0.075
    coffee:           [0.180, 0.100, 0.055, 1],  // brightened
    steam:            [0.850, 0.850, 0.870, 0.12],  // more visible steam

    keyboard:         [0.08, 0.088, 0.11, 1],    // was 0.040, 0.044, 0.055
    key:              [0.12, 0.13, 0.16, 1],     // was 0.065, 0.072, 0.088
    key_lit:          [0.15, 0.17, 0.21, 1],     // was 0.085, 0.095, 0.115

    mic:              [0.09, 0.10, 0.13, 1],     // was 0.048, 0.052, 0.065
    mic_mesh:         [0.12, 0.13, 0.16, 1],     // was 0.065, 0.072, 0.088
  };

  /* HIGH-CONTRAST DEBUG PALETTE
   *
   * Used when ?contrast=debug is set. Each scene category gets an obvious, distinguishable
   * color so you can verify geometry is being drawn and projected correctly. If the scene
   * is STILL blank with this palette, the issue is projection/viewport/depth ordering. */
  const DEBUG_COLORS = {
    trader:   [0.95, 0.75, 0.55, 1],   // warm tan/orange for character
    desk:     [0.55, 0.35, 0.20, 1],   // brown for desk
    monitors: [0.30, 0.50, 0.85, 1],   // blue for monitors
    chair:    [0.30, 0.30, 0.35, 1],   // dark gray for chair
    city:     [0.15, 0.25, 0.45, 1],   // navy/blue for city
    plants:   [0.25, 0.55, 0.30, 1],   // green for plants
    lights:   [0.95, 0.75, 0.35, 1],   // amber for lights
    wall:     [0.25, 0.28, 0.35, 1],   // visible gray for walls
    floor:    [0.15, 0.18, 0.22, 1],   // visible dark for floor
    sky:      [0.10, 0.15, 0.30, 1],   // visible blue for sky
  };

  const X = [1, 0, 0], Y = [0, 1, 0], Z = [0, 0, 1];

  function hash(n) {
    const s = Math.sin(n * 127.1) * 43758.5453;
    return s - Math.floor(s);
  }

  function lerp(a, b, t) {
    return a + (b - a) * Math.max(0, Math.min(1, t));
  }

  function lerpColor(c1, c2, t) {
    return [
      lerp(c1[0], c2[0], t),
      lerp(c1[1], c2[1], t),
      lerp(c1[2], c2[2], t),
      lerp(c1[3], c2[3], t),
    ];
  }

  function withAlpha(colour, alpha) {
    return [colour[0], colour[1], colour[2], alpha];
  }

  // ========================================================= PROOF ART RENDERER

  class ProofArt {
    constructor(runtime) {
      this.runtime = runtime;
      this.scene = runtime.scene;
      this.room = this.scene.room;
      this.series = new Map();
      this.contrastMode = 'normal';
      for (const quad of this.scene.quads) {
        if (quad.live) this.series.set(quad.id, this._series(quad.id));
      }
      this.cityLights = this._cityLights();
      this.clouds = this._clouds();
    }

    /* Get a color, using debug overrides when in contrast=debug mode. */
    _col(category, baseColor) {
      if (this.contrastMode === 'debug' && DEBUG_COLORS[category]) {
        return DEBUG_COLORS[category];
      }
      return baseColor;
    }

    _series(seed) {
      let h = 0;
      for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) & 0x7fffffff;
      const points = [];
      let value = 0.5;
      for (let i = 0; i < 180; i++) {
        h = (h * 1103515245 + 12345) & 0x7fffffff;
        value = Math.max(0.06, Math.min(0.94, value + ((h / 0x7fffffff) - 0.5) * 0.075));
        points.push(value);
      }
      return points;
    }

    _cityLights() {
      const lights = [];
      for (let tower = 0; tower < 28; tower++) {
        const t = hash(tower * 3.7);
        const width = 160 + t * 380;
        const height = 380 + hash(tower * 7.1) * 2200;
        const x = 100 + tower * 260 + hash(tower * 11.3) * 120;
        const depth = hash(tower * 5.9);
        const windows = [];
        const cols = Math.max(2, Math.floor(width / 60));
        const rows = Math.max(3, Math.floor(height / 95));
        for (let c = 0; c < cols; c++) {
          for (let r = 0; r < rows; r++) {
            const lit = hash(tower * 31 + c * 7 + r * 13);
            if (lit < 0.38) continue;
            windows.push({
              cx: x - width / 2 + (c + 0.5) * (width / cols),
              cz: (r + 0.5) * (height / rows),
              warm: hash(tower * 17 + c * 3 + r * 5) > 0.68,
              red: hash(tower * 23 + c * 11 + r * 7) > 0.97,
              phase: hash(tower * 23 + c * 11 + r * 2) * 100,
              flickers: hash(tower * 41 + c * 13 + r * 3) > 0.92,
              w: (width / cols) * 0.38,
              h: (height / rows) * 0.30,
            });
          }
        }
        // Crown lights on tall towers
        const hasCrown = height > 1600 && hash(tower * 67) > 0.5;
        lights.push({ x, width, height, depth, windows, hasCrown });
      }
      return lights;
    }

    _clouds() {
      const clouds = [];
      for (let i = 0; i < 6; i++) {
        clouds.push({
          x: hash(i * 17) * 7000 - 500,
          z: 2200 + hash(i * 23) * 800,
          w: 600 + hash(i * 31) * 1200,
          h: 120 + hash(i * 37) * 200,
          speed: 2 + hash(i * 41) * 4,
          alpha: 0.03 + hash(i * 47) * 0.04,
        });
      }
      return clouds;
    }

    // ------------------------------------------------------- LAYER 1: CITY

    city(now) {
      const r = this.runtime;
      r.primitive = 'proof:city';
      const [rw, rd] = this.room;

      /* In debug contrast mode, use highly visible sky colors */
      const skyLow = this._col('sky', C.night_sky_low);
      const skyTop = this._col('sky', C.night_sky_top);
      const cityCol = this._col('city', C.city_far);

      // Sky gradient - deep blue to warmer horizon
      r._quad([rw / 2, rd + 800, 2000], [8000, 4200], X, Z,
              skyLow, S.GRADIENT, 0, { colour2: skyTop });

      // Horizon glow
      r._quad([rw / 2, rd + 600, 600], [8000, 1400], X, Z,
              withAlpha(cityCol, 0.5), S.GLOW, 0, { colour2: C.clear });

      // Moving clouds
      for (const cloud of this.clouds) {
        const cx = ((cloud.x + now * cloud.speed / 1000) % 8000) - 500;
        r._quad([cx, rd + 700, cloud.z], [cloud.w, cloud.h], X, Z,
                withAlpha([0.15, 0.18, 0.25, 1], cloud.alpha), S.GLOW, 0, { colour2: C.clear });
      }

      // City towers
      for (const tower of this.cityLights) {
        const far = tower.depth > 0.5;
        const mid = tower.depth > 0.25 && tower.depth <= 0.5;
        const y = rd + 700 + tower.depth * 3000;

        if (!r._nearFrame([tower.x, y, tower.height / 2], 1.4)) continue;

        // Tower body with subtle gradient
        const baseColor = far ? C.city_far : (mid ? C.city_mid : C.city_near);
        r._quad([tower.x, y, tower.height / 2], [tower.width, tower.height], X, Z,
                baseColor, S.GRADIENT, 0, { colour2: withAlpha(baseColor, 0.7) });

        // Tower crown lights
        if (tower.hasCrown) {
          const crownAlpha = 0.5 + 0.3 * Math.sin(now / 1800 + tower.x);
          r._quad([tower.x, y - 30, tower.height - 20], [tower.width * 0.6, 40], X, Z,
                  withAlpha(C.city_window_red, crownAlpha), S.GLOW, 0, { colour2: C.clear });
        }

        // Windows
        for (const w of tower.windows) {
          if (!r._nearFrame([w.cx, y - 20, w.cz], 0.15)) continue;
          let alpha = far ? 0.45 : (mid ? 0.60 : 0.80);
          if (w.flickers) {
            alpha *= 0.4 + 0.6 * (0.5 + 0.5 * Math.sin(now / 2200 + w.phase));
          }
          const color = w.red ? C.city_window_red : (w.warm ? C.city_window_warm : C.city_window);
          r._quad([w.cx, y - 25, w.cz], [w.w, w.h], X, Z, withAlpha(color, alpha), S.RECT);
        }
      }
    }

    // ------------------------------------------------- LAYER 2: REAR OFFICE

    rearOffice(now) {
      const r = this.runtime;
      r.primitive = 'proof:rear_office';
      const [rw, rd, rh] = this.room;

      /* In debug contrast mode, use visible colors for floor and walls */
      const floorCol = this._col('floor', C.floor);
      const wallCol = this._col('wall', C.wall);
      const wallShadow = this._col('wall', C.wall_shadow);

      // Floor with subtle reflection
      r._quad([rw / 2, rd / 2, 0], [rw, rd], X, Y, floorCol, S.RECT);
      r._quad([rw / 2, rd / 2, 2], [rw * 0.8, rd * 0.6], X, Y,
              withAlpha(C.floor_reflect, 0.15), S.GLOW, 0, { colour2: C.clear });

      // Rear wall with warm lighting falloff
      r._quad([rw / 2, rd - 15, rh / 2], [rw, rh], X, Z,
              wallCol, S.GRADIENT, 0, { colour2: wallShadow });
      // Warm pool from desk lamp
      r._quad([rw - 800, rd - 20, 1400], [2200, 1800], X, Z,
              withAlpha(C.wall_warm, 0.35), S.GLOW, 0, { colour2: C.clear });

      // Side walls
      r._quad([15, rd / 2, rh / 2], [rd, rh], Y, Z, wallShadow, S.RECT);
      r._quad([rw - 15, rd / 2, rh / 2], [rd, rh], Y, Z, wallShadow, S.RECT);

      // Window frame
      r._quad([rw / 2 + 600, rd - 5, 1500], [1800, 2000], X, Z,
              withAlpha(C.bezel, 0.6), S.ROUNDED, 0.02);

      this._shelves(now);
      this._plants(now);
      this._wallDecor();
    }

    _shelves(now) {
      const r = this.runtime;
      const shelves = this.scene.boxes.find((b) => b.id === 'SHELVES');
      if (!shelves) return;

      const [x0, y0, z0] = shelves.min;
      const [x1, y1, z1] = shelves.max;
      const cx = (x0 + x1) / 2 + 80;

      // Shelf unit back
      r._quad([cx, (y0 + y1) / 2, (z0 + z1) / 2], [y1 - y0 + 40, z1 - z0 + 40], Y, Z,
              C.shelf, S.ROUNDED, 0.02);

      const books = [C.book_red, C.book_blue, C.book_green, C.book_gold];

      for (let level = 0; level < 5; level++) {
        const z = z0 + 200 + level * ((z1 - z0 - 280) / 4);

        // Shelf surface
        r._quad([cx, (y0 + y1) / 2, z], [y1 - y0 - 20, 28], Y, Z, C.shelf_lit, S.RECT);

        // Books with varying heights
        for (let b = 0; b < 10; b++) {
          const seed = level * 17 + b * 5;
          if (hash(seed) < 0.18) continue;
          const h = 160 + hash(seed * 3) * 100;
          const w = 28 + hash(seed * 7) * 35;
          const tilt = (hash(seed * 11) - 0.5) * 0.08;
          const bookColor = books[Math.floor(hash(seed * 13) * 4)];

          r._quad([cx,
                   y0 + 160 + b * ((y1 - y0 - 280) / 9),
                   z + 14 + h / 2],
                  [w, h], Y, Z,
                  withAlpha(bookColor, 0.92), S.ROUNDED, 0.08);
        }

        // Occasional small object
        if (hash(level * 29) > 0.65) {
          const objX = y0 + 80 + hash(level * 37) * (y1 - y0 - 160);
          // Small bull sculpture on one shelf
          if (level === 2) {
            r._quad([cx - 10, objX, z + 80], [60, 90], Y, Z, C.gold_dim, S.ROUNDED, 0.3);
            r._quad([cx - 10, objX + 35, z + 95], [18, 35], Y, Z, C.gold, S.RECT);
          }
        }
      }

      // Shelf light strip
      r._quad([cx - 30, (y0 + y1) / 2, z1 + 20], [y1 - y0 - 60, 8], Y, Z,
              withAlpha(C.amber, 0.6), S.RECT);
      r._quad([cx - 40, (y0 + y1) / 2, z1 - 100], [y1 - y0 + 100, 400], Y, Z,
              C.amber_glow, S.GLOW, 0, { colour2: C.clear });
    }

    _plants(now) {
      const r = this.runtime;
      const cabinet = this.scene.boxes.find((b) => b.id === 'CABINET');
      if (!cabinet) return;

      const cx = (cabinet.min[0] + cabinet.max[0]) / 2;
      const cy = (cabinet.min[1] + cabinet.max[1]) / 2;
      const top = cabinet.max[2];

      // Pot
      r._quad([cx, cy, top + 75], [130, 150], X, Z, C.pot, S.TAPER, 0.8);
      r._quad([cx, cy, top + 145], [140, 30], X, Z, C.pot, S.RECT);

      // Leaves with gentle sway
      for (let leaf = 0; leaf < 9; leaf++) {
        const drift = Math.sin(now / 4800 + leaf * 0.8) * 12;
        const angle = (leaf / 9) * Math.PI * 2;
        const radius = 30 + hash(leaf * 7) * 50;
        const lx = cx + Math.cos(angle) * radius + drift;
        const ly = cy + Math.sin(angle) * radius * 0.3;
        const lz = top + 280 + hash(leaf * 3) * 200;
        const lh = 180 + hash(leaf * 11) * 140;
        const lw = 32 + hash(leaf * 13) * 20;
        const isLit = angle > Math.PI * 0.5 && angle < Math.PI * 1.5;

        r._quad([lx, ly, lz], [lw, lh], X, Z,
                isLit ? C.plant_light : C.plant_dark, S.ELLIPSE);
      }
    }

    _wallDecor() {
      const r = this.runtime;
      const [rw, rd, rh] = this.room;

      // Trade Fix wall mark - elegant gold bars
      r._quad([rw / 2 + 1600, rd - 35, 2100], [480, 12], X, Z,
              withAlpha(C.gold, 0.65), S.RECT);
      r._quad([rw / 2 + 1600, rd - 35, 2050], [280, 8], X, Z,
              withAlpha(C.gold, 0.40), S.RECT);
      r._quad([rw / 2 + 1600, rd - 35, 2010], [160, 4], X, Z,
              withAlpha(C.gold, 0.25), S.RECT);

      // Small clock on wall
      const clockX = 850, clockY = rd - 45, clockZ = 2100;
      r._quad([clockX, clockY, clockZ], [220, 220], X, Z, C.bezel, S.ELLIPSE);
      r._quad([clockX, clockY - 8, clockZ], [190, 190], X, Z,
              withAlpha(C.wall, 0.95), S.ELLIPSE);

      // Clock hands from real time
      const date = new Date(Date.now());
      const minute = (date.getMinutes() + date.getSeconds() / 60) / 60;
      const hour = ((date.getHours() % 12) + minute) / 12;
      for (const [turn, length, width] of [[hour, 55, 8], [minute, 80, 5]]) {
        const a = turn * Math.PI * 2;
        r._quad([clockX + Math.sin(a) * length / 2, clockY - 12,
                 clockZ + Math.cos(a) * length / 2],
                [width, length], [Math.cos(a), 0, -Math.sin(a)],
                [Math.sin(a), 0, Math.cos(a)], C.gold, S.RECT);
      }
      // Center cap
      r._quad([clockX, clockY - 10, clockZ], [18, 18], X, Z, C.gold, S.ELLIPSE);
    }

    // --------------------------------------------------- LAYER 3: MONITORS

    monitors(now) {
      const r = this.runtime;
      r.primitive = 'proof:monitors';
      const screen = this.runtime.screen;
      const advancing = !(screen && screen.honesty && screen.honesty.charts_advance === false);
      const symbol = (screen && screen.active_symbol) || 'XAUUSD';
      const accent = symbol === 'BTCUSD' ? C.amber : C.gold_bright;

      /* In debug contrast mode, use visible blue for monitors */
      const monitorCol = this._col('monitors', C.bezel);
      const screenBg = this._col('monitors', C.screen_bg);

      for (const quad of this.scene.quads) {
        const yaw = quad.yaw * Math.PI / 180;
        const bx = [Math.cos(yaw), Math.sin(yaw), 0];

        if (quad.role === 'window') continue;
        if (!r._nearFrame(quad.centre, 0.55)) continue;

        // Monitor glow behind
        r._quad(quad.centre, [quad.width + 80, quad.height + 80], bx, Z,
                C.screen_glow, S.GLOW, 0, { colour2: C.clear });

        // Bezel with subtle highlight
        r._quad(quad.centre, [quad.width + 40, quad.height + 40], bx, Z,
                monitorCol, S.ROUNDED, 0.04);
        r._quad([quad.centre[0], quad.centre[1], quad.centre[2] + quad.height * 0.48],
                [quad.width + 30, 6], bx, Z, C.bezel_lit, S.RECT);

        // Screen
        r._quad(quad.centre, [quad.width, quad.height], bx, Z, screenBg, S.RECT);

        const series = this.series.get(quad.id);
        if (!series) continue;

        const role = this._screenRole(quad.id);
        if (role === 'watchlist') {
          this._watchlist(quad, bx, symbol, now, advancing);
        } else if (role === 'dashboard') {
          this._dashboard(quad, bx, symbol, now);
        } else {
          this._chart(quad, bx, series, now, advancing,
                      role === 'hero' ? accent : C.blue_fill, role === 'hero');
        }

        // Header
        const headerColor = role === 'hero' ? accent :
                           (role === 'alternate' ? C.blue_fill : C.screen_grid);
        r._quad([quad.centre[0], quad.centre[1], quad.centre[2] + quad.height * 0.43],
                [quad.width * 0.94, quad.height * 0.08], bx, Z,
                withAlpha(headerColor, 0.55), S.RECT);

        // Symbol label on hero
        if (role === 'hero') {
          r._quad([quad.centre[0] - bx[0] * quad.width * 0.38,
                   quad.centre[1] - bx[1] * quad.width * 0.38,
                   quad.centre[2] + quad.height * 0.43],
                  [quad.width * 0.18, quad.height * 0.055], bx, Z,
                  withAlpha(accent, 0.9), S.ROUNDED, 0.2);
        }

        if (!advancing) {
          r._quad(quad.centre, [quad.width, quad.height], bx, Z,
                  withAlpha(C.screen_bg, 0.70), S.RECT);
        }

        // Monitor spill
        if (quad.id === 'MON_1') {
          r._quad([quad.centre[0], quad.centre[1] + 350, quad.centre[2] - 150],
                  [2200, 1800], bx, Z, C.blue_glow, S.GLOW, 0, { colour2: C.clear });
        }
      }
    }

    _screenRole(id) {
      if (id === 'MON_1') return 'hero';
      if (id === 'MON_3') return 'alternate';
      if (id === 'MON_2') return 'watchlist';
      if (id === 'WALL_1' || id === 'WALL_2') return 'dashboard';
      return 'secondary';
    }

    _chart(quad, bx, series, now, advancing, accent, hero) {
      const r = this.runtime;
      const bars = hero ? 38 : 26;
      const phase = advancing ? Math.floor(now / (hero ? 1200 : 2400)) : 0;
      const innerW = quad.width * 0.88;
      const innerH = quad.height * 0.58;
      const baseZ = quad.centre[2] - quad.height * 0.06;

      // Grid lines
      for (let line = 0; line < 5; line++) {
        r._quad([quad.centre[0], quad.centre[1],
                 baseZ - innerH / 2 + (line + 0.5) * (innerH / 5)],
                [innerW, 1.5], bx, Z, withAlpha(C.screen_grid, 0.45), S.RECT);
      }

      const barW = innerW / (bars * 1.5);
      for (let i = 0; i < bars; i++) {
        const value = series[(i + phase) % series.length];
        const previous = series[(i + phase + series.length - 1) % series.length];
        const up = value >= previous;
        const offset = (i / (bars - 1) - 0.5) * innerW;
        const bodyH = Math.max(innerH * 0.015, Math.abs(value - previous) * innerH * 2.4);
        const mid = baseZ - innerH / 2 + ((value + previous) / 2) * innerH;
        const colour = advancing ? (up ? C.up : C.down) : withAlpha(C.screen_grid, 0.55);

        // Wick
        r._quad([quad.centre[0] + bx[0] * offset, quad.centre[1] + bx[1] * offset, mid],
                [Math.max(1.2, barW * 0.14), bodyH * 2.3], bx, Z,
                withAlpha(colour, 0.55), S.RECT);
        // Body
        r._quad([quad.centre[0] + bx[0] * offset, quad.centre[1] + bx[1] * offset, mid],
                [barW, bodyH], bx, Z, colour, S.RECT);
      }

      // Price line
      const last = series[(bars - 1 + phase) % series.length];
      r._quad([quad.centre[0], quad.centre[1], baseZ - innerH / 2 + last * innerH],
              [innerW, 2.5], bx, Z, withAlpha(accent, 0.9), S.RECT);
      // Price glow
      r._quad([quad.centre[0] + bx[0] * innerW * 0.42, quad.centre[1] + bx[1] * innerW * 0.42,
               baseZ - innerH / 2 + last * innerH],
              [innerW * 0.16, quad.height * 0.06], bx, Z,
              withAlpha(accent, 0.85), S.ROUNDED, 0.3);
    }

    _watchlist(quad, bx, symbol, now, advancing) {
      const r = this.runtime;
      const rows = 8;
      const innerW = quad.width * 0.86;
      const innerH = quad.height * 0.72;

      for (let row = 0; row < rows; row++) {
        const z = quad.centre[2] + innerH / 2 - (row + 0.5) * (innerH / rows);
        const active = row === (symbol === 'BTCUSD' ? 1 : 0);

        r._quad([quad.centre[0], quad.centre[1], z], [innerW, innerH / rows * 0.72],
                bx, Z, withAlpha(active ? C.gold : C.screen_grid, active ? 0.35 : 0.22), S.RECT);

        // Symbol block
        r._quad([quad.centre[0] - bx[0] * innerW * 0.32,
                 quad.centre[1] - bx[1] * innerW * 0.32, z],
                [innerW * 0.24, innerH / rows * 0.28], bx, Z,
                withAlpha(active ? C.gold_bright : C.screen_grid, 0.85), S.RECT);

        // Mini sparkline
        for (let i = 0; i < 12; i++) {
          const sparkH = (hash(row * 17 + i * 7) * 0.6 + 0.2) * innerH / rows * 0.35;
          r._quad([quad.centre[0] + bx[0] * (innerW * 0.05 + i * innerW * 0.025),
                   quad.centre[1] + bx[1] * (innerW * 0.05 + i * innerW * 0.025),
                   z - innerH / rows * 0.15 + sparkH / 2],
                  [innerW * 0.018, sparkH], bx, Z,
                  withAlpha(C.screen_grid, 0.6), S.RECT);
        }

        // Change indicator
        const change = hash(row * 13 + (advancing ? Math.floor(now / 3500) : 0)) - 0.5;
        r._quad([quad.centre[0] + bx[0] * innerW * 0.36,
                 quad.centre[1] + bx[1] * innerW * 0.36, z],
                [innerW * 0.14, innerH / rows * 0.28], bx, Z,
                withAlpha(change >= 0 ? C.up : C.down, 0.82), S.ROUNDED, 0.15);
      }
    }

    _dashboard(quad, bx, symbol, now) {
      const r = this.runtime;
      const innerW = quad.width * 0.85;
      const innerH = quad.height * 0.75;

      // "TRADE FIX RADIO" header
      r._quad([quad.centre[0], quad.centre[1], quad.centre[2] + innerH * 0.38],
              [innerW * 0.7, innerH * 0.08], bx, Z,
              withAlpha(C.gold, 0.75), S.ROUNDED, 0.25);

      // Status blocks
      for (let i = 0; i < 4; i++) {
        const bz = quad.centre[2] + innerH * 0.1 - i * innerH * 0.22;
        r._quad([quad.centre[0], quad.centre[1], bz],
                [innerW * 0.9, innerH * 0.16], bx, Z,
                withAlpha(C.screen_grid, 0.28), S.ROUNDED, 0.1);

        // Status indicator dot
        const isActive = i === 0 || (i === 1 && symbol === 'XAUUSD');
        r._quad([quad.centre[0] - bx[0] * innerW * 0.38,
                 quad.centre[1] - bx[1] * innerW * 0.38, bz],
                [16, 16], bx, Z, isActive ? C.up : C.screen_grid, S.ELLIPSE);
      }
    }

    // ------------------------------------------------------- LAYER 5: DESK

    desk() {
      const r = this.runtime;
      r.primitive = 'proof:desk';
      const desk = this.scene.boxes.find((b) => b.id === 'DESK');
      if (!desk) return;

      const [x0, y0, z0] = desk.min;
      const [x1, y1, z1] = desk.max;
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;

      /* In debug contrast mode, use visible brown for desk */
      const deskTop = this._col('desk', C.desk_top);
      const deskLit = this._col('desk', C.desk_top_lit);

      // Desk shadow on floor
      r._quad([cx, cy - 200, 5], [x1 - x0 + 200, 400], X, Y,
              withAlpha(C.desk_shadow, 0.4), S.GLOW, 0, { colour2: C.clear });

      // Desk surface with lit area
      r._quad([cx, cy, z1], [x1 - x0, y1 - y0], X, Y, deskTop, S.RECT);
      r._quad([x1 - 700, cy, z1 + 2], [1400, y1 - y0 - 100], X, Y,
              withAlpha(deskLit, 0.5), S.GLOW, 0, { colour2: C.clear });

      // Desk mat
      r._quad([cx - 200, cy - 80, z1 + 3], [900, 450], X, Y,
              withAlpha([0.025, 0.028, 0.035, 1], 0.95), S.ROUNDED, 0.03);

      // Near edge
      r._quad([cx, y0, (z0 + z1) / 2 + 15], [x1 - x0, (z1 - z0) + 30], X, Z, C.desk_edge, S.RECT);

      // Front panel
      r._quad([cx, y0 - 12, z0 / 2], [x1 - x0, z0], X, Z, C.desk_front, S.RECT);

      // Warm lamp glow on desk
      r._quad([x1 - 650, y1 - 280, z1 + 50], [2000, 1300], X, Y,
              C.amber_glow, S.GLOW, 0, { colour2: C.clear });
    }

    // --------------------------------------------- LAYER 6: FOREGROUND PROPS

    props(now) {
      const r = this.runtime;
      r.primitive = 'proof:props';
      const carried = this.runtime.propOffsets;

      for (const box of this.scene.boxes) {
        const held = carried.get(box.id);
        const d = held || [0, 0, 0];
        const min = [box.min[0] + d[0], box.min[1] + d[1], box.min[2] + d[2]];
        const max = [box.max[0] + d[0], box.max[1] + d[1], box.max[2] + d[2]];
        const centre = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
        const w = max[0] - min[0], dy = max[1] - min[1], h = max[2] - min[2];

        switch (box.id) {
          case 'KEYBOARD': {
            // Keyboard body
            r._quad([centre[0], centre[1], max[2]], [w, dy], X, Y, C.keyboard, S.ROUNDED, 0.06);
            // Key rows
            for (let row = 0; row < 5; row++) {
              const keysInRow = row === 4 ? 3 : 14;
              for (let key = 0; key < keysInRow; key++) {
                const kw = row === 4 ? (w - 30) / 3 * 0.9 : (w - 28) / 14 * 0.78;
                const kx = row === 4
                  ? min[0] + 14 + key * ((w - 24) / 3) + kw / 2
                  : min[0] + 14 + key * ((w - 24) / 14) + kw / 2;
                const ky = min[1] + 14 + row * ((dy - 26) / 5);
                const isLit = hash(row * 17 + key * 3) > 0.85;
                r._quad([kx, ky, max[2] + 3], [kw, (dy - 26) / 5 * 0.68], X, Y,
                        isLit ? C.key_lit : C.key, S.ROUNDED, 0.15);
              }
            }
            break;
          }
          case 'MOUSE':
            r._quad([centre[0], centre[1], max[2]], [w, dy], X, Y, C.keyboard, S.ELLIPSE);
            r._quad([centre[0], centre[1] - dy * 0.2, max[2] + 3], [w * 0.12, dy * 0.28],
                    X, Y, withAlpha(C.gold, 0.55), S.ROUNDED, 0.3);
            r._quad([centre[0], centre[1] + dy * 0.15, max[2] + 2], [w * 0.6, dy * 0.35],
                    X, Y, C.key, S.ROUNDED, 0.2);
            break;
          case 'MUG': {
            const isHeld = !!held;
            const bodyColor = isHeld ? C.gold : C.mug_body;
            const rimColor = isHeld ? C.gold_bright : C.mug_rim;

            // Mug body
            r._quad(centre, [w, h], X, Z, bodyColor, S.ROUNDED, 0.15);
            // Rim
            r._quad([centre[0], centre[1], max[2] - 5], [w + 6, 14], X, Z, rimColor, S.RECT);
            // Handle
            r._quad([centre[0] + w * 0.62, centre[1], centre[2]], [w * 0.28, h * 0.45],
                    X, Z, withAlpha(bodyColor, 0.9), S.OUTLINE, 0.35);
            // Coffee
            r._quad([centre[0], centre[1], max[2] - 10], [w * 0.75, dy * 0.75], X, Y,
                    C.coffee, S.ELLIPSE);
            // Steam
            if (!isHeld) {
              for (let puff = 0; puff < 4; puff++) {
                const t = ((now / 2400) + puff / 4) % 1;
                const wobble = Math.sin(now / 800 + puff * 2.5) * 22;
                r._quad([centre[0] + wobble, centre[1], max[2] + 35 + t * 220],
                        [50 + t * 80, 50 + t * 80], X, Z,
                        withAlpha(C.steam, (1 - t) * 0.9), S.GLOW, 0, { colour2: C.clear });
              }
            }
            break;
          }
          case 'NOTEBOOK':
            // Cover
            r._quad([centre[0], centre[1], max[2]], [w, dy], X, Y,
                    [0.12, 0.10, 0.085, 1], S.ROUNDED, 0.035);
            // Pages
            r._quad([centre[0], centre[1], max[2] + 3], [w * 0.92, dy * 0.94], X, Y,
                    [0.82, 0.78, 0.72, 1], S.RECT);
            // Lines
            for (let line = 0; line < 7; line++) {
              r._quad([centre[0], min[1] + 22 + line * ((dy - 36) / 7), max[2] + 4],
                      [w * 0.76, 2], X, Y, withAlpha(C.screen_grid, 0.55), S.RECT);
            }
            // Binding
            r._quad([centre[0] - w * 0.47, centre[1], max[2] + 2], [w * 0.04, dy * 0.9], X, Y,
                    [0.08, 0.07, 0.06, 1], S.RECT);
            break;
          case 'STREAM_PAD': {
            r._quad([centre[0], centre[1], max[2]], [w, dy], X, Y, C.keyboard, S.ROUNDED, 0.08);
            for (let i = 0; i < 6; i++) {
              const lit = hash(i * 7 + Math.floor(now / 2800)) > 0.45;
              const bx = min[0] + 16 + (i % 3) * ((w - 28) / 3);
              const by = min[1] + 16 + Math.floor(i / 3) * ((dy - 28) / 2);
              r._quad([bx + ((w - 32) / 6), by + ((dy - 32) / 4), max[2] + 3],
                      [(w - 38) / 3 * 0.8, (dy - 38) / 2 * 0.8], X, Y,
                      lit ? withAlpha(C.gold, 0.8) : withAlpha(C.screen_grid, 0.5), S.ROUNDED, 0.12);
            }
            break;
          }
          default:
            break;
        }
      }

      // Pen
      const pen = this.scene.anchors.ANCHOR_PEN;
      if (pen) {
        const penHeld = carried.get('PEN');
        const at = penHeld ? [pen[0] + penHeld[0], pen[1] + penHeld[1], pen[2] + penHeld[2]] : pen;
        r._quad(at, [140, 12], X, Y, penHeld ? C.gold_bright : C.gold, S.ROUNDED, 0.45);
        r._quad([at[0] - 60, at[1], at[2]], [22, 12], X, Y,
                penHeld ? C.gold : C.gold_dim, S.ROUNDED, 0.3);
      }

      // Microphone
      const desk = this.scene.boxes.find((b) => b.id === 'DESK');
      if (desk) {
        const mx = desk.min[0] + 480;
        const my = desk.max[1] - 220;
        const mz = desk.max[2];

        // Arm
        r._quad([mx, my, mz + 160], [22, 320], X, Z, C.mic, S.RECT);
        r._quad([mx, my - 50, mz + 320], [100, 22], X, Y, C.mic, S.RECT);

        // Mic head
        r._quad([mx, my - 50, mz + 420], [100, 180], X, Z, C.mic, S.ROUNDED, 0.35);
        r._quad([mx, my - 60, mz + 420], [75, 140], X, Z, C.mic_mesh, S.ELLIPSE);

        // Phone
        r._quad([desk.max[0] - 380, desk.min[1] + 220, mz + 8], [140, 270],
                X, Y, C.bezel, S.ROUNDED, 0.12);
        r._quad([desk.max[0] - 380, desk.min[1] + 220, mz + 10], [120, 230],
                X, Y, C.screen_bg, S.RECT);

        // Small desk clock
        r._quad([desk.max[0] - 180, desk.min[1] + 140, mz + 50], [80, 80],
                X, Z, C.bezel, S.ROUNDED, 0.15);
        r._quad([desk.max[0] - 180, desk.min[1] + 135, mz + 50], [60, 60],
                X, Z, withAlpha(C.screen_bg, 0.9), S.ELLIPSE);
      }
    }

    // -------------------------------------------------- LAYER 7: ATMOSPHERE

    atmosphere(now) {
      const r = this.runtime;
      r.primitive = 'proof:atmosphere';
      const [rw, rd, rh] = this.room;

      // Warm key light pool
      r._quad([rw - 850, rd - 1100, 1850], [3000, 2400], X, Z,
              withAlpha(C.amber_soft, 0.065), S.GLOW, 0, { colour2: C.clear });

      // Cool fill from monitors
      r._quad([3100, 2000, 1150], [2800, 2000], X, Z,
              withAlpha(C.blue_fill, 0.055), S.GLOW, 0, { colour2: C.clear });

      // Subtle dust motes in warm light
      for (let mote = 0; mote < 8; mote++) {
        const seed = mote * 13;
        const drift = (now / 8000 + hash(seed)) % 1;
        const mx = rw - 1000 + (hash(seed * 3) - 0.5) * 800;
        const mz = 1200 + hash(seed * 5) * 900;
        const my = rd - 600 + drift * 500;
        const size = 3 + hash(seed * 7) * 4;
        const alpha = 0.15 * (1 - Math.abs(drift - 0.5) * 2);
        r._quad([mx, my, mz], [size, size], X, Z,
                withAlpha(C.amber, alpha), S.ELLIPSE);
      }
    }

    // --------------------------------------------------- LAYER 4: CHARACTER

    character(now, lidClose, gazeEase) {
      const r = this.runtime;
      const worlds = this.runtime.worlds;
      if (!worlds) return;

      const at = (id) => {
        const world = worlds.get(id);
        return world ? world.position : null;
      };
      const rot = (id) => {
        const world = worlds.get(id);
        return world ? world.rotation : 0;
      };

      const pelvis = at('pelvis'), torso = at('torso'), neck = at('neck');
      const head = at('head');
      if (!pelvis || !torso || !neck || !head) return;

      // Lower body
      this._characterLower(pelvis, r);

      // Torso and arms
      this._characterTorso(torso, neck, at, rot, r);

      // Arms
      this._characterArms(at, rot, r);

      // Hands
      this._characterHands(at, rot, r, now);

      // Head and face
      this._characterHead(head, neck, at, rot, r, now, lidClose, gazeEase);

      // Headphones
      this._characterHeadphones(at, rot, r);
    }

    _characterLower(pelvis, r) {
      r.primitive = 'proof:character_lower';
      const plane = r._billboard(0);
      const bx = plane.right, bz = plane.up;

      // Chair back visible behind
      r._quad([pelvis[0], pelvis[1] + 220, pelvis[2] + 200], [480, 520], bx, bz,
              C.hoodie_shadow, S.ROUNDED, 0.15);

      // Trousers
      r._quad([pelvis[0], pelvis[1] + 100, pelvis[2] - 200], [400, 480], bx, bz,
              C.trousers, S.ROUNDED, 0.18);
      r._quad([pelvis[0], pelvis[1] + 90, pelvis[2] - 200], [380, 440], bx, bz,
              C.trousers_shadow, S.ROUNDED, 0.18);

      // Legs
      r._quad([pelvis[0] - 120, pelvis[1] - 100, 280], [160, 480], bx, bz,
              C.trousers, S.ROUNDED, 0.25);
      r._quad([pelvis[0] + 120, pelvis[1] - 100, 280], [160, 480], bx, bz,
              C.trousers, S.ROUNDED, 0.25);

      // Sneakers
      r._quad([pelvis[0] - 120, pelvis[1] - 300, 55], [175, 110], bx, bz,
              [0.045, 0.048, 0.058, 1], S.ROUNDED, 0.32);
      r._quad([pelvis[0] + 120, pelvis[1] - 300, 55], [175, 110], bx, bz,
              [0.045, 0.048, 0.058, 1], S.ROUNDED, 0.32);
    }

    _characterTorso(torso, neck, at, rot, r) {
      r.primitive = 'proof:character_torso';
      const plane = r._billboard(rot('torso'));
      const bx = plane.right, bz = plane.up;

      /* In debug contrast mode, use visible tan/orange for trader */
      const hoodieCol = this.contrastMode === 'debug' ? DEBUG_COLORS.trader : C.hoodie;
      const hoodieShadow = this.contrastMode === 'debug'
        ? withAlpha(DEBUG_COLORS.trader, 0.7) : C.hoodie_shadow;

      // Hoodie body - layered for depth
      r._quad([torso[0], torso[1] + 50, torso[2] - 50], [580, 620], bx, bz,
              hoodieShadow, S.TAPER, 0.82);
      r._quad([torso[0], torso[1] + 35, torso[2] - 40], [560, 600], bx, bz,
              hoodieCol, S.TAPER, 0.82);

      // Hoodie lit side
      r._quad([torso[0] + 180, torso[1] + 30, torso[2] - 20], [200, 480], bx, bz,
              withAlpha(C.hoodie_lit, 0.6), S.ROUNDED, 0.3);

      // Hoodie folds
      r._quad([torso[0] - 60, torso[1] + 20, torso[2] + 80], [80, 180], bx, bz,
              withAlpha(C.hoodie_fold, 0.5), S.ROUNDED, 0.4);
      r._quad([torso[0] + 40, torso[1] + 25, torso[2] - 120], [60, 140], bx, bz,
              withAlpha(C.hoodie_shadow, 0.4), S.ROUNDED, 0.4);

      // Shoulders
      const shoulderL = at('shoulder_l'), shoulderR = at('shoulder_r');
      if (shoulderL) {
        r._quad(shoulderL, [280, 210], bx, bz, C.hoodie_lit, S.ELLIPSE);
        r._quad([shoulderL[0] - 30, shoulderL[1] + 20, shoulderL[2] - 20], [180, 140], bx, bz,
                withAlpha(C.hoodie, 0.7), S.ELLIPSE);
      }
      if (shoulderR) {
        r._quad(shoulderR, [280, 210], bx, bz, C.hoodie, S.ELLIPSE);
        r._quad([shoulderR[0] + 30, shoulderR[1] + 20, shoulderR[2] - 20], [180, 140], bx, bz,
                withAlpha(C.hoodie_shadow, 0.6), S.ELLIPSE);
      }

      // Hood bunched behind neck
      r._quad([torso[0], torso[1] + 140, torso[2] + 220], [400, 200], bx, bz, C.hood, S.ELLIPSE);
      r._quad([torso[0], torso[1] + 130, torso[2] + 260], [320, 120], bx, bz,
              withAlpha(C.hoodie_shadow, 0.7), S.ELLIPSE);

      // Black shirt in V-opening
      r._quad([torso[0], torso[1] - 55, torso[2] + 100], [160, 280], bx, bz, C.shirt, S.TAPER, 0.45);

      // Gold pendant
      r._quad([torso[0], torso[1] - 65, torso[2] + 30], [48, 8], bx, bz,
              withAlpha(C.gold, 0.92), S.ROUNDED, 0.4);
      r._quad([torso[0], torso[1] - 65, torso[2] + 85], [2.5, 100], bx, bz,
              withAlpha(C.gold, 0.5), S.RECT);
    }

    _characterArms(at, rot, r) {
      r.primitive = 'proof:character_arms';
      const plane = r._billboard(rot('torso'));
      const bx = plane.right, bz = plane.up;

      // Upper arms and forearms
      for (const [a, b, width, isLeft] of [
        ['shoulder_l', 'forearm_l', 175, true],
        ['forearm_l', 'hand_l', 140, true],
        ['shoulder_r', 'forearm_r', 175, false],
        ['forearm_r', 'hand_r', 140, false],
      ]) {
        const from = at(a), to = at(b);
        if (from && to) {
          const color = isLeft ? C.hoodie_lit : C.hoodie;
          const shadowColor = isLeft ? C.hoodie : C.hoodie_shadow;
          this._segment(r, from, to, width, color);
          // Shadow on inner side
          const mid = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2];
          r._quad([mid[0] + (isLeft ? -25 : 25), mid[1] + 20, mid[2]],
                  [width * 0.5, 80], bx, bz, withAlpha(shadowColor, 0.4), S.ELLIPSE);
        }
      }

      // Elbow joints
      for (const [id, isLeft] of [['forearm_l', true], ['forearm_r', false]]) {
        const p = at(id);
        if (p) {
          r._quad(p, [155, 155], bx, bz, isLeft ? C.hoodie : C.hoodie_shadow, S.ELLIPSE);
        }
      }
    }

    _characterHands(at, rot, r, now) {
      r.primitive = 'proof:character_hands';
      const plane = r._billboard(rot('torso'));
      const bx = plane.right, bz = plane.up;

      for (const [id, isLeft] of [['hand_l', true], ['hand_r', false]]) {
        const p = at(id);
        if (!p) continue;

        // Hand palm
        r._quad(p, [145, 105], bx, bz, C.skin, S.ELLIPSE);
        r._quad([p[0], p[1] - 20, p[2] - 10], [130, 80], bx, bz, C.skin_shadow, S.ELLIPSE);

        // Fingers - simplified but readable
        const fingerOffsets = [[-40, -50], [-12, -60], [18, -58], [45, -48]];
        for (let f = 0; f < 4; f++) {
          const [dx, dy] = fingerOffsets[f];
          const fingerH = 65 + (f === 1 ? 15 : (f === 3 ? -10 : 0));
          r._quad([p[0] + dx, p[1] + dy, p[2] - 12], [26, fingerH], bx, bz,
                  f === 0 || f === 3 ? C.skin_shadow : C.skin, S.ROUNDED, 0.38);
        }
        // Thumb
        r._quad([p[0] + (isLeft ? 55 : -55), p[1] - 15, p[2]], [45, 55], bx, bz,
                C.skin_warm, S.ROUNDED, 0.4);
      }

      // Watch on left wrist
      const wrist = at('hand_l');
      if (wrist) {
        r._quad([wrist[0], wrist[1] + 55, wrist[2] + 28], [135, 38], bx, bz,
                C.watch_band, S.ROUNDED, 0.28);
        r._quad([wrist[0], wrist[1] + 50, wrist[2] + 28], [48, 28], bx, bz,
                C.watch_face, S.ROUNDED, 0.25);
        r._quad([wrist[0], wrist[1] + 48, wrist[2] + 28], [38, 20], bx, bz,
                withAlpha(C.gold, 0.65), S.ELLIPSE);
      }
    }

    _characterHead(head, neck, at, rot, r, now, lidClose, gazeEase) {
      r.primitive = 'proof:character_head';
      const plane = r._billboard(rot('head'));
      const bx = plane.right, bz = plane.up;

      // Neck
      r._quad([neck[0], neck[1] + 15, neck[2]], [145, 180], bx, bz, C.skin_shadow, S.RECT);
      r._quad([neck[0] - 20, neck[1] + 5, neck[2]], [100, 160], bx, bz, C.skin, S.RECT);

      // Hair back
      r._quad([head[0], head[1] + 20, head[2] + 65], [255, 175], bx, bz, C.hair_shadow, S.ELLIPSE);

      // Skull base
      r._quad(head, [235, 280], bx, bz, C.skin, S.ELLIPSE);

      // Face structure - jaw and cheekbones
      r._quad([head[0], head[1] - 18, head[2] - 85], [195, 140], bx, bz, C.skin, S.ELLIPSE);
      // Cheekbone shadows
      r._quad([head[0] - 75, head[1] - 5, head[2] - 30], [50, 90], bx, bz,
              withAlpha(C.skin_shadow, 0.4), S.ELLIPSE);
      r._quad([head[0] + 75, head[1] - 5, head[2] - 30], [50, 90], bx, bz,
              withAlpha(C.skin_shadow, 0.5), S.ELLIPSE);

      // Jaw line
      r._quad([head[0], head[1] - 30, head[2] - 115], [175, 70], bx, bz, C.skin_warm, S.ELLIPSE);

      // Nose
      r._quad([head[0], head[1] - 42, head[2] - 20], [35, 70], bx, bz, C.nose_shadow, S.ROUNDED, 0.3);
      r._quad([head[0] - 8, head[1] - 48, head[2] - 5], [18, 45], bx, bz,
              withAlpha(C.nose_highlight, 0.6), S.ELLIPSE);
      // Nostrils hint
      r._quad([head[0] - 12, head[1] - 40, head[2] - 50], [12, 10], bx, bz,
              withAlpha(C.skin_deep, 0.5), S.ELLIPSE);
      r._quad([head[0] + 12, head[1] - 40, head[2] - 50], [12, 10], bx, bz,
              withAlpha(C.skin_deep, 0.5), S.ELLIPSE);

      // Beard/stubble
      r._quad([head[0], head[1] - 32, head[2] - 100], [185, 130], bx, bz,
              C.beard, S.ELLIPSE);
      r._quad([head[0], head[1] - 25, head[2] - 85], [165, 100], bx, bz,
              withAlpha(C.beard_shadow, 0.6), S.ELLIPSE);
      // Stubble texture above beard line
      r._quad([head[0] - 55, head[1] - 25, head[2] - 45], [40, 70], bx, bz, C.stubble, S.ELLIPSE);
      r._quad([head[0] + 55, head[1] - 25, head[2] - 45], [40, 70], bx, bz, C.stubble, S.ELLIPSE);

      // Mouth
      r._quad([head[0], head[1] - 38, head[2] - 68], [55, 12], bx, bz, C.lip_shadow, S.ELLIPSE);
      r._quad([head[0], head[1] - 40, head[2] - 62], [48, 8], bx, bz, C.lip, S.ELLIPSE);

      // Brow ridge
      r._quad([head[0], head[1] - 40, head[2] + 32], [185, 24], bx, bz, C.skin_shadow, S.ROUNDED, 0.35);

      // Eyebrows
      r._quad([head[0] - 52, head[1] - 42, head[2] + 42], [70, 14], bx, bz, C.brow, S.ROUNDED, 0.4);
      r._quad([head[0] + 52, head[1] - 42, head[2] + 42], [70, 14], bx, bz, C.brow, S.ROUNDED, 0.4);

      // Eyes
      this._drawEyes(head, at, r, bx, bz, lidClose);

      // Hair
      this._drawHair(head, r, bx, bz, now);

      // Ears
      r._quad([head[0] - 118, head[1] + 8, head[2] - 15], [42, 80], bx, bz, C.skin_shadow, S.ELLIPSE);
      r._quad([head[0] - 115, head[1] + 5, head[2] - 15], [32, 65], bx, bz, C.skin, S.ELLIPSE);
      r._quad([head[0] + 118, head[1] + 8, head[2] - 15], [42, 80], bx, bz, C.skin_shadow, S.ELLIPSE);
      r._quad([head[0] + 115, head[1] + 5, head[2] - 15], [32, 65], bx, bz, C.skin, S.ELLIPSE);

      // Forehead highlight
      r._quad([head[0] - 30, head[1] - 35, head[2] + 70], [80, 50], bx, bz,
              withAlpha(C.skin_highlight, 0.35), S.ELLIPSE);
    }

    _drawEyes(head, at, r, bx, bz, lidClose) {
      r.primitive = 'proof:character_eyes';

      for (const [side, dx] of [['l', -48], ['r', 48]]) {
        const eye = at(`eye_${side}`);
        const ex = eye ? eye[0] : head[0] + dx;
        const ey = eye ? eye[1] : head[1] - 38;
        const ez = eye ? eye[2] : head[2] + 8;

        // Eye socket shadow
        r._quad([ex, ey + 5, ez + 8], [70, 35], bx, bz,
                withAlpha(C.skin_shadow, 0.5), S.ELLIPSE);

        // Sclera
        r._quad([ex, ey, ez], [58, 36], bx, bz, C.sclera, S.ELLIPSE);
        r._quad([ex + (side === 'l' ? 8 : -8), ey + 3, ez], [50, 30], bx, bz,
                withAlpha(C.sclera_shadow, 0.4), S.ELLIPSE);

        // Iris - hazel/brown with depth
        r._quad([ex, ey - 4, ez], [30, 30], bx, bz, C.iris_outer, S.ELLIPSE);
        r._quad([ex, ey - 5, ez], [24, 24], bx, bz, C.iris_inner, S.ELLIPSE);
        r._quad([ex - 4, ey - 6, ez + 4], [12, 14], bx, bz,
                withAlpha(C.iris_light, 0.6), S.ELLIPSE);

        // Pupil
        r._quad([ex, ey - 5, ez], [14, 14], bx, bz, C.pupil, S.ELLIPSE);

        // Catchlight - anime style dual catchlight
        r._quad([ex - 8, ey - 7, ez + 8], [9, 9], bx, bz, C.eye_catch, S.ELLIPSE);
        r._quad([ex + 5, ey - 3, ez + 5], [5, 5], bx, bz, C.eye_catch2, S.ELLIPSE);

        // Upper lash line
        r._quad([ex, ey - 5, ez + 18], [62, 7], bx, bz, C.lash, S.ROUNDED, 0.4);

        // Eyelid - closes from top
        if (lidClose > 0.015) {
          const lidH = Math.max(2, 38 * lidClose);
          r._quad([ex, ey - 6, ez + 18 - lidH / 2 - 2], [64, lidH], bx, bz, C.skin, S.ELLIPSE);
          // Lash line on closed lid
          if (lidClose > 0.5) {
            r._quad([ex, ey - 6, ez + 18 - lidH + 4], [58, 5], bx, bz, C.lash, S.ROUNDED, 0.4);
          }
        }
      }
    }

    _drawHair(head, r, bx, bz, now) {
      r.primitive = 'proof:character_hair';

      // Main hair mass
      r._quad([head[0], head[1] + 8, head[2] + 75], [250, 170], bx, bz, C.hair, S.ELLIPSE);

      // Lit side
      r._quad([head[0] - 50, head[1] - 20, head[2] + 105], [130, 80], bx, bz, C.hair_lit, S.ELLIPSE);

      // Hair highlight
      r._quad([head[0] - 35, head[1] - 30, head[2] + 115], [70, 45], bx, bz,
              withAlpha(C.hair_highlight, 0.55), S.ELLIPSE);

      // Side hair mass
      r._quad([head[0] - 100, head[1] + 5, head[2] + 25], [80, 120], bx, bz, C.hair_shadow, S.ELLIPSE);
      r._quad([head[0] + 100, head[1] + 5, head[2] + 25], [80, 120], bx, bz, C.hair_shadow, S.ELLIPSE);

      // Front textured strands
      const strands = [
        [-85, -35, 130, 28, 60],
        [-55, -42, 138, 24, 55],
        [-25, -45, 142, 22, 62],
        [5, -44, 140, 25, 58],
        [35, -42, 136, 23, 54],
        [65, -38, 128, 26, 50],
        [90, -32, 118, 28, 45],
      ];

      for (let i = 0; i < strands.length; i++) {
        const [dx, dy, dz, w, h] = strands[i];
        const sway = Math.sin(now / 6000 + i * 0.7) * 3;
        const isLit = i < 3;
        r._quad([head[0] + dx + sway, head[1] + dy, head[2] + dz],
                [w, h], bx, bz,
                isLit ? C.hair_lit : C.hair, S.ROUNDED, 0.4);
      }

      // A few stray strands over forehead
      r._quad([head[0] - 40, head[1] - 50, head[2] + 80], [18, 45], bx, bz,
              withAlpha(C.hair, 0.85), S.ROUNDED, 0.5);
      r._quad([head[0] + 15, head[1] - 48, head[2] + 85], [15, 40], bx, bz,
              withAlpha(C.hair_shadow, 0.8), S.ROUNDED, 0.5);
    }

    _characterHeadphones(at, rot, r) {
      r.primitive = 'proof:character_headphones';
      const cans = this.runtime.worlds.get('headphones');
      if (!cans) return;

      const plane = r._billboard(rot('head'));
      const bx = plane.right, bz = plane.up;
      const p = cans.position;

      // Headband
      r._quad([p[0], p[1] + 12, p[2] + 8], [280, 50], bx, bz, C.cans, S.ROUNDED, 0.42);
      r._quad([p[0], p[1] + 8, p[2] + 18], [240, 22], bx, bz, C.cans_lit, S.ROUNDED, 0.42);
      // Gold accent on band
      r._quad([p[0], p[1] + 5, p[2] + 22], [220, 14], bx, bz,
              withAlpha(C.gold, 0.6), S.ROUNDED, 0.42);

      // Ear cups
      for (const dx of [-140, 140]) {
        const isLeft = dx < 0;
        // Cup outer
        r._quad([p[0] + dx, p[1] + 8, p[2] - 115], [110, 155], bx, bz,
                isLeft ? C.cans_lit : C.cans, S.ROUNDED, 0.32);
        // Cup shadow
        r._quad([p[0] + dx + (isLeft ? 15 : -15), p[1] + 12, p[2] - 115], [85, 130], bx, bz,
                C.cans_shadow, S.ROUNDED, 0.28);
        // Gold ring accent
        r._quad([p[0] + dx, p[1] - 4, p[2] - 115], [55, 95], bx, bz,
                withAlpha(C.gold, 0.5), S.OUTLINE, 0.25);
        // Ear cushion
        r._quad([p[0] + dx, p[1] - 8, p[2] - 115], [75, 110], bx, bz,
                C.cans, S.ELLIPSE);
      }
    }

    _segment(r, a, b, width, colour) {
      const d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
      const length = Math.hypot(d[0], d[1], d[2]);
      if (length < 1) return;
      const x = [d[0] / length, d[1] / length, d[2] / length];
      const up = Math.abs(x[2]) > 0.98 ? [1, 0, 0] : [0, 0, 1];
      const y = [
        x[1] * up[2] - x[2] * up[1],
        x[2] * up[0] - x[0] * up[2],
        x[0] * up[1] - x[1] * up[0],
      ];
      const yl = Math.hypot(y[0], y[1], y[2]) || 1;
      r._quad([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2],
              [length, width], x, [y[0] / yl, y[1] / yl, y[2] / yl],
              colour, S.ROUNDED, 0.28);
    }
  }

  // ========================================== PLATE SUBSTITUTION (unchanged)

  function drawPlate(runtime, layer, centre, size) {
    const art = runtime.art;
    if (!art) return false;
    const plate = art.plateFor(layer, runtime.cameraId);
    if (!plate) return false;

    const plane = runtime._billboard(0);
    runtime.primitive = `art:${layer}`;
    runtime._quad(centre, size, plane.right, plane.up,
                  [1, 1, 1, 1], S.TEXTURE, 0, { texture: plate.texture });
    return true;
  }

  function draw(runtime, now, lidClose, gazeEase, contrastMode) {
    if (!runtime.proof || runtime.proof.scene !== runtime.scene) {
      runtime.proof = new ProofArt(runtime);
    }
    const art = runtime.proof;

    /* Pass contrast mode to the art renderer so it can use debug colors if needed. */
    art.contrastMode = contrastMode || 'normal';

    const [rw, rd, rh] = runtime.scene.room;
    const desk = runtime.scene.boxes.find((b) => b.id === 'DESK');
    const head = runtime.worlds && runtime.worlds.get('head');
    const torso = runtime.worlds && runtime.worlds.get('torso');

    // Layer composition - each can be replaced by imported plate
    if (!drawPlate(runtime, 'city', [rw / 2, rd + 2400, rh * 0.6], [9600, 3600])) {
      art.city(now);
    }
    if (!drawPlate(runtime, 'rear_office', [rw / 2, rd - 40, rh / 2], [rw, rh])) {
      art.rearOffice(now);
    }
    drawPlate(runtime, 'monitors', [3200, 2360, 1130], [3400, 900]);
    art.monitors(now);

    if (torso && head) {
      const plated = drawPlate(runtime, 'character',
                               [torso[0], torso[1], torso[2] + 120], [1400, 2200]);
      if (!plated) art.character(now, lidClose, gazeEase);
    } else {
      art.character(now, lidClose, gazeEase);
    }

    if (desk) {
      const centre = [(desk.min[0] + desk.max[0]) / 2,
                      desk.min[1] - 40,
                      (desk.min[2] + desk.max[2]) / 2];
      if (!drawPlate(runtime, 'desk', centre, [desk.max[0] - desk.min[0], 1400])) {
        art.desk();
      }
    } else {
      art.desk();
    }

    art.props(now);
    art.atmosphere(now);

    /* Log first frame diagnostics in debug mode */
    if (contrastMode === 'debug' && runtime.frames === 0) {
      console.log('[proof] Drawing with contrast=debug, using brightened palette');
      console.log('[proof] DEBUG_COLORS available for manual override');
    }
  }

  const api = { draw, ProofArt, PROOF_PALETTE: C, DEBUG_PALETTE: DEBUG_COLORS, SHAPE: S };
  if (typeof window !== 'undefined') window.TF_PROOF = api;
  if (typeof module !== 'undefined' && typeof module.exports === 'object') {
    module.exports = api;
  }
})();
