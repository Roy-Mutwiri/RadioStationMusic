# TRADE FIX RADIO — Implementation Plan

**Created:** 2026-10-02 · **Basis:** `docs/INITIAL_AUDIT.md` · **Build order:** per brief §82

This plan decomposes the build into **milestones that are each independently verifiable**.
Every milestone states its *exit test* — the concrete, runnable check that proves it works.
A milestone is not complete until its exit test passes. No milestone's exit test is "the
code exists" or "the UI looks right" (§86, §102).

---

## 0. Guiding constraints

These apply to every milestone and are not restated per-milestone:

| Constraint | Source | Enforcement |
| --- | --- | --- |
| No magic numbers; everything configurable | §71 | config schema validated on startup; lint rule review |
| Versioned typed contracts across boundaries, never bare dicts | §90 | Pydantic `*V1` models; mypy |
| Playout never synchronously depends on generation | §26, §69 | architecture; Scenario C test |
| No broad `except:` without logging | §86 | ruff `BLE001`, `E722` |
| No TODO placeholders in critical paths | §86 | ruff `FIX`/`TD` rules on `tradefix_radio/` |
| Tests accompany features | §58 | phase reports list coverage |
| Never fake success / never hide failures | §102 | phase reports paste real output |

**Definition of "tested"** for this project: unit tests for pure logic, property tests
(Hypothesis) for invariants, integration tests for cross-subsystem flow with the mock
provider, system scenarios for §63 A–E, and accelerated endurance for §64.

---

## PHASE 1 — Core domain
*Goal: a validated, migrated, observable foundation. No music, no market yet.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 1.1 | Monorepo scaffold, `pyproject.toml` (`requires-python >=3.10,<3.11`), ruff + mypy config, venv bootstrap script | `ruff check` and `mypy` both clean on an empty package; venv creates on `py -3.10` |
| 1.2 | Config system: YAML + `.env` + env-var overlay, nested Pydantic settings, `validate()` that fails loudly with field paths | unit: valid config loads; each invalid field produces a *specific* error naming the path; unknown keys rejected |
| 1.3 | Versioned data contracts: `MarketSnapshotV1`, `MarketFeaturesV1`, `MarketStateV1`, `MusicBlueprintV1`, `GenerationRequestV1/ResultV1`, `TrackAnalysisV1`, `QueueItemV1`, `LyricsV1` | unit: round-trip JSON serialise/deserialise for each; schema version pinned; forbidden extra fields |
| 1.4 | Structured logging (`structlog`): JSON to rotating file + human console, bound context (`track_id`, `job_id`, `regime`) | unit: emitted record contains required §55 keys; rotation triggers at configured size |
| 1.5 | Async event bus (§91) with typed topics, subscriber isolation (one bad subscriber cannot break publish), backpressure policy | unit: publish/subscribe; a raising subscriber is logged and does not block others; unsubscribe leaks nothing |
| 1.6 | Persistence: SQLAlchemy 2.0 async models for all §37 entities, portable-JSON columns, Alembic initial migration, SQLite WAL pragmas | integration: `alembic upgrade head` on fresh SQLite; CRUD round-trip per repository; same migration applies on PostgreSQL (skipped if absent) |
| 1.7 | Generation **state machine** (§92) as an explicit transition table; illegal transitions raise `IllegalTransition` | unit: **every** legal transition accepted, **every** illegal pair rejected — exhaustive over the state product |
| 1.8 | Health registry + `tradefix doctor` skeleton (§73, §79) with remediation strings | CLI: `tradefix doctor` prints the §79 table with real pass/fail; exits non-zero when a required check fails |
| 1.9 | Disk-retention-ready file layout: `track_files` indirection, `generated/`, `emergency/`, `logs/`, `data/` creation + permission check | unit: retention planner selects correct deletion candidates and **never** selects queued/emergency/unplayed files |

**Phase exit:** `pytest tests/unit tests/integration -q` green; `ruff`+`mypy` clean;
`tradefix doctor` runs. → `docs/status/PHASE_1_REPORT.md`.

---

