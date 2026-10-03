# Phase 7 — ACE-Step 1.5 integration

**Baseline:** `aa3507a` (Phase 6)
**Scope:** §7.1–§7.32
**Status:** complete. The full path works end to end; the limitations are in §8.

---

## 1. What was asked, and what the answer is

> MUSIC BLUEPRINT → ACE-STEP → RAW TRACK → PHASE 6 PIPELINE → APPROVED MASTER → RADIO QUEUE

That path runs. Real market states produce real blueprints, the real model renders them, and
the output goes through every Phase 6 stage unmodified before anything reaches the queue.

§7's structural requirement — *"ACE-Step must become one implementation of the existing
generation-provider interface, not a special case spread through the codebase"* — is
checkable rather than asserted. Outside `tradefix_radio/generation/ace_step/`, the only
modules that mention ACE-Step are the provider factory, the config schema, and two `doctor`
checks. No station, queue, scheduler, playout or Phase 6 code knows the model exists.

---

## 2. Environment and installation

Full audit: [`PHASE_7_ENVIRONMENT.md`](PHASE_7_ENVIRONMENT.md), recorded **before** anything
was installed.

| | |
|---|---|
| OS | Windows 11 Pro 10.0.26200 |
| CPU / RAM | AMD Ryzen 7 PRO 5750G, 8C/16T, 31.8 GB |
| GPU | **NVIDIA GeForce RTX 3060, 12 GB**, driver 610.47, CC 8.6 |
| CUDA | 12.8 |
| ACE-Step | `ace-step/ACE-Step-1.5`, **commit `ca1e85fe9430`**, package version **1.5.0**, MIT |
| ACE-Step Python | **3.12.15**, provisioned by `uv` into `D:\ace-step\.venv` |
| ACE-Step torch | **2.7.1+cu128**, CUDA available |
| Station Python | **3.10.11**, untouched |
| Models | `acestep-v15-turbo` (4.79 GB) + `acestep-5Hz-lm-0.6B` (1.37 GB) = **6.2 GB** |

### The architecture decision (§7.3)

ACE-Step declares `requires-python = ">=3.11,<3.13"`. ADR-01 pins this project to **3.10**
for librosa's numba wheels, which all of Phase 6 rests on. The ranges are mutually exclusive.

| option | verdict |
|---|---|
| A — same environment | **Impossible.** Not a judgement call; a hard version-range exclusion. |
| B — dedicated venv, imported in-process | **Impossible for the same reason.** A 3.12 package cannot be imported into a 3.10 interpreter. |
| **C — separate local process** | **Chosen.** Own interpreter, own directory, REST over loopback. |

Option C is independently what §7.5 requires: a model that deadlocks cannot take the playout
engine with it if it is not in the same process. ACE-Step lives at `D:\ace-step`, outside the
repository, so the station's tree never holds a 2.5 GB torch install or a 6 GB checkpoint.

### The LM backend, measured rather than assumed

ACE-Step's hardware table recommends the `vllm` backend for the 8–16 GB tier. On this host it
loaded with the **PyTorch backend** instead (`5Hz LM initialized successfully using PyTorch`).
That is the documented fallback for the 6–8 GB tier and it works; it is recorded because the
table's recommendation and this machine's behaviour differ, and a future reader should not be
surprised.

---

## 3. Three protocol defects found by reading the wire

§7.2 says *"Do not rely on stale commands remembered from previous versions."* The stronger
lesson was that the project's **own current documentation** differs from its running server,
and every difference fails silently.

### 3.1 `/query_result` reads `task_id_list`, not `task_ids`

The handler reads `body.get("task_id_list", "[]")`. A request using the documented
`task_ids` parses as an empty list and returns `data: []` with **HTTP 200** — byte-identical
to "still running".

Symptom: the first real generation completed on the GPU at 02:28:51 and the station polled it
until the timeout. Nothing errored, nothing logged, the audio existed on disk the whole time.

### 3.2 `result` is a JSON-encoded string, not an object

It holds a *list*, one record per `batch_size`, and the per-item `status` is the real one. A
client expecting the documented flat object never finds a status at all.

### 3.3 `GenerateMusicRequest` has no `instrumental` field

The station was sending `instrumental: true` alongside the `"[Instrumental]"` marker, on
belt-and-braces reasoning. Checked against the running server's own schema: no such field,
and pydantic's default policy is to ignore unknown keys. The flag did **nothing** while
reading like a second line of defence — worse than no enforcement, because it stops anyone
looking for the real mechanism.

