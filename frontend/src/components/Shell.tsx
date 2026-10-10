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

import { api } from '../lib/api'
import { useLive } from '../lib/live'
import { ABSENT, clockTime, price, signed, since, titleCase } from '../lib/format'
import { StatusDot } from './primitives'
import type { ConnectionState, ControlResult } from '../lib/types'

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
  const routing = state?.routing ?? null
  const alerts = state?.alerts ?? []
  const critical = alerts.filter((alert) => alert.severity === 'critical')

  // The symbol comes from the market state, not from the routing summary: the price and
  // session beside it are that state's, and labelling them with a symbol read from
  // somewhere else would mislabel them for the moment between a switch and the next tick.
  const symbol = market?.symbol ?? routing?.active_symbol ?? null
  const onFallback = routing !== null && routing.has_active_market && !routing.is_primary
  const noMarket = routing !== null && !routing.has_active_market

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
        <MusicSourceChip />
      </div>

      <div className="flex items-center gap-5 border-l border-ink-800 pl-5">
        <div className="flex items-baseline gap-2" data-testid="topbar-price">
          <span
            className={clsx('label', onFallback && 'text-gold-300')}
            data-testid="topbar-symbol"
            title={
              onFallback
                ? `${routing?.primary_symbol} is unavailable; the station is programming against ${symbol}.`
                : undefined
            }
          >
            {symbol ?? ABSENT}
          </span>
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
        {noMarket && (
          // Both markets are shut. Said plainly, because the station is still playing and
          // the obvious reading of a frozen price panel is "the dashboard has hung".
          <span
            className="chip border-status-degraded/40 bg-status-degraded/10 text-status-degraded"
            data-testid="no-market-chip"
            title="No market is currently open. The station keeps broadcasting from its buffer; it is not planning against live data."
          >
            No active market
          </span>
        )}
        {onFallback && (
          <span
            className="chip border-gold-500/40 bg-gold-500/10 text-gold-300"
            data-testid="fallback-chip"
            title={`${routing?.primary_symbol} is closed. ${routing?.active_symbol} is on air.`}
          >
            Fallback market
          </span>
        )}
        {state?.status.test_mode && (
          // Alongside the simulation badge, not instead of it: they are different claims.
          // Simulation says the market is not real; test mode says the buffer targets are
          // not production's. A run can be either, both, or neither.
          <span
            className="chip border-status-degraded/45 bg-status-degraded/10 text-status-degraded"
            data-testid="test-mode-chip"
            title="Interactive test run: buffer targets are lowered so playback starts within minutes. QC, originality and mastering gates are unchanged."
          >
            TEST MODE
          </span>
        )}
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
        <TransportBar />
        <VolumeControl />
        <span className="font-mono text-xs tnum text-ink-400">{time}</span>
        <ConnectionIndicator />
      </div>
    </header>
  )
}

/**
 * What the listener is actually hearing, stated plainly in the chrome: a generated track
 * from the real music model, synthetic placeholder audio from the mock generator, the
 * station's emergency filler, or nothing. The dashboard panels say the same in more detail;
 * this is the one-glance answer to "is this the real thing right now?".
 */
function MusicSourceChip() {
  const { state } = useLive()
  if (!state) return null
  const track = state.now_playing
  const tier = state.emergency?.tier
  const provider = state.generation?.provider
  const paused = state.status.playout_state === 'paused'
  const generated = track !== null && /^TF-/.test(track.track_id) && tier === 'scheduled'

  let label: string
  let tone: string
  let title: string
  if (!state.status.is_broadcasting && !paused) {
    label = 'NO AUDIO'
    tone = 'border-ink-700 text-ink-500'
    title = 'The playout engine is not running.'
  } else if (generated && provider === 'ace_step') {
    label = 'AI MUSIC · LIVE'
    tone = 'border-status-healthy/50 bg-status-healthy/10 text-status-healthy'
    title = 'A track generated by ACE-Step from the live market is on air.'
  } else if (generated) {
    label = 'SYNTHETIC AUDIO'
    tone = 'border-status-degraded/50 bg-status-degraded/10 text-status-degraded'
    title = 'The mock generator is on air: placeholder audio, not real music. Install ACE-Step for real music.'
  } else {
    label = 'FILLER'
    tone = 'border-status-critical/50 bg-status-critical/10 text-status-critical'
    title = 'Emergency filler is on air while fresh tracks are generated.'
  }
  if (paused) {
    label = `${label} · PAUSED`
  }
  return (
    <span className={clsx('chip', tone)} data-testid="music-source-chip" title={title}>
      {label}
    </span>
  )
}

const TRANSPORT_BUTTON =
  'flex h-7 min-w-7 items-center justify-center rounded border border-ink-700 px-1.5 text-ink-200 transition-colors hover:border-ink-500 hover:bg-ink-800 disabled:cursor-not-allowed disabled:opacity-40'