## PHASE 2 — Market engine
*Goal: XAUUSD understanding, fully offline.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 2.1 | `MarketFeed` interface + `SimulatedFeed`; `MarketSimulationEngine` with all 15 §7 scenarios | unit per scenario: generated series exhibits the *statistical signature* it claims (e.g. `compression` → falling ATR percentile; `violent_breakout` → range expansion > threshold) |
| 2.2 | Feature engine: all 20 §4 features over rolling windows, NaN-safe during warm-up | unit: known-input fixtures → expected values; warm-up emits `insufficient_history` rather than garbage; property: no NaN/inf ever escapes |
| 2.3 | Rolling-distribution normaliser (percentile ranks) — **no absolute XAUUSD price assumptions** (§6) | property: feature percentiles invariant under affine price rescaling (×10 price ⇒ same energy) |
| 2.4 | Energy score: weighted, configurable, emits `raw_energy`, `smoothed_energy`, `energy_velocity` | property: always ∈ [0,100]; monotone in each input weight; velocity sign matches direction of change |
| 2.5 | Regime engine: 14 §5 regimes + hysteresis, confidence threshold, min duration, smoothing, transition confirmation, cooldown | unit: adversarial oscillating input produces **≤ N transitions** (no flicker); each regime reachable; `UNKNOWN` on insufficient data; min-duration never violated |
| 2.6 | Session classifier (Asia/London/NY/overlaps) incl. DST and weekend/holiday gap handling | unit: boundary timestamps across DST transitions map to correct sessions |
| 2.7 | `MarketState` assembly + staleness tracking + last-good-state retention (§63-E) | unit: feed silence ⇒ state marked stale with age; **no fabricated price** exposed |

**Phase exit:** full market suite green + flicker test + affine-invariance property test.
Market Lab page is deferred to Phase 5 (needs the API); the engine is independently
verifiable via a CLI `tradefix market-sim --scenario breakout_up --report` that prints the
feature/energy/regime timeline. → `docs/status/PHASE_2_REPORT.md`.

---

## PHASE 3 — Directors
*Goal: the market composes the radio — provably, statistically.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 3.1 | Genre library + weight matrix (config-driven, §10); `MusicBlueprintV1` builder | unit: every regime yields a valid blueprint; unknown genre in config fails validation at startup not at runtime |
| 3.2 | Constrained weighted selection engine (**no bare `random.choice`**, §9) with seedable RNG | unit: with fixed seed, deterministic; weights respected within tolerance over 10k draws |
| 3.3 | BPM / key / duration / structure mapping from `MarketState` | property: BPM always within the configured band for the regime; duration within bounds; key always valid |
| 3.4 | `DiversityDirector`: all nine §11 trackers, multi-horizon (5/20/100/24h/7d/all-time, §12), **diversity score 0–100** | unit: each §11 rule independently enforced; score falls when fed monotonous history and recovers after forced divergence |
| 3.5 | `RadioEnergyPlanner` (§98): smooths station energy against market energy | unit: no extreme oscillation under oscillating market input; sharp change permitted when market justifies it |
| 3.6 | Creative temperature (§95) + job priority (§94) coupling to buffer health | unit: low buffer ⇒ temperature drops ⇒ experimental genres excluded; high buffer ⇒ admitted |
| 3.7 | Trading topic graph (§14) with `certainty` qualifiers; topic selection with history | unit: no topic repeats inside its configured horizon; topic+subtopic pair never intentionally repeats |
| 3.8 | `LyricsDirector` + §15 formats + §16 branding frequency + §100 personas | unit: Trade Fix mention counts match configured distribution over 1000 draws; no persona impersonates a real artist (curated list only) |
| 3.9 | Lyric validators (§17): guaranteed-profit, fabricated-signal, certainty, gibberish, excessive repetition, duplicate-vs-history | unit: a labelled corpus of bad lyrics is **100% rejected**; good lyrics pass; each rule has its own case |
| 3.10 | Original title generator (§99) with semantic-similarity rejection vs title history | unit: 5000 titles → zero exact duplicates, banned-generic list excluded, similarity below threshold |
| 3.11 | **Radio memory** (§96) persisted — restart must not cause immediate repeats | integration: generate 50 decisions, restart process, next decisions still respect history horizons |
| 3.12 | **Statistical report**: 10 000 simulated decisions across all regimes | `tradefix report-director` emits genre/BPM/key/topic/vocal distributions + diversity-over-time; asserts no collapse (no genre > configured share; entropy above floor) |