The marker is the real mechanism, which the server's docstring names. A test now asserts the
field is **absent** from every submission.

> The fake server in `tests/fake_ace_step.py` was corrected to the real wire format. A fake
> implementing the documentation would have passed all 24 protocol tests while production
> hung — which is exactly what happened for one iteration.

---

## 4. One Phase 6 contract defect, exposed by real audio

The brief permitted modifying Phase 6 only if *"real ACE-Step output exposes a genuine
contract defect."* It exposed one.

**A clean, mastered UK drill track was rejected as "50 impossible sample steps; the file is
probably corrupt."** It was not corrupt: 60 s, peak 0.89, no clipping, valid loudness.

The discontinuity detector had an absolute floor of 0.5 amplitude between consecutive
samples. At 48 kHz a sine of amplitude *A* at frequency *f* steps by `A·2πf/fs`, so a loud
component at just **5 kHz reaches 0.58**. Hi-hats and transients live well above 5 kHz. The
Phase 6 fixtures that justified the floor were all built from low sine partials and could
never reach it, so nothing caught this until real music arrived.

Measured across real output and the existing fixtures:

| material | max \|Δ\| | hits at 0.5 | hits at peak |
|---|---|---|---|
| ACE-Step master, UK drill 142 BPM | 0.666 | **50 (FAIL)** | 0 |
| ACE-Step master, DnB 174 BPM | 0.607 | 18 | 0 |
| ACE-Step master, trap 148 BPM | 0.713 | 1 | 0 |
| `clicking` fixture, 64 injected clicks | 1.248 | 114 | **113** |
| `musical` fixture, clean | 0.057 | 0 | 0 |
| `noise` fixture, broadband | 1.243 | 0 | 0 |

The floor is now **the track's own peak**, which separates real music from real clicks with a
wide margin and has a physical reading: exceeding the peak in one step requires full-scale
content above `fs/2π` (7.6 kHz at 48 kHz), which mastered music does not have, while a click
— a sample flipped to the opposite rail — steps about twice the peak.

Two regression tests were added: a deliberately bright 48 kHz fixture whose steps exceed 0.5
must pass, and the clicking fixture must still fail. The threshold was **raised, not removed**.

---

## 5. Measured performance

### Capacity (§7.21)

Twenty consecutive 60-second generations, `balanced` profile, RTX 3060:

| | |
|---|---|
| Succeeded | **20 / 20**, zero failures |
| Cold load (checkpoint on disk) | **57.9 s** |
| First-ever load incl. 6.2 GB download | 1229 s |
| Latency min / p50 / p95 / max | 44.2 / **51.4** / 56.0 / 61.0 s |
| **Realtime factor, p50** | **1.17×** |
| Seven-track run, 60 s tracks | 41.6–49.0 s, p50 45.9 s → **1.31×** |

**Can one GPU keep a 45-minute buffer full?** At 1.17× it produces 70 s of music per minute
of wall clock, so it generates faster than it broadcasts — but only by 17 %. A 45-minute
buffer takes ~38 minutes of uninterrupted generation to build from empty and has **17 % headroom**
to absorb rejections. With the 29 % rejection rate observed in §6 below, the margin is
effectively consumed. This is the phase's main operational finding and §8 records it.

Notably, generation time is **weakly dependent on track length**: 45-second tracks took 50–55 s
while 60-second tracks took 42–49 s. The LM planning phase dominates and is roughly
duration-independent, so longer tracks are *more* efficient per second of audio.

### VRAM (§7.7)

| | |
|---|---|
| Card total | 12 288 MB |
| Used by desktop alone | ~4 700 MB |
| Used with model resident, idle | 9 334 MB (**2 782 MB free**) |
| Peak during generation | 10 565 – 11 525 MB |
| **Attributable to one generation** | **1 077 – 1 170 MB** |

This inverted a configured default. `min_free_vram_mb` was **5200**, an ADR-04 placeholder
explicitly marked for revision after this benchmark — and it would have **refused every job on
the hardware it was written for**, since only 2.8 GB is free with the model loaded. It is now
**1800 MB**: measured demand plus ~50 % margin.

### Long-run stability (§7.23)

