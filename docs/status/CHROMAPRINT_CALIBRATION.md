# Chromaprint calibration

What bit agreement between two Chromaprint signatures actually means, measured
rather than assumed. The ReviewResolver's duplication thresholds are derived from
this table and from nothing else.

Base recording: `TF-20261002-00002.wav`

Agreement is the fraction of matching bits across aligned 32-bit subfingerprints.
Unrelated material sits near 0.5 because half the bits match by chance.

## Same recording, modified

| variant | what it simulates | agreement |
|---|---|---|
| exact copy | byte-identical file | **1.0000** |
| WAV rewrite | decoded and re-encoded, same format | **1.0000** |
| FLAC round trip | lossless re-encode | **1.0000** |
| MP3 192k round trip | lossy re-encode | **0.9984** |
| gain +3 dB | louder, same performance | **1.0000** |
| gain -6 dB | quieter, same performance | **1.0000** |
| peak normalised | what mastering does to level | **1.0000** |
| EQ shelf | +4 dB above 4 kHz, -3 dB below 150 Hz | **0.9196** |
| dynamics compressed | 4:1 above -18 dB | **0.9068** |
| 0.5 s leading silence | offset at the head | **0.6829** |
| 2 s trailing silence | offset at the tail | **1.0000** |
| 5 s head crop | the hardest same-recording case | **0.6222** |

## Different recordings

| track | agreement |
|---|---|
| TF-20261002-00003 | 0.6404 |
| TF-20261002-00004 | 0.5018 |
| TF-20261002-00005 | 0.5638 |
| TF-20261002-00006 | 0.5610 |
| TF-20261002-00007 | 0.4849 |
| TF-20261002-00008 | 0.4733 |
| TF-20261002-00009 | 0.4761 |
| TF-20261002-00010 | 0.5319 |

## Observed bands

* same recording, modified: **0.6222 – 1.0000** (n=12)
* different recordings: **0.4733 – 0.6404** (n=8)

Separation between the two populations: **-0.0182**.

A positive margin means a single threshold can separate them. A negative one means it cannot, and the resolver must require corroboration rather than treating fingerprint agreement as sufficient on its own.