**Phase exit:** suite green + the §3.12 statistical report committed as evidence.
→ `docs/status/PHASE_3_REPORT.md`.

---

## PHASE 4 — Mock radio (the station must already run 24/7 here)
*Goal: continuous broadcast with zero AI involvement.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 4.1 | `MusicGenerationProvider` interface (§18) + `MockMusicProvider` producing **real** synthetic audio fast (§62) | unit: output is valid audio of requested duration, passes the Phase-6 QC rules, differs per seed |
| 4.2 | `GenerationManager`: job lease/idempotency (§70), cancellation, timeouts, retry policy | integration: two workers cannot claim one job; a killed worker's lease expires and the job is reclaimed, not stuck |
| 4.3 | Capacity predictor (§93): `generation_capacity_ratio` from measured rates | unit: synthetic rate histories ⇒ correct urgency escalation *before* starvation |
| 4.4 | Queue + states (§27) with persisted transitions; layered locking (§28) | property: currently-playing item never disappears; locked items stay locked; queue duration never negative |
| 4.5 | Scheduler: buffer targets (20/45/90 min, configurable), replanning that respects locks | integration: mid-stream regime change replaces *future* items only, leaving current + next intact (§29) |
| 4.6 | Playout engine + `AudioSink` abstraction + sample-accurate crossfade/transitions (§30) | unit: crossfade output has **no gap and no clipping** at the seam (assert on the PCM); `NullSink` run advances a virtual clock deterministically |
| 4.7 | Station IDs (§31) with separate anti-repeat history | unit: identical ID not reused within configured window |
| 4.8 | Three-tier emergency audio (§33); Tier 3 **procedural, layered** — not one 60 s loop | unit: Tier 3 output over 10 min has no exact repeating segment; tier escalation ordering correct |
| 4.9 | Crash recovery (§75) + graceful shutdown (§74) | integration: kill mid-track, restart ⇒ queue + history restored, incomplete track **not** marked played |
| 4.10 | **Continuous-run proof**: 2 h simulated broadcast, mock provider, `NullSink` | `tradefix soak --simulated-hours 2` ⇒ zero underruns, zero unhandled exceptions, diversity score above floor |

**Phase exit:** §63 Scenarios **C** (generator killed) and **D** (queue depletion) pass here,
before any AI model exists. → `docs/status/PHASE_4_REPORT.md`.

---

## PHASE 5 — API + Control Center
*Goal: a premium broadcast console over real backend state only.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 5.1 | FastAPI app, all §38 routes, dependency-injected services, error envelopes | contract tests per route: status codes, schema validation, destructive endpoints protected (§68) |
| 5.2 | WebSocket live-state channel with snapshot + delta, reconnect, heartbeat | integration: client receives snapshot then deltas; survives server restart; no unbounded buffering on a slow client |
| 5.3 | `POST /api/simulation/state` drives the **real** engines (§7) | integration: simulation button ⇒ market state ⇒ next blueprint actually changes. **No fake UI values** (§7) |
| 5.4 | Frontend scaffold: Vite + React 19 + TS + Tailwind v4, design tokens, §84 status colours, typed API/WS client | `vitest` + `tsc --noEmit` clean; token contrast audited (§85) |
| 5.5 | Dashboard (§41, §42, §43, §44) answering the six §41 questions above the fold | Playwright: at 1920×1080 / 1440×900 / 1366×768 — no horizontal overflow, all six answers visible, skeleton + empty + error states render |
| 5.6 | Market Lab (§47) with simulation controls | Playwright: scenario switch changes backend state and chart |
| 5.7 | Generation Lab (§45) — manual jobs **never auto-enter the live queue** | integration: manual job requires explicit approval before queueing |
| 5.8 | Track Library (§46) + detail page; Originality (§48); System (§49); Settings (§50) with masked secrets | Playwright per page; unit: a saved secret is never returned unmasked by the API |
| 5.9 | Stream overlay (§52, §53) as a separate entry point, 1920×1080, burn-in mitigation | Playwright: renders at exactly 1920×1080; element positions vary over time; no layout thrash |

