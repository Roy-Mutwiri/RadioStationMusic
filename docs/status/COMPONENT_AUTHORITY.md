# Component authority — what MFCC and tempo actually measure

MFCC decides 51% of the station's review/reject verdicts and tempo decides 38%.
This measures whether either is evidence of *duplication* or of *style*.

A duplication signal should separate same-recording from different-recording and
should barely move between genres. A signal that instead moves with genre and BPM
is describing a production family, which belongs to creative rotation rather than
to a duplicate verdict.

Pairs compared: 25425

| component | all pairs | same genre | cross genre | genre delta |
|---|---|---|---|---|
| fingerprint | 0.511 ±0.021 [0.419–0.646] | 0.515 ±0.021 [0.428–0.646] | 0.510 ±0.021 [0.419–0.643] | **+0.005** |
| mfcc | 0.973 ±0.023 [0.751–0.999] | 0.978 ±0.022 [0.827–0.999] | 0.973 ±0.023 [0.751–0.999] | **+0.005** |
| chroma | 0.284 ±0.270 [0.000–0.985] | 0.349 ±0.290 [0.000–0.978] | 0.280 ±0.268 [0.000–0.985] | **+0.070** |
| tempo | 0.386 ±0.354 [0.000–1.000] | 0.774 ±0.264 [0.000–1.000] | 0.361 ±0.344 [0.000–1.000] | **+0.413** |

| component | BPM within 4 | BPM further apart | BPM delta |
|---|---|---|---|
| fingerprint | 0.512 ±0.021 [0.428–0.619] | 0.510 ±0.021 [0.419–0.646] | **+0.002** |
| mfcc | 0.975 ±0.023 [0.806–0.999] | 0.973 ±0.023 [0.751–0.999] | **+0.002** |
| chroma | 0.307 ±0.273 [0.000–0.978] | 0.280 ±0.269 [0.000–0.985] | **+0.027** |
| tempo | 0.861 ±0.233 [0.000–1.000] | 0.307 ±0.305 [0.000–1.000] | **+0.554** |

## Reading

* **MFCC** averages 0.973 across *every* pair in the corpus, with a
  spread of ±0.023. A measure that returns the same answer for
  everything cannot be evidence about anything. It is saturated on one generator's
  output, which is the condition this station permanently operates in.
* **Tempo** averages 0.386 and is the component most sensitive to
  BPM proximity by construction — the director picks BPM from a narrow band per
  genre and energy, so tempo agreement is largely a restatement of the brief.
* **Fingerprint** is the only component whose high values were shown, by direct
  calibration, to mean 'the same recording'. See CHROMAPRINT_CALIBRATION.md.

Conclusion: MFCC and tempo are style signals. They remain useful to the diversity
director, which exists to vary consecutive programming, and should carry little or
no authority in a duplication verdict.
