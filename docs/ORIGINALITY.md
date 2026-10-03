# Originality

> What the station checks, how, and — importantly — what it cannot tell you.

## The claim, stated precisely

**This system cannot prove a song has never existed before. That is not technically
defensible, and nothing here claims it.**

What it does is narrower and achievable: it compares every candidate against **this station's
own library** and refuses exact duplicates, near-duplicates, suspiciously similar lyrics and
excessively repeated creative blueprints. It stops the station repeating itself.

It is **not** a copyright clearance, not evidence of originality against any other music, and
not a guarantee of anything beyond the corpus it holds. The API sends that sentence alongside
every number it returns, and the Originality page renders it above the data rather than behind
a tooltip, so the claim and the figures can never drift apart.

## Five independent checks

Each answers a different question, and each can reject on its own. They are kept separate
because an overall score hides exactly the cases that matter — a different song with the same
chorus scores low overall and is an obvious repeat to a listener.

### 1. Exact audio (§6.3)

SHA-256 of the **decoded PCM**, not the file bytes.

The normalisation is deliberate: fold to mono so a channel swap does not change the digest,
resample to 22 050 Hz so a rate change does not, quantise to 16-bit so sub-LSB noise does not.
Hashing file bytes would let a re-save defeat duplicate detection entirely.

**Measured scope.** It catches the same decoded audio written twice, and the same PCM in
different containers — both verified. It does **not** survive a change of sample format:
writing float32 to 24-bit perturbs samples by ~6e-08, inaudible but enough to cross a
quantisation boundary. No hash can absorb that, because a hash has boundaries by construction.
That gap is covered by the similarity engine's exact-audio threshold rather than papered over.

### 2. Perceptual fingerprint (§6.4)

Chromaprint via `fpcalc` when it is installed; a built-in chroma-and-timbre signature
otherwise. The provider name is stored with every fingerprint and **two fingerprints from
different providers are never compared** — the numbers are not on the same scale, and the
result would be meaningless rather than merely imprecise.

`tradefix doctor` reports which one is active. The built-in is weaker at near-duplicate
detection, and says so.

### 3. Audio similarity (§6.5)

A staged search, because comparing everything against everything does not survive a library of
tens of thousands:

| stage | cost | what it does |
|---|---|---|
| 1 | O(1) indexed lookup | exact canonical hash |
| 2 | one matrix multiply | cosine over stored embeddings → shortlist of 24 |
| 3 | expensive, bounded | full multi-component comparison, shortlist only |

Measured on this machine: 3.7 ms against 100 tracks, 20.8 ms against 10 000, 95.5 ms against
50 000.

#### The embedding, and why it is version 2

The embedding is chroma and timbre, each reduced to a *shape*, unit-normalised separately,
then concatenated and normalised again.

Version 1 concatenated the raw means. It did not work, and the failure was structural rather
than a matter of tuning:

- **Chroma is non-negative**, so the cosine of any two chroma vectors sits near the top of the
  scale. Four tracks of different genres, tempos and keys measured 0.86–0.91 against each
  other. On that scale a 0.84 rejection threshold rejects everything.
- **MFCC[0] is log-energy, not timbre.** An order of magnitude larger than the coefficients
  that describe spectral shape, so it dominated the dot product and every pair scored 0.998.

Version 2 centres chroma — turning the cosine into a correlation over *which pitch classes
stand out* — and drops MFCC[0], since loudness is measured directly and two tracks being
equally loud is not similarity. The same correction applies to the `chroma` and `mfcc`
components of the full comparison; fixing one and not the others left one calibrated component
outvoted by two saturated ones.

`EMBEDDING_VERSION` is stored with every embedding. Two embeddings from different versions are
not comparable, and recording the version is what makes that detectable instead of silent.

#### Components

Every comparison keeps its parts: `audio_fingerprint`, `embedding`, `chroma`, `mfcc`, `tempo`,
`lyrics`, `blueprint`. Weights are renormalised over the components that actually have an
input, so an instrumental does not look more novel merely because the lyric weight contributed
zero.

The rejection rules are checked against **every** comparison, not only the highest-scoring one:
a track can be an exact lyric match to one track and a near-audio match to another, and the
overall score hides both.

### 4. Lyric similarity (§6.8)

Entirely lexical — hashes, five-word shingles, line and hook hashes. No cloud embedding
dependency; the interface is shaped so a local model can be added without changing callers.

The score is the **maximum** of its components, not a blend. A lyric sharing one whole hook and
nothing else is a repeat a listener notices instantly, and averaging that against a low overall
overlap would hide it — which is the failure mode a blended score has in precisely the cases
that matter.

Section markers like `[Chorus]` are stripped: structure is not content, and two different
lyrics both having a chorus is not similarity.

Internal repetition is flagged separately. A lyric can be unlike everything in the library and
still be one line forty times, which is a quality defect rather than an originality one.

**This compares against the station's own lyrics only.** It will catch the station repeating
itself and will not detect text copied from outside the corpus — copyrighted lyrics are not
detectable from text, and §86 forbids claiming otherwise.

### 5. Blueprint repetition (§6.7)

Catches what audio comparison structurally cannot: two tracks whose *creative decision* is the
same, rendered differently.

Weighted over genre (0.26), BPM (0.18), energy (0.12), key (0.10), topic (0.10), vocal (0.08),
secondary genre (0.06), persona (0.06) and duration (0.04). Key similarity is musical rather
than a string match — relative major/minor scores 0.5, same tonic 0.25.

**Recency matters more than history.** The same blueprint twice in an hour is obvious; twice in
a fortnight is a station with a consistent sound. So the threshold tightens to 0.86 inside a
six-hour window and relaxes to 0.95 outside it. An unknown timestamp is treated as recent:
being strict about something unknown risks a regeneration, being lax risks airing a repeat, and
the first is much cheaper.

## The novelty score (§6.6)

```
novelty = 1 − max_similarity
```

That is the whole formula, and the simplicity is the point. Any curve applied here — a power, a
sigmoid — would make the score easier to tune and impossible to explain. *"Novelty 0.73 means
the closest track in the library scores 0.27"* is a sentence an operator can check.

It is the **maximum**, not the mean. A candidate that closely resembles one track and nothing
else is a duplicate; averaging would dilute that to nothing.

## Verdicts

| verdict | when | what happens |
|---|---|---|
| `approve` | below the review threshold | continues to mastering |
| `review` | between review and reject | in autonomous mode, treated as a rejection and regenerated — a track cannot sit in limbo on a station with nobody watching |
| `reject` | at or above the reject threshold, or any independent rule fired | rejected with the deciding component named |

## Inspecting it

```console
$ tradefix fingerprint track.wav
$ tradefix compare candidate.wav existing.wav
$ tradefix lyrics a.txt b.txt
```

Or in the Control Center: **Originality** shows the novelty distribution, recent verdicts, and
for any track the full evidence chain — every QC check with its measured value, every
similarity component, and what mastering did.

## API

| endpoint | returns |
|---|---|
| `GET /api/originality/summary` | library size, verdict counts, novelty histogram, active fingerprint provider, scope note |
| `GET /api/originality/recent?limit=` | recent verdicts with their component breakdowns |
| `GET /api/originality/tracks/{id}` | the complete evidence chain; 404 when the track never went through post-production |
