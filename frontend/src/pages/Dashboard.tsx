/**
 * PAGE 1 — the live dashboard.
 *
 * Laid out to answer the brief's eleven questions in reading order rather than in panel
 * order: the top bar says *is it broadcasting* and *what is gold doing*; the first row says
 * *what is playing* and *why*; the timeline says *is the radio following the market*; the
 * third row says *what comes next* and *is anything broken*.
 *
 * Everything reads from one WebSocket frame, so no panel can disagree with another about the
 * station. The only extra request is the energy timeline's history, which is windowed and
 * therefore genuinely a separate question.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { EnergyTimeline, MarketPanel, type TimelineWindow } from '../components/MarketPanel'
import { AlertRail, GenerationStrip, HealthPanel } from '../components/HealthPanel'
import { NowPlayingPanel } from '../components/NowPlaying'
import { BufferPanel, EmergencyBanner, QueuePanel } from '../components/QueuePanel'
import { Button, LoadingState, Panel } from '../components/primitives'
import { api } from '../lib/api'
import { humanDuration, integer, percent } from '../lib/format'
import { useLive } from '../lib/live'

/**
 * Startup progress bar - shows when fresh, non-repetitive music will be ready.
 * Disappears once the target is met.
 */
function StartupProgress() {
  const { state, connection } = useLive()

  // Show connection status if not connected
  if (connection !== 'connected') {
    return (
      <div className="rounded-panel border border-red-500/40 bg-red-900/20 px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="h-2 w-2 animate-pulse rounded-full bg-red-500" />
          <span className="text-sm text-red-400">
            WebSocket: {connection} — waiting for connection...
          </span>
        </div>
      </div>
    )
  }

  if (!state) return null

  const buffer = state.buffer
  const generation = state.generation
  const status = state.status

  // Target: 8 minutes of fresh content OR station has been running for 5+ minutes
  const TARGET_MINUTES = 8
  const readyMinutes = buffer?.ready_minutes ?? 0
  const progress = Math.min(1, readyMinutes / TARGET_MINUTES)
  const secondsOnAir = status?.seconds_on_air ?? 0
  const isReady = progress >= 1 || secondsOnAir > 300

  // Don't show if we're already ready
  if (isReady) return null

  const remainingMinutes = Math.max(0, TARGET_MINUTES - readyMinutes)
  const genRate = generation?.capacity_ratio ?? 1
  const etaSeconds = genRate > 0 ? (remainingMinutes * 60) / genRate : 0
  const etaMinutes = Math.ceil(etaSeconds / 60)

  const progressPercent = Math.round(progress * 100)
  const barWidth = `${progressPercent}%`

  return (
    <div className="relative overflow-hidden rounded-panel border border-gold-500/40 bg-gradient-to-r from-ink-900 to-ink-900/80 px-4 py-3">
      {/* Progress bar background */}
      <div
        className="absolute inset-0 bg-gradient-to-r from-gold-500/20 to-gold-500/10 transition-all duration-500"
        style={{ width: barWidth }}
      />

      <div className="relative flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-6 w-6 items-center justify-center rounded-full border border-gold-500/50 bg-gold-500/20">
            <div className="h-2 w-2 animate-pulse rounded-full bg-gold-500" />
          </div>
          <div>
            <div className="text-sm font-semibold text-gold-400">
              PREPARING FRESH MUSIC
            </div>
            <div className="text-xs text-ink-400">
              On air: {Math.floor(secondsOnAir)}s • Updates every 2s
            </div>
          </div>
        </div>

        <div className="flex items-center gap-6">
          <div className="text-right">
            <div className="font-mono text-lg font-bold text-gold-400">
              {progressPercent}%
            </div>
            <div className="text-xs text-ink-500">
              {readyMinutes.toFixed(1)} / {TARGET_MINUTES} min
            </div>
          </div>

          <div className="text-right">
            <div className="font-mono text-sm text-ink-300">
              ~{etaMinutes} min
            </div>
            <div className="text-xs text-ink-500">remaining</div>
          </div>

          <div className="h-8 w-32 overflow-hidden rounded-full border border-ink-700 bg-ink-800">
            <div
              className="h-full bg-gradient-to-r from-gold-500 to-gold-400 transition-all duration-500"
              style={{ width: barWidth }}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

function StationStrip() {
  const { state } = useLive()
  const status = state?.status
  if (!status) return null

  const cells = [
    { label: 'Uptime', value: humanDuration(status.uptime_seconds) },
    { label: 'Tracks played', value: integer(status.tracks_played) },
    { label: 'Generated', value: integer(status.tracks_generated) },
    { label: 'On air', value: humanDuration(status.seconds_on_air) },
    {
      label: 'Audio coverage',
      value: percent(status.audio_coverage, 1),
      // The honest continuity metric (ADR-12). Called out because it is the one figure that
      // cannot be fooled by a zero underrun count.
      tone: (status.audio_coverage ?? 1) < 0.98 ? 'text-status-degraded' : 'text-status-healthy',
    },
    {
      label: 'Dead air',
      value: `${status.unintended_silence_seconds.toFixed(1)}s`,
      tone: status.unintended_silence_seconds > 0 ? 'text-status-critical' : 'text-status-healthy',
    },
  ]

  return (
    <div className="grid grid-cols-6 gap-4 rounded-panel border border-ink-800 bg-ink-900/60 px-4 py-2.5">
      {cells.map((cell) => (
        <div key={cell.label}>
          <div className="label truncate">{cell.label}</div>
          <div className={`font-mono text-sm tnum ${cell.tone ?? 'text-ink-200'}`}>{cell.value}</div>
        </div>
      ))}
    </div>
  )
}

export function DashboardPage() {
  const { state, connection } = useLive()
  const [window, setWindow] = useState<TimelineWindow>('1h')

  const history = useQuery({
    queryKey: ['market-history', window],
    queryFn: () => api.marketHistory(window),
    // The timeline is the one panel not on the socket; a slow poll keeps it current without
    // putting a query on every frame.
    refetchInterval: 15_000,
    retry: 1,
  })

  if (!state) {
    return (
      <div className="p-4">
        <Panel title="Dashboard">
          <LoadingState
            label={connection === 'offline' ? 'Cannot reach the station' : 'Connecting to the station'}
          />
        </Panel>
      </div>
    )
  }

  return (
    <div className="space-y-4 p-4" data-testid="dashboard">
      <StartupProgress />
      <AlertRail alerts={state.alerts} />
      <EmergencyBanner emergency={state.emergency} />
      <StationStrip />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <NowPlayingPanel track={state.now_playing} />
        <MarketPanel market={state.market} />
      </div>

      <EnergyTimeline
        history={history.data ?? null}
        window={window}
        onWindowChange={setWindow}
      />

      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="min-w-0">
          <QueuePanel queue={state.queue} />
        </div>
        <div className="space-y-4">
          <BufferPanel buffer={state.buffer} />
          <GenerationStrip generation={state.generation} />
          <HealthPanel components={state.health} />
        </div>
      </div>

      <div className="flex items-center justify-between rounded-panel border border-ink-800 bg-ink-900/60 px-4 py-2">
        <span className="label">Operator controls</span>
        <div className="flex items-center gap-2">
          <SkipButton />
          <span className="text-2xs text-ink-600">
            Only controls the runtime actually supports are offered.
          </span>
        </div>
      </div>
    </div>
  )
}

function SkipButton() {
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const { state } = useLive()

  async function skip() {
    setBusy(true)
    try {
      const result = await api.skip()
      setMessage(result.message)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'The skip failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center gap-3">
      {message && <span className="text-2xs text-ink-400">{message}</span>}
      <Button
        onClick={() => void skip()}
        disabled={busy || !state?.now_playing}
        title="End the current track at the next block boundary"
        data-testid="skip-button"
      >
        Skip track
      </Button>
    </div>
  )
}
