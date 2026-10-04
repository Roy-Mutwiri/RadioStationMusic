/*
 * Imported art: loading, layer lookup, and camera applicability.
 *
 * The runtime's job here is to do nothing until real plates exist, and then to use them
 * without anything upstream changing. `BehaviorDirector`, `CameraDirector`,
 * `VisualStateV1` and the market/music bridge are untouched by this file: it turns a
 * validated PNG into a GL texture and answers "is there a plate for this layer, on this
 * camera?". Everything else about the system carries on as it already does.
 *
 * Three rules it will not bend:
 *
 * **Only validated plates load.** `/api/visual/assets` emits a URL for a slot only when
 * that slot passed `art.py`'s checks, so a plate with the wrong dimensions or no alpha is
 * never fetched. The browser does not get a second opinion.
 *
 * **A plate is used only on the cameras it was painted for.** A flat hero plate stretched
 * across the hands shot is a fake view. When imported art is live and the selected camera
 * has no plate, the page says ART VIEW NOT YET AVAILABLE for that camera.
 *
 * **Missing art is stated, never filled in.** With nothing imported the page shows a
 * full-screen REAL ART NOT IMPORTED notice with the counts. A dark frame must never again
 * be mistakeable for a working visual.
 */
'use strict';

(function () {
  /* Layers the renderer can replace with a plate, back to front. The order matters: it
   * is the compositing order, and it matches the seven-layer split the proof renderer
   * already draws in. */
  const LAYER_ORDER = [
    'city', 'rear_office', 'monitors', 'character',
    'torso', 'arm_l', 'arm_r', 'head', 'hair', 'eyes', 'eyelids', 'headphones',
    'hand_l', 'hand_r',
    'desk', 'prop_mug', 'prop_notebook', 'prop_pen',
  ];

  /* The nine the brief names as enough to see the engine working. Reported separately so
   * the HUD can say MOTION 0/9 rather than burying it in a total. */
  const MOTION_LAYERS = [
    'head', 'torso', 'arm_l', 'arm_r', 'hand_l', 'hand_r',
    'eyes', 'eyelids', 'headphones',
  ];

  class ArtLibrary {
    constructor(gl) {
      this.gl = gl;
      /** layer -> {texture, width, height, cameras, pivot, filename} */
      this.layers = new Map();
      this.status = null;
      this._loading = new Set();
      this._failed = new Map();
      this.generation = 0;
    }

    /* Take a status report and load any newly-valid plate.
     *
     * Called on the same slow poll the HUD uses, so dropping a file into the source
     * directory brings it in without a restart — and a reload is always enough. Nothing
     * here blocks a frame: a texture appears in `layers` when it has decoded, and until
     * then the layer draws procedurally. */
    sync(status) {
      this.status = status;
      if (!status || !Array.isArray(status.slots)) return;

      for (const slot of status.slots) {
        if (!slot.valid || !slot.url) {
          /* A plate that was valid and is now not — overwritten with a bad file — must
           * stop being drawn rather than lingering as a stale texture. */
          if (this.layers.has(slot.layer)
              && this.layers.get(slot.layer).filename === slot.filename) {
            this._release(slot.layer);
          }
          continue;
        }
        const existing = this.layers.get(slot.layer);
        if (existing && existing.filename === slot.filename) {
          existing.cameras = slot.cameras;     // a sidecar edit takes effect at once
          continue;
        }
        if (this._loading.has(slot.url)) continue;
        if (this._failed.has(slot.url)) continue;
        this._load(slot);
      }
    }

    _load(slot) {
      this._loading.add(slot.url);
      const image = new Image();
      image.onload = () => {
        this._loading.delete(slot.url);
        try {
          this.layers.set(slot.layer, {
            texture: this._upload(image),
            width: image.naturalWidth,
            height: image.naturalHeight,
            cameras: slot.cameras,
            pivot: slot.pivot || [0.5, 0.5],
            filename: slot.filename,
            layer: slot.layer,
          });
          this.generation++;
          console.info(`[visual] loaded ${slot.filename} as layer ${slot.layer}`);
        } catch (error) {
          this._failed.set(slot.url, error.message);
          console.error(`[visual] ${slot.filename} failed to upload`, error);
        }
      };
      image.onerror = () => {
        this._loading.delete(slot.url);
        /* Recorded rather than retried forever: a 404 loop on every poll would bury the
         * console it is trying to report into. Fixed by a reload once the file is right. */
        this._failed.set(slot.url, 'failed to fetch or decode');
        console.error(`[visual] could not load ${slot.url}`);
      };
      image.src = slot.url;
    }

    _upload(image) {
      const gl = this.gl;
      const texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      /* Clamped and linear, no mips: these are large plates drawn at roughly 1:1, and a
       * mip chain costs a third more VRAM for a blur nobody asked for. ACE-Step has
       * priority on this GPU. */
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      return texture;
    }

    _release(layer) {
      const entry = this.layers.get(layer);
      if (!entry) return;
      try {
        this.gl.deleteTexture(entry.texture);
      } catch (error) { /* a lost context has already freed it */ }
      this.layers.delete(layer);
      this.generation++;
    }

    /** The plate for `layer` on `cameraId`, or null — which means "draw procedurally". */
    plateFor(layer, cameraId) {
      const entry = this.layers.get(layer);
      if (!entry) return null;
      if (cameraId && entry.cameras && entry.cameras.length
          && !entry.cameras.includes(cameraId)) {
        return null;
      }
      return entry;
    }

    /** Whether any plate at all has loaded. */
    get any() {
      return this.layers.size > 0;
    }

    /* Whether this camera has no plate while others do.
     *
     * The honest case the brief asks for: a flat plate painted for the hero shot cannot
     * serve the hands shot, and stretching it there would be faking a view. Only true
     * once art is actually imported — with nothing imported the whole scene is
     * procedural and every camera is equally valid. */
    cameraUnavailable(cameraId) {
      if (!this.any) return false;
      for (const entry of this.layers.values()) {
        if (!entry.cameras || !entry.cameras.length) return false;
        if (entry.cameras.includes(cameraId)) return false;
      }
      return true;
    }

    /** Counts for the HUD and the missing-art notice. */
    counts() {
      const status = this.status;
      if (!status) {
        return {
          character: { valid: 0, required: 9 },
          environment: { valid: 0, required: 4 },
          motion: { valid: 0, required: MOTION_LAYERS.length },
          anyImported: false,
          sourceRoot: null,
          guide: null,
          problems: [],
          unexpected: [],
        };
      }
      return {
        character: {
          valid: status.character.required_valid,
          required: status.character.required,
        },
        environment: {
          valid: status.environment.required_valid,
          required: status.environment.required,
        },
        motion: {
          valid: status.motion_layers.valid,
          required: status.motion_layers.required,
        },
        anyImported: Boolean(status.any_art_valid),
        sourceRoot: status.source_root,
        guide: status.import_guide,
        problems: status.problems || [],
        unexpected: status.unexpected_files || [],
      };
    }
  }

  const api = { ArtLibrary, LAYER_ORDER, MOTION_LAYERS };
  if (typeof window !== 'undefined') window.TF_ART = api;
  if (typeof module !== 'undefined' && typeof module.exports === 'object') {
    module.exports = api;
  }
})();