**Phase exit:** all frontend tests green; screenshots at the three resolutions attached to
the report. → `docs/status/PHASE_5_REPORT.md`.

---

## PHASE 6 — Audio QC, fingerprinting, originality, mastering

| # | Milestone | Exit test |
| --- | --- | --- |
| 6.1 | Audio analysis (§24): silence, clipping, duration, DC offset, channel layout, loudness, corruption | unit: a crafted fixture per defect is rejected; a clean file passes |
| 6.2 | Fingerprints (§21): sha256, chroma, MFCC, spectral, tempo, key, lyric hash, blueprint signature | unit: deterministic for identical input; stable under re-encode; stored and reloaded losslessly |
| 6.3 | Similarity engine + weighted novelty score + APPROVE/REVIEW/REJECT policy (§22) | unit (§59): **identical audio rejected**, near-duplicate rejected, genuinely different accepted, duplicate lyric rejected, seed reuse detected |
| 6.4 | Seed registry (§23) — metadata, explicitly *not* a uniqueness proof | unit: reuse detected; docs assert the non-guarantee |
| 6.5 | Mastering (§25): trim, optional EQ/compression, loudness normalisation, true-peak limiter, metadata, FLAC output | unit: output hits target LUFS ±0.5 and true peak ≤ configured ceiling; **dynamic range not crushed** (LRA retained above floor) |
| 6.6 | Rejected tracks can never reach the queue | property: for any candidate with verdict REJECT, no queue path admits it |

**Phase exit:** duplicate-rejection suite green with real audio. → `docs/status/PHASE_6_REPORT.md`.

---

## PHASE 7 — ACE-Step 1.5
*Goal: real music, isolated from the radio.*

| # | Milestone | Exit test |
| --- | --- | --- |
| 7.1 | **Pin the REST contract**: read `docs/en/API.md` + live `/docs` OpenAPI of `acestep-api`; record in `docs/GENERATION.md` | the recorded schema matches a live round-trip; no guessed fields |
| 7.2 | Provisioning script + doc: Python 3.12, `uv`, clone, `uv sync`, checkpoint download, `ACESTEP_CHECKPOINTS_DIR` | `tradefix doctor` reports ACE-Step OK/missing with accurate remediation |
| 7.3 | `AceStepProvider` HTTP adapter: blueprint→prompt mapping, lyrics, seed, timeout, cancellation | isolated validation: **one real track generated**, outside the radio |
| 7.4 | `GPUManager` (§20): VRAM pre-flight, utilisation/temperature/OOM counters | unit with injected fakes: insufficient VRAM ⇒ refuse (not OOM); OOM ⇒ documented recovery ladder executes in order |
| 7.5 | Wire into worker, then queue — generation failure must be invisible to playout | §63 Scenario C re-run **with ACE-Step**: kill it mid-generation, radio keeps playing |
| 7.6 | Benchmark (§80) on this RTX 3060 and revisit ADR-04 default with real numbers | `tradefix benchmark` report committed |

**Phase exit:** §81 criteria 4–10 demonstrated end-to-end. → `docs/status/PHASE_7_REPORT.md`.

---

## PHASE 8 — OBS

| # | Milestone | Exit test |
| --- | --- | --- |
| 8.1 | obs-websocket 5.x client, password from env/secret store only (§68) | integration against the local OBS 32.1.2: connect, read streaming/recording state |
| 8.2 | Configurable source mapping (§51 `TF_*` sources), text + artwork updates | integration: now-playing change propagates to OBS text sources |
| 8.3 | Procedural artwork generator (§54) from title/genre/regime/energy/seed | unit: deterministic per seed, correct dimensions, no external assets |
| 8.4 | Connection health + graceful degradation when OBS is absent | integration: OBS down ⇒ radio unaffected, health shows disconnected |

