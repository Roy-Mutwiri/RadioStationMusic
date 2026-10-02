# PHASE 1 REPORT — Core Domain

**Date:** 2026-10-02 · **Status:** complete · **Basis:** `docs/IMPLEMENTATION_PLAN.md` Phase 1

Per §101 this report provides evidence, not assertions. Command output below is
pasted verbatim from the runs described.

---

## 1. Summary

The foundation is in place and verified: validated configuration, versioned data
contracts, structured logging, an isolating event bus, an exhaustively-tested state
machine, a migrated database, a health/diagnostic system with a working
`tradefix doctor`, and a retention planner that cannot delete anything it must not.

| Metric | Value |
| --- | --- |
| Python files in package | 44 |
| Package lines | 8 488 |
| Test lines | 4 354 |
| **Tests passing** | **615 / 615** |
| **Statement + branch coverage** | **88 %** |
| `ruff check tradefix_radio tests` | clean |
| `mypy` (42 source files) | clean |
| Database tables created by migration | 21 (+ `alembic_version`) |
| Indexes created | 108 |

All nine Phase 1 milestones met their exit tests. Seven genuine defects were found
*by* those tests and fixed — listed in §5, because they are the most useful part of
this report.

---

## 2. Implemented

### 2.1 Core primitives — `tradefix_radio/core/`

| Module | Purpose |
| --- | --- |
| `errors.py` | Exception hierarchy rooted at `RadioError`, so supervisory code can catch *our* failures narrowly instead of using the blind `except` §86 forbids. `DependencyMissingError` carries `remediation`. |
| `clock.py` | `Clock` protocol, `SystemClock`, **`VirtualClock`**. The virtual clock is the prerequisite for §64: it makes a simulated week complete in seconds. Wake-ups are ordered `(deadline, insertion)` so endurance runs are reproducible, and a runaway zero-delay loop raises rather than hanging. |
| `ids.py` | Human-readable sortable track ids (`TF-20261002-00017`), matching the format §48 cites. |
| `events.py` | Async event bus (§91) — per-subscription worker task, bounded queues, three overflow policies, handler exceptions contained and counted. |
| `state_machine.py` | §92/§27 lifecycle as a single transition **table**, so the test suite can enumerate the complement. |

### 2.2 Contracts — `tradefix_radio/contracts/` (§90)

Nine modules, ~45 exported types, all `extra="forbid"` and `frozen=True`.
Three contract-level rules are enforced structurally rather than by convention:

* **A price cannot exist on a non-live `MarketStateV1`.** §32/§86 forbid presenting
  stale or simulated data as a current price; the model raises instead. There is
  no code path anywhere that can produce a misleading price.
* **Lyrics cannot exist without vocals.** Metadata that lied about a track's
  content would permanently poison the §12 topic-repetition horizons.
* **`UNKNOWN` regime cannot carry confidence above 0.5.** Confidence in "we don't
  know" is incoherent.

`MusicBlueprintV1.signature()` implements §11's "same blueprint: never repeat" by
hashing the *creative decision* (genre, BPM, key, topic, structure, persona) while
excluding incidentals (track id, seed, title, market context, priority).

### 2.3 Configuration — `tradefix_radio/config/` (§50, §71)

Layered: schema defaults → `defaults.yaml` → `<mode>.yaml` → operator file →
`.env` → `TRADEFIX_*` environment. 19 sections, ~190 validated fields.

`defaults.yaml` is deliberately near-empty: the schema holds the authoritative
default for every value, because restating them in YAML creates two sources of
truth that drift on the first upgrade.

**Cross-field invariants** are the valuable half. A config where
`minimum_buffer_minutes > target_buffer_minutes` type-checks perfectly and is
nonsense, so ten such rules are enforced, including: buffer ordering; crossfade
fitting inside the shortest track; the mastering loudness target sitting inside
the QC acceptance window; locked slots fitting inside the target buffer; and four
production guards (control token required, no simulated feed, no mock provider, no
empty OBS password).

Secrets use `SecretStr`; `masked_dump()` is the only sanctioned serialisation and
reports *that* a value is set plus its length, never any content.

### 2.4 Persistence — `tradefix_radio/persistence/` (§37, ADR-05, ADR-07)

21 tables implementing every §37 entity plus three additions the brief implies:
`state_transitions` (§27's audit trail, queryable), `track_titles` (§99 history
including rejected candidates), and `radio_memory` (§96 creative memory).

The schema's organising principle is ADR-07's split: **permanent metadata,
expendable bytes.** Audio is addressed only through `track_files`, so retention can
reclaim gigabytes while duplicate prevention keeps working forever.

Two portability types carry real weight:

* `UtcDateTime` — SQLite has no timezone type; a naive datetime leaking back out
  would corrupt session classification and every §12 horizon. Naive input is
  rejected outright rather than guessed at.
