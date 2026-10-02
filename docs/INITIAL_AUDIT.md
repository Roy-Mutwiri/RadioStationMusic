# TRADE FIX RADIO — Initial Repository & Environment Audit

**Date:** 2026-10-02
**Auditor:** Lead architect (automated engineering session)
**Audit target:** `D:\.Music`
**Status:** Phase 0 complete

---

## 1. Executive summary

`D:\.Music` is **completely empty** and is **not a git repository**. There is no existing
code, dependency manifest, test suite, virtual environment, or configuration to inspect,
reuse, or preserve.

**Consequence:** this is a *greenfield* build. The "do not destroy existing working
systems" and "create backups before major destructive refactors" directives are
satisfied trivially — there is nothing to destroy. No backup step is required for
Phase 1.

The surrounding **machine**, however, is unusually well provisioned for this project and
materially changes several architecture decisions (see §5). Four pre-existing local
assets are decisive:

| Asset | Why it matters |
| --- | --- |
| **MetaTrader 5 installed** | A real, credential-light XAUUSD tick feed is available locally. No paid market-data API is required for production mode. |
| **OBS Studio 32.1.2 + obs-websocket config present** | obs-websocket **5.x** is bundled (OBS ≥ 28). The §51 integration target is already satisfiable. |
| **FFmpeg 8.0.1 full build on PATH** | The §25 mastering pipeline has its primary tool with no install step. |
| **VB-Audio Virtual Cable installed** | Gives a clean app→OBS audio route without a loopback hack. This is the intended production audio path. |

---

## 2. Repository state

```
D:\.Music\
    (empty — 0 top-level entries, 0 files recursively)
```

