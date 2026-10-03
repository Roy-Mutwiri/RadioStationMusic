# Initial visual audit

> Recorded **before** any installation, exactly as `docs/status/PHASE_7_ENVIRONMENT.md` was.
> Nothing was installed, enabled or upgraded to produce this document. Every figure below is
> an observation of the machine as it stands on the date given, with the station's ACE-Step
> provider already resident in VRAM.

Audited: 2026-10-03, 13:09 UTC+3.

---

## 0. The one-paragraph version

The machine has **no 3D or 2D animation software of any kind** — no Blender, Unreal, Unity,
Godot, Live2D or Spine — and **2 487 MiB of free VRAM** because ACE-Step is holding roughly
4.9 GB and the desktop another 4.8 GB. It does have OBS Studio 32.1.2 with the stock
`obs-browser` (CEF) and `obs-websocket` plugins, a working Node 24 / Vite / React / TypeScript
frontend toolchain, and Photoshop. There is **no existing avatar, character, rig or animation
asset in the repository**, and **no image generator installed anywhere on either disk**.

Those three facts — negligible spare VRAM, no 3D toolchain, a browser renderer already
installed and already wired to a live state WebSocket — determine the runtime decision
recorded in `ADR_VISUAL_RUNTIME.md` far more than any preference about engines does.

---

## 1. Host

| | |
|---|---|
| OS | Microsoft Windows 11 Pro |
| Version | 10.0.26200 (build 26200), 64-bit |
| Motherboard | Gigabyte B550 UD AC-Y1 |
| CPU | AMD Ryzen 7 PRO 5750G, 8 cores / 16 threads, 3 801 MHz max |
| RAM | 31.85 GB total, **6.1 GB free at audit** |
| Disk D: (project) | 476 GB total, **159 GB free** |
| Disk C: (system) | 953 GB total, 141 GB free |

Two host-level notes that bear on the visual layer specifically.

**The CPU has an integrated GPU that Windows cannot see.** The 5750G is a Cezanne APU with
Radeon graphics on die, but `Get-PnpDevice -Class Display` returns only the RTX 3060 and a
Parsec virtual adapter — the iGPU is not present even as a disabled device, which means it is
switched off in firmware, the normal B550 default once a discrete card is fitted. This is
worth recording because it is the single cheapest possible win available to this project: an
enabled iGPU could drive the animation renderer and the desktop compositor while the 3060 did
nothing but generate music. It is listed as an opportunity, not a plan, because it requires a
BIOS change and a reboot of a station that is supposed to run continuously, and because the
Cezanne iGPU's eight CUs are weak enough that the renderer would have to be sized for them
rather than merely moved onto them. See §7.

**This machine is driven over Parsec.** `Parsec Virtual Display Adapter` is an active display
device and `parsecd.exe` is a resident GPU client. Parsec encodes the desktop with NVENC, and
OBS will also want NVENC for the stream. The 3060 (GA106) has one encoder engine. Encoder
utilisation was 0 % at audit, so there is no contention right now, but a 24/7 stream plus a
remote session plus Parsec's own encode is three claims on one engine and it should be
measured in Phase V10 rather than assumed free.

## 2. GPU — the binding constraint

| | |
|---|---|
| Model | NVIDIA GeForce RTX 3060 (GA106, compute 8.6) |
| Driver | 610.47, CUDA UMD 13.3 |
| VRAM total | 12 288 MiB |
| **VRAM used at audit** | **9 629 MiB** |
| **VRAM free at audit** | **2 487 MiB** |
| GPU utilisation | 40 % |
| Memory-bus utilisation | 6 % |
| Encoder / decoder utilisation | 0 % / 2 % |
| Core clock | 1 950 MHz of 2 130 MHz |
| Temperature | 54 °C |
| Power | 48.5 W of 170 W |

`Win32_VideoController.AdapterRAM` reports 4 293 918 720 bytes. That is the well-known 32-bit
WMI overflow and not a 4 GB card; `nvidia-smi`'s 12 288 MiB is authoritative.

### Where the 12 GB has gone

