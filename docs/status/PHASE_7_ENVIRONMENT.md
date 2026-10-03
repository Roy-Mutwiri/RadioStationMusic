# Phase 7 — Environment audit

> §7.1. Recorded **before** any installation. Nothing was installed or upgraded to produce
> this document; every figure is an observation of the machine as it stands.

Audited: 2026-10-03.

---

## 1. Host

| | |
|---|---|
| OS | Microsoft Windows 11 Pro |
| Version | 10.0.26200 (build 26200), 64-bit |
| CPU | AMD Ryzen 7 PRO 5750G, 8 cores / 16 threads |
| RAM | 31.8 GB total, **4.2 GB free at audit time** |
| Disk D: (project) | 477 GB total, **181 GB free** |
| Disk C: (system) | 953 GB total, 176 GB free |

**Free RAM is the first thing worth flagging.** 4.2 GB of 31.8 GB was available with the
user's normal desktop running — browsers, Cursor, Spotify, TradingView, Telegram, LiveForge
Studio, Parsec, Voicemod. ACE-Step's CPU-offload modes trade VRAM for system RAM, so the
low-VRAM configurations are the ones most affected by this, not helped by it.

## 2. GPU

| | |
|---|---|
| Model | NVIDIA GeForce RTX 3060 |
| Driver | 610.47 |
| Compute capability | 8.6 (Ampere) |
| VRAM total | 12 288 MiB (12 GB) |
| VRAM used at audit | **4 772 MiB** |
| VRAM free at audit | **7 344 MiB** |
| Utilisation at audit | 43 % |
| Temperature | 54 °C |
| CUDA toolkit | 12.8 (V12.8.61) — `nvcc` on PATH |

`nvidia-smi --query-compute-apps` shows **no compute process** holding memory. All 4.77 GB is
desktop composition: ~25 `C+G` clients including two browsers, Cursor, Spotify, TradingView,
Telegram and the NVIDIA overlay.

**This is the binding constraint of Phase 7, and it is an operational one rather than a
hardware one.** The card is 12 GB, but a 24/7 station sharing this machine with a working
desktop has roughly **7.3 GB** to generate in. Any configuration sized against "12 GB" will
OOM the first time a browser tab opens a video. Sizing is done against the free figure.

> Note on §7.25's example text: it shows `VRAM x / 16 GB`. This card is 12 GB. The Generation
> page will render the measured total, not the figure from the example.

## 3. Python and the project environment

| | |
|---|---|
| Project venv | `D:\.Music\.venv` — **Python 3.10.11** |
| System interpreters | 3.14 (`C:\Python314`), 3.10 (`...\Programs\Python\Python310`) |
| **Python 3.11 / 3.12** | **not present** |
| `uv` | **not installed** |
| git | 2.52.0.windows.1 |
| FFmpeg | 8.0.1-full (loudnorm, ebur128, alimiter — verified in Phase 6) |

Machine-learning packages in the project venv:

| package | state |
|---|---|
| torch, torchaudio, torchvision | **not installed** |
| transformers, diffusers, accelerate | **not installed** |
| peft, safetensors, huggingface_hub | **not installed** |

The project venv has never contained a deep-learning stack. Phase 6's audio work is librosa,
numpy, scipy, soundfile and pyloudnorm.

## 4. ACE-Step 1.5 — what it actually requires

Verified against the repository, not from memory.

| | |
|---|---|
| Repository | `github.com/ace-step/ACE-Step-1.5` |
| Package version | `ace-step` **1.5.0** |
| Head commit at audit | **`ca1e85fe9430`**, 2026-08-29 |
| Last push | 2026-10-01 |
| License | **MIT** |
| Stars | ~13 000 |

### Declared constraints (`pyproject.toml`, verbatim)

```toml
requires-python = ">=3.11,<3.13"

"torch==2.7.1+cu128;       sys_platform == 'win32'"
"torchvision==0.22.1+cu128; sys_platform == 'win32'"
"torchaudio==2.7.1+cu128;   sys_platform == 'win32'"
"transformers>=4.51.0,<4.58.0"
"diffusers>=0.37.0"
"gradio==6.2.0"
"numba>=0.63.1"
"accelerate>=1.12.0"
"peft>=0.18.0"
"lightning>=2.0.0"
"tensorboard>=2.20.0"
"nano-vllm"
"torchao>=0.16.0,<0.17.0"
"torchcodec>=0.9.1"
…
```