| Check | Result |
| --- | --- |
| Directory exists | Yes |
| Top-level entries (incl. hidden) | **0** |
| Recursive file count | **0** |
| Git repository | **No** (`.git` absent) |
| Existing `pyproject.toml` / `package.json` / lockfiles | None |
| Existing tests | None |
| Existing virtualenv / `node_modules` | None |
| Existing ACE-Step checkout or `checkpoints/` | None anywhere on `D:\` or in the user profile |
| Prior `tradefix` artifacts | None |

**Reusable components identified: none.** Everything is written from scratch.

### 2.1 Note on the directory name

The working directory is literally named `.Music` (leading dot). On Windows this is not
a hidden directory, but it *is* treated as hidden by many Unix-oriented tools, and some
tooling (notably certain Node resolvers and Python packaging utilities) behaves
inconsistently with a leading dot in an ancestor path. The Python **package** will
therefore be named `tradefix_radio` and all tooling paths will be absolute, so the
ancestor directory name never participates in module resolution. This is recorded as a
low-grade risk (R-07, §7).

---

## 3. Host environment

### 3.1 Hardware

| Component | Value | Assessment |
| --- | --- | --- |
| CPU | AMD Ryzen 7 PRO 5750G — 8 cores / 16 threads | Comfortable. Allows the API, worker, and playout to run as separate processes without contention. |
| RAM | 31.8 GB | Ample. Endurance tests can hold long rolling windows in memory. |
| GPU | **NVIDIA GeForce RTX 3060, 12 GB VRAM** | Sufficient for ACE-Step 1.5 2B-class models; marginal for 4B XL. See §5.4. |
| GPU driver | 610.47 | Current. |
| VRAM in use at audit time | **4079 MiB / 12288 MiB** | ⚠ ~4 GB already consumed by other processes (Parsec virtual display + desktop). Only **~8 GB is realistically free**. Drives the model-variant decision. |
| GPU temperature | 54 °C idle | Healthy. Thermal headroom for sustained 24/7 generation, but sustained-load temps must be monitored (§20). |
| Secondary display adapter | Parsec Virtual Display Adapter | Indicates remote-access usage; one reason VRAM is pre-consumed. |
| Disk `D:` free | **193.6 GB** free of 476 GB | Good, but 24/7 WAV generation will consume this quickly without the §36 retention policy. See §5.7. |

### 3.2 Software toolchain

| Tool | Version found | Verdict |
| --- | --- | --- |
| `python` (default on PATH) | **3.14.2** (`C:\Python314`) | ⚠ **Too new.** See §5.1. |
| `py -3.10` | **3.10.11** (`%LOCALAPPDATA%\Programs\Python\Python310`) | Usable but older than ideal. |
| Python **3.11 / 3.12** | **ABSENT** | ⚠ Required by ACE-Step 1.5. Must be installed in Phase 7. |
| `torch` in 3.10 env | **2.9.1+cpu** — `torch.cuda.is_available() == False` | ⚠ CPU-only wheel. Useless for GPU generation. Irrelevant to our core app after the §5.3 decision. |
| `torch` in 3.14 env | Not installed | — |
| Node.js | **24.13.0** | Current LTS-class. Fine for Vite 7 + React 19. |
| npm | 11.6.2 | Fine. |
| Git | 2.52.0.windows.1 | Fine. |
| FFmpeg | **8.0.1-full_build** (gyan.dev) | Excellent — full build includes `loudnorm`, `ebur128`, `alimiter`, all needed filters. |
| CUDA toolkit | **12.8** (`nvcc` V12.8.61) | Present; matches cu128 PyTorch wheels. |
| `uv` | **NOT installed** | Required by ACE-Step's documented install flow. Phase 7 dependency. |
| Docker | **NOT installed** | `docker-compose.yml` will be provided but is optional; local dev will not require Docker. |
| PostgreSQL | **No service installed** | SQLite is the practical default for this host. See §5.5. |
| `winget` | 1.29.380 available | Useful for *advising* remediation in `tradefix doctor` (never for silent installs — §77). |
| OBS Studio | **32.1.2** at `C:\Program Files\obs-studio` | obs-websocket 5.x bundled. Config file present at `%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json`. |
| MetaTrader 5 | **Installed** at `C:\Program Files\MetaTrader 5` | Primary production market feed. |

### 3.3 Audio devices

```
AMD High Definition Audio Device                     OK
NVIDIA High Definition Audio                         OK
High Definition Audio Device                         OK
NVIDIA Virtual Audio Device (Wave Extensible) (WDM)  OK
NVIDIA Broadcast                                     OK
Voicemod                                             OK
VB-Audio Virtual Cable                               OK
NoMachine Microphone Adapter                         Error   <-- pre-existing, unrelated
```

**VB-Audio Virtual Cable is the intended playout target.** The playout engine writes PCM
to `CABLE Input`, and OBS captures `CABLE Output` as an Application/Device audio source.
This gives a bit-exact, latency-stable, volume-independent path to the stream that does
not fight with the operator's desktop audio. The pre-existing `NoMachine Microphone
Adapter` error is unrelated to this project and is left untouched.

---

## 4. External dependency research (ACE-Step 1.5)

Researched live rather than recalled, per §19.

| Fact | Finding | Source |
| --- | --- | --- |
| Official repository | `github.com/ace-step/ACE-Step-1.5` (~13k stars) | GitHub |
| **License** | **MIT** — permissive, commercial use and redistribution permitted | repo LICENSE |
| **Required Python** | **3.11–3.12 stable** (not pre-release) | official INSTALL doc |
| Install mechanism | `uv sync` inside a clone of the repo | official INSTALL doc |
| Model download | Automatic on first run into `./checkpoints/`; override via `ACESTEP_CHECKPOINTS_DIR`; manual via `uv run acestep-download [--all]` | official INSTALL doc |
| Disk for core models | **~10 GB** | official INSTALL doc |
| **Entry points** | `uv run acestep` → Gradio UI (:7860) · **`uv run acestep-api` → REST API server (:8001)** · direct `python acestep/acestep_v15_pipeline.py` | official INSTALL doc |
| 2B DiT variants | `acestep-v15-base`, `-sft`, `-turbo` — **~4.7 GB VRAM** | repo README |
| 4B XL DiT variants | `-xl-base`, `-xl-sft`, `-xl-turbo` — ~9 GB bf16; **≥12 GB with offload+quant, ≥20 GB without** | repo README |
| LM variants | `acestep-5Hz-lm-0.6B` / `-1.7B` / `-4B` (Qwen3-based). 6–8 GB VRAM → 0.6B; 12–16 GB → 1.7B | repo README |
| Inference steps | base/sft ≈ 50 steps; **turbo ≈ 8 steps** | repo README |
| Duration range | 10 s – 600 s | repo README |
| Speed claim | < 10 s per full song on RTX 3090 | repo README |
| Exposed params | duration, seed, CFG/guidance scale, inference steps, lyrics (50+ languages, structure tags), style/instrument tag prompt, optional reference audio | repo README |
| REST API schema | **Not published in README**; `docs/en/API.md` exists in-repo but was not retrievable during this audit (GitHub returned 503) | — |

### 4.1 Open item carried into Phase 7

The exact REST request/response schema of `acestep-api` is **not yet known**. This is
deliberately deferred: the `AceStepProvider` adapter will be written against the
`MusicGenerationProvider` interface, and the HTTP wire format will be pinned by reading
`docs/en/API.md` and the live `/docs` OpenAPI schema of the running server at the start of
Phase 7. **No wire format is being guessed now.** The mock provider and the entire
pipeline above it do not depend on it.

### 4.2 Licensing conclusion

MIT licence permits use in this product. **However** — and this is recorded explicitly
because §86 forbids overclaiming — an MIT-licensed *model code* repository is not the same
as a clear copyright position on *generated audio*. The ACE-Step model weights' own terms
and the legal status of AI-generated audio are **outside the scope of what this system can
verify**. The originality engine (§21/§22) therefore implements **internal duplication
prevention** and will never be described in code, docs, or UI as proof of copyright
originality.

---

## 5. Architecture decisions (ADRs)

### ADR-01 — Python version: core app targets **3.10.11**; ACE-Step gets its own 3.12

**Context.** The default `python` on PATH is **3.14.2**. The scientific/audio stack this
project depends on (`numpy`, `librosa`, `numba`, `soundfile`, `scipy`) historically lags
new CPython releases by 6–18 months, and `numba` — a hard transitive dependency of
`librosa` — is the usual blocker. Meanwhile ACE-Step requires **3.11–3.12**, which is not
installed at all. No single interpreter on this machine satisfies both the core app and
ACE-Step.

**Decision.**
- The **core application** (`tradefix_radio`, api, worker, playout) targets **Python
  3.10.11**, which is already installed and has mature wheels for the entire audio stack.
- **ACE-Step runs under its own `uv`-managed Python 3.12 environment**, installed in
  Phase 7, in a directory outside this repository.
- `python` (3.14) is **not used**. All scripts invoke the interpreter explicitly via
  `py -3.10` or the project venv path, never bare `python`.

**Consequences.** `pyproject.toml` sets `requires-python = ">=3.10,<3.11"`. Code uses
3.10-compatible syntax only — notably **no PEP 695 `type` statements, no `except*`**, and
`X | Y` unions are fine (3.10+). `tradefix doctor` hard-fails on a wrong interpreter
rather than producing confusing downstream errors.

---

### ADR-02 — ACE-Step integrated **out-of-process over HTTP**, not in-process

**Context.** §18 demands the generator be hidden behind an interface; §19 demands
timeouts; §20 demands OOM survival; §34 demands the station survive an "ACE-Step crash";
§26 states playback must never depend on generation; §89 wants future remote GPU workers.
ACE-Step ships a first-class REST server (`uv run acestep-api`, :8001).

**Decision.** `AceStepProvider` is an **HTTP client**. ACE-Step runs as a separate OS
process in its own interpreter and venv.

**Consequences — all of them favourable:**
- **No `torch`, CUDA, or transformers in our dependency tree.** Our venv stays small and
  installs in seconds; CI never downloads multi-GB wheels.
- The Python-version conflict (ADR-01) dissolves.
- "ACE-Step crash" becomes an ordinary connection error, not a process-killing
  exception — §34 is satisfied structurally rather than by defensive coding.
- A GPU OOM inside ACE-Step cannot corrupt our heap or kill playout (§20).
- Timeouts and cancellation are plain HTTP concerns.
- **§89 two-PC support becomes nearly free**: a remote worker is the same adapter pointed
  at a different host. The provider config carries a `base_url`.

**Trade-off accepted.** Audio crosses a process boundary (file path on shared disk, or
response body). Negligible: generation takes seconds, and both processes share local disk.

**Rejected alternative.** Importing `acestep` in-process. Rejected because it forces the
whole app onto Python 3.12, pulls torch/CUDA into our venv and CI, and makes a C-level GPU
fault fatal to the radio.

---

### ADR-03 — Market feed: pluggable, with **MetaTrader 5 as the production default**

**Context.** XAUUSD live data normally requires a broker connection or a paid data API.
MT5 is already installed on this host, and its Python package needs **no API key** — it
attaches to the running terminal.

**Decision.** `MarketFeed` is an interface with three implementations:

| Implementation | Mode | Credentials |
| --- | --- | --- |
| `SimulatedFeed` | development, simulation | none — fully offline |
| `MetaTrader5Feed` | **production default** | none beyond a logged-in MT5 terminal |
| `RestPollingFeed` | production alternative | API key (Twelve Data / Polygon / Finnhub-shaped) |
| `ReplayFeed` | simulation | reads recorded tick/candle files |

**Consequences.** The entire application is testable and demonstrable with **zero
credentials and no internet**, satisfying §7. `MetaTrader5Feed` is Windows-only — acceptable,
since §77 declares this a Windows-first product — and is an optional extra so that CI on
Linux still installs cleanly. The specific broker/symbol suffix (brokers name gold
`XAUUSD`, `XAUUSD.m`, `GOLD`, etc.) is **configuration**, resolved by a symbol-discovery
step in the feed, not hard-coded.

---

### ADR-04 — Default ACE-Step model: **2B `turbo`**, not 4B XL

**Context.** The RTX 3060 has 12 GB total but **~4 GB is already in use** (≈8 GB free).
4B XL needs ≥12 GB even with offload+quantisation. 2B variants need ~4.7 GB. The LM sizing
table puts a 12 GB card at the 1.7B LM, but with only 8 GB genuinely free the 0.6B LM is
the safe pick.

**Decision.** Default configuration: DiT `acestep-v15-turbo` (8 inference steps) + LM
`acestep-5Hz-lm-0.6B`. Variant and LM size are **config values**, not constants, so an
operator with a larger card changes one YAML line. Phase 7 benchmarks base/sft/turbo on
this actual hardware and the default is revisited with real numbers (§80).

**Rationale beyond VRAM.** A 24/7 station is throughput-bound, not quality-bound at the
margin: turbo's 8 steps vs 50 gives roughly a 6× throughput advantage, which directly
buys queue buffer (§93) and therefore reliability. §94/§95 then spend surplus buffer on
*creative* risk rather than on per-track step count.

---

### ADR-05 — Database: **SQLAlchemy 2.0 async + Alembic**, SQLite default, PostgreSQL supported

**Context.** §37 asks for PostgreSQL in production with SQLite for local dev, and
migrations rather than auto-created schema. No PostgreSQL service exists on this host, and
installing a database server is a system modification §77 forbids doing silently.

**Decision.** One SQLAlchemy 2.0 model layer, dialect-agnostic, with Alembic migrations.
`aiosqlite` is the default driver; `asyncpg` is an optional extra selected purely by the
`TRADEFIX_DATABASE_URL` DSN. SQLite is configured for concurrent use: **WAL journal mode,
`busy_timeout`, `synchronous=NORMAL`**.

**Consequences.** Schema avoids PostgreSQL-only types — no native `ARRAY`, no `JSONB`.
Lists and nested structures are stored as **portable JSON text** with a typed accessor
layer, so blueprints and fingerprint vectors round-trip identically on both engines. A
single `docker-compose.yml` is still provided for operators who want PostgreSQL, but it is
never required.

---

### ADR-06 — Playout: in-process PCM mixer → pluggable `AudioSink`

**Context.** §30 requires true crossfades and forbids silence between tracks; §63
Scenario C/D require playout to continue while generation and even the market feed are
dead; tests must run headless in CI with no sound card.

**Decision.** The playout engine decodes to PCM, performs its own **sample-accurate mixing
and crossfading** in NumPy, and pushes frames to an `AudioSink` interface:

| Sink | Use |
| --- | --- |
| `SoundDeviceSink` | production — PortAudio → **VB-Audio `CABLE Input`** → OBS |
| `NullSink` | unit/integration tests — consumes frames, advances a virtual clock |
| `WavFileSink` | capturing proof-of-broadcast audio for review |
| `FFmpegPipeSink` | future Icecast/RTMP direct streaming |

**Consequences.** Crossfade logic is **pure, deterministic, and unit-testable** — it is
NumPy array maths, not a side effect of a media library. Driving `NullSink` with an
accelerated clock is exactly what §64's 7-day endurance test needs: the whole playout path
runs for simulated weeks in seconds. Rejected: handing files to `ffplay`, which gives no
sample-level control over transitions.

---

### ADR-07 — Disk retention is a **Phase-1 concern**, not a Phase-9 afterthought

**Context.** 193 GB free sounds generous. It is not. A 3.5-minute 44.1 kHz stereo 16-bit
WAV is ~37 MB. At ~17 tracks/hour that is **~15 GB/day**, exhausting the disk in **under
two weeks** — well inside the "days or weeks without intervention" target. §36 is
therefore a *correctness* requirement, not a nicety.

**Decision.** The schema separates **permanent metadata** (`tracks`, `track_blueprints`,
`audio_fingerprints`, `lyrics`, `used_seeds`) from **expendable bytes** (`track_files`)
from day one. Audio files are addressed only through `track_files` rows, so retention can
delete bytes while every originality check keeps working forever — exactly the §36
intent. Default masters are written as **FLAC** (lossless, ~55% of WAV) rather than WAV.

**Consequence.** No migration is needed later to enable retention; Phase 9 only adds the
sweeper that acts on a structure that already exists.

---

### ADR-08 — Three processes, one event bus, versioned contracts

**Decision.** `apps/api` (FastAPI + WebSocket), `apps/worker` (generation/QC/originality/
mastering), `apps/playout` (market feed + scheduler + mixer) are separate entry points.
§69 forbids GPU work in a request thread; §70 demands job leases. In-process they
communicate via an async event bus (§91); across processes, via the database with **job
leases carrying an owner and expiry**, so a crashed worker's lease expires and the job is
reclaimed rather than locked forever.

All cross-boundary payloads are **versioned Pydantic models** (`MarketStateV1`,
`MusicBlueprintV1`, …) per §90 — never bare dicts. For single-box operation a
`tradefix run` supervisor hosts all three in one process with the same interfaces, so the
common case needs no orchestration.

---

### ADR-09 — Frontend: React 19 + TypeScript + Vite + Tailwind + Recharts

**Decision.** As recommended in §39. Node 24.13 is present. No existing frontend exists to
evaluate against (§39's caveat is moot). Tailwind v4, Radix-based primitives for
accessibility (§85), Recharts for the energy/market timelines, native `WebSocket` with a
typed reconnecting client.

The **stream overlay** (§52) is a **separate Vite entry point** from the control centre —
different performance budget, different lifetime (24/7 vs operator-present), no shared
layout chrome, and it must not pull dashboard JS into an OBS browser source.

---

## 6. Rejected approaches (recorded so they are not re-litigated)

| Rejected | Reason |
| --- | --- |
| Using `python` 3.14 for the app | `numba`/`librosa` wheels not available; would block the audio stack. |
| Single interpreter for app + ACE-Step | Impossible: app stack wants ≤3.10 here, ACE-Step wants 3.11–3.12. |
| `random.choice()` for genre/BPM selection | Explicitly forbidden (§9); produces the boredom collapse §11 exists to prevent. |
| Generating audio inside a FastAPI request | Forbidden (§69). |
| `ffplay` subprocess per track | No sample-level crossfade control (§30). |
| PostgreSQL as a hard requirement | Not installed; §77 forbids silent system modification. |
| Monolithic single Python file | Forbidden (§86). |
| Downloaded/copyrighted audio as fallback | Forbidden (§86). Tier-3 fallback is **procedurally synthesised**. |
| Claiming fingerprints prove originality | Forbidden (§21, §86). |

---

## 7. Risk register

| ID | Risk | Severity | Mitigation |
| --- | --- | --- | --- |
| R-01 | **VRAM starvation** — only ~8 GB of 12 GB free; Parsec/desktop may grow | High | ADR-04 picks 2B turbo. GPUManager (§20) pre-flight checks free VRAM and refuses rather than OOMs. Out-of-process isolation (ADR-02) means an OOM never touches playout. |
| R-02 | **Disk exhaustion in <2 weeks** at full rate | High | ADR-07 makes retention structural from Phase 1. FLAC masters. Disk alert threshold in §57. |
| R-03 | **ACE-Step REST schema unknown** | Medium | Deferred to Phase 7 by design; pinned from `docs/en/API.md` + live OpenAPI. Nothing upstream depends on it. Mock provider keeps Phases 1–6 unblocked. |
| R-04 | **MT5 feed is Windows-only and needs a logged-in terminal** | Medium | Optional extra; `SimulatedFeed` covers all dev/CI. Stale-feed handling (§63-E) treats a dead terminal as a first-class state with no fabricated prices. |
| R-05 | **`torch 2.9.1+cpu` already in the 3.10 env misleads diagnostics** | Low | Our venv is isolated and does not depend on torch at all. `doctor` reports the ACE-Step env separately. |
| R-06 | **Regime flicker** driving musical whiplash | High (product) | §5 hysteresis + min-duration + confirmation + cooldown, with dedicated unit tests asserting no flicker under adversarial input. |
| R-07 | Leading-dot ancestor path `D:\.Music` confusing tooling | Low | Package name `tradefix_radio`; absolute paths everywhere; no reliance on CWD-relative resolution. |
| R-08 | **Python 3.10 reaches EOL October 2026** | Medium | Security-fix-only now. Documented upgrade path: once `numba` supports 3.13, move the app forward; ADR-02 already decoupled us from ACE-Step's version, so the move is independent. |
| R-09 | Sustained 24/7 GPU load thermals | Medium | GPUManager records temperature; §57 alerts. Idle 54 °C gives headroom but sustained load is unmeasured until Phase 7 benchmarks. |
| R-10 | Endurance tests that only *simulate* may hide real leaks | Medium | §64 accelerated **and** §65 real-time soak are both built; soak uses the real event loop and real DB. |

---

## 8. Dependencies to be installed

**Phase 1 (our venv, Python 3.10.11) — none are system modifications:**
`pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]`, `alembic`, `aiosqlite`,
`fastapi`, `uvicorn`, `structlog`, `pyyaml`, `psutil`, `httpx`,
`pytest`, `pytest-asyncio`, `hypothesis`, `ruff`, `mypy`.

**Later phases:** `numpy`, `soundfile`, `librosa`, `pyloudnorm`, `sounddevice`,
`obsws-python`, `Pillow` (artwork), `MetaTrader5` (Windows extra), `asyncpg` (pg extra).

**Operator-visible prerequisites NOT installed by us** (reported by `tradefix doctor`
with remediation text, never installed silently per §77):

| Missing | Needed for | Remediation shown |
| --- | --- | --- |
| **Python 3.12** | ACE-Step 1.5 | `winget install Python.Python.3.12` |
| **`uv`** | ACE-Step install flow | `irm https://astral.sh/uv/install.ps1 \| iex` |
| **ACE-Step checkout + ~10 GB checkpoints** | Phase 7 | documented in `docs/GENERATION.md` |
| PostgreSQL *(optional)* | production DB | `docker-compose.yml` provided |