* `PortableJson` — canonical sorted-key JSON text, so blueprints and fingerprint
  vectors round-trip byte-identically on SQLite and PostgreSQL (ADR-05 bans
  `JSONB`).

SQLite pragmas (`WAL`, `foreign_keys=ON`, `busy_timeout`, `synchronous=NORMAL`) are
applied per connection — they are what makes the three-process layout (ADR-08)
workable on one file.

### 2.5 Observability — `tradefix_radio/monitoring/`

* `logging.py` — structlog, console + rotating JSONL, bound context via
  contextvars. Each service writes its own file so Windows rotation does not
  contend across processes.
* `health.py` — concurrent checks, per-check timeout, exceptions converted to
  CRITICAL results rather than propagating, optional components able to degrade
  but not block.
* `checks.py` — 11 concrete checks backing both §73 startup and §79 doctor, each
  with actionable remediation text.
* `resources.py` — CPU/RAM/disk/GPU/handle sampling for §49 and §64 leak detection.
  Unavailable measurements stay `None` rather than becoming a misleading `0`.

### 2.6 Storage — `tradefix_radio/storage/` (§36, ADR-07)

`plan_retention()` is a **pure function** over value objects: no I/O, no clock, no
database. That is deliberate — it is the most dangerous code in the phase, and
purity is what made it possible to test adversarial cases (disk pressure while the
queue is full) exhaustively without creating a file.

### 2.7 CLI — `tradefix_radio/cli/` (§78, §79)

`tradefix doctor | init | migrate | config | version`. `argparse` rather than a CLI
framework, because this is the one component that must run on a half-broken
installation to explain what is broken.

---

## 3. Evidence

### 3.1 Lint and type check

```
$ ruff check tradefix_radio tests --output-format concise
All checks passed!

$ mypy
Success: no issues found in 42 source files
```

### 3.2 Test suite

```
$ pytest tests/unit tests/integration --cov=tradefix_radio --cov-report=term
...
TOTAL                                                  3821    380    724    119    88%
615 passed, 1 warning in 38.37s
```

Per-module counts:

| Test module | Tests | Covers |
| --- | --- | --- |
| `unit/test_state_machine.py` | **210** | §92, §27 — full state product |
| `unit/test_config.py` | 83 | §50, §71 |
| `unit/test_contracts.py` | 80 | §90, §32, §11 |
| `integration/test_persistence.py` | 58 | §37, ADR-05 |
| `unit/test_retention.py` | 45 | §36, ADR-07 |
| `unit/test_health.py` | 42 | §35, §73, §79 |
| `unit/test_event_bus.py` | 27 | §91 |
| `unit/test_clock.py` | 24 | §64 prerequisite |
| `integration/test_cli.py` | 23 | §78, §79 |
| `unit/test_logging.py` | 16 | §55 |
| `integration/test_migrations.py` | 7 | §37 migrations |

### 3.3 Migration applies to a clean database

```
$ alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 6213f53625cc, initial schema

$ python -c "... sqlite_master ..."
22 tables: alembic_version, audio_fingerprints, generation_jobs, health_events,
lyrics, market_snapshots, market_states, metric_samples, play_events, queue_items,
radio_memory, setting_overrides, similarity_results, state_transitions,
station_ids, system_events, topics, track_blueprints, track_files, track_titles,
tracks, used_seeds
indexes: 108
```

`tests/integration/test_migrations.py` additionally asserts **no schema drift**
(Alembic autogenerate finds nothing structural left to do after `upgrade head`),
that `downgrade base` removes every table, that there is exactly one migration
head, and that every revision has a non-empty `downgrade`.

### 3.4 `tradefix doctor` on this machine

Verbatim, after `tradefix init`:

```
  TRADE FIX RADIO - doctor   (v0.1.0, mode=development)

  Python                OK             3.10.11 (D:\.Music\.venv\Scripts\python.exe)
  Node                  OK             v24.13.0 (optional)
  FFmpeg                OK             ffmpeg version 8.0.1-full_build-www.gyan.dev (optional)
  Configuration         OK             mode=development, feed=simulated, provider=mock, sink=null_sink
  Directories           OK             6 directories present and writable
  Database              OK             22 tables, 3.1 ms round trip
  GPU                   OK             NVIDIA GeForce RTX 3060: 8044 MB free of 12288 MB, 35% util, 53C (optional)
  ACE-Step toolchain    WARN           uv not found on PATH; no Python 3.11-3.12 interpreter detected (optional)
  Generation provider   OK             mock provider (synthetic audio, no GPU required) (optional)
  Market Feed           OK             simulated feed (no external dependency) (optional)
  OBS WebSocket         OK             disabled in configuration (optional)
  Audio Device          OK             sink=null_sink (no device needed) (optional)
  Disk Space            OK             207.6 GB free of 511.4 GB on D:

  Overall: WARN

doctor exit: 0
```

