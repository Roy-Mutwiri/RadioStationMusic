/**
 * MARKET STATE, and the energy timeline.
 *
 * The regime badge is the loudest element on the panel because it is the single fact that
 * explains the music. Everything else — volatility, trend, momentum, compression — is the
 * evidence behind it, rendered as horizontal bars so they can be scanned as a group and
 * compared against each other, which radial gauges make impossible.
 *
 * The price is the one field that may be *missing by design*. §21 forbids showing a price
 * that is not from a validated, current state, and the backend enforces that by sending
 * `null` — so this renders an explicit reason rather than a stale number.
 */

import clsx from 'clsx'
import { useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { ABSENT, clockTime, decimal, integer, price, signed, titleCase } from '../lib/format'
import type {
  ActiveMarket,
  HealthState,
  MarketAvailability,
  MarketHistory,
  MarketState,
} from '../lib/types'
import { api } from '../lib/api'

import { Button, EmptyState, Panel, StatusChip } from './primitives'

/**
 * How a regime is shown. Energetic regimes lean gold, quiet ones stay neutral.
 *
 * Not a per-regime palette: the brief rules out an RGB console, and twelve coloured badges
 * would be exactly that. Three tones carry the distinction that matters — is the market
 * asleep, normal, or doing something.
 */
function regimeTone(regime: string): string {
  if (/breakout|extreme|volatility_spike|reversal/.test(regime)) {
    return 'border-gold-500/50 bg-gold-500/15 text-gold-300'
  }
  if (/quiet|low_volatility|compression/.test(regime)) {
    return 'border-ink-700 bg-ink-850 text-ink-300'
  }
  return 'border-ink-600 bg-ink-800 text-ink-200'
}

function Gauge({
  label,
  value,
  hint,
}: {
  label: string
  value: number | null
  hint?: string
}) {
  const absent = value === null || !Number.isFinite(value)
  const fraction = absent ? 0 : Math.max(0, Math.min(1, value / 100))
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="label truncate">{label}</span>
        <span className={clsx('font-mono text-xs tnum', absent ? 'text-ink-600' : 'text-ink-200')}>
          {absent ? ABSENT : decimal(value, 0)}
        </span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-ink-800">
        <div
          className="h-full rounded-full bg-ink-500 transition-[width] duration-700"
          style={{ width: `${fraction * 100}%` }}
        />
      </div>
      {hint && <div className="text-2xs text-ink-600">{hint}</div>}
    </div>
  )
}