| | first | last | drift |
|---|---|---|---|
| VRAM used after generation | 9 638 MB | 9 608 MB | **−30 MB** |
| ACE-Step process RSS | 5 012 MB | 5 116 MB | +105 MB (**5.2 MB/track**) |
| Mean latency, 1st half vs 2nd | 53.0 s | 49.3 s | **−3.7 s** |

**No leak.** VRAM is net negative. RSS rose ~200 MB over the first four generations and then
plateaued — allocator warm-up, not accumulation; it is flat from run 4 to run 20. Latency
*improved* across the run, which rules out progressive degradation.

---

## 6. The seven representative tracks (§7.14)

60 s each, `balanced`, each through the complete Phase 6 pipeline.

| # | configuration | gen | tempo req→meas | key req→meas | raw QC | novelty | verdict |
|---|---|---|---|---|---|---|---|
| 1 | quiet lo-fi instrumental | 44.5 s | 72 → 143.6 | C major → **C major** | warn | 1.00 | **approved** |
| 2 | deep house instrumental | 49.0 s | 122 → **123.0** | A minor → **A minor** | warn | 0.54 | **approved** |
| 3 | high-energy trap instrumental | 46.7 s | 148 → 76.0 (½) | F# minor → **F# minor** | pass | 0.50 | **approved** |
| 4 | rap, Trade Fix lyrics | 45.2 s | 142 → **143.6** | G minor → **G minor** | pass | 0.34 | rejected¹ |
| 5 | educational trading rap | 41.6 s | 92 → **89.1** | D minor → A major | pass | 0.19 | rejected² |
| 6 | high-volatility DnB | 47.2 s | 174 → **172.3** | E minor → **E minor** | pass | 0.43 | **approved** |
| 7 | lower-energy R&B | 45.8 s | 88 → 117.5 | B minor → **B minor** | warn | 0.37 | **approved** |

¹ final QC, "50 impossible sample steps" — **the Phase 6 defect in §4**. Fixed; this track
would pass now.
² originality: 0.81 similarity to #2, above the 0.72 review threshold. With
`regenerate_on_review` the station regenerates rather than airing it. Working as designed.

**Key adherence is the standout: 6 of 7 exact.** Tempo matched or octave-matched on 5 of 7.
Approval rate 5/7 before the §4 fix, 6/7 after.

### Blueprint adherence (§7.16)

Measured honestly, and the report contains **no genre score and no single adherence number** —
there is no genre classifier in this project, and §7.16 forbids inventing one. Tests assert
their absence. Vocal presence on a *vocal* track is reported `UNMEASURED`: the absence of a
vocal-shaped spectrum does not establish the absence of vocals.

---

## 7. The market-energy test (§7.17)

The phase's critical test, and the project's founding claim. Four market conditions, driven
through the **real** `MusicDirector` (not hand-written blueprints), two tracks each, measured:

| condition | blueprint energy | tempo | **LUFS** | **RMS** | **centroid** | flux |
|---|---|---|---|---|---|---|
| QUIET | 0.08 | 118.8 | **−16.73** | **0.1291** | **1 079 Hz** | 0.0696 |
| NORMAL_TREND | 0.40 | 101.4 | −15.85 | 0.1363 | 1 858 Hz | 0.1072 |
| BREAKOUT | 0.77 | 140.6 | −14.73 | 0.1490 | 2 540 Hz | 0.0789 |
| EXTREME_VOL | 0.96 | 142.3 | **−13.69** | **0.1609** | 2 494 Hz | 0.0841 |

**Loudness and RMS are strictly monotonic in market energy**, across a 3.0 LU span.
**Spectral centroid rises 2.3×** from quiet to breakout. The director chose lo-fi and jazzhop
at 75–80 BPM for QUIET and DnB at 177 BPM for EXTREME_VOLATILITY, unprompted.