The single WARN is the expected, pre-recorded Phase 0 gap (no `uv`, no Python 3.12)
and is correctly classified **optional** in development, so the exit code is 0 — a
CI smoke test using `doctor` is not broken by it. Before `init`, the same command
exits **1** and names `directories` as a blocking failure with the remediation
`Run 'tradefix init'`.

---

## 4. Milestone exit tests

| # | Milestone | Exit test | Result |
| --- | --- | --- | --- |
| 1.1 | Scaffold, pyproject, ruff/mypy, venv | both clean | ✅ |
| 1.2 | Config, layering, loud validation | invalid field names its own path; all errors at once | ✅ 83 tests |
| 1.3 | Versioned contracts | JSON round-trip per type; extras rejected | ✅ 80 tests |
| 1.4 | Structured logging | §55 keys present; rotation triggers | ✅ 16 tests |
| 1.5 | Event bus | raising subscriber isolated; bounded memory | ✅ 27 tests |
| 1.6 | Persistence + migrations | `upgrade head` on fresh DB; CRUD round-trip; no drift | ✅ 65 tests |
| 1.7 | State machine | **exhaustive** over the state product | ✅ 210 tests |
| 1.8 | Health registry + doctor | §79 table with real pass/fail; non-zero when blocking | ✅ 65 tests |
| 1.9 | Retention-ready layout | correct candidates; **never** queued/emergency/unplayed | ✅ 45 tests |

Milestone 1.7's exit criterion deserves a note. The transition table has
14 states → 196 ordered pairs. The suite asserts that each of the 29 declared edges
is accepted and that **all 167 remaining pairs raise** `IllegalTransitionError`
naming the allowed set. It further proves four graph properties rather than
individual edges:

* no state transitions to itself;
* `REJECTED` and `QUARANTINED` can never *reach* a playable state by any path —
  §22's "never place a rejected track into radio queue", verified as reachability
  so no future edge can open a back door;
* every non-terminal state can reach a terminal state (no slot can leak forever);
* `PLAYING → FAILED` exists and `FAILED` cannot reach `PLAYED`, so §75's rule
  against recording an incomplete airing as played is structural.

---

## 5. Defects found by the tests and fixed

This is the part worth reading. Each of these was a real bug that would have
surfaced later and more expensively.

| # | Defect | Why it mattered | Fix |
| --- | --- | --- | --- |
| 1 | **Contracts could not round-trip their own JSON.** `model_dump()` emits computed fields (`mid`, `spread`, `is_instrumental`); `extra="forbid"` then rejected them on re-validation. | `TrackRepository.get_blueprint()` does exactly `model_validate(stored_payload)`. **Every blueprint read would have raised at runtime** — as would every WebSocket round trip. | `Contract` gained a `before` validator that strips computed-field keys on input, keeping strict typo rejection while making serialise/deserialise an identity. |
| 2 | **Two-word musical modes were rejected.** The key validator split on all whitespace, so `"A harmonic minor"` failed. | `harmonic minor` is in the configured mode list, so the director could legitimately produce a key the contract refused — an intermittent crash in the generation path. | Split on the first space only. |
| 3 | **Tracebacks were missing from the durable log.** The JSONL file recorded `"exc_info": true` instead of the formatted traceback (the console happened to show it). | The JSONL file is the record for a week of unattended operation. §55 lists `error`; §86 forbids suppressing errors. A logged failure with no traceback is barely a log. | Added `structlog.processors.format_exc_info` before the JSON renderer. |
| 4 | **`TRADEFIX_ROOT` broke configuration entirely.** It shares the `TRADEFIX_` prefix, so the env nester turned it into an unknown `root` setting and the whole config was rejected. | `.env.example` and the setup docs tell operators to set this variable. Following the documentation would have made the station refuse to start. | Added `RESERVED_ENV_VARS`, and translated the variable into a proper `paths.root_dir` value at the environment layer so the loader's injected `environ` is honoured consistently. |
| 5 | **`tradefix migrate` broke whenever data was relocated.** `alembic.ini` was located via `paths.root_dir` — the *data* root — but migrations ship with the *code*. | Any operator pointing `TRADEFIX_ROOT` at a separate data volume (the documented pattern) could not migrate. An installed non-editable package has no `alembic.ini` at all. | The CLI now builds the Alembic `Config` programmatically against `MIGRATIONS_DIR`, derived from the package. |
| 6 | **Read sessions returned unusable objects.** `rollback()` expires every loaded instance, so any ORM object used after the `async with` raised `DetachedInstanceError`. | Every API GET route would have had to consume results inside the context manager — a footgun failing at runtime, far from its cause. | `read_session` now calls `expunge_all()` before the rollback, handing back detached but fully-loaded read-only snapshots. |
| 7 | **`alembic.ini` lacked `path_separator`.** Alembic 1.20+ falls back to splitting `prepend_sys_path` on spaces, commas **and colons** — which mangles `D:\.Music` — and warns on every invocation. | A Windows-first product splitting paths on colons is a latent breakage, not just noise. | Added `path_separator = os`. |

