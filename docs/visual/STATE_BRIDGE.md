# Visual state bridge

How the station's live state reaches the behaviour director, and what happens when it
stops.

**Code:** `tradefix_radio/visual/bridge.py` · **Contract:**
`tradefix_radio/visual/contracts.py::VisualStateV1`
**Tests:** `tests/unit/test_visual_bridge.py` — 34 tests, including the isolation guards.

```
TRADE FIX RADIO (station + api, :8080)
        │  existing /ws — LiveStateV1 @ 2 Hz, position @ 0.5 s.  READ-ONLY.
        ▼
VisualStateBridge  ──────────────────────┐
        │  VisualStateV1 @ 2 Hz          │  tradefix_radio/visual/, own OS process
        ▼                                 │
BehaviorDirector  ·  CameraState (stub)  │
        │  VisualCommandV1                │
        ▼                                ─┘
WebGL2 / placeholder renderer (OBS browser source)
        │  RendererTelemetryV1 @ 1 Hz
        ▼
BehaviorDirector
```

---

## 1. The station does not change

Everything the visual layer needs is already published by `api/live.py`'s `LiveHub`. The
bridge is a WebSocket **client** of an endpoint that already exists.

That is the whole reason the brief's rule — *the radio should not know how character
animation works* — costs nothing here: the radio already does not know, and adding
nothing to it is how it stays that way.

`LiveHub` also already solves the hard parts. Bounded per-client queues that drop the
oldest message rather than buffering without limit; a hub that cannot be back-pressured
by a slow client. A visual process that stalls is handled by code already in production.

**Zero station changes were made.** One optional addition remains available if wanted: a
single entry in `api/capabilities.py` reporting whether a visual layer is configured, so
the console can show the page as unavailable rather than broken. It carries no animation
knowledge and the visual layer works without it.

## 2. Read-only, asserted structurally

Three tests, and they read the source rather than trusting the docstrings — "read-only"
is exactly the kind of property that decays silently the first time someone needs one
small write.

| Test | Asserts |
|---|---|
`test_visual_package_never_writes_to_the_station` | no module under `visual/` imports `persistence`, `core.events`, `radio` or `generation` |
`test_station_link_sends_nothing_but_a_keepalive` | no `send`/`send_text`/`send_json` call exists in `bridge.py` |
`test_band_mapping_is_the_only_place_a_regime_is_read` | `MarketRegime` is imported in `bridge.py` and nowhere else under `visual/` |

The third is the architectural seam. If a second module imports `MarketRegime`, a new
regime stops being a one-line change and market vocabulary has leaked into behaviour.

## 3. `VisualStateV1`

Visual-system inputs only. Two properties are load-bearing.

**Nullable, never zero-filled.** `music_bpm` of `None` means nothing is playing; a zero
would mean playing at 0 BPM. The brief forbids replacing unavailable values with fake
zeros, and the behavioural consequence is real — an energy of zero reads as a calm market
rather than an absent one, and `behavior_energy` substitutes a neutral 0.5 for absence
precisely because of it.

**Interpretation, not observation.** `intensity_band` and `behavior_energy` are carried
instead of a regime the director would have to reason about. `market_regime` is kept
verbatim for display and debug, and `director.py` does not read it.

```python
class VisualStateV1(Contract):
    # provenance
    at: datetime
    source_age_seconds: float
    feed_trust: FeedTrust                      # LIVE | DEGRADED | STALE
    degraded_reason: str | None

    # market
    active_symbol: str                         # "XAUUSD" | "BTCUSD" | NO_ACTIVE_MARKET
    symbol_changed_seconds_ago: float | None
    market_regime: str | None                  # display and debug ONLY
    intensity_band: IntensityBand              # what behaviour actually uses
    market_energy: Score100 | None
    market_energy_velocity: float | None
    market_direction: MarketDirection | None
    market_confidence: Unit | None
    market_health: str | None
    session: str | None

    # music
    music_bpm: int | None
    music_energy: Unit | None
    music_genre: str | None
    vocal_style: VocalStyle | None
    track_progress: Unit | None
    transition_state: TransitionState

    # station
    station_mode: StationMode
    emergency_tier: str | None
    broadcasting: bool

    # derived, computed by the bridge
    behavior_energy: Unit
    reaction_salience: Unit
    salience_components: dict[str, float]
```

### `FeedTrust` has three values, not two

Because the degradation ladder needs the middle one. A five-second socket gap must not
visibly change the performance; a sixty-second gap must stop market-driven behaviour
entirely. A boolean cannot express both.

`SIMULATED` maps to `LIVE`: simulated data is real, coherent, synthetic data, and the
station's own rule is that it must be visibly *badged* rather than hidden. The badge is
the renderer's job; the behaviour may use it.

### Honesty made unrepresentable

