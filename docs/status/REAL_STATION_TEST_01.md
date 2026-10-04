# REAL_STATION_TEST_01

A human validation run of the actual product: real ACE-Step generation, real Phase 6
pipeline, real Phase 4 scheduler and playout, real Windows audio output, and the market
routing added immediately before this run.

**Headline.** The station works end to end and never went silent. It also cannot currently
sustain music: it approves **7% of what it generates** and spends most of its airtime on the
procedural fallback. The operator heard that directly — *"one song was being repeated for a
long time"* — and it is the correct audible symptom of the finding below. Four router
tests, including the mandatory feed-outage test, passed on the running station.

---

## Run metadata

| | |
|---|---|
| Routing commit | **`c8bea67`** — `feat: add automatic XAUUSD to BTCUSD market routing` |
| Prerequisite commit | `60d92d1` — station entry point + Windows device resolution |
| Date | 2026-10-03 (Saturday — gold closed, which this run exploited) |
| Host | Windows 11 26200, AMD64 Family 25 (Zen 3) |
| GPU | NVIDIA GeForce RTX 3060, 12 288 MiB |
| VRAM during run | 9 595 MiB used / 2 413 MiB free; peak attributed 10 814 MiB |
| Audio device | **[24] Headphones (C15B) — Windows WASAPI**, 44 100 Hz stereo |
| ACE-Step | `acestep-v15-turbo` + `acestep-5Hz-lm-0.6B`, API on 127.0.0.1:8001 |
| Profile | `balanced` |
| Entry point | `tradefix station start --provider ace_step --sink sounddevice --device "Headphones (C15B)" --host-api WASAPI --test-mode` |
| Station runtime | ~75 min across four process lifetimes (restarts were to load fixes) |
| Listening time | ~45 min |

Four restarts happened during the run, each to pick up a fix the run itself exposed. Counts
below are cumulative across the day's database unless stated otherwise.

---

## 1. Pre-flight

`tradefix doctor` — one blocking failure, one accepted warning.

| Check | Result |
|---|---|
| Python 3.10.11 · Node · FFmpeg 8.0.1 | OK |
| Audio analysis (librosa 0.11.0 + BS.1770) | OK |
| **Fingerprinting** | **WARN** — `fpcalc` absent; built-in chroma fingerprint used |
| Configuration | OK |
| **Directories** | **FAIL → fixed** — `D:\.Music\emergency` missing; `tradefix init` created it |
| Database (27 tables) · Migrations (`161f873f4932`) | OK |
| GPU · ACE-Step toolchain · models (6.2 GB) | OK |
| **Market routing** | OK — primary XAUUSD, fallback BTCUSD, 120/300/180 s |
| Market feed · OBS (disabled) · Audio device · Disk (175 GB) | OK |

Two checks the brief asked for did not exist and were added: **Migrations** (head vs applied
revision) and **Market routing** (symbols, fallback presence, and a guard that the reopen
window is not shorter than the close window). The single "Market Feed" line stopped being a
complete answer once the station could programme against more than one market.

The fingerprinting warning was accepted: originality still runs, with weaker near-duplicate
discrimination. It is relevant to the capacity finding and is revisited there.

## 2–3. Audio output

`tradefix audio devices` resolved the device table. **48 kHz is not available on this
hardware** — WASAPI on the C15B accepts 44 100 Hz stereo only (88.2/96 kHz also refused).
That is a device limitation, not a station defect: masters are still produced at 48 kHz and
ADR-14 has playout conform to the sink.

Smoke test at 44 100 Hz stereo, −18 dBFS: open → resolved `(24, 'Headphones (C15B)',
'Windows WASAPI')` → 52 920 frames written in 1.09 s (device-clock paced) → close. No
exceptions. Three ascending beeps at −16 dBFS were **confirmed audible by the operator**.

Separately: the shipped default `audio.device_name: "CABLE Input"` **matches nothing on this
machine** — VB-Audio enumerates as "CABLE In 16 Ch". Recorded as a bug against the Phase 8
OBS path.

## 4–6. Test mode and startup

`--test-mode` lowered the buffer targets to **1.5 / 12 / 25 min** by `model_copy`; no file on
disk changed. Two gaps were closed before the run could proceed honestly:

* `station start` had no way to select the provider (development ships `mock`). Added
  `--provider`.
