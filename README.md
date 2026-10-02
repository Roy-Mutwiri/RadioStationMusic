# TRADE FIX RADIO

**Autonomous 24/7 market-reactive AI music radio station.**

> The market composes the radio.

Trade Fix Radio watches XAUUSD continuously, classifies what the market is doing,
turns that into musical direction, generates original music locally, masters it,
and broadcasts it — indefinitely, without human intervention.

It is not a playlist app and not a random-music generator. The station observes,
classifies, plans, generates, validates, masters, queues, plays, transitions,
monitors, recovers, and repeats.

---

## Current status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Repository & environment audit | ✅ `docs/INITIAL_AUDIT.md` |
| 1 | Core domain: config, contracts, persistence, logging, events, health | ✅ `docs/status/PHASE_1_REPORT.md` |
| 2 | Market engine: simulator, features, energy, regimes | ✅ `docs/status/PHASE_2_REPORT.md` |
| 3 | Music director, lyric director, diversity engine | ⏳ next |
| 4 | Mock radio: queue, scheduler, playout, fallback | — |
| 5 | API + control centre + stream overlay | — |
| 6 | Audio QC, fingerprinting, duplication prevention, mastering | — |
| 7 | ACE-Step 1.5 integration | — |
| 8 | OBS integration | — |
| 9 | Resilience: watchdog, recovery, chaos | — |
| 10 | Endurance: 24 h / 72 h / 7 d | — |

**919 tests passing · 88 % coverage · ruff and mypy clean.**

Plan and milestone exit criteria: `docs/IMPLEMENTATION_PLAN.md`.

---

## Requirements

| Requirement | Why | Notes |
| --- | --- | --- |
| **Python 3.10.x** | The audio stack (`librosa`/`numba`) has no wheels for newer CPython | Enforced by `tradefix doctor`; see ADR-01 |
| **FFmpeg** (full build) | Mastering pipeline (`loudnorm`, `ebur128`, `alimiter`) | `winget install Gyan.FFmpeg` |
| Node.js 20+ | Control-centre frontend only | The station broadcasts without it |
| NVIDIA GPU, ≥ 6 GB free VRAM | Only for the `ace_step` provider | Not needed with the mock provider |
| Python 3.11–3.12 + `uv` | Only for ACE-Step, which runs in its **own** environment | See ADR-02 |
| MetaTrader 5 terminal | Only for the production market feed | No API key required |
| OBS Studio 28+ | Only for streaming | obs-websocket 5.x is bundled |

Nothing but Python 3.10 is required to run the station in development mode.

---

## Quick start

```powershell
# 1. Create the environment (Python 3.10 specifically)
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"

# 2. Create directories and apply migrations
.\.venv\Scripts\tradefix init

# 3. Verify the environment
.\.venv\Scripts\tradefix doctor
```

`doctor` prints a pass/fail table with **remediation for anything that fails**, and
exits non-zero only when a *blocking* dependency is missing — so it is safe to use
as a CI smoke test on a machine with no GPU and no OBS.

Then copy `.env.example` to `.env` and edit. `.env` is git-ignored and is the only
place secrets belong.

---

## Commands

```
tradefix doctor [--json]       environment and dependency report
tradefix init                  create directories, apply migrations
tradefix migrate [up|down|current|history]
tradefix config [--section S]  resolved configuration, secrets masked
tradefix market-sim            run a market scenario, print the engine's reading
tradefix version
```

Later phases add `report-director`, `soak`, `benchmark`, and the
`dev` / `simulation` / `production` runners.

### Seeing the market engine work

```powershell
# A dead market that suddenly breaks out
.\.venv\Scripts\tradefix market-sim --scenario flat `
    --switch-to violent_breakout --switch-at 150 --bars 300 --every 60
```

Prints the real feature engine, energy score and regime classifier reacting to a
simulated §7 scenario — energy climbing from ~10 to ~80, the regime passing through
`extreme_volatility` into `bullish_trend`, and roughly one regime change per 27 bars
rather than one per bar. Reproducible from `--seed`.

---

## Run modes (§72)

| Mode | Market | Generator | Audio | Needs |
| --- | --- | --- | --- | --- |
| `development` | simulated | mock | null sink | nothing external |
| `simulation` | simulated | mock or ACE-Step | null sink or device | production-shaped config |
| `production` | MetaTrader 5 / REST | ACE-Step | sound device → OBS | GPU, feed, OBS, secrets |

Select with `--mode` or `TRADEFIX_MODE`. Production **refuses to start** without a
control token, or with a simulated feed, or with the mock provider — a
misconfigured production launch fails at startup rather than on air.

---

## Architecture

```
LIVE MARKET  ->  FEATURES  ->  REGIME ENGINE  ->  MARKET STATE
                                                       |
                                                       v
                                   MUSIC DIRECTOR  +  LYRIC DIRECTOR
                                                       |
                                                 MUSIC BLUEPRINT
                                                       |
                                                 GENERATION ENGINE
                                                       |
                            QUALITY CONTROL -> DUPLICATION CHECK -> MASTERING
                                                       |
                                                   TRACK VAULT
                                                       |
                                          SCHEDULER  ->  PLAYOUT
                                                       |
                                             OBS / STREAM  +  UI / API
```

The generator only ever receives a `MusicBlueprint`. Nothing market-aware exists
downstream of that boundary, and nothing model-specific exists upstream — which is
what makes a second generation backend a new adapter rather than a rewrite.

Details: `docs/ARCHITECTURE.md`. Decisions and their rationale:
`docs/INITIAL_AUDIT.md` §5.

---

## Development

```powershell
.\.venv\Scripts\python -m pytest tests/unit tests/integration -q   # fast suite
.\.venv\Scripts\ruff check tradefix_radio tests
.\.venv\Scripts\mypy
```

Test markers: `slow`, `endurance`, `soak`, `gpu`, `obs`, `postgres`,
`audio_device`. The fast suite needs none of that hardware.

---

## Honest scope notes

Two claims this project deliberately does **not** make:

* **Duplication prevention is internal, not a copyright guarantee.** The
  originality engine compares candidates against *this station's own library* using
  fingerprints, embeddings and lyric hashes. It prevents the station repeating
  itself. It is not evidence that any track is original in a legal sense, and
  nothing in the code, docs or UI says otherwise.
* **Trading content is educational, never predictive.** Lyrics and the AI DJ may
  not state uncertain market relationships as guaranteed, may not promise profit,
  and may not emit signals. Every topic carries a `certainty` qualifier and the
  lyric validators enforce it. Prices are only ever shown or spoken from a
  validated, non-stale market state — when the feed is stale, no price is
  displayed at all.
