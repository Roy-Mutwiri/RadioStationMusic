/**
 * SYSTEM HEALTH and the alert rail.
 *
 * Each row shows a state, a short explanation and — where there is one — a single number.
 * The explanation is the point: the brief's standard is that an operator must not have to
 * open logs to understand a basic problem, which means "GENERATOR DEGRADED" is not a
 * finished message. "2 failed and 1 timed out; buffer remains safe at 38 min" is.
 *
 * The backend composes those sentences, because only it knows whether the broadcast is
 * actually at risk. This component does not interpret, rank or soften them.
 */

import clsx from 'clsx'

import { clockTime } from '../lib/format'
import type { Alert, HealthComponent } from '../lib/types'
import { EmptyState, Panel, StatusChip } from './primitives'

export function HealthPanel({ components }: { components: HealthComponent[] }) {
  if (components.length === 0) {
    return (
      <Panel title="System health" data-testid="health-panel">
        <EmptyState title="No health data" detail="The API is not reporting component health." />
      </Panel>
    )
  }

  return (
    <Panel title="System health" data-testid="health-panel" bodyClassName="p-0">
      <ul className="divide-y divide-ink-800/80">
        {components.map((component) => (
          <li
            key={component.name}
            className="flex items-start gap-3 px-4 py-2"
            data-testid={`health-${component.name}`}
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate text-xs font-medium text-ink-200">
                  {component.label}
                </span>
                {component.metric && (
                  <span className="shrink-0 font-mono text-2xs tnum text-ink-500">
                    {component.metric}
                  </span>
                )}
              </div>
              <p className="mt-0.5 text-2xs leading-snug text-ink-500">{component.detail}</p>
            </div>
            <StatusChip state={component.state} className="mt-0.5 shrink-0" />
          </li>
        ))}
      </ul>
    </Panel>
  )
}

const SEVERITY_STYLE: Record<Alert['severity'], string> = {
  info: 'border-ink-700 bg-ink-850',
  warning: 'border-status-degraded/40 bg-status-degraded/10',
  critical: 'border-status-critical/45 bg-status-critical/10',
}

const SEVERITY_TEXT: Record<Alert['severity'], string> = {
  info: 'text-ink-300',
  warning: 'text-status-degraded',
  critical: 'text-status-critical',
}

/**
 * Alerts, each with what to do about it.
 *
 * An alert without a remediation is a notification, and a console full of notifications
 * teaches an operator to ignore the rail. The backend attaches one wherever there is a clear
 * answer; where there is not, the alert still says what happened rather than inventing advice.
 */
export function AlertRail({ alerts }: { alerts: Alert[] }) {
  if (alerts.length === 0) return null

  return (
    <div className="space-y-1.5" data-testid="alert-rail">
      {alerts.map((alert) => (
        <div
          key={alert.key}
          role="alert"
          className={clsx('flex items-start gap-3 rounded-panel border px-3 py-2', SEVERITY_STYLE[alert.severity])}
        >
          <span
            aria-hidden="true"
            className={clsx(
              'mt-1 h-1.5 w-1.5 shrink-0',
              alert.severity === 'critical' ? 'rounded-[1px] bg-status-critical' : 'rounded-full bg-status-degraded',
            )}
          />
          <div className="min-w-0 flex-1">
            <p className={clsx('text-xs font-medium', SEVERITY_TEXT[alert.severity])}>
              {alert.message}
            </p>
            {alert.remediation && (
              <p className="mt-0.5 text-2xs leading-snug text-ink-400">{alert.remediation}</p>
            )}
          </div>
          <span className="shrink-0 font-mono text-2xs text-ink-600">
            {clockTime(alert.raised_at)}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * The generation health strip.
 *
 * Capacity ratio leads because it is the figure that predicts dead air: below 1.0 the station
 * is generating less audio than it plays, and no amount of queue depth fixes that.
 */
export function GenerationStrip({
  generation,
}: {
  generation: {
    provider: string
    capacity_ratio: number
    latency_p50_seconds: number | null
    latency_p95_seconds: number | null
    completed: number
    failed: number
    timeouts: number
    retries: number
    in_flight: number
  } | null
}) {
  if (!generation) {
    return (
      <Panel title="Generation" data-testid="generation-strip">
        <EmptyState title="No generator attached" detail="This API process has no generation manager." />
      </Panel>
    )
  }

  const losing = generation.capacity_ratio < 1.0
  const cells: { label: string; value: string; tone?: string }[] = [
    {
      label: 'Capacity',
      value: `${generation.capacity_ratio.toFixed(2)}×`,
      tone: losing ? 'text-status-degraded' : 'text-status-healthy',
    },
    {
      label: 'p50',
      value: generation.latency_p50_seconds === null ? '—' : `${generation.latency_p50_seconds.toFixed(0)}s`,
    },
    {
      label: 'p95',
      value: generation.latency_p95_seconds === null ? '—' : `${generation.latency_p95_seconds.toFixed(0)}s`,
    },
    { label: 'Done', value: String(generation.completed) },
    {
      label: 'Failed',
      value: String(generation.failed),
      tone: generation.failed > 0 ? 'text-status-degraded' : undefined,
    },
    { label: 'In flight', value: String(generation.in_flight) },
  ]

  return (
    <Panel
      title="Generation"
      data-testid="generation-strip"
      action={<span className="font-mono text-2xs text-ink-500">{generation.provider}</span>}
    >
      <div className="grid grid-cols-6 gap-2">
        {cells.map((cell) => (
          <div key={cell.label}>
            <div className="label truncate">{cell.label}</div>
            <div className={clsx('font-mono text-sm tnum', cell.tone ?? 'text-ink-200')}>
              {cell.value}
            </div>
          </div>
        ))}
      </div>
      {losing && (
        <p className="mt-2 border-t hairline pt-2 text-2xs leading-relaxed text-status-degraded">
          Generating slower than playback. The buffer drains until this recovers;
          experimentation is already withheld.
        </p>
      )}
    </Panel>
  )
}