| Consumer | VRAM | How it was established |
|---|---|---|
| Desktop composition (~30 `C+G` clients) | ≈ 4 772 MiB | Phase 7 audit baseline, same desktop, no compute process resident |
| ACE-Step provider (`uv` Python 3.12, PID 30568, type `C+G`) | ≈ 4 857 MiB | Difference between the Phase 7 baseline and the figure above |
| **Free** | **2 487 MiB** | Measured |

The `C+G` clients are the ordinary cost of a working desktop: two Chrome processes, two Brave
processes, Edge, Cursor, Spotify, TradingView, Telegram, Voicemod, LiveForge Studio, the
TradeFixLive desktop app, Parsec, seven PowerToys processes and the NVIDIA overlay. None of
that is waste the project can reclaim — it is the user's actual working environment, and
Phase 7 already made the decision to size against the free figure rather than the nameplate.
The visual layer inherits that decision and is sized against **2.4 GB, not 12 GB**.

Per-process VRAM attribution is unavailable: `nvidia-smi --query-compute-apps` returns `[N/A]`
for every process and `[Insufficient Permissions]` for four of them, because WDDM hands memory
management to the OS and the query is not elevated. The table above is therefore a
difference-of-totals estimate against a known-good earlier baseline, which is honest but
coarser than a direct reading. Phase V11 should re-measure with the station idle, the station
generating, and the renderer attached, so the renderer's own cost is isolated by subtraction
rather than inferred.

### What 2.4 GB rules out

A lit Unreal Engine 5 scene at 1080p wants 4–6 GB of VRAM for the runtime alone, before the
editor, and UE5's own documentation treats 8 GB as the floor for development. Unity's URP in a
built player is lighter but still measured in gigabytes for an environment of the described
richness. Neither fits in 2.4 GB alongside a music model that must not be starved, and neither
degrades gracefully when it does not fit — they OOM, or they begin evicting textures and
stuttering, and a stuttering 24/7 stream is worse than a simpler one that holds frame time.

This is not a statement that Unreal is unsuitable for this *kind* of product. It is a
statement that it is unsuitable on *this* GPU while ACE-Step is resident, which is the only
configuration that matters.

## 3. 3D / 2D animation software

Checked by directory probe of the conventional install roots and by a registry sweep of all
three `Uninstall` hives.

| Package | Present | Notes |
|---|---|---|
| Unreal Engine | **No** | No `Epic Games` directory on C: or D:, no registry entry, no Epic launcher |
| Unity / Unity Hub | **No** | Neither the Hub nor any editor |
| Godot | **No** | No install, no portable binary found |
| Blender | **No** | No install |
| Live2D Cubism | **No** | No install |
| Spine | **No** | No install |
| Houdini / Maya / Cinema 4D | **No** | None |
| Krita / GIMP / Affinity | **No** | None |
| **Adobe Photoshop 2026** | **Yes** | The only raster paint tool on the machine |

There is no VTuber-adjacent tooling either — no VSeeFace, no VTube Studio, no Warudo.

**No Live2D, Spine, Godot, Unity or Unreal asset exists in the repository or on either disk.**

## 4. Streaming and transport

| | |
|---|---|
| OBS Studio | **32.1.2**, `C:\Program Files\obs-studio` |
| Streamlabs Desktop | 1.20.8 (installed; not the intended output path) |
| OBS plugin set | **entirely stock** — no third-party plugins in either the install tree or `%APPDATA%\obs-studio\plugins` |

Stock plugins that matter here:

| Plugin | Relevance |
|---|---|
| `obs-browser.dll` | A full CEF browser source. This is a renderer that is **already installed**, already composited by OBS, and already able to open a WebSocket. It is the most consequential finding in this section. |
| `obs-websocket.dll` | Present **and already configured** — `%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json` exists, and the project already carries `ObsSettings` (`config/schema.py:956`) targeting port 4455 with a named source map. |
| `obs-nvenc.dll`, `nv-filters.dll` | Hardware encode and NVIDIA effects available. |