**Phase exit:** §81 criterion 12. → `docs/status/PHASE_8_REPORT.md`.

---

## PHASE 9 — Resilience

| # | Milestone | Exit test |
| --- | --- | --- |
| 9.1 | Watchdog: HEALTHY/DEGRADED/CRITICAL/RECOVERING, restart policies with **exponential backoff and no crash loops** (§35) | unit: repeated failures back off and eventually stop retrying with an alert, rather than looping |
| 9.2 | Explicit fallback behaviour for **each** §34 failure mode | one test per §34 row — 13 tests |
| 9.3 | Alerts (§57) surfaced through API + UI | integration: each alert condition raises and clears correctly |
| 9.4 | Disk retention sweeper (§36) acting on the Phase-1 structure | integration: old played audio removed, metadata/fingerprints retained, queued/emergency untouched |
| 9.5 | Metrics (§56) — all listed counters/gauges exposed | contract test: every §56 metric present and non-static |
| 9.6 | Chaos suite (§66): injected generator/db/market timeouts, corrupt audio, OBS drop, disk pressure, process restart | chaos run: radio alive throughout; every injection logged and recovered |

**Phase exit:** §63 Scenarios A–E all green. → `docs/status/PHASE_9_REPORT.md`.

---

## PHASE 10 — Endurance

| # | Milestone | Exit test |
| --- | --- | --- |
| 10.1 | Accelerated-clock endurance harness (virtual time, `NullSink`) | 24 h simulated completes |
| 10.2 | 24 h / 72 h / 7 d runs with measurement of memory, DB growth, task/handle leaks, diversity & topic entropy, seed uniqueness, retention | **zero queue underruns** (§81-17); memory growth below threshold; no diversity collapse (§81-18) |
| 10.3 | Real-time soak mode (§65), overnight, mock provider | event-loop lag, handle count, DB connections all stable |
| 10.4 | Fix every finding; re-run | final report with before/after numbers |

**Phase exit:** §81 criteria 16–20. → `docs/status/PHASE_10_REPORT.md`.

---

## Acceptance traceability (§81)

| § 81 criterion | Proven in |
| --- | --- |
| 1 Simulator drives real MarketState | 2.1, 2.7 |
| 2 MarketState drives MusicBlueprint | 3.1, 3.12 |
| 3 Blueprint drives mock generator | 4.1 |
| 4 ACE-Step generates a valid track | 7.3 |
| 5 QC pass | 6.1 |
| 6 Fingerprint | 6.2 |
| 7 Originality pass | 6.3 |
| 8 Mastered | 6.5 |
| 9 Enters scheduler | 4.5 |
| 10 Playout transitions to it | 4.6 |
| 11 UI displays it | 5.5 |
| 12 OBS metadata updates | 8.2 |
| 13 Generator killable without stopping radio | 4.2 / 7.5 |
| 14 Feed disconnect without stopping radio | 2.7 / 9.2 |
| 15 Restart restores state | 4.9 |
| 16 Integration test covers mock pipeline | 4.10 |
| 17 24 h accelerated, no underrun | 10.2 |
| 18 Diversity does not collapse | 3.12 / 10.2 |
| 19 No unhandled exceptions in simulation | 4.10 / 10.2 |
| 20 Deployment + recovery documented | §76 docs, 9.x |

---

## Deferred by design (extension points only — §88)

Not built now; architecture must not preclude them: additional symbols/stations,
simultaneous channels, AI DJ speech synthesis (the *decision* layer §32 is built; the TTS
voice is not), chat/requests/voting, remote GPU workers (ADR-02 leaves the door open),
mobile control centre, archive/podcast modes, personalised stations.

---

## Reporting

Per §101, each phase ends with `docs/status/PHASE_X_REPORT.md` containing: what was
implemented, changed files, architecture decisions, tests run with **real pasted output**,
performance numbers, known limitations, and the next step. "Done." is not a report.
