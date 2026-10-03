# Audio QC

> Every check, what it measures, and the threshold it is held to.

QC answers one question: **is this file broken?** It does not answer "is this good music" — that
is the director's job and the listener's. The distinction matters, because a QC stage that
quietly becomes a taste filter rejects legitimate output for reasons nobody specified and
nobody can see.

## The shape of a result

§6.1 forbids collapsing validation into a single Boolean, so every check returns five things:

| field | meaning |
|---|---|
| `name` | stable identifier, used by the API and the Originality page |
| `status` | `pass`, `warn` or `fail` |
| `value` | the measurement, or `null` when the check could not run — **never** a stand-in number |
| `threshold` | the limit it was held to, as a readable string |
| `reason` | one sentence an operator can act on |

A track's overall status is `fail` if any check failed, `warn` if any warned, `pass` otherwise.
The individual checks are persisted, so "why was this rejected" always has an answer.

`warn` is not a weaker `fail`. It marks something worth knowing that is not grounds for
rejection — a dark mix, a little boundary silence. Tracks with warnings go to air.

## Two stages

QC runs twice, on different files, asking different questions.

**`raw`** — the generator's output, before mastering. Is there audio here at all, is it the
right length, is it broken? Loudness is *not* judged against intent here: mastering exists to
set level, and rejecting a quiet raw render that mastering was about to fix would reject most
of what any generator produces.

**`mastered`** — the finished master. Did mastering produce something sound? Plus the separate
loudness-drift check described in [MASTERING.md](MASTERING.md).

## The checks

### Presence and shape

| check | fails when | why |
|---|---|---|
| `non_empty` | the file has no samples | a zero-length render is a generator fault |
| `duration` | shorter than `qc.min_duration_seconds` | a truncated render is audible as a cut-off |
| `duration_match` | more than `qc.max_duration_deviation` from the blueprint | the generator ignored its instruction |
| `sample_rate` | below the configured floor | unusable for broadcast |

### Level and dynamics

| check | fails when | why |
|---|---|---|
| `peak_level` | peak ≥ 1.0 | already clipped before it reached us |
| `rms` | RMS is zero | silent, whatever the peak says |
| `clipping` | clipped fraction > `qc.max_clipped_sample_ratio` | audible distortion |
| `dc_offset` | offset > `qc.max_dc_offset` | wastes headroom and thumps on transitions |
| `crest_factor` | below 1.8 | suspiciously flat — heavy limiting or a stuck generator |
| `integrated_loudness` | outside the energy-aware band | see below |

**Level is measured across all channels, not on the mono fold.** Two channels in anti-phase
average to exactly zero, so a fold-based peak reports a perfectly loud track as digitally
silent. That track *is* defective, but it is defective for mono incompatibility, and a wrong
diagnosis sends an operator looking for a dead renderer.

#### The energy-aware loudness floor

§6.1 is explicit that *"a quiet ambient track should not fail simply because RMS is low"*. So
the floor slides with the blueprint's intended energy:

```
floor = qc.min_loudness_lufs − 8.0 × (1 − energy)
```

A blueprint at energy 0.1 is asking for something barely there and gets 7.2 LU of extra
latitude; one at energy 0.95 gets almost none. With no blueprint — a station identity, a
reserve track — energy is taken as a neutral 0.5 rather than guessed.

The allowance is bounded, and that bound is tested: audio below the meter's measurement floor
fails regardless of what the blueprint claimed. A quiet track passes; an inaudible one does
not.

**`integrated_loudness` distinguishes two kinds of "no value".** No meter installed is an
environment gap and warns. A meter that ran and found nothing to measure is a dead file and
fails. Reporting both as "not measured" once told an operator to install software they already
had while a silent track passed.

### Structure

| check | fails when | why |
|---|---|---|
| `silence_ratio` | more than `qc.max_silence_ratio` silent | mostly-empty render |
| `internal_silence` | a gap longer than 6 s | a dropout mid-track, inaudible in a waveform thumbnail |
| `leading_silence` / `trailing_silence` | over 3 s | warns only — mastering trims it |
| `discontinuities` | more than 32 sample-level jumps | the sound of a bad concatenation |

Silence is detected from a **per-instant maximum across channels**, for the same reason level
is: anti-phase content is present audio, not silence.

### Stereo

| check | fails when | why |
|---|---|---|
| `stereo_correlation` | below −0.2 | the channels are fighting |
| `mono_compatibility` | fold-down loss worse than −6 dB | vanishes on a phone speaker |

Total cancellation reports −120 dB rather than −∞, which no column can store. It previously
reported **0 dB** — the worst possible case recorded as "perfectly mono-compatible" — because
the guard against `log10(0)` fell through to the initialiser.

### Spectrum

| check | behaviour |
|---|---|
| `low_frequency_balance` | warns above 75 % of energy below 120 Hz |
| `high_frequency_content` | warns below 0.05 % above 8 kHz, fails below 1e-9 |
| `spectral_bandwidth` | warns at zero |

`high_frequency_content` **declines to measure when 8 kHz is at or above the source's Nyquist
limit.** A 16 kHz file contains no energy above 8 kHz by construction — sampling theory, not a
fault. Measuring anyway reported "the top end has collapsed, which means a decode or render
fault" for every track in a 16 kHz soak: 355 of 355 rejected, the station carried entirely by
its emergency tiers, and the stated cause had not occurred. The check now reports `pass` with
`value: null` and a reason naming the Nyquist limit — no number, because nothing was measured.

The failure threshold is 1e-9 rather than something comfortable because the first value tried
rejected every track in the fixture set including the clean one. Music built from low partials
genuinely has almost nothing above 8 kHz, and so does plenty of real dark, warm music.

## Running it by hand

```console
$ tradefix qc path/to/track.wav
$ tradefix qc path/to/master.wav --mastered
$ tradefix qc path/to/track.wav --json
```

Exit code is 0 when the track passes (warnings included) and 1 when it fails.

## What QC does not do

It does not judge musicality, originality, or lyrical content. Originality is
[ORIGINALITY.md](ORIGINALITY.md); lyric safety is Phase 3's validators, which work by
constraining what the composer may produce rather than by recognising what it did.
