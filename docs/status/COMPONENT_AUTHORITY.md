# Component authority — what MFCC and tempo actually measure

MFCC decides 51% of the station's review/reject verdicts and tempo decides 38%.
This measures whether either is evidence of *duplication* or of *style*.

A duplication signal should separate same-recording from different-recording and
should barely move between genres. A signal that instead moves with genre and BPM
is describing a production family, which belongs to creative rotation rather than
to a duplicate verdict.

Pairs compared: 10440

| component | all pairs | same genre | cross genre | genre delta |
|---|---|---|---|---|
| fingerprint | 0.511 ±0.021 [0.425–0.643] | 0.514 ±0.021 [0.428–0.614] | 0.511 ±0.021 [0.425–0.643] | **+0.003** |
| mfcc | 0.971 ±0.026 [0.751–0.999] | 0.977 ±0.024 [0.827–0.999] | 0.971 ±0.026 [0.751–0.999] | **+0.007** |
| chroma | 0.308 ±0.271 [0.000–0.978] | 0.379 ±0.290 [0.000–0.978] | 0.301 ±0.268 [0.000–0.974] | **+0.078** |
| tempo | 0.397 ±0.360 [0.000–1.000] | 0.810 ±0.245 [0.000–1.000] | 0.360 ±0.345 [0.000–1.000] | **+0.450** |

| component | BPM within 4 | BPM further apart | BPM delta |
|---|---|---|---|
| fingerprint | 0.513 ±0.021 [0.428–0.592] | 0.511 ±0.021 [0.425–0.643] | **+0.002** |
| mfcc | 0.973 ±0.025 [0.819–0.999] | 0.971 ±0.026 [0.751–0.999] | **+0.003** |
| chroma | 0.337 ±0.272 [0.000–0.978] | 0.303 ±0.270 [0.000–0.974] | **+0.034** |
| tempo | 0.904 ±0.177 [0.000–1.000] | 0.306 ±0.304 [0.000–1.000] | **+0.598** |

## Reading

* **MFCC** averages 0.971 across *every* pair in the corpus, with a
  spread of ±0.026. A measure that returns the same answer for
  everything cannot be evidence about anything. It is saturated on one generator's
  output, which is the condition this station permanently operates in.
* **Tempo** averages 0.397 and is the component most sensitive to
  BPM proximity by construction — the director picks BPM from a narrow band per
  genre and energy, so tempo agreement is largely a restatement of the brief.
* **Fingerprint** is the only component whose high values were shown, by direct
  calibration, to mean 'the same recording'. See CHROMAPRINT_CALIBRATION.md.

Conclusion: MFCC and tempo are style signals. They remain useful to the diversity
director, which exists to vary consecutive programming, and should carry little or
no authority in a duplication verdict.
