/**
 * The console shell: left navigation, top bar, and the connection story.
 *
 * The top bar carries the four facts an operator checks without thinking — is it live, what
 * is gold doing, what session are we in, what time is it — and the sidebar carries everything
 * else. That split is the reason the dashboard can be read "within seconds": the persistent
 * chrome answers the standing questions so the page below only has to answer the current one.
 *
 * The connection indicator is load-bearing rather than decorative. When the socket drops the
 * interface keeps its last known values and this is what tells you they are last-known — the
 * brief's requirement that a disconnect must not blank the interface only works if something
 * visible says the data is stale.
 */

import clsx from 'clsx'
import { NavLink, Outlet } from 'react-router-dom'
import { useEffect, useState } from 'react'

import { useLive } from '../lib/live'
import { ABSENT, clockTime, price, signed, since, titleCase } from '../lib/format'
import { StatusDot } from './primitives'
import type { ConnectionState } from '../lib/types'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/market', label: 'Market' },
  { to: '/radio', label: 'Radio' },
  { to: '/generation', label: 'Generation' },
  { to: '/library', label: 'Library' },
  { to: '/originality', label: 'Originality' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/obs', label: 'OBS' },
  { to: '/system', label: 'System' },
  { to: '/settings', label: 'Settings' },
] as const

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  connecting: 'CONNECTING',
  connected: 'CONNECTED',
  reconnecting: 'RECONNECTING',
  offline: 'OFFLINE',
}

const CONNECTION_TONE: Record<ConnectionState, string> = {
  connecting: 'text-ink-400',
  connected: 'text-status-healthy',
  reconnecting: 'text-status-degraded',
  offline: 'text-status-critical',
}

function useWallClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  return clockTime(now)
}

function LiveIndicator({ broadcasting }: { broadcasting: boolean }) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-label',
        broadcasting ? 'text-gold-400' : 'text-ink-500',
      )}
      // Spoken as a sentence rather than as "LIVE", which is meaningless out of context.
      aria-label={broadcasting ? 'The station is broadcasting' : 'The station is not broadcasting'}
    >
      <span
        aria-hidden="true"
        className={clsx(
          'h-1.5 w-1.5 rounded-full',
          broadcasting ? 'animate-pulse-live bg-gold-400 shadow-glow' : 'bg-ink-600',
        )}
      />
      {broadcasting ? 'LIVE' : 'OFF AIR'}
    </span>
  )
}

function ConnectionIndicator() {
  const { connection, ageMs, reconnect } = useLive()
  const degraded = connection !== 'connected'
  return (
    <button
      type="button"
      onClick={degraded ? reconnect : undefined}
      title={degraded ? 'Retry now' : 'Live connection is healthy'}
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-2xs font-medium uppercase',
        'tracking-label transition-colors',
        CONNECTION_TONE[connection],
        degraded ? 'hover:bg-ink-850' : 'cursor-default',
      )}
      data-testid="connection-indicator"
    >
      <StatusDot
        state={
          connection === 'connected'
            ? 'healthy'
            : connection === 'offline'
              ? 'critical'
              : 'degraded'
        }
      />
      {CONNECTION_LABEL[connection]}
      {degraded && ageMs !== null && (
        <span className="font-mono text-ink-500">· {since(ageMs)}</span>
      )}
    </button>
  )
}

function TopBar() {
  const { state, isStale } = useLive()
  const time = useWallClock()
  const market = state?.market
  const alerts = state?.alerts ?? []
  const critical = alerts.filter((alert) => alert.severity === 'critical')

  const change = market?.price_change ?? null
  const changeTone =
    change === null ? 'text-ink-500' : change > 0 ? 'text-market-up' : change < 0 ? 'text-market-down' : 'text-ink-400'

  return (
    <header className="flex h-14 shrink-0 items-center gap-6 border-b border-ink-800 bg-ink-900/70 px-5 backdrop-blur">
      <div className="flex items-baseline gap-3">
        <span className="text-sm font-semibold uppercase tracking-[0.16em] text-ink-100">
          Trade Fix Radio
        </span>
        <LiveIndicator broadcasting={state?.status.is_broadcasting ?? false} />
      </div>

      <div className="flex items-center gap-5 border-l border-ink-800 pl-5">
        <div className="flex items-baseline gap-2" data-testid="topbar-price">
          <span className="label">XAUUSD</span>
          <span className="font-mono text-sm tnum text-ink-100">{price(market?.price)}</span>
          <span className={clsx('font-mono text-2xs tnum', changeTone)}>
            {change === null ? '' : signed(change)}
          </span>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="label">Session</span>
          <span className="font-mono text-xs text-ink-200">
            {market ? titleCase(market.session) : ABSENT}
          </span>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-4">
        {market?.is_simulated && (
          // §72: simulation must never be mistaken for live. Persistent, in the chrome, on
          // every page — not a badge tucked into one panel.
          <span
            className="chip border-gold-500/40 bg-gold-500/10 text-gold-300"
            title="The market feed is simulated. Prices are not shown because none are real."
          >
            Simulation mode
          </span>
        )}
        {isStale && <span className="chip border-status-degraded/40 text-status-degraded">Stale</span>}
        {critical.length > 0 && (
          <span
            className="chip border-status-critical/45 bg-status-critical/10 text-status-critical"
            data-testid="alert-chip"
          >
            {critical.length} alert{critical.length === 1 ? '' : 's'}
          </span>
        )}
        <span className="font-mono text-xs tnum text-ink-400">{time}</span>
        <ConnectionIndicator />
      </div>
    </header>
  )
}

function Sidebar() {
  const { state } = useLive()
  const status = state?.status

  return (
    <nav
      aria-label="Sections"
      className="flex w-[190px] shrink-0 flex-col border-r border-ink-800 bg-ink-900/50"
    >
      <div className="border-b border-ink-800 px-4 py-3">
        <div className="text-2xs font-semibold uppercase tracking-[0.14em] text-gold-400">
          Control Center
        </div>
        <div className="mt-0.5 text-2xs leading-tight text-ink-500">
          The market composes the radio
        </div>
      </div>

      <ul className="flex-1 overflow-y-auto py-2">
        {NAV.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={'end' in item ? item.end : false}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-2 border-l-2 px-4 py-1.5 text-xs transition-colors',
                  isActive
                    ? 'border-gold-500 bg-gold-500/[0.07] font-medium text-gold-300'
                    : 'border-transparent text-ink-400 hover:bg-ink-850 hover:text-ink-200',
                )
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>

      <div className="space-y-1 border-t border-ink-800 px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="label">Station</span>
          <span
            className={clsx(
              'font-mono text-2xs uppercase',
              status?.is_broadcasting ? 'text-status-healthy' : 'text-ink-500',
            )}
          >
            {status?.playout_state ?? ABSENT}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="label">Env</span>
          <span className="font-mono text-2xs text-ink-400">{status?.environment ?? ABSENT}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="label">Version</span>
          <span className="font-mono text-2xs text-ink-400">{status?.version ?? ABSENT}</span>
        </div>
        <div className="pt-1">
          <ConnectionIndicator />
        </div>
      </div>
    </nav>
  )
}

export function Shell() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TopBar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto" id="main">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