function TransportBar() {
  const { state } = useLive()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const playing = Boolean(state?.now_playing)
  const paused = state?.status.playout_state === 'paused'
  const muted = state?.status.muted ?? false

  async function run(action: () => Promise<ControlResult>, failure: string) {
    setBusy(true)
    try {
      const result = await action()
      setMessage(result.message)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : failure)
    } finally {
      setBusy(false)
    }
  }

  // The message fades on its own; the transport lives in the chrome and must not grow.
  useEffect(() => {
    if (message === null) return
    const id = setTimeout(() => setMessage(null), 4000)
    return () => clearTimeout(id)
  }, [message])

  return (
    <div className="flex items-center gap-1.5 border-l border-ink-800 pl-4" data-testid="transport">
      {message && (
        <span className="mr-1 max-w-56 truncate text-2xs text-ink-400" title={message}>
          {message}
        </span>
      )}
      <button
        type="button"
        className={TRANSPORT_BUTTON}
        onClick={() => void run(api.previous, 'Going back failed.')}
        disabled={busy || !playing}
        aria-label="Previous track"
        title="Replay the previous track, or restart this one"
        data-testid="previous-button"
      >
        <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z" /></svg>
      </button>
      <button
        type="button"
        className={clsx(TRANSPORT_BUTTON, paused && 'border-gold-500 bg-gold-500/15 text-gold-300')}
        onClick={() => void run(paused ? api.resume : api.pause, paused ? 'Resume failed.' : 'Pause failed.')}
        disabled={busy || !state}
        aria-label={paused ? 'Resume playback' : 'Pause playback'}
        title={paused ? 'Resume' : 'Pause'}
        data-testid="play-pause-button"
      >
        {paused ? (
          <svg className="h-3.5 w-3.5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
        ) : (
          <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" /></svg>
        )}
      </button>
      <button
        type="button"
        className={TRANSPORT_BUTTON}
        onClick={() => void run(api.skip, 'The skip failed.')}
        disabled={busy || !playing}
        aria-label="Next track"
        title="End the current track at the next block boundary"
        data-testid="skip-button"
      >
        <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z" /></svg>
      </button>
      <button
        type="button"
        className={clsx(TRANSPORT_BUTTON, muted && 'border-status-degraded/60 bg-status-degraded/10 text-status-degraded')}
        onClick={() => void run(api.mute, 'The mute failed.')}
        disabled={busy || !state}
        aria-label={muted ? 'Unmute' : 'Mute'}
        aria-pressed={muted}
        title={muted ? 'Restore the output' : 'Silence the output; playout keeps running'}
        data-testid="mute-button"
      >
        {muted ? (
          <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.8 8.8 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z" /></svg>
        ) : (
          <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" /></svg>
        )}
      </button>
    </div>
  )
}

const VOLUME_STEP = 0.1

function VolumeControl() {
  const { state } = useLive()
  // The server's figure is the truth; a pending value covers the moment between a click
  // and the next live state so repeated clicks step from where the operator sees it.
  const [pending, setPending] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const live = state?.status.volume ?? null
  const volume = pending ?? live
  const muted = state?.status.muted ?? false

  useEffect(() => {
    if (pending !== null && live !== null && Math.abs(live - pending) < 0.005) setPending(null)
  }, [live, pending])

  async function change(delta: number) {
    if (volume === null) return
    const next = Math.round(Math.min(1, Math.max(0, volume + delta)) * 100) / 100
    setPending(next)
    setBusy(true)
    try {
      await api.setVolume(next)
    } catch {
      setPending(null)
    } finally {
      setBusy(false)
    }
  }

  const percent = volume === null ? ABSENT : `${Math.round(volume * 100)}%`
  const buttonClass =
    'rounded border border-ink-700 px-1.5 text-xs leading-5 text-ink-200 hover:border-ink-500 hover:bg-ink-800 disabled:cursor-not-allowed disabled:opacity-40'

  return (
    <div
      className="flex items-center gap-1.5 border-l border-ink-800 pl-4"
      data-testid="volume-control"
      title="Music volume. Applied on the way to the sound device; the mastered files are untouched."
    >
      <span className="label">{muted ? 'Vol (muted)' : 'Vol'}</span>
      <button
        type="button"
        className={buttonClass}
        onClick={() => void change(-VOLUME_STEP)}
        disabled={busy || volume === null || volume <= 0}
        aria-label="Decrease volume"
        data-testid="volume-down"
      >
        −
      </button>
      <span
        className={clsx('w-9 text-center font-mono text-xs tnum', muted ? 'text-ink-500' : 'text-ink-100')}
        data-testid="volume-value"
      >
        {percent}
      </span>
      <button
        type="button"
        className={buttonClass}
        onClick={() => void change(VOLUME_STEP)}
        disabled={busy || volume === null || volume >= 1}
        aria-label="Increase volume"
        data-testid="volume-up"
      >
        +
      </button>
    </div>
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
