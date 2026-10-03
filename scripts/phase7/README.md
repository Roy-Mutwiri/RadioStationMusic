# Phase 7 benchmark scripts

The measurements in `docs/status/PHASE_7_REPORT.md` came from these. They are kept so the
numbers can be re-derived on different hardware rather than taken on trust — a benchmark
whose script is gone is an anecdote.

All three need a running ACE-Step service:

```console
cd D:\ace-step && uv run acestep-api
```

| script | § | what it measures |
|---|---|---|
| `representative_tracks.py` | 7.14 | the seven representative configurations, each through the full Phase 6 pipeline |
| `market_energy_probe.py` | 7.17 | whether market regime measurably changes the audio |
| `generation_endurance.py` | 7.21, 7.23 | capacity, latency distribution, VRAM/RAM drift over 20 consecutive generations |

Output lands in `artifacts/phase7/`, which is git-ignored: it is hundreds of megabytes of
real audio, reproducible from here.

These are **not** tests. They take 10–20 minutes each and produce numbers for a human to
read. The assertions that must hold on every run live in
`tests/integration/test_ace_step_gpu.py`, marked `gpu` and `slow`.