`VisualStateV1`'s validator rejects, rather than discourages:

- a stale feed carrying a band above dormant
- a stale feed carrying non-zero salience
- `NO_ACTIVE_MARKET` carrying a regime
- `station_mode = OFFLINE` with `broadcasting = True`

The same technique `MarketStateV1` uses for prices: the dishonest state cannot be
constructed.

## 4. Field provenance

Every field, and where it comes from. Nothing is invented.

| `VisualStateV1` | Source |
|---|---|
`active_symbol` | `routing.active_symbol`, or `NO_ACTIVE_MARKET` when `has_active_market` is false |
`market_regime` | `market.regime` |
`intensity_band` | **computed** — §5 |
`market_energy` | `market.energy` |
`market_energy_velocity` | `market.energy_velocity` |
`market_direction` | `market.direction` |
`market_confidence` | `market.regime_confidence` |
`market_health` | `market.feed_status` |
`session` | `market.session` |
`feed_trust` | `market.feed_status` + `market.data_age_seconds` |
`music_bpm` | `now_playing.bpm` |
`music_energy` | `now_playing.planned_energy` |
`music_genre` | `now_playing.genre` |
`vocal_style` | `now_playing.vocal_style` |
`track_progress` | `now_playing.progress` |
`transition_state` | `now_playing.is_station_id`, `.remaining_seconds`, `.progress` |
`emergency_tier` | `emergency.tier` |
`broadcasting` | `status.is_broadcasting` |
`station_mode` | **computed** — §6 |
`symbol_changed_seconds_ago` | tracked across frames by the bridge |
`behavior_energy` | **computed** — `modulation.behavior_energy()` |
`reaction_salience` | **computed** — §7 |

`planned_energy` as `music_energy` deserves a note: it is the energy the music director
*committed to* for this track, not a measured loudness. That is the right input — the
character should respond to the music's intent, which is stable for the whole track,
rather than to a level meter, which would make him twitch on every transient.

## 5. The band mapping

The only place a `MarketRegime` is read. Fourteen regimes, six bands.

| Band | Regimes |
|---|---|
`B0_DORMANT` | `UNKNOWN` |
`B1_QUIET` | `QUIET`, `LOW_VOLATILITY_RANGE` |
`B2_STEADY` | `NORMAL_RANGE`, `COMPRESSION`, `POST_EVENT_NORMALIZATION` |
`B3_FOCUSED` | `BULLISH_TREND`, `BEARISH_TREND`, `BREAKOUT_BUILDUP` |
`B4_ALERT` | `BULLISH_BREAKOUT`, `BEARISH_BREAKOUT`, `HIGH_VOLATILITY_RANGE`, `REVERSAL` |
`B5_PEAK` | `EXTREME_VOLATILITY` |

`test_every_regime_maps_to_a_band` asserts completeness: a regime with no entry would
silently fall to dormant and kill reactivity.

### Three overrides, applied immediately and bypassing the damping gate

1. **Stale or disconnected feed → B0.** Behaviour driven by a frozen regime that may be
   hours old performs market activity that is not happening.
2. **No active market → B0.** There is no regime to map.
3. **An absent or unparseable regime → B0.** The station reported something this version
   does not understand. Letting the gate hold the previous band through that would have
   the character behaving as though the market were steady while we have no idea what it
   is doing — the same class of error as presenting a stale price as current.

And one override applied *before* the gate: **a closed session caps the band at B1.**
Gold is not continuously traded; he may be at the desk during the weekend gap, but he is
not reacting to a market that is shut.

### Band damping

| Rule | Value |
|---|---|
Maximum movement | **one step per 20 s** |
Downward moves additionally require | the new band to hold **45 s** |

Regime classification is already minimum-duration-constrained upstream, but the extra
damping is nearly free and prevents a flapping classification producing a character who
oscillates between calm and alert — a far more visible artefact than the classification
itself. Calming down slowly and waking up quickly is both more realistic and more
forgiving of noise.

## 6. `station_mode`

Derived, first match wins:

| Mode | Condition | Effect on the character |
|---|---|---|
`OFFLINE` | not broadcasting | band forced B0 |
`SWITCHING_MARKET` | symbol changed within 30 s | **a gaze bias only** — see §8 |
`PROCEDURAL` | `emergency.tier == "procedural"` | **none** |
`RESERVE` | `emergency.tier == "emergency_reserve"` | **none** |
`BUFFER_LOW` | `buffer.level` in `{critical, low}` | **none** |
`NORMAL` | otherwise | — |

**Almost none of this is visible on the character, deliberately.** A low buffer is the
station's problem, not the trader's — he is watching a market, not a generation queue.
Dramatising it would make him react to something with no market meaning, and a viewer
would correctly read that as noise. It appears on the station monitor, which is what that
surface is for.