export function MarketPanel({ market }: { market: MarketState | null }) {
  if (!market) {
    return (
      <Panel title="Market state" data-testid="market-panel">
        <EmptyState
          title="Market feed offline"
          detail="No market state has reached the station. The radio keeps broadcasting; programming will hold its current direction."
        />
      </Panel>
    )
  }

  const change = market.price_change
  const changeTone =
    change === null ? 'text-ink-500' : change > 0 ? 'text-market-up' : change < 0 ? 'text-market-down' : 'text-ink-400'

  return (
    <Panel
      title="Market state"
      data-testid="market-panel"
      action={
        <span className="font-mono text-2xs text-ink-500">
          {market.data_age_seconds < 1
            ? 'live'
            : `${decimal(market.data_age_seconds, 0)}s old`}
        </span>
      }
    >
      <div className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="label">{market.symbol}</div>
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-2xl tnum text-ink-100">{price(market.price)}</span>
              {change !== null && (
                <span className={clsx('font-mono text-xs tnum', changeTone)}>{signed(change)}</span>
              )}
            </div>
            {market.price === null && (
              // The explanation matters as much as the absence: an operator seeing a dash
              // needs to know whether the feed died or whether this is a simulated run.
              <div className="mt-0.5 text-2xs leading-snug text-ink-500">
                {market.is_simulated
                  ? 'Simulated feed — no real price exists to show.'
                  : `Feed is ${market.feed_status}; price withheld while stale.`}
              </div>
            )}
          </div>

          <div className="shrink-0 text-right">
            <div className="label">Regime</div>
            <div
              className={clsx('chip mt-0.5 text-xs', regimeTone(market.regime))}
              data-testid="regime-badge"
            >
              {market.regime_label}
            </div>
            <div className="mt-1 font-mono text-2xs tnum text-ink-500">
              {decimal(market.regime_confidence * 100, 0)}% confidence
            </div>
          </div>
        </div>

        <div className="border-t hairline pt-3">
          <div className="flex items-baseline justify-between">
            <span className="label">Market energy</span>
            <span className="font-mono text-lg tnum text-gold-400" data-testid="market-energy">
              {decimal(market.energy, 0)}
            </span>
          </div>
          <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-ink-800">
            <div
              className="h-full rounded-full bg-gold-500 transition-[width] duration-700"
              style={{ width: `${Math.max(0, Math.min(100, market.energy))}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between text-2xs text-ink-600">
            <span>quiet</span>
            <span className={clsx('font-mono', market.energy_velocity > 0 ? 'text-market-up' : market.energy_velocity < 0 ? 'text-market-down' : '')}>
              {signed(market.energy_velocity, 1)} /min
            </span>
            <span>violent</span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-x-5 gap-y-2.5 border-t hairline pt-3">
          <Gauge label="Volatility" value={market.volatility} />
          <Gauge label="Trend strength" value={market.trend_strength} />
          <Gauge label="Momentum" value={market.momentum} />
          <Gauge label="Compression" value={market.compression} />
        </div>

        <div className="flex items-center justify-between border-t hairline pt-2 text-2xs text-ink-500">
          <span>
            Direction <span className="text-ink-300">{titleCase(market.direction)}</span>
          </span>
          <span>
            Session <span className="text-ink-300">{titleCase(market.session)}</span>
          </span>
          <span>
            Age <span className="text-ink-300">{decimal(market.regime_age_seconds / 60, 0)} min</span>
          </span>
        </div>
      </div>
    </Panel>
  )
}

// ----------------------------------------------------------------- timeline

const WINDOWS = ['15m', '1h', '4h', 'session'] as const
export type TimelineWindow = (typeof WINDOWS)[number]

/**
 * Market energy against radio energy, with regime changes banded behind them.
 *
 * This chart is the thesis of the whole project in one picture — the market gets aggressive
 * and the radio follows — so the two series are deliberately distinguishable by *shape* as
 * well as colour: market energy is a filled area, radio energy a bright line on top.
 */
export function EnergyTimeline({
  history,
  window,
  onWindowChange,
}: {
  history: MarketHistory | null
  window: TimelineWindow
  onWindowChange: (next: TimelineWindow) => void
}) {
  const points = (history?.points ?? []).map((point) => ({
    at: point.at,
    time: clockTime(point.at),
    market: point.market_energy,
    radio: point.radio_energy,
    regime: point.regime,
  }))

  const bands: { x1: string; x2: string; regime: string }[] = []
  const changes = history?.regime_changes ?? []
  changes.forEach((change, index) => {
    const start = points[change.index]
    const nextChange = changes[index + 1]
    const end = nextChange ? points[nextChange.index] : points[points.length - 1]
    if (start && end && /breakout|extreme|volatility_spike/.test(change.regime)) {
      bands.push({ x1: start.time, x2: end.time, regime: change.regime })
    }
  })

  return (
    <Panel
      title="Market energy + regime timeline"
      data-testid="energy-timeline"
      action={
        <div className="flex gap-0.5" role="group" aria-label="Timeline window">
          {WINDOWS.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => onWindowChange(option)}
              aria-pressed={window === option}
              className={clsx(
                'rounded-sm px-2 py-0.5 text-2xs font-medium uppercase tracking-label transition-colors',
                window === option
                  ? 'bg-gold-500/15 text-gold-300'
                  : 'text-ink-500 hover:bg-ink-850 hover:text-ink-300',
              )}
            >
              {option}
            </button>
          ))}
        </div>
      }
      bodyClassName="p-2"
    >
      {points.length < 2 ? (
        <EmptyState
          title="Collecting market history"
          detail="The timeline fills as the station runs. It needs a few samples before a trend is meaningful."
        />
      ) : (
        <div className="h-[180px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: -24 }}>
              <defs>
                <linearGradient id="marketEnergyFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#c9a227" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="#c9a227" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              {bands.map((band, index) => (
                <ReferenceArea
                  key={`${band.x1}-${index}`}
                  x1={band.x1}
                  x2={band.x2}
                  fill="#c9a227"
                  fillOpacity={0.05}
                  strokeOpacity={0}
                />
              ))}
              <CartesianGrid stroke="#1b202b" vertical={false} />
              <XAxis
                dataKey="time"
                tick={{ fill: '#6b7383', fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: '#232935' }}
                minTickGap={48}
              />
              <YAxis
                domain={[0, 100]}
                tick={{ fill: '#6b7383', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                // 52, not 44: at 44 the "100" tick was clipped to "00", which reads as a
                // broken axis rather than a narrow one.
                width={52}
              />
              <Tooltip
                contentStyle={{
                  background: '#0c0e12',
                  border: '1px solid #232935',
                  borderRadius: 4,
                  fontSize: 11,
                }}
                labelStyle={{ color: '#9aa1ad' }}
                formatter={(value: number | string, name: string) => [
                  typeof value === 'number' ? value.toFixed(0) : value,
                  name === 'market' ? 'Market energy' : 'Radio energy (on air)',
                ]}
              />
              <Area
                type="monotone"
                dataKey="market"
                stroke="#c9a227"
                strokeWidth={1.5}
                fill="url(#marketEnergyFill)"
                isAnimationActive={false}
                dot={false}
              />
              <Line
                type="stepAfter"
                dataKey="radio"
                stroke="#e6e8ec"
                strokeWidth={1.5}
                strokeDasharray="3 2"
                dot={false}
                isAnimationActive={false}
                connectNulls
              />
            </AreaChart>
          </ResponsiveContainer>
          <div className="flex items-center justify-center gap-4 pt-1 text-2xs text-ink-500">
            <span className="flex items-center gap-1.5">
              <span aria-hidden="true" className="h-2 w-3 rounded-sm bg-gold-500/40 ring-1 ring-gold-500" />
              Market energy
            </span>
            <span
              className="flex items-center gap-1.5"
              title="The energy of the track currently on air. It trails the market by the depth of the buffer — that lag is the station scheduling ahead, not a delay in reacting."
            >
              <span aria-hidden="true" className="h-px w-4 border-t border-dashed border-ink-100" />
              Radio energy (on air)
            </span>
            {bands.length > 0 && (
              <span className="flex items-center gap-1.5">
                <span aria-hidden="true" className="h-2 w-3 rounded-sm bg-gold-500/10" />
                High-energy regime
              </span>
            )}
          </div>
        </div>
      )}
    </Panel>
  )
}

/**
 * Market routing: which symbol is on air, and how each configured market is doing.
 *
 * The two facts this panel exists to keep apart are `state` and `feed_degraded`. A closed
 * market and a dead feed look identical from a price chart — no ticks either way — and the
 * station treats them completely differently: one moves the programming to Bitcoin, the
 * other leaves it on gold and raises an alert. Collapsing them into one badge would hide
 * exactly the distinction the operator needs.
 */
const AVAILABILITY_TONE: Record<string, HealthState> = {
  open: 'healthy',
  closed: 'offline',
  stale: 'degraded',
  unavailable: 'critical',
  unknown: 'degraded',
}

function MarketRow({
  entry,
  routing,
  canSimulate,
}: {
  entry: MarketAvailability
  routing: ActiveMarket
  canSimulate: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const isClosed = entry.state === 'closed'

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      await api.setMarketClosed(entry.symbol, !isClosed)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The override could not be applied.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={clsx('border-l-2 py-2 pl-3', entry.is_active ? 'border-gold-500' : 'border-ink-800')}
      data-testid={'market-row-' + entry.symbol}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm text-ink-100">{entry.symbol}</span>
        {entry.is_active && (
          <span className="chip border-gold-500/40 bg-gold-500/10 text-gold-300">On air</span>
        )}
        {entry.symbol === routing.primary_symbol && (
          <span className="chip border-ink-700 text-ink-400">Primary</span>
        )}
        <StatusChip
          state={AVAILABILITY_TONE[entry.state] ?? 'degraded'}
          label={entry.state.toUpperCase()}
        />
        {entry.feed_degraded && (
          // Deliberately *in addition to* the state chip, never instead of it. A silent
          // gold feed on a trading day is "STALE + feed degraded"; a closed gold market is
          // "CLOSED" with no degradation at all, and nothing needs fixing.
          <span
            className="chip border-status-critical/45 bg-status-critical/10 text-status-critical"
            title="The data path looks broken. This is not the market being closed."
          >
            Feed degraded
          </span>
        )}
        {canSimulate && (
          <Button
            className="ml-auto"
            onClick={() => void toggle()}
            disabled={busy}
            data-testid={'market-closure-' + entry.symbol}
          >
            {isClosed ? 'Reopen ' + entry.symbol : 'Simulate ' + entry.symbol + ' closed'}
          </Button>
        )}
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-ink-500">{entry.reason}</p>
      <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 font-mono text-2xs text-ink-400">
        <span data-testid={'market-tick-' + entry.symbol}>
          last tick{' '}
          {entry.data_age_seconds === null ? ABSENT : decimal(entry.data_age_seconds, 0) + 's ago'}
        </span>
        <span>price {price(entry.last_price)}</span>
        <span>{integer(entry.bars_processed)} bars</span>
        <span>{entry.feed_status}</span>
      </div>
      {error && (
        <p className="mt-1 text-2xs text-status-critical" role="status">
          {error}
        </p>
      )}
    </div>
  )
}

export function MarketRoutingPanel({
  routing,
  canSimulate = false,
}: {
  routing: ActiveMarket | null
  canSimulate?: boolean
}) {
  if (!routing) {
    return (
      <Panel title="Market routing" data-testid="market-routing">
        <EmptyState
          title="Routing unavailable"
          detail="This process has no market-routing service attached, so there is nothing to report."
        />
      </Panel>
    )
  }

  return (
    <Panel
      title="Market routing"
      data-testid="market-routing"
      action={
        <span className="font-mono text-2xs text-ink-400">
          {integer(routing.switch_count)} switch{routing.switch_count === 1 ? '' : 'es'}
        </span>
      }
    >
      {!routing.has_active_market && (
        <p className="mb-3 border border-status-degraded/30 bg-status-degraded/5 px-3 py-2 text-2xs leading-relaxed text-status-degraded">
          No market is open. The station is still broadcasting from its buffer; it is not
          planning new programming against live data, and no market conditions are being
          invented in the meantime.
        </p>
      )}
      {routing.pending_symbol && (
        // The hysteresis, made visible. Without this a confirmed closure looks like the
        // station ignoring it for five minutes.
        <p className="mb-3 text-2xs leading-relaxed text-ink-400" data-testid="routing-pending">
          Confirming a move to{' '}
          <span className="font-mono text-ink-200">{routing.pending_symbol}</span>
          {routing.pending_seconds_remaining !== null && (
            <> — {decimal(routing.pending_seconds_remaining, 0)}s of the confirmation window left.</>
          )}
        </p>
      )}
      <div className="space-y-1">
        {routing.symbols.map((entry) => (
          <MarketRow
            key={entry.symbol}
            entry={entry}
            routing={routing}
            canSimulate={canSimulate}
          />
        ))}
      </div>
      <p className="mt-3 border-t hairline pt-2 text-2xs leading-relaxed text-ink-600">
        The station moves to a fallback only when the primary market is confirmed{' '}
        <em>closed</em>. A feed outage leaves it where it is — a broken data path is not
        evidence that the market has shut.
      </p>
    </Panel>
  )
}
