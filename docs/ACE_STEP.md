# ACE-Step integration

> How the station talks to a music model, and why it talks to it the way it does.

## The shape, in one line

```
MusicBlueprint → AceStepPromptBuilder → AceStepProvider → HTTP → ACE-Step service (own process)
                                              ↓
                                         raw .flac → Phase 6 pipeline → master → radio queue
```

ACE-Step is **one implementation of `MusicGenerationProvider`**. Nothing outside
`tradefix_radio/generation/ace_step/` imports from it except `generation/factory.py`, which
is the single place a provider is chosen. That was §7's opening requirement and it is
checkable: `grep -r ace_step tradefix_radio/ --include=*.py` outside that package returns
the factory, the config schema, the doctor checks and nothing else.

## Why a separate process

Not a preference. ACE-Step 1.5 declares:

```toml
requires-python = ">=3.11,<3.13"
```

and ADR-01 pins this project to **Python 3.10** because that is where librosa's numba wheels
were available, which the whole of Phase 6 rests on. The ranges are mutually exclusive, so
in-process is unavailable at any price — a dedicated virtualenv does not help either, since
a 3.12 package cannot be imported into a 3.10 interpreter.

It is also independently what §7.5 asks for: *"If ACE-Step deadlocks or crashes, PlayoutEngine
must not be affected."* A crash in another OS process cannot take the broadcast with it.

Installed at `D:\ace-step`, **outside this repository**, so the station's working tree never
contains a 2.5 GB torch install or a 6 GB checkpoint.

```console
git clone https://github.com/ACE-Step/ACE-Step-1.5.git D:\ace-step
cd D:\ace-step && uv sync          # uv provisions Python 3.12 here; 3.10 is untouched
uv run acestep-api                 # 127.0.0.1:8001
```

## The protocol, as it actually behaves

The client is written against the running server, not against `docs/en/API.md`. Three
differences were found by reading the wire, and **all three fail silently** — which is why
they are called out here rather than quietly handled:

| documented | actual | what the documented version did |
|---|---|---|
| `POST /query_result {"task_ids": [...]}` | `{"task_id_list": [...]}` | handler reads `task_id_list`, defaults to `"[]"`, returns `data: []` with HTTP 200 — indistinguishable from "still running". A finished generation polled until it timed out. |
| `result` is an object | `result` is a **JSON-encoded string** holding a list, one entry per `batch_size` | parsed as an object, the per-item `status` was never found |
| `GET /v1/audio?file=` | `?path=`, and `file` already arrives as a ready-made relative URL | re-encoding double-escaped the drive colon |

Also: `GenerateMusicRequest` has **no `instrumental` field**. Pydantic ignores unknown keys,
so an `instrumental: true` we were sending did nothing while reading like enforcement. The
real mechanism is the `"[Instrumental]"` lyric marker, which the server's own docstring
names. A test now asserts the field is *absent*.

The fake server in `tests/fake_ace_step.py` speaks the real format. A fake that implemented
the documentation would have passed every test while production hung.

## What the provider owns

| § | responsibility |
|---|---|
| 7.6 | model lifecycle — `UNAVAILABLE → LOADING → READY → GENERATING → …`, illegal transitions raise |
| 7.7 | VRAM pre-flight, per-generation measurement, OOM classification |
| 7.8 | which profile a request runs under, including the retry step-down |
| 7.4 | `generate`, `healthcheck`, `cancel`, `describe`, `load`, `unload`, `status` |

What it does **not** own: QC, originality, mastering, retries, queueing. §7.18 is explicit
that no Phase 6 stage may be skipped because ACE-Step produced the audio, and a provider that
second-guessed the pipeline would make the verdict depend on which provider ran.

## Cancellation, honestly

**The API has no cancellation endpoint.** `cancel()` stops the station waiting and frees the
job slot; the GPU keeps rendering to completion. The provider says so — in its return value,
in the log, and by remembering the abandoned task — because reporting success would let the
manager start a second generation into a card that is still busy, turning a cancellation into
an OOM.

## Profiles (§7.8)

| profile | steps | guidance | when |
|---|---|---|---|
| `fast` | 4 | 2.0 | buffer CRITICAL/EMPTY |
| `balanced` | 8 | 3.0 | normal operation |
| `quality` | 16 | 4.5 | buffer HEALTHY, if configured |

Configuration, not constants, so they can be tuned against measured hardware.

Two rules in the implementation:

* **The ladder only moves down.** A station configured for `fast` is never upgraded to
  `quality` because its buffer is full — the operator chose `fast`, and spending three times
  the GPU time on their behalf is not the scheduler's decision.
* **A profile cannot carry a QC threshold.** §7.20: *"Operational urgency may change
  generation parameters, not QC integrity."* `GenerationProfile` has nowhere to put one, so
  the rule is structural rather than remembered. A test asserts the field set.

## Prompting (§7.9)

`AceStepPromptBuilder` is the only place ACE-Step vocabulary exists. The director emits
musical intent; this turns it into a caption and a tagged lyric block.

* Energy becomes an **adjective**, not a number — a text-conditioned model cannot read 0.73.
* Intensities within ±0.18 of neutral are **left unsaid**; a caption describing every axis as
  "moderate" has described nothing.
* The caption is capped at the documented 512 characters, **trimmed on clause boundaries** so
  no descriptor survives as a fragment, and the truncation is reported as a warning.
* It is a pure function. Same blueprint, byte-identical caption — without which §7.13's seed
  test could not hold the input constant.

The final caption is stored with the track (§7.26).

## Lyrics (§7.10)

Already-tagged lyrics pass through **untouched**, and `lyrics_modified` records it. Untagged
lyrics get section tags derived from the blueprint's own structure — intro/outro are skipped
so they cannot consume a stanza and mislabel a verse — and the modification is recorded with
a note.

A vocal blueprint with **no composed lyrics generates an instrumental**, with a warning. The
alternative is letting the model invent its own words about trading, which Phase 3's §14 and
§17 validators have never seen. That is the one place this project cannot be casual.

## Measured on an RTX 3060 (12 GB)

| | |
|---|---|
| Cold load (checkpoint resident) | 58 s |
| First-ever load incl. 6.2 GB download | 1229 s |
| Generation, 60 s track | 42–49 s, p50 46 s |
| Realtime factor | **1.31×** |
| VRAM, model resident idle | 9.3 GB used / 2.8 GB free |
| VRAM peak during generation | 10.7–11.5 GB |
| VRAM attributable to one generation | 1077–1170 MB |

The card is nominally 12 GB but a working desktop holds ~4.7 GB of it. Every figure above is
with that desktop running, which is the condition the station will actually run in.

See `docs/status/PHASE_7_REPORT.md` for the full benchmark and the seven representative
tracks.

## Operating it

```console
tradefix doctor                      # reports ACE-Step toolchain + models
tradefix models status               # what is installed. Downloads nothing.
tradefix models install ace-step     # explicit, states the size, needs --yes
```

`GET /api/generation/provider` returns live state: model, VRAM, in-flight track, p50/p95.
The Generation page renders it, and shows **elapsed seconds rather than a percentage** —
ACE-Step's progress field is real but coarse (it sits at 0.1 for the whole LM phase), so a
bar would spend most of a generation claiming 10 %.
