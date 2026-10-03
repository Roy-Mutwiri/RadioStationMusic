/**
 * UPCOMING QUEUE, the buffer display, and the emergency banner.
 *
 * The queue table is where §28's layered locking becomes visible, and the important design
 * decision is that **protection is shown, not just lock level**. A HARD chip tells you the
 * rule; a disabled unlock button tells you what you can do about it. Both, together, mean an
 * operator never discovers a constraint by having an action rejected.
 *
 * The buffer display leads with minutes rather than track count because minutes are what the
 * station runs out of. "8 tracks" means nothing without their durations; "47 min" is
 * immediately comparable to the target, the minimum, and how long it would take to fix a
 * dead generator.
 */

import clsx from 'clsx'
import { useState } from 'react'

import { api } from '../lib/api'
import {
  ABSENT,
  bufferTrend,
  decimal,
  duration,
  humanDuration,
  minutes,
  ratio,
  startsIn,
  titleCase,
} from '../lib/format'
import type { BufferState, EmergencyState, QueueItem } from '../lib/types'
import { Button, Chip, EmptyState, Meter, Panel } from './primitives'

// ----------------------------------------------------------------- buffer

const LEVEL_TONE: Record<BufferState['level'], { text: string; meter: 'healthy' | 'degraded' | 'critical'; label: string }> = {
  healthy: { text: 'text-status-healthy', meter: 'healthy', label: 'HEALTHY' },
  low: { text: 'text-status-degraded', meter: 'degraded', label: 'LOW' },
  critical: { text: 'text-status-critical', meter: 'critical', label: 'CRITICAL' },
  empty: { text: 'text-status-critical', meter: 'critical', label: 'EMPTY' },
}

export function BufferPanel({ buffer }: { buffer: BufferState | null }) {
  if (!buffer) {
    return (
      <Panel title="Station buffer" data-testid="buffer-panel">
        <EmptyState title="No buffer reading" detail="The station is not reporting queue depth." />
      </Panel>
    )
  }

  const tone = LEVEL_TONE[buffer.level]
  const draining = buffer.trend_minutes_per_hour < -0.1
  const failingSoon = buffer.seconds_to_failure !== null

  return (
    <Panel title="Station buffer" data-testid="buffer-panel">
      <div className="space-y-3">
        <div className="flex items-end justify-between gap-3">
          <div>
            <div className="flex items-baseline gap-2">
              <span
                className={clsx('font-mono text-3xl tnum leading-none', tone.text)}
                data-testid="buffer-minutes"
              >
                {decimal(buffer.ready_minutes, 0)}
              </span>
              <span className="text-sm text-ink-400">min ready</span>
            </div>
            <div className={clsx('mt-1 text-2xs font-semibold uppercase tracking-label', tone.text)}>
              {tone.label}
            </div>
          </div>
          <div className="text-right">
            <div className="label">Trend</div>
            <div
              className={clsx(
                'font-mono text-sm tnum',
                draining ? 'text-status-degraded' : 'text-status-healthy',
              )}
              data-testid="buffer-trend"
            >
              {bufferTrend(buffer.trend_minutes_per_hour)}
            </div>
          </div>
        </div>

        <div>
          <Meter
            value={buffer.ready_minutes}
            max={buffer.maximum_minutes}
            tone={tone.meter}
            label="Ready audio against the configured maximum"
            marks={[
              { at: buffer.minimum_minutes, label: `minimum ${buffer.minimum_minutes} min` },
              { at: buffer.target_minutes, label: `target ${buffer.target_minutes} min` },
            ]}
          />
          <div className="mt-1 flex justify-between text-2xs text-ink-600">
            <span>min {decimal(buffer.minimum_minutes, 0)}</span>
            <span>target {decimal(buffer.target_minutes, 0)}</span>
            <span>max {decimal(buffer.maximum_minutes, 0)}</span>
          </div>
        </div>

        {/* Shown only when it means something. A countdown that is always on screen is
            furniture; one that appears when the buffer starts losing is a warning. */}
        {failingSoon && (
          <div
            className="rounded-sm border border-status-critical/40 bg-status-critical/10 px-3 py-2"
            data-testid="buffer-failure-warning"
          >
            <div className="label text-status-critical">At the current rate</div>
            <div className="font-mono text-sm text-status-critical">
              Critical in ~{humanDuration(buffer.seconds_to_failure)}
            </div>
          </div>
        )}

        <div className="grid grid-cols-3 gap-3 border-t hairline pt-2">
          <div>
            <div className="label">In flight</div>
            <div className="font-mono text-sm tnum text-ink-200">
              {minutes(buffer.pending_minutes, 0)}
            </div>
          </div>
          <div>
            <div className="label">Capacity</div>
            <div
              className={clsx(
                'font-mono text-sm tnum',
                buffer.capacity_ratio >= 1 ? 'text-ink-200' : 'text-status-degraded',
              )}
            >
              {ratio(buffer.capacity_ratio)}
            </div>
          </div>
          <div>
            <div className="label">Tracks</div>
            <div className="font-mono text-sm tnum text-ink-200">
              {buffer.ready_tracks}
              <span className="text-ink-600"> / {buffer.ready_tracks + buffer.pending_tracks}</span>
            </div>
          </div>
        </div>

        <p className="text-2xs leading-relaxed text-ink-500">{buffer.reason}</p>
      </div>
    </Panel>
  )
}