| Transport | Present | Consequence |
|---|---|---|
| **Spout** | **No** | No `obs-spout2-plugin`. Spout would need both a plugin install and a renderer that publishes a Spout sender. |
| **NDI** | **No** | No `C:\Program Files\NDI`, no `obs-ndi`/`distroav` plugin. NDI would additionally add a network hop and an encode/decode round trip for a purely local handoff. |
| Window / game capture | Yes | `win-capture.dll` is stock, so a windowed renderer *can* be captured — at the cost of a visible window, a capture composite step, and a dependency on that window never being minimised or occluded. |
| **Browser source** | **Yes** | Zero install. No capture step. No window. Alpha supported. |

The absence of both Spout and NDI is a genuine input to the decision rather than a detail: a
runtime that needs either one costs an unaudited third-party plugin install into the
broadcast-critical process, whereas a browser renderer costs nothing because the transport is
already there and already stock.

## 5. Project toolchain

| | |
|---|---|
| Node | v24.13.0, npm 11.6.2 |
| Frontend stack | Vite + React + TypeScript + Tailwind, in `frontend/` |
| Project Python | 3.10.11 (`D:\.Music\.venv`), pinned `>=3.10,<3.11` by ADR-01 |
| ACE-Step Python | 3.12.15 via `uv`, out of process in `D:\ace-step\.venv` (ADR-02) |
| Other interpreters | 3.13.9 (Anaconda3), 3.13.11 (Miniconda3), 3.14.2 |
| CUDA | 12.8 toolkit with Visual Studio integration; Nsight VSE 2025.1 |
| Build tools | Visual Studio Build Tools 2022 **and** 2026 |
| Editors | VS Code, Cursor |

Three things here are directly reusable by the visual layer and should not be rebuilt:

- **A live state WebSocket already exists.** `api/live.py` runs a `LiveHub` that pushes a
  whole coherent `state` frame every 2.0 s and a small `position` frame every 0.5 s, with
  bounded per-client queues that drop the oldest message rather than buffering without limit.
  Its docstring already states the principle the visual layer needs — "one component's failure
  must not stop the radio" applied to a socket. A renderer is just another client.
- **An overlay route already exists.** `frontend/src/pages/Overlay.tsx` (417 lines) is served
  at `/overlay/live` with SPA history fallback (`api/app.py:151`), which means OBS can already
  point a browser source at a project-served URL today.
- **The brand palette is already codified.** `frontend/tailwind.config.js` is not a theme file,
  it is a written art direction: an `ink` scale from `#07080a` to `#e6e8ec`, a four-stop `gold`
  accent from `#8a6f1c` to `#f0e0a6`, deliberately desaturated status colours, and an explicit
  rule — *"Gold is an accent, not a theme. It marks exactly one thing per screen."* The office
  and character must be painted to this palette, not to a new one. It is reproduced as the
  authoritative source in `OFFICE_BIBLE.md` §2.

## 6. Existing visual work

**None.** This is a greenfield visual layer.

| Checked | Result |
|---|---|
| Any `visual/`, `avatar/`, `character/`, `animation/`, `scene/`, `render/` directory | Absent |
| `artwork/` | Exists, **empty** |
| `artifacts/` | Contains only `phase7/` |
| `docs/` | 19 files, none visual |
| Character, rig, mesh, sprite or texture asset of any format | None anywhere in the repo |

There *is* an artwork **slot** in the architecture, unfilled: `storage/paths.py:94` defines
`artwork(track_id, when) -> artwork/<date>/<track_id>.png`, `StorageKind.ARTWORK` exists,
`contracts/queue.py:166` carries a nullable `artwork_path`, `persistence/models.py:198`
accepts `'artwork'` as a file kind, and `ObsSettings.sources` maps `"artwork"` to a
`TF_TRACK_ART` OBS source. Nothing writes to it. The station was built expecting visuals and
has never had any.

### The asset gap, stated plainly

There is **no image generator on this machine** — no ComfyUI, no Stable Diffusion
installation, no local diffusion checkpoint, and nothing in the repository that produces a
raster image. `pillow` is an optional dependency used for audio analysis plotting, not for
illustration.

And I cannot generate images myself: I have no image-generation tool in this session.