Two smaller fixes: a `PathSettings` validator returned a modified copy (pydantic v2
silently ignores this when built via `__init__`), now a `before` validator; and
`RadioMemoryRepository.delete` did not flush, so a second delete reported success
twice.

---

## 6. Architecture decisions made during Phase 1

These extend, and do not contradict, the Phase 0 ADRs.

1. **`paths.root_dir` is the data root, not the code root.** Established while
   fixing defect 5. Code-relative resources (migrations, shipped YAML) are located
   from the package; everything operator-relocatable hangs off `paths.*`.
2. **A relative SQLite path is anchored to `paths.data_dir`, never the CWD.** With
   three processes (ADR-08) launched from different directories, a CWD-relative DSN
   would give each its own database and the station would appear to lose its queue.
3. **`sink: "null_sink"`, not `"null"`.** Bare `null` in YAML parses as `None`, so a
   config saying `sink: null` would fail validation with a confusing message.
4. **Settings are passed explicitly; there is no global singleton.** A module-level
   settings object would be mutated by tests and would imply shared state the three
   processes do not have.
5. **Repositories never commit.** The caller owns the transaction, because one
   logical operation ("generation finished") spans several repositories and must be
   atomic. A repository that opened its own session would leave half-written tracks
   behind on failure.
6. **One state machine for both §27 and §92.** They are the same lifecycle seen
   from two angles; modelling them separately would mean two sources of truth about
   where a track is, and inevitable drift.
7. **`tzdata` is a hard dependency.** Windows ships no system tz database, so
   `zoneinfo` has nothing to read — and §2.6 session classification needs
   `Europe/London` and `America/New_York` DST boundaries.

---

## 7. Known limitations

| Limitation | Assessment |
| --- | --- |
| **One `ResourceWarning` remains in the suite summary**: `unclosed event loop <ProactorEventLoop>`. | A pytest-asyncio artifact, not station code. On Windows the Proactor loop's self-pipe is a `socket.socketpair()`, finalised by the GC at an arbitrary later point; pytest's unraisable plugin then attributed it to an unrelated test, making pass/fail depend on allocation timing. A session fixture now closes stray loops and `ResourceWarning` is reported rather than fatal, with the reasoning recorded in `pyproject.toml`. **No station code opens a socket**; every engine, session, subprocess and file handle we own is closed through a context manager or explicit disconnect, asserted by the persistence and CLI suites. To be revisited in Phases 7–8, which add code that *does* own sockets. |
| Coverage is 88 %, not higher. | The uncovered remainder is concentrated in `resources.py` (21 %), `storage/paths.py` (45 %) and `checks.py` (65 %) — code whose branches require real GPU/audio/MT5/OBS hardware states. Those paths are exercised in Phases 7–8 against the real dependencies; faking them now would test the mock. |
| PostgreSQL is untested on this host. | No server installed (Phase 0). The schema avoids PG-only types by design (ADR-05) and `test_migrations.py` is engine-agnostic, but the claim "runs on PostgreSQL" is **unverified**, not proven. A `postgres`-marked test will run it when a server is available. |
| No market data, no music, no API, no UI yet. | Correct for Phase 1. §82 explicitly orders the build this way. |
| `apps/api`, `apps/worker`, `apps/playout` are not yet created. | Deferred to Phases 4–5 where they have something to host. The interfaces they will use (event bus, repositories, settings, clock) are in place. |

---

## 8. Performance observations

Not yet benchmarked (§80 is Phase 7), but two measurements taken incidentally:

* Full 615-test suite: **38 s**, including 11 migration runs against real SQLite.
* Database round trip (`SELECT 1`, WAL, local SSD): **3.1 ms**.
* `VirtualClock` executes **3 600 simulated one-second ticks in well under a
  second** of real time (`test_run_for_executes_a_long_simulated_span_quickly`),
  which is the budget §64's 7-day run depends on.

---

## 9. Next step

**Phase 2 — Market engine.** `MarketSimulationEngine` with all 15 §7 scenarios, the
20-feature engine, rolling-percentile normalisation (§6), the energy triple, and
the regime classifier with hysteresis, confirmation, minimum duration and cooldown
(§5).

The exit test that matters most is the anti-flicker one: adversarial oscillating
input must not produce `RANGE → BREAKOUT → RANGE → BREAKOUT`. The second is the
affine-invariance property — multiplying every price by ten must produce identical
energy, proving §6's "do not make assumptions about absolute XAUUSD price values".