* TEST MODE was terminal-only. It now travels on `AppSettings.test_mode` →
  `StationStatusV1.test_mode` → a persistent **TEST MODE** chip in the Control Center header,
  beside the simulation badge. It gates nothing: QC, originality, mastering, lock rules and
  retry policy are unreachable from it.

---

## MARKETS

### Startup state — exactly as specified

```
ACTIVE MARKET : BTCUSD
XAUUSD        : closed   degraded=false  "outside the trading week for this instrument"
BTCUSD        : open     degraded=false  "fresh data is arriving"
switches      : 0
```

The station reached this with no intervention: the real calendar closed gold, the router
selected the fallback, and the startup acquisition was correctly **not** counted as a switch.
The Control Center header showed `BTCUSD` with the `Fallback market` chip.

Note on fidelity: the *calendar* is real; the *feeds* are simulated (development mode), and
the simulation badge is displayed throughout. Prices are withheld (`None`) under §21.

### Router tests

| Test | Result | Evidence |
|---|---|---|
| **16 — closed → fallback** | **PASS** | 180 s dwell held with no pending; then pending=BTCUSD at **110 s remaining** (the 120 s close window); switched at t+260 s; `switches=2`; `silence=0.0` |
| **17 — reopen** | **PASS** | dwell held ~180 s; pending=XAUUSD at **295 s** (the 300 s reopen window, correctly the longer one); one fresh tick did not switch back; switched at t+401 s; playback uninterrupted |
| **18 — feed down, not closed** | **PASS** | see below |
| **19 — no active market** | **PASS** | 120 s countdown → `NO_ACTIVE_MARKET`; `market=NONE` to the director; `broadcasting=True`, `silence=0.0`; restore was **immediate** (acquire-from-nothing), `switches=1` |

Both confirmation windows were observed at their real configured lengths and the asymmetry
(leave 120 s, return 300 s) was visible in both directions.

### Test 18 — the one that matters

XAUUSD forced open, feed stopped, market **not** closed:

```
t+ 91s  XAUUSD=stale        degraded=True   "data is 90s old"
t+301s  XAUUSD=unavailable  degraded=True   "no data for 301s while the market should be trading"

states observed : ['open', 'stale', 'unavailable']
CLOSED reported : False
final active    : XAUUSD          ← did not route to BTCUSD
broadcasting    : True, silence 0.0 throughout
```

A dead feed was never read as a closure and never authorised the fallback. Feed restored →
`open`, `degraded=false`.

**This test found a defect in the simulator before it could validate anything.** See
CRITICAL-adjacent finding B1.

### Feed degradation and switch events

* `market.active_symbol_changed` fired with `reason=primary_market_closed` /
  `primary_market_reopened` and both previous/new states.
* `feed_degraded` was raised on STALE and UNAVAILABLE only; a closed market never set it.
* `NO_ACTIVE_MARKET` events: 1 (test 19, deliberate).

---

## MUSIC

| | |
|---|---|
| Generated (ACE-Step jobs) | 142 tracks, 22 510 s of audio |
| **Approved** | **10 tracks, 1 590 s** |
| Rejected | 62 (`audio_near_duplicate`) |
| Review-unresolved | 61 (discarded — see B2) |
| Quarantined / QC defects | 9 (mid-track silent gaps) |
| Played | 15 (incl. repeats of the 10 approved) |
| **Approval rate** | **7.0 %** |
| Vocals actually produced | **0** (see B3) |
| Instrumentals | all of them |

Raw QC: 75 pass · 58 warn · 9 fail. Mastered QC: 8 pass · 2 warn · 0 fail. Phase 6 was
**not** bypassed — every approved track has both a `raw` and a `mastered` QC row, 65+ check
rows, features, fingerprint, similarity and mastering records.

First real station-generated track, full chain observed:

> **TF-20261003-00007 — "Thin Momentum in the Closed Book"**
> BTCUSD · `normal_range` · market energy 17.70 · lofi · 87 BPM · B minor · 155 s ·
> instrumental · profile `balanced` · raw QC **warn** (21/22: *"76% of energy is below
> 120 Hz; the low end dominates"*) · mastered QC **warn** (20/21) · peak 0.891 · clipping
> 0.0000 % · DC +0.0018 · RMS 0.1532 → **READY** → aired on tier `scheduled`.

The queue behind it was entirely BTCUSD at 84–92 BPM (lofi / jazzhop / downtempo),
consistent with BTCUSD energy 17.7. Energy→genre mapping is working, and every queue row
carried `planned_symbol = BTCUSD`.

---

## CAPACITY — the blocking finding

| Measure | Value |
|---|---|
| Generation p50 | **66.1 s** (54–58 s earlier in the run) |
| Generation p95 | 68.9 s |
| Model load | 51–103 s (once per process) |
| Failures / OOM | **0 / 0** |
| Raw capacity | **≈ 2.9×** (audio produced ÷ GPU wall time) |
| **Effective approved capacity** | **≈ 0.24×** |
| Mean max-similarity | **0.816** (review > 0.72, reject > 0.84) |
| Mean novelty | 0.184 |

The station generates audio nearly three times faster than it plays it, then discards 93% of
it as near-duplicates of its own earlier output — so it consumes roughly four times what it
keeps. The buffer never rose above zero ready minutes after the first track, and the station
lived on the §33 procedural tier.

This is a far better *raw* number than Phase 7's 1.17× and a far worse *effective* one. The
gap is entirely originality rejection, and it splits in two:

* **61 `review_unresolved` (43%)** — not a quality judgement. The pipeline can emit three
  verdicts and the station implements two dispositions, so REVIEW is silently equivalent to
  REJECT (B2).
* **62 `audio_near_duplicate` (44%)** — genuine similarity. One turbo model asked repeatedly
  for lofi/downtempo at 84–92 BPM under a single quiet regime produces genuinely alike
  material, and the weaker built-in chroma fingerprint (no `fpcalc`) is doing the
  discriminating.

Because the buffer never filled, **steps 10, 12, 14, 15 and 21 could not be run** — regime
programming comparison, instrumental energy contrast, the 60/180/240 s duration experiment,
fast-vs-balanced, and transition review all need a supply of approved music to listen to.
They are not failures; they are blocked on the approval rate and should be re-run after it is
addressed. No fast/balanced or per-duration numbers are reported, because none were measured.

### Buffer

| | |
|---|---|
| Start | 0.00 min |
| Maximum | ~2.6 min (one approved track, briefly) |
| Minimum | 0.00 min |
| End | 0.00 min |
| Trend | nominally "filling", actually flat at zero |

## AUDIO

| | |
|---|---|
| Unintended silence | **0.0 s** |
| Underruns | **0** |
| Audio coverage | **98.2 %** (0.9918 peak) |
| Sink failures | 0 |
| Transition failures | 0 |

The continuity guarantee held completely. Every gap in real music was covered by the
procedural tier rather than by silence.

## QUALITY — human ratings

Persisted to the new `operator_feedback` table, which nothing on the generation path reads.

| Track | Verdict | Reason | Note |
|---|---|---|---|
| TF-…00007 | **good** | — | "sounds like a station" |
| TF-…00010 | **good** | excellent | overall session verdict |
| TF-…00010 | **bad** | bad_vocals | planned vocal, audibly none |

Operator's own words on the listening experience: *"one song was being repeated for a long
time"* — the procedural fallback, confirming the capacity finding by ear.

Strongest: TF-00007, "Thin Momentum in the Closed Book" — genuinely apt for a quiet market.
Weakest: the procedural fallback, which is not music and was never meant to carry an hour.

---

## FINDINGS

### CRITICAL BUG

None. The one test classified in advance as critical-if-failed (18) passed.

### BUG

**B1 — the simulator's force-open masked the staleness it existed to test.**
`force_open` rewrote a CLOSED assessment to OPEN *after* `assess_availability` returned. The
calendar branch returns early, so every check below it — staleness included — had never run.
A symbol forced open with a feed dead for five minutes reported OPEN and healthy. Found when
step 18 ran for eight minutes with nothing changing. **Fixed**: the override is now applied
to the calendar *input* (`calendar_open_override`), so the normal ordering runs beneath it.
Two regression tests pin both directions.

**B2 — `REVIEW` has no consumer, so 43% of generated audio is discarded by a missing
workflow.** `OriginalityVerdict.REVIEW` maps directly to
`RejectionReason.REVIEW_UNRESOLVED`. There is no review queue, no operator surface, no
deferred decision and no re-roll. A three-way verdict with two dispositions means the third
is a silent reject. **Not fixed** — this is a design decision, not a typo.

**B3 — no vocal track can be produced; every one silently becomes an instrumental.**
`LyricComposer` exists, is exported and is tested, but **has no production caller**. The
`lyrics` table is empty across the entire database. The ACE-Step prompt builder detects
this and correctly refuses to let the model invent its own words — §7.10 and §14 are
honoured, with a recorded warning — and downgrades to instrumental. Confirmed by ear.
Consequence: the station cannot currently do rap or vocals at all, and §8's rap requirement
and this plan's steps 10–11 are unreachable. **Not fixed.**

**B4 — recovery left orphaned jobs that could never succeed.** Recovery drops a restored
queue slot whose blueprint is missing (correct), but the generation job for that track
stayed claimable. It was claimed, failed, retried to exhaustion, and the cleanup called
`queue.remove()` on a slot recovery had already dropped — raising `QueueError` *inside* the
failure handler and masking the real cause on every cycle. **Fixed**: `classify_on_startup`
now abandons jobs whose track has no blueprint, and the terminal-failure cleanup uses a new
`RadioQueue.discard()` for which already-absent is success.

**B5 — `TrackFileRepository` has no production caller.** No `track_files` row is ever
written. Two consequences: the §46 detail page always reports `has_audio: false`, and §36
retention sweeps through this table, so **nothing is ever deleted** — `generated/` is
already 1.3 GB. **Not fixed.**

**B6 — `now_playing.is_instrumental` reports blueprint intent, not what was generated.**
A track downgraded by B3 shows as vocal. Misleading wherever B3 applies, which is
everywhere.

**B7 — shipped `audio.device_name: "CABLE Input"` matches no device.** VB-Audio enumerates
as "CABLE In 16 Ch". Affects the Phase 8 OBS path, not this run.

### TUNING ISSUE

**T1 — originality thresholds are too tight for a single-model station.** Mean
max-similarity 0.816 against a 0.72 review / 0.84 reject band. Deliberately *not* changed
during the run. Worth considering together: a per-regime or per-genre similarity budget,
re-rolling the blueprint rather than discarding the audio, and whether `fpcalc` materially
changes discrimination.

**T2 — low-end heavy output.** The recurring raw-QC warning is *"76% of energy is below
120 Hz"*. Consistent across tracks, so it is a property of the model and caption rather than
of one generation.

**T3 — `generated/` has no working retention** (consequence of B5), currently 1.3 GB.

### MODEL LIMITATION

**M1 — ACE-Step turbo emits multi-second silent gaps mid-track.** Nine tracks rejected with
`qc_defect: a 6.2–8.2 s gap mid-track`. QC caught every one; this is the gate working.

**M2 — one model, one voice.** Repeated requests under a single quiet regime converge. This
is the upstream cause of T1 and is not fixable by threshold tuning alone.

### UX ISSUE

**U1 — the §46 detail page exposes no QC, mastering or originality evidence.** It returns
`summary` / `blueprint` / `has_audio` only. The evidence *is* available, on
`/api/originality/tracks/{id}`, with full measured values and thresholds — but an operator
on the library page cannot reach it.

**U2 — `supports_progress: false`** and the UI correctly shows indeterminate progress. §7.25
compliant; noted so it is not mistaken for a stall.

**U3 — the procedural tier is not visibly distinguished enough.** The operator heard an hour
of fallback and read it as "one song repeating" rather than "the station has no music". The
tier is in the API and on the dashboard; it is not loud enough given what it means.

### EXPECTED BEHAVIOUR

* Gold `closed` on a Saturday with `degraded=false`, station on BTCUSD — the design working.
* Procedural tier covering an empty buffer with 0.0 s silence — §33 working.
* Price withheld (`None`) on a simulated feed — §21 working.
* Startup acquisition not counted as a switch — the fix made before this run.
* 15-second fixture teardown in the API test suite — pre-existing, reproduces at `50eb6f7`,
  unrelated to this work.

---

## What passed

* Market routing end to end, including the mandatory feed-outage distinction.
* Phase 6 applied in full to ACE-Step audio with nothing bypassed.
* Real ACE-Step music through a real Windows device, confirmed audible.
* Continuity: 0.0 s unintended silence, 0 underruns, 98.2 % coverage.
* Test mode visible, and provably gate-free.
* Generation reliability: 0 failures, 0 OOM across 142 jobs.

## What is blocked

The station cannot sustain a listening session at a 7 % approval rate. **B2 is the cheapest
large win** — 43 % of output is discarded by a workflow that was never built, independent of
any threshold judgement. **B3 is the largest functional gap** — a station specified around
market-aware lyrics currently produces none.

Recommended order: B2 → B3 → B5 → T1, then re-run steps 10, 12, 14, 15 and 21, which this
run could not reach.

Stopping at the test boundary. Phase 8 not started.