### Interfaces offered

| interface | entry point |
|---|---|
| Gradio web UI | `uv run acestep` |
| **REST API** | `uv run acestep-api` (default `127.0.0.1:8001`) |
| Python API | `acestep.inference.generate_music` |
| CLI wizard | interactive |
| VST3 plugin | DAW |

### Generation parameters (Python `GenerationParams`)

`caption`, `lyrics` (`"[Instrumental]"` for instrumental), `instrumental: bool`,
`seed: int` (`-1` = random), `bpm` (30–300), `keyscale`, `timesignature`,
`duration` (10–600 s, `≤0` = auto), `vocal_language`, `inference_steps`
(turbo 1–20, base 1–200), `guidance_scale` (1.0–15.0, base only), `shift` (1.0–5.0),
`infer_method` (`ode` | `sde`), `thinking`, `lm_temperature`, `task_type`.

`GenerationConfig`: `batch_size` (1–8), `use_random_seed`, `seeds`, `audio_format`
(`flac` | `mp3` | `wav` | `opus`).

Every control §7.9–§7.13 asks for is present: **seed, lyrics, instrumental, duration, bpm,
key, and inference-step/guidance knobs for the §7.8 profiles.**

### REST protocol

| endpoint | method | purpose |
|---|---|---|
| `/release_task` | POST | submit, returns `task_id` |
| `/query_result` | POST | poll; `status` 1 = success, 2 = failure |
| `/v1/audio` | GET | download the rendered file |
| `/health` | GET | liveness |
| `/v1/stats` | GET | runtime statistics |
| `/v1/models` | GET | list DiT models |
| `/v1/init` | POST | load / switch model |

**There is no cancellation endpoint.** That is a real gap against §7.4's `cancel` requirement
and is handled explicitly rather than papered over — see §6 below.

### Model zoo and VRAM tiers

| VRAM | recommended DiT | recommended LM | backend |
|---|---|---|---|
| ≤ 6 GB | 2B turbo | none (DiT only) | — |
| 6–8 GB | 2B turbo | `acestep-5Hz-lm-0.6B` | `pt` |
| **8–16 GB** | **2B turbo/sft** | **0.6B for 8–12 GB**, 1.7B for 12–16 GB | `vllm` |
| 16–20 GB | 2B sft / XL turbo | 1.7B | `vllm` |
| ≥ 24 GB | XL sft | 4B | `vllm` |

2B models: ~4.7 GB VRAM. XL (4B): ~9 GB bf16 — **out of reach on this card**, and the table
agrees (XL needs CPU offload below 20 GB).

Quoted speed: "under 2 s per full song on A100, under 10 s on RTX 3090". The 3060 is roughly
half a 3090, so **15–25 s per song is the figure to expect** — to be measured in §7.21, not
assumed.

## 5. Compatibility determination

> §7.1: *"Do not blindly install or upgrade packages. Determine compatibility first."*

### The decisive conflict

```
ACE-Step 1.5   requires-python = ">=3.11,<3.13"
Trade Fix Radio  pinned to Python 3.10.11 by ADR-01
```

ADR-01 pinned 3.10 because librosa's numba wheels were available there, and Phase 6's entire
analysis stack rests on that decision. **The two cannot share an interpreter.** This is not a
preference or a dependency-resolution difficulty; it is a hard version-range exclusion in both
directions.

Secondary conflicts, each sufficient on its own:

| package | Trade Fix Radio | ACE-Step | conflict |
|---|---|---|---|
| numba | pinned by librosa 0.11 for py3.10 | `>=0.63.1` | likely incompatible resolutions |
| scipy | installed for Phase 6 | `>=1.10.1` | resolvable, but would be re-resolved |
| soundfile | 0.14 | `>=0.13.1` | compatible |
| torch + CUDA | absent | `2.7.1+cu128` (~2.5 GB) | would be forced into the radio venv |
| gradio | absent | `==6.2.0` exactly | a web UI inside the station's venv |
| lightning, tensorboard, peft | absent | training stack | none of it needed to *infer* |