This matters for Phase V1's last line — *"produce the first canonical static references"* —
and it is better said now than discovered later. The painted plates for TF_TRADER_01 and
TRADE_FIX_OFFICE_01 cannot be produced by me. What **can** be produced, and is produced in
this pass, is everything that makes those plates reproducible and consistent once someone or
something does paint them:

| Deliverable | Status |
|---|---|
| Numeric, to-scale room geometry — the spatial source of truth all seven cameras share | Authored: `visual/environment/TRADE_FIX_OFFICE_01.blockout.json` |
| Floor plan and camera layout drawings | Authored: `visual/references/office/*.svg` |
| Character construction and proportion sheet, with rig joints and interaction anchors | Authored: `visual/references/character/*.svg` |
| Frozen identity lock — the canonical description, the invariants, the acceptance checklist | Authored: `CHARACTER_BIBLE.md` §3–§5 |
| Reusable generation prompt block and negative constraints per reference view | Authored: `CHARACTER_BIBLE.md` §9 |
| **The painted plates themselves** | **Blocked — needs a paint capability this machine does not have** |

Three ways to close it, for the user to choose between in Phase V1's review gate:

1. **Photoshop 2026's generative tools**, driven by hand against the §9 prompt blocks. Already
   installed. Weakest identity consistency across nine views, best colour control, and the
   plates would be finished in the same tool that composites them.
2. **A hosted image model**, driven by the §9 prompt blocks plus an image reference of the
   first accepted portrait. Best consistency; costs an external dependency and per-image spend.
3. **A local diffusion install with an identity adapter** (a LoRA or IP-Adapter trained on the
   first accepted portrait). Best consistency and reproducibility, zero marginal cost — but it
   is another multi-gigabyte VRAM tenant on a card with 2.4 GB free, so it can only run when
   the station is stopped. Workable, because reference painting is offline work by design.

Option 3 is the recommendation for a 24/7 identity that must stay recognisable for years, run
offline between station sessions. Nothing in the architecture below depends on which is chosen.

## 7. Opportunities and risks carried forward

| # | Item | Severity | Carried to |
|---|---|---|---|
| R1 | 2 487 MiB free VRAM. The renderer's entire budget. | **Critical** | `ADR_VISUAL_RUNTIME.md` §4, plan V2/V11 |
| R2 | No paint capability for the canonical plates. | **Blocking for V1 sign-off** | §6 above, plan V1 gate |
| R3 | 6.1 GB free RAM. A CEF renderer wants 400–700 MB. | Moderate | Plan V11 |
| R4 | One NVENC engine shared by Parsec, OBS and any recording. | Moderate | Plan V10 |
| R5 | 40 % GPU utilisation at rest with ACE-Step merely resident. | Moderate | Plan V11 |
| R6 | No Spout, no NDI. Third-party plugin install needed for either. | Low, if browser transport is chosen | `ADR_VISUAL_RUNTIME.md` §5 |
| O1 | Cezanne iGPU exists but is firmware-disabled. Enabling it could move the renderer *and* the desktop compositor off the 3060 entirely. | Opportunity | `ADR_VISUAL_RUNTIME.md` §8 |
| O2 | `LiveHub`, the `/overlay/live` route and `ObsSettings` already exist and already do most of the transport work. | Opportunity | `STATE_BRIDGE.md` |

---

## 8. Conclusion

The audit does not leave the technology choice open. A real-time 3D engine cannot be fitted
into 2 487 MiB beside a resident music model, none of the candidate engines is installed, and
the two transports a 3D engine would normally use to reach OBS are both absent. Meanwhile a
GPU-cheap renderer is already installed inside the broadcast process, already speaks the
protocol the station already publishes, and is written in the language the project's frontend
is already built in.

The recommendation is therefore a **hybrid 2.5D pipeline**: a numeric 3D blockout used
offline to lock room geometry and camera placement, high-resolution painted layer stacks
derived from it, and a WebGL2 runtime inside an OBS browser source that animates those layers
under the direction of a behaviour engine living in Python beside the station. Quality comes
from the art, which this GPU does not have to pay for, rather than from the renderer, which it
cannot afford.

The reasoning, the rejected alternatives, and the conditions that would reverse the decision
are recorded in `ADR_VISUAL_RUNTIME.md`.