---

## 9. Conformance notes on the specification

Three points in the brief are deliberately interpreted rather than taken literally, and
are flagged here so the interpretation is reviewable:

1. **§21 originality.** The brief itself forbids claiming proof that music "can never
   resemble anything ever created." All naming, documentation, and UI copy will say
   *"internal duplication prevention"* and *"novelty score"* — never "copyright-safe" or
   "proven original."

2. **§14 lyrical accuracy.** Trading education in lyrics must not state uncertain market
   relationships as guaranteed. The topic graph stores a `certainty` qualifier per concept,
   and the lyric validator (§17) rejects guaranteed-outcome phrasing. This is enforced by
   tests, not convention.

3. **§7 / §32 no fabricated prices.** The AI DJ and overlay may only cite a price carried
   on a *validated, non-stale* `MarketState`. When the feed is stale, price display is
   suppressed and marked stale rather than showing a last-known value as current. This is
   a correctness rule with a test, not a UI preference.

---

## 10. Phase 0 conclusion & next step

- Repository: **empty**, greenfield, no reuse or backup obligations.
- Environment: **well suited**; MT5, OBS 32 + websocket 5.x, FFmpeg 8, VB-Cable, CUDA 12.8,
  RTX 3060 12 GB all present.
- Two blocking-for-Phase-7-only gaps: **Python 3.12** and **`uv`**. Neither blocks
  Phases 1–6.
- Nine architecture decisions recorded; ten risks registered with mitigations.

**Next:** `docs/IMPLEMENTATION_PLAN.md`, then Phase 1 (core domain).