Installing ACE-Step into the radio environment would mean a Python upgrade that breaks Phase 6,
plus a training stack and a second web framework inside the broadcast process.

### §7.3 evaluation

| option | verdict |
|---|---|
| **A. Same Python environment** | **Impossible.** 3.10 vs ≥3.11 is mutually exclusive. Not a judgement call. |
| **B. Dedicated Python environment, imported in-process** | **Impossible for the same reason.** A 3.12 package cannot be imported into a 3.10 process; "dedicated environment" only helps if something crosses the boundary, and here nothing can. |
| **C. Separate local inference process** | **Chosen.** ACE-Step runs under its own 3.12 interpreter, in its own directory, and the station talks to it over the documented REST API. |

Option C is not a workaround for the version clash — it is also what §7.5 asks for
independently (*"Real generation must run outside the playout-critical event loop… If ACE-Step
deadlocks or crashes, PlayoutEngine must not be affected"*). A crash in a separate OS process
cannot take the broadcast with it, and a model upgrade changes nothing inside the radio.

### What installation will require

Nothing below has been done yet.

1. `uv` (not installed) — ACE-Step's documented installer and Python-toolchain manager.
2. **Python 3.12**, provisioned by `uv` into ACE-Step's own directory. The system 3.10 and
   3.14 are untouched; the project venv is untouched.
3. `git clone` into `D:\ace-step` — **outside the Trade Fix Radio repository**, so the
   station's working tree never contains a 2.5 GB torch install or a model checkpoint.
4. `uv sync` — downloads torch 2.7.1+cu128 and the dependency set. Expect several GB.
5. Model checkpoints, **auto-downloaded on first run**. Per §7.28 this must not happen
   silently: `tradefix doctor` reports what is missing, and an explicit command performs the
   download.

Disk: D: has 181 GB free. Comfortable for the 2B tier.

## 6. Known gaps to carry into the implementation

Each of these is a fact about the model or the host that the integration has to accommodate
rather than wish away.

1. **No cancellation endpoint.** §7.4 requires `cancel`. The provider can stop *waiting* for a
   task and free the job slot, but the remote generation continues to completion on the GPU.
   The provider must report cancellation honestly — "stopped waiting", not "cancelled" — and
   must not reuse the GPU as though it were immediately free.
2. **~7.3 GB of usable VRAM, not 12 GB.** Profiles are sized against the free figure, and the
   OOM path (§7.7) is a certainty on this machine, not a contingency.
3. **`vllm` backend is the documented recommendation for the 8–16 GB tier.** The repository
   depends on `nano-vllm` rather than full vLLM, which is the lighter reimplementation; the
   6–8 GB tier uses the `pt` backend instead. Which of the two actually runs on Windows is to
   be **measured during installation**, with `pt` as the stated fallback.
4. **Low free system RAM (4.2 GB).** CPU-offload configurations move weights into system RAM;
   on this desktop that headroom is thin. A measurement, not a guess, will decide whether
   offload is usable.
5. **Speed is unmeasured.** The 3090 figure is published; the 3060 figure is not. §7.21
   measures it, and the §7.8 profiles are defined against what is measured here.
6. **`timesignature` and `vocal_language` exist** and are currently unused by the Phase 3
   blueprint. They are left unset rather than filled with a guess.

## 7. Summary

The environment is **suitable**, with one architectural consequence and one operational limit:

- **Consequence:** ACE-Step must run as a separate process under its own Python 3.12. This is
  forced by the interpreter constraint and is independently what §7.3 and §7.5 prefer.
- **Limit:** 7.3 GB of practically-available VRAM puts the 2B turbo DiT with the 0.6 B LM at
  the top of what fits. The XL models are out of reach on this card and will not be attempted.

Everything §7.4–§7.13 requires — seed, lyrics, instrumental, duration, BPM, key, step and
guidance control — is exposed by both the Python and REST interfaces, so nothing in the
planned provider depends on a capability the model does not have.