## 7. Reaction salience

Computed **here**, not in the director, because it is a market judgement and putting it
downstream would be exactly the market logic leaking into animation that the architecture
forbids. The director consumes one number and a threshold.

```
salience = 0.40 × normalised |Δ energy over 30 s|
         + 0.30 × band distance from the previous band / 2
         + 0.20 × normalised |velocity|
         + 0.10 × |Δ confidence| × 2
```

Zeroed outright when the feed is not live or the band is dormant: a reaction with no
market event behind it is the visual equivalent of a fabricated price.

`salience_components` is carried so the debug overlay can *explain* a reaction rather
than merely assert one.

## 8. Symbol routing

Supported: `XAUUSD`, `BTCUSD`, `NO_ACTIVE_MARKET`. The sentinel is the station's own —
`ActiveMarketV1` surfaces it rather than translating to `None`, and the bridge keeps it
for the same reason: it is a real state, not an absence.

**On a symbol change, nothing resets.** The brief is explicit, and the director's
`_observe_symbol` does exactly one thing: records a timestamp. No state change, no lock
release, no history clear, no cooldown reset, no chain abort.

What the timestamp buys is a **brief attention shift**: for 30 seconds, actions whose
gaze target is the main chart get a ×1.8 weight. He looks at the chart that just changed,
then carries on working.

Two tests assert the non-reset:
`test_symbol_change_does_not_reset_behaviour` checks state, history count and fatigue
phase are carried across the tick; `test_symbol_switch_does_not_reset_behaviour` runs 20
minutes across a real switch and checks the *breadth* of behaviour either side.

## 9. The degradation ladder

The brief: if Trade Fix Radio disappears, do not freeze mid-action.

| Gap | Behaviour |
|---|---|
**0–5 s** | Hold the last state exactly. Shorter than three frame intervals; a blip must not be visible in the performance |
**5–20 s** | `feed_trust = DEGRADED`. Band decays **one step toward B2**, not to B0 — a brief gap should not visibly change what he is doing, and steady is the honest "working, no strong claim" posture. Reactions disabled |
**20–60 s** | Band forced **B0**. `market_energy` cleared to `None`. Charts stop advancing and show `NO FEED` |
**60 s+** | **Neutral idle.** `VisualStateV1.neutral()` — no symbol, no regime, no energy, not broadcasting. He keeps breathing, blinking, reading and drinking coffee. He simply has nothing to react to |

Mid-action safety comes from the chain system rather than from the ladder: a committed
chain runs to completion regardless of what the bridge reports, so a feed loss during a
coffee sip finishes the sip.

**Reconnection** is exponential backoff 1 s → 30 s, indefinitely. On reconnect the bridge
takes the first full frame — which `LiveHub` already sends immediately on connect rather
than at the next tick — and the band ramps back at one step per 20 s. A character who
jumps from neutral idle to B5 in one frame reads as a glitch, not as a recovery.

Five tests cover the four rungs plus the never-connected case.

## 10. Screen content honesty

Carried on every `SCREEN` command (`renderer.py::screen_command`) as explicit flags
rather than left for the renderer to infer:

1. **No fabricated market activity, in any state.**
2. **`feed_trustworthy` false → charts stop advancing** and show `NO FEED`. They do not
   extrapolate, idle-animate or loop recorded data. A chart drawing invented candles on
   a public stream is a fabricated price with extra steps.
3. **Simulated state carries a `SIMULATED` badge.** A test mode visually
   indistinguishable from live is a test mode that can be streamed by accident.
4. **No price text when the price is absent.**
5. **Green and red only in chart marks and P&L figures.**
6. **No account, position, balance or order data on any surface, at any time.** Nothing
   in the station produces it and nothing in the renderer may invent it. The screens show
   a market, not a trader's book.

## 11. Simulation without the station

The director takes a `VisualStateV1`; it does not care where one came from. So the
simulator builds them directly and the whole behaviour layer runs with no station, no
socket and no artwork:

```
tradefix visual simulate --scenario breakout --duration 2h
```

Eleven scenarios, including `feed_down` and `no_active_market` so the degraded paths are
exercised rather than assumed. `tests/endurance/test_visual_soak.py` runs all eleven for
30 simulated minutes each and asserts structural soundness in every one.

## 12. What the bridge must never do

- Write to the station. Anything, ever.
- Add a station endpoint, table or event for the visual layer's benefit.
- Pass a `MarketRegime` past itself to anything that selects a motion.
- Let the director see `price`, or any absolute market level.
- Drive behaviour from a stale, disconnected or uninterpretable feed.
- Replace an unavailable value with a zero.
- Show a chart that advances without data behind it.
- Present simulated state without the badge.
- Block, retry synchronously, or back-pressure toward the station.
- Reset behaviour on a symbol change.
