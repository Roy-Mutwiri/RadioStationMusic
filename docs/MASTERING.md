# Mastering

> Consistent broadcast loudness, not maximum loudness.

That distinction from §6.9 decides the whole design. Peak-normalising every track to full scale
would make a quiet ambient piece and a breakout-session trap track equally loud, destroying
exactly the dynamic §1 asks the director to create. The station would *look* reactive on the
dashboard and sound flat on air.

## The chain

```
raw render
  → trim boundary silence   (numpy, bounded)
  → remove DC offset        (numpy, per channel)
  → conform to 48 kHz stereo
  → two-pass FFmpeg loudnorm
  → measure the result      (ebur128: integrated loudness + true peak)
  → canonical master
```

Boundary trimming and DC correction happen in numpy because those operations are exact and
inspectable there. The loudness work happens in FFmpeg because it is a solved problem with a
reference implementation, and reimplementing a true-peak limiter in numpy would be inventing a
worse one.

Trimming is **bounded** by `mastering.max_trim_seconds`. Without the cap, a mostly-silent
broken render would be trimmed to a fraction of a second and presented as a valid short track.

## The loudness target is a band

A single −14 LUFS target applied to everything removes the audible difference between a quiet
market and a violent one. §6.9 explicitly permits energy-based bands if documented, so:

```
target = mastering.target_lufs + (energy − 0.5) × 2 × 2.0 LU
```

Energy 0 lands 2 LU below the configured centre, energy 1 lands 2 LU above. The band is narrow
on purpose: 4 LU is clearly perceptible, so a listener can tell a quiet track from a loud one,
and narrow enough that nobody reaches for the volume control between songs. Wider starts to
defeat normalising at all.

With no blueprint — a station identity, a reserve track — the configured target is used
unchanged. There is no intended energy to read, and inventing one would be worse than neutral.

## Two passes, not one

Pass 1 measures; pass 2 applies the correction with the measured values supplied. Single-pass
`loudnorm` is a dynamic estimate that drifts on material whose loudness varies, which is most
music. Final QC holds the result to ±1.5 LU, which one pass does not reliably meet.

## When the target cannot be reached

`loudnorm` in linear mode scales by a constant and falls back to its dynamic limiter when that
constant would breach the true-peak ceiling. On material with a high crest factor the limiter
reaches the ceiling before it reaches the target.

Measured on this station's mock output: a track at −22.1 LUFS with a 20.8 dB crest factor and
a −3.0 dBTP true peak. Reaching a −12.4 LUFS target linearly needs +9.7 dB, which would put the
true peak at +6.7 dBTP. The limiter did 5 dB of crest reduction and stopped at −0.9 dBTP,
landing at −14.5 LUFS.

That master is **correct**. The only ways further are breaching the ceiling or crushing the
dynamics, and §6.9 rules out both. So the result records `peak_constrained: true` with the
measured true peak and ceiling beside it, and final QC's loudness test is asymmetric:

- **Louder than target** is always a defect. Nothing physical forces an overshoot, so it means
  normalisation misbehaved — and a track 2 LU hotter than the rest of the station is exactly
  what §6.9 exists to prevent.
- **Quieter than target** is a defect *only when there was room to go louder*. When the limiter
  is already on the ceiling, the undershoot is the material's crest factor. Failing it would
  reject every dynamic recording while passing every crushed one, inverting what mastering is
  for.

This is not a relaxed threshold. The ±1.5 LU tolerance is unchanged; the undershoot branch is
excused only on *measured evidence* that the ceiling was reached. A master that is quiet with
headroom to spare still fails.

An approved track that was peak-constrained says so in its approval note. A station where many
tracks are peak-constrained is telling the operator the configured target is too ambitious for
what the generator produces — a tuning decision a person should make, not a number the pipeline
quietly absorbs.

## What is recorded

`gain_applied_db` is the gain that was **actually applied** (`after − before`), not the gain
that was requested. With the limiter engaged those differ, and storing the request as though it
were the result would make the mastering record disagree with the file it describes.

| field | meaning |
|---|---|
| `target_lufs` | the energy-derived target |
| `measured_lufs_before` / `_after` | FFmpeg's own measurements, before and after |
| `true_peak_dbtp` | inter-sample peak from `ebur128`, not a sample maximum |
| `true_peak_ceiling_dbtp` | the configured ceiling, stored so the pair can be read together |
| `peak_constrained` | the target was traded away for the ceiling |
| `gain_applied_db` | what actually happened |
| `trimmed_start_seconds` / `_end_seconds`, `dc_removed` | what pre-processing changed |

## The canonical master format (§6.11)

**48 kHz, stereo, 24-bit PCM WAV.**

The rate and layout match the playout format exactly, so `conform` is a no-op for an approved
track. A master at any other rate would be silently resampled on every play — the "hidden
resampling surprise" §6.11 asks to avoid — through the linear interpolator `audio/format.py`
already documents as a compromise. 24-bit is lossless and wide enough that the limiter's output
is not re-quantised into audibility.

## Failure, not pretence

Without FFmpeg there is no defensible loudness normalisation, so mastering **fails** rather than
writing the conformed file and calling it mastered. `tradefix doctor` reports the missing tool.

Silence fails too: loudness cannot be measured, so there is nothing to normalise to. Writing a
silent "master" would reach final QC looking like a real file and consume a queue slot before
anything noticed.

## By hand

```console
$ tradefix master track.wav
$ tradefix master track.wav --out mastered.wav --json
```

## Cost

Measured against 180 s of 44.1 kHz stereo: 16.2 s, or 11× real time — the largest single cost
in post-production at 57 % of the total.
