# Market routing

> XAUUSD is the station's subject. It is also shut for roughly 49 hours a week, and a radio
> station cannot take the weekend off.

The station programmes against one market at a time. Which one is a decision, not a
constant, and this document is about where that decision lives and the one distinction it
is built around.

```
MT5 feed ─┐
          ├─► MarketDataService(XAUUSD) ─┐
REST feed ┘                              │
                                         ├─► MarketRouter ─► ActiveMarket ─► MusicDirector
          ┌─► MarketDataService(BTCUSD) ─┘                        │              LyricsDirector
crypto ───┘                                                       │              Scheduler
                                                                  └─► market.active_symbol_changed
```

Everything downstream of `ActiveMarket` already existed and is unchanged. The feature
engine, the energy calculator, the regime engine, the director and the scheduler do not
know that routing exists; they are handed a `MarketStateV1`, and that state now carries the
symbol it describes.

---

## The distinction the whole subsystem exists for

**A closed market and a broken feed look identical from the data.** No ticks either way.
They are not remotely the same thing, and conflating them produces the worst failure this
system can have: a gold feed drops out on a Tuesday afternoon, the station concludes gold
has closed, and spends the London session playing Bitcoin music about a market that is
trading normally twenty feet away.

So the two are separated by construction rather than by convention:

```python
class MarketAvailability(enum.Enum):
    OPEN
    CLOSED        # the market is shut
    STALE         # data is late
    UNAVAILABLE   # data has stopped
    UNKNOWN       # nothing observed yet

    @property
    def authorises_fallback(self) -> bool:
        return self is MarketAvailability.CLOSED
```

`authorises_fallback` is true for exactly one state. STALE and UNAVAILABLE describe a data
path that is not working; they raise `feed_degraded`, they show on the Market page in red,
and they do **not** move the station. A degraded primary keeps the primary — the station
goes on planning against the last reading it trusts, and an operator is told to go and fix
the feed.

Closure is established from evidence, in this order (`assess_availability`):

1. the feed itself reports the market closed — a broker saying so outranks everything;
2. a market calendar says so;
3. otherwise, silence is silence: STALE, then UNAVAILABLE, never CLOSED.

Wall-clock reasoning alone is not evidence. There is no `datetime.weekday()` anywhere in
the decision path.

---

## Hysteresis

Three windows, all measured on the injectable clock so they are testable without sleeping:

| Setting | Default | What it prevents |
|---|---|---|
| `switch_confirmation_seconds` | 120 s | leaving on a straggling tick either side of a session boundary |
| `reopen_confirmation_seconds` | 300 s | crossing back and forth across the open |
| `minimum_active_market_seconds` | 180 s | a market that just came on air being replaced immediately |

Reopening is held two and a half times longer than closing, deliberately. Leaving a closed
market two minutes late costs nothing — the data has genuinely stopped. Returning two
minutes early means oscillating over the boundary, which a listener hears as the station
changing its mind.

**One transition is exempt.** Going from `NO_ACTIVE_MARKET` to any usable market happens
immediately and is not counted as a switch. Hysteresis protects against oscillation and
against abandoning a working market; neither applies when there is no market to oscillate
with. This is the ordinary startup path — every process begins having assessed nothing —
and without the exemption the station would sit on emergency tiers for two minutes at every
launch and then log a switch nobody made. `test_the_first_feed_to_come_up_is_adopted_immediately`
pins it, and its companion pins that the exemption does not leak into later transitions.

---

## ADR-17 — one `MarketDataService` per symbol, not one that switches feeds

A `MarketDataService` owns a `FeatureEngine`, an `EnergyCalculator` and a `RegimeEngine`,
and all three hold rolling state built from the symbol's own history. A single service
re-pointed at a different feed would carry gold's ATR into Bitcoin's regime classification
and emit a confident reading of a market it had never seen.

Separate services make "no regime-state contamination between symbols" **structural** rather
than a rule to remember. They also mean returning to gold returns to gold's own warmed
history rather than to a cold start, which is what makes the Monday-morning switch-back
sound like programming rather than like a reboot.

The cost is that the inactive symbol keeps polling. That is not overhead to be optimised
away — it is the mechanism. A market nobody watches cannot be assessed, and the entire
reopen path depends on noticing gold come back while Bitcoin is on air.

---

## What the lyrics know

`BlueprintMarketContextV1.symbol` records which market a track was planned against, and
topics carry an optional `markets` scope:

```yaml
london_session:
  markets: [XAUUSD]
btc_weekend_liquidity:
  markets: [BTCUSD]
position_sizing:          # unscoped — true everywhere
```

Scoping is a **filter**, not a weight. "The London open brings a step up in volume" is not
merely a poor fit over Bitcoin; it is false, and §14 forbids presenting a wrong market
relationship as fact. A low weight would still let it through occasionally, which is the one
outcome that is unacceptable. An unscoped topic suits every market; a scoped topic with no
active market is excluded, because a market-specific claim with no market behind it is
exactly the fabricated context §14 rules out.

Of the 57 topics, 10 are gold-scoped, 8 are Bitcoin-scoped and 39 are market-neutral — most
of what the station has to say about discipline, position sizing and risk is true of any
instrument, and stays available whichever market is on air.

The branding does not change. It is TRADE FIX RADIO on Bitcoin exactly as on gold.

---

## When nothing is open

`active_symbol` becomes `NO_ACTIVE_MARKET` and `current_state` becomes `None`. The director
is given nothing rather than a neutral reading, because a fabricated 50/50 market is a lie
the whole rest of the system would then act on.

**The radio does not go silent.** It keeps playing from its buffer and falls back through
the §33 emergency tiers exactly as it does for any other reason it cannot plan. Not being
able to see the market is a planning problem, never a broadcast problem.

---

## Replanning on a switch

A switch is its own replan trigger in the scheduler, independent of the energy test: the
flexible slots were planned against a market the station has left, and they would stay that
way however quiet the new market happens to be.

What is *not* replaced:

- the track on air — it finishes;
- HARD-locked and operator-pinned slots;
- SOFT-locked slots, which are already generated and imminent;
- anything at all, if the buffer is urgent. Survival outranks fit here as everywhere: a
  queue that fits the wrong market still beats silence.

The queue is never flushed. `RadioQueue.replan` keeps everything protected in its original
order and appends after it.

---

## Originality stays station-wide

There is one fingerprint library and one blueprint-signature history, covering both markets.
`OriginalityRepository.load_library` has no symbol parameter and must not acquire one.

The reason is not convenience. §11's "never repeat the same blueprint" is a promise to the
listener, and the listener hears one station. Two libraries would let the same track air on
Saturday and again on Monday with both halves individually correct.
`test_the_originality_library_is_station_wide_not_per_market` guards the query.

---

## Operating it

```
GET  /api/markets                     both symbols, state, feed health, last tick
POST /api/simulation/market-closure   {symbol, closed} — simulation modes only
```

The closure control overrides the *calendar*, never the feed. A symbol forced closed reports
CLOSED with a reason naming the override, so nobody reading the Market page mistakes a test
for a real closure — and the closed-vs-degraded distinction stays as visible under
simulation as in production. It is gated on the same capability as the scenario control: in
production the market calendar is not ours to invent, and an endpoint that could tell the
station gold was shut would be a way to make the dashboard lie.

The Control Center header shows the active symbol, with a `Fallback market` chip when the
station is not on its primary and a `No active market` chip when neither is usable. The
Market page shows both symbols with their state, feed health, tick age and bar count, and
surfaces a confirmation window in progress — without that, a closure the station has
correctly noticed looks for five minutes like a closure it has ignored.