Measured *tempo* is not monotonic (QUIET averages 118.8 against NORMAL_TREND's 101.4) — but
the **requested** tempos are, and the discrepancy is beat-tracker octave error on one QUIET
track measured at double time. Reported as it is rather than corrected, because the honest
statement is "loudness and brightness track market energy; measured tempo is confounded by
estimator octave errors."

**The market measurably composes the radio.**

---

## 8. Acceptance gates (§7.32)

| gate | result | evidence |
|---|---|---|
| **A** ACE-Step loads on target GPU | **pass** | 57.9 s warm load, `READY`, healthcheck green |
| **B** instrumental generation | **pass** | verified across 3 seeds; files decoded, not assumed |
| **C** lyric/vocal generation | **pass** | real composed lyrics, `lyrics_modified: false` |
| **D** seven configurations tested | **pass** | §6 table |
| **E** real output passes Phase 6 | **pass** | every stage runs; verdict is QC's, not the provider's |
| **F** rejected output cannot reach READY | **pass** | Phase 6's station boundary, unchanged and retested |
| **G** survives process failure | **pass** | `ProviderUnavailableError`, provider recovers without restart |
| **H** OOM handled without playout failure | **pass** | classified `GpuOutOfMemoryError`, counted, provider stays healthy |
| **I** market changes output | **pass** | §7 |
| **J** capacity measured | **pass** | 1.17× p50, 1.31× on the 60 s set |
| **K** no unacceptable memory leak | **pass** | VRAM −30 MB, RSS flat after warm-up, latency improving |
| **L** Generation Lab shows real state | **pass** | `/api/generation/provider` + provider panel |
| **M** existing tests green | **pass** | full non-GPU suite exit 0 |
| **N** ruff / mypy clean | **pass** | 136 source files, both clean |

---

## 9. Model limitations, measured

### Same-seed generation is **not** deterministic

§7.13: *"Do not assume deterministic behavior without measuring it."* Measured — two
generations with identical seed, caption and parameters produced **different audio**.

Consequences, stated rather than worked around:

* **§23's seed registry still works.** It prevents *intentional* reuse, which is all it ever
  claimed; §21's documentation already says seeds are metadata, not proof of uniqueness.
* **A different seed does produce different audio**, which is asserted and is the property the
  station actually depends on.
* **Exact reproduction from stored metadata is not available.** The stored prompt, seed and
  settings reproduce the *intent*, not the waveform. The Originality page should not imply
  otherwise.

### Other limits

* **No cancellation endpoint.** `cancel()` stops the station waiting; the GPU finishes the
  track. Reported honestly, and the abandoned task is remembered so the next job does not
  assume a free card.
* **Progress is coarse.** ACE-Step reports 0.1 for the whole LM phase, then 1.0. Real, but a
  bar would claim 10 % for most of a 50-second generation, so the UI shows **elapsed seconds
  and an indeterminate indicator** (§7.25).
* **Duration is approximate but close.** All seven tracks came back within 0.1 s of request.
  Not faked as exact; Phase 6's tolerance governs.
* **~2.8 GB free VRAM with a desktop running.** The integration fits, with ~1.6 GB of margin
  above measured demand. Another GPU application would break it.

---

## 10. The operational finding worth acting on

**Capacity headroom is thin.** 1.17× realtime generation against a 29 % observed rejection
rate (2 of 7, one of which was the now-fixed Phase 6 defect) leaves very little margin for
keeping a 45-minute buffer full. The arithmetic:

* generation produces 70 s of audio per wall-clock minute;
* broadcast consumes 60 s;
* a 1-in-7 rejection removes ~10 s of that surplus.

It works, and it will not work with much less margin. Three levers exist and none needs new
architecture: the `fast` profile (4 steps rather than 8), longer tracks (generation cost is
weakly duration-dependent, so a 3-minute track is far cheaper per second than a 1-minute
one), and §7.20's buffer-aware profile ladder, which is implemented and wired.

This is a measurement, not a blocker — but it is the number to watch in Phase 10's endurance
runs.

---

## 11. What was deliberately not done

Per §IMPORTANT: no LoRA training, no OBS, no distributed workers, and no attempt to craft the
Trade Fix sound. Phase 7 proved correctness, stability, throughput and integration. The five
approved tracks in `artifacts/phase7/acceptance/` are **engineering artefacts**, not a
playlist, and the manifest says so in its first field.

---

## 12. Phase 8 dependencies

Nothing in Phase 7 blocks OBS integration. Two things it should know about:

1. **Generation holds the GPU for ~50 s at a time**, peaking at 11.5 GB of 12 GB. OBS
   hardware encoding on the same card will contend. NVENC is a separate engine from CUDA
   compute, so this is likely fine, but it needs measuring before it is assumed.
2. **`/api/generation/provider` already exposes everything an overlay would want** — model,
   state, VRAM, current track, latencies — so Phase 8 needs no new backend work for a
   "now generating" indicator.