// ----------------------------------------------------------------- emergency

/**
 * The tier banner.
 *
 * Subtle when normal — a single muted line — and unmissable when not. §33's tiers are the
 * station's last line against dead air, so an operator must never have to go looking for
 * which one is carrying the output.
 */
export function EmergencyBanner({ emergency }: { emergency: EmergencyState | null }) {
  if (!emergency) return null

  if (!emergency.is_degraded) {
    return (
      <div
        className="flex items-center justify-between gap-3 rounded-panel border border-ink-800 bg-ink-900/60 px-3 py-1.5"
        data-testid="emergency-banner"
      >
        <span className="label">Audio tier</span>
        <span className="flex items-center gap-2 text-2xs text-ink-400">
          <span className="font-medium text-status-healthy">NORMAL</span>
          <span className="text-ink-600">
            Tier 2 reserve {decimal(emergency.reserve_minutes, 0)} min
          </span>
        </span>
      </div>
    )
  }

  return (
    <div
      className="flex items-center justify-between gap-3 rounded-panel border border-status-degraded/50 bg-status-degraded/10 px-3 py-2 shadow-glow"
      role="status"
      data-testid="emergency-banner"
    >
      <div className="flex items-center gap-2.5">
        <span aria-hidden="true" className="h-2 w-2 animate-pulse-live rounded-sm bg-status-degraded" />
        <div>
          <div className="text-xs font-semibold uppercase tracking-label text-status-degraded">
            {emergency.tier_label}
          </div>
          <div className="text-2xs text-ink-400">
            {emergency.last_reason ?? 'Emergency audio is carrying the broadcast.'}
          </div>
        </div>
      </div>
      <div className="text-right text-2xs text-ink-500">
        <div>Returns automatically when a track is ready</div>
        <div className="font-mono">
          {emergency.recoveries} recover{emergency.recoveries === 1 ? 'y' : 'ies'} this run
        </div>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------- queue

const LOCK_TONE: Record<QueueItem['lock_label'], 'hard' | 'soft' | 'flexible' | 'gold'> = {
  HARD: 'hard',
  SOFT: 'soft',
  FLEXIBLE: 'flexible',
  PINNED: 'gold',
}

function readinessChip(item: QueueItem) {
  if (item.readiness === 'unavailable') return <Chip tone="danger">UNAVAILABLE</Chip>
  if (item.readiness === 'ready') return <Chip tone="soft">READY</Chip>
  const progress = item.generation_progress
  return (
    <Chip tone="warn">
      {item.generation_state.toUpperCase()}
      {progress !== null && progress > 0 ? ` ${Math.round(progress * 100)}%` : ''}
    </Chip>
  )
}

export function QueuePanel({
  queue,
  onChanged,
}: {
  queue: QueueItem[]
  /** Called after a successful mutation so the caller can refresh immediately. */
  onChanged?: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function mutate(trackId: string, action: 'lock' | 'unlock') {
    setBusy(trackId)
    setMessage(null)
    try {
      const result = action === 'lock' ? await api.lock(trackId) : await api.unlock(trackId)
      // The API answers with what it actually did, which is what gets shown. "Accepted" is
      // not an outcome, and a pin that was refused must say why.
      setMessage(result.message)
      onChanged?.()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'The control failed.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel
      title="Upcoming queue"
      data-testid="queue-panel"
      action={<span className="font-mono text-2xs text-ink-500">{queue.length} queued</span>}
      bodyClassName="flex min-h-0 flex-col"
    >
      {queue.length === 0 ? (
        <EmptyState
          title="Nothing queued"
          detail="The scheduler plans tracks as the buffer drains. An empty queue with a healthy generator resolves within a cycle."
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">
          <table className="w-full border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-ink-900">
              <tr className="border-b border-ink-800 text-left">
                <th scope="col" className="label px-3 py-2 font-medium">#</th>
                <th scope="col" className="label px-2 py-2 font-medium">Lock</th>
                <th scope="col" className="label px-2 py-2 font-medium">Track</th>
                <th scope="col" className="label px-2 py-2 font-medium">Genre</th>
                <th scope="col" className="label px-2 py-2 text-right font-medium">BPM</th>
                <th scope="col" className="label px-2 py-2 text-right font-medium">Energy</th>
                <th scope="col" className="label px-2 py-2 font-medium">Regime</th>
                <th scope="col" className="label px-2 py-2 font-medium">Generation</th>
                <th scope="col" className="label px-2 py-2 text-right font-medium">Length</th>
                <th scope="col" className="label px-2 py-2 text-right font-medium">Starts in</th>
                <th scope="col" className="label px-3 py-2 text-right font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {queue.map((item) => (
                <tr
                  key={item.track_id}
                  className="border-b hairline transition-colors hover:bg-ink-850/60"
                  data-testid="queue-row"
                >
                  <td className="px-3 py-1.5 font-mono text-2xs tnum text-ink-500">
                    {item.position + 1}
                  </td>
                  <td className="px-2 py-1.5">
                    <Chip tone={LOCK_TONE[item.lock_label]} title={item.lock_reason ?? undefined}>
                      {item.lock_label}
                    </Chip>
                  </td>
                  <td className="max-w-[220px] px-2 py-1.5">
                    <div className="truncate text-ink-200" title={item.title}>
                      {item.title}
                    </div>
                    <div className="truncate font-mono text-2xs text-ink-600">{item.track_id}</div>
                  </td>
                  <td className="px-2 py-1.5 text-ink-300">{titleCase(item.genre)}</td>
                  <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                    {item.bpm ?? ABSENT}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                    {decimal(item.energy, 0)}
                  </td>
                  <td className="px-2 py-1.5 text-ink-400">{titleCase(item.planned_regime)}</td>
                  <td className="px-2 py-1.5">{readinessChip(item)}</td>
                  <td className="px-2 py-1.5 text-right font-mono tnum text-ink-400">
                    {duration(item.duration_seconds)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                    {startsIn(item.starts_in_seconds)}
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    {item.lock === 'operator_pinned' ? (
                      <Button
                        tone="ghost"
                        onClick={() => void mutate(item.track_id, 'unlock')}
                        disabled={busy === item.track_id}
                        title="Release the operator pin"
                      >
                        Unpin
                      </Button>
                    ) : (
                      <Button
                        tone="ghost"
                        onClick={() => void mutate(item.track_id, 'lock')}
                        disabled={busy === item.track_id || item.is_protected}
                        title={
                          item.is_protected
                            ? 'Already protected by its position in the queue'
                            : 'Pin this track so a replan cannot replace it'
                        }
                      >
                        Pin
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {message && (
        <p
          className="border-t hairline px-3 py-1.5 text-2xs text-ink-400"
          role="status"
          data-testid="queue-message"
        >
          {message}
        </p>
      )}
    </Panel>
  )
}
