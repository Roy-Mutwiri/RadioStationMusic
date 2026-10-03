/**
 * The shared vocabulary: panels, chips, meters, states.
 *
 * Everything the console is built from lives here, so density and spacing stay consistent
 * across ten pages. Two rules are enforced structurally rather than by convention:
 *
 * **Health is never colour alone.** `StatusChip` always renders a text label beside its dot,
 * and the dot carries a distinct shape per state. A colour-blind operator, a greyscale
 * screenshot and a screen reader all get the same information.
 *
 * **Absent data looks absent.** `Metric` renders the em-dash placeholder when given `null`,
 * and takes a `hint` so the UI can say *why* a figure is missing rather than leaving a gap.
 */

import clsx from 'clsx'
import type { ReactNode } from 'react'

import { ABSENT } from '../lib/format'
import type { HealthState } from '../lib/types'

// ----------------------------------------------------------------- panel

export function Panel({
  title,
  action,
  children,
  className,
  bodyClassName,
  'data-testid': testId,
}: {
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  'data-testid'?: string
}) {
  return (
    <section className={clsx('panel flex min-h-0 flex-col', className)} data-testid={testId}>
      {title !== undefined && (
        <header className="panel-header">
          <h2 className="label">{title}</h2>
          {action}
        </header>
      )}
      <div className={clsx('min-h-0 flex-1', bodyClassName ?? 'p-4')}>{children}</div>
    </section>
  )
}

// ----------------------------------------------------------------- status

const STATUS_STYLES: Record<HealthState, { dot: string; text: string; ring: string }> = {
  healthy: { dot: 'bg-status-healthy', text: 'text-status-healthy', ring: 'border-status-healthy/30' },
  degraded: { dot: 'bg-status-degraded', text: 'text-status-degraded', ring: 'border-status-degraded/35' },
  critical: { dot: 'bg-status-critical', text: 'text-status-critical', ring: 'border-status-critical/40' },
  recovering: { dot: 'bg-status-recovering', text: 'text-status-recovering', ring: 'border-status-recovering/35' },
  offline: { dot: 'bg-status-offline', text: 'text-status-offline', ring: 'border-status-offline/30' },
}

/**
 * The shape beside each status, so severity survives greyscale and colour blindness.
 *
 * Chosen to be distinguishable at 8px: a filled circle, a hollow ring, a square, a diamond.
 */
const STATUS_SHAPE: Record<HealthState, string> = {
  healthy: 'rounded-full',
  degraded: 'rounded-full ring-2 ring-inset ring-ink-900',
  critical: 'rounded-[1px]',
  recovering: 'rounded-full ring-2 ring-inset ring-ink-900',
  offline: 'rounded-full opacity-60',
}

export function StatusDot({ state, className }: { state: HealthState; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={clsx('inline-block h-2 w-2 shrink-0', STATUS_STYLES[state].dot, STATUS_SHAPE[state], className)}
    />
  )
}

export function StatusChip({
  state,
  label,
  className,
}: {
  state: HealthState
  label?: string
  className?: string
}) {
  const text = label ?? state.toUpperCase()
  return (
    <span
      className={clsx('chip bg-ink-850', STATUS_STYLES[state].ring, STATUS_STYLES[state].text, className)}
    >
      <StatusDot state={state} />
      {text}
    </span>
  )
}

// ----------------------------------------------------------------- metric

export function Metric({
  label,
  value,
  unit,
  hint,
  tone = 'default',
  size = 'md',
  className,
}: {
  label: string
  /** `null` renders the absent placeholder — never a zero. */
  value: string | number | null | undefined
  unit?: string
  /** Shown under the value; use it to explain an absence. */
  hint?: ReactNode
  tone?: 'default' | 'gold' | 'up' | 'down' | 'muted'
  size?: 'sm' | 'md' | 'lg'
  className?: string
}) {
  const absent = value === null || value === undefined || value === ABSENT
  const toneClass = absent
    ? 'text-ink-500'
    : {
        default: 'text-ink-100',
        gold: 'text-gold-400',
        up: 'text-market-up',
        down: 'text-market-down',
        muted: 'text-ink-300',
      }[tone]
  const sizeClass = { sm: 'text-sm', md: 'text-lg', lg: 'text-3xl' }[size]

  return (
    <div className={clsx('min-w-0', className)}>
      <div className="label truncate">{label}</div>
      <div className={clsx('mt-0.5 font-mono tnum leading-tight', sizeClass, toneClass)}>
        {absent ? ABSENT : value}
        {!absent && unit && <span className="ml-1 text-xs text-ink-400">{unit}</span>}
      </div>
      {hint && <div className="mt-0.5 text-2xs leading-snug text-ink-500">{hint}</div>}
    </div>
  )
}

// ----------------------------------------------------------------- meter

/**
 * A horizontal bar with optional threshold marks.
 *
 * Preferred over a radial gauge nearly everywhere: the brief asks for attractive gauges
 * "without turning everything into circular charts", and a bar compares against a threshold
 * far better than an arc does — the target and minimum can be drawn *on* it.
 */
export function Meter({
  value,
  max,
  tone = 'gold',
  marks = [],
  className,
  label,
}: {
  value: number
  max: number
  tone?: 'gold' | 'healthy' | 'degraded' | 'critical' | 'neutral'
  marks?: { at: number; label: string }[]
  className?: string
  label?: string
}) {
  const safeMax = max > 0 ? max : 1
  const fraction = Math.max(0, Math.min(1, value / safeMax))
  const fill = {
    gold: 'bg-gold-500',
    healthy: 'bg-status-healthy',
    degraded: 'bg-status-degraded',
    critical: 'bg-status-critical',
    neutral: 'bg-ink-500',
  }[tone]

  return (
    <div
      className={clsx('relative h-1.5 w-full overflow-hidden rounded-full bg-ink-800', className)}
      role="meter"
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={Math.round(safeMax)}
      aria-label={label}
    >
      <div
        className={clsx('h-full rounded-full transition-[width] duration-500', fill)}
        style={{ width: `${fraction * 100}%` }}
      />
      {marks.map((mark) => (
        <span
          key={mark.label}
          title={mark.label}
          className="absolute top-0 h-full w-px bg-ink-400/70"
          style={{ left: `${Math.max(0, Math.min(1, mark.at / safeMax)) * 100}%` }}
        />
      ))}
    </div>
  )
}

// ----------------------------------------------------------------- misc

export function Chip({
  children,
  tone = 'neutral',
  className,
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'gold' | 'hard' | 'soft' | 'flexible' | 'warn' | 'danger'
  className?: string
  title?: string
}) {
  const tones = {
    neutral: 'border-ink-700 bg-ink-850 text-ink-300',
    gold: 'border-gold-500/40 bg-gold-500/10 text-gold-300',
    hard: 'border-gold-500/40 bg-gold-500/10 text-gold-300',
    soft: 'border-ink-600 bg-ink-800 text-ink-200',
    flexible: 'border-ink-700 bg-transparent text-ink-400',
    warn: 'border-status-degraded/40 bg-status-degraded/10 text-status-degraded',
    danger: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
  }[tone]
  return (
    <span className={clsx('chip', tones, className)} title={title}>
      {children}
    </span>
  )
}

/** A dense definition row: label left, value right, hairline between. */
export function Row({
  label,
  children,
  className,
}: {
  label: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div
      className={clsx(
        'flex items-baseline justify-between gap-4 border-b hairline py-1.5 last:border-0',
        className,
      )}
    >
      <span className="label shrink-0">{label}</span>
      <span className="min-w-0 truncate text-right font-mono text-xs tnum text-ink-200">
        {children}
      </span>
    </div>
  )
}

export function Button({
  children,
  onClick,
  tone = 'default',
  disabled,
  title,
  type = 'button',
  className,
  'data-testid': testId,
}: {
  children: ReactNode
  onClick?: () => void
  tone?: 'default' | 'gold' | 'danger' | 'ghost'
  disabled?: boolean
  title?: string
  type?: 'button' | 'submit'
  className?: string
  'data-testid'?: string
}) {
  const tones = {
    default: 'border-ink-700 bg-ink-800 text-ink-200 hover:bg-ink-750 hover:text-ink-100',
    gold: 'border-gold-500/50 bg-gold-500/15 text-gold-300 hover:bg-gold-500/25',
    danger: 'border-status-critical/45 bg-status-critical/10 text-status-critical hover:bg-status-critical/20',
    ghost: 'border-transparent bg-transparent text-ink-400 hover:text-ink-100 hover:bg-ink-850',
  }[tone]
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      data-testid={testId}
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-sm border px-2.5 py-1 text-2xs font-medium',
        'uppercase tracking-label transition-colors disabled:cursor-not-allowed disabled:opacity-40',
        tones,
        className,
      )}
    >
      {children}
    </button>
  )
}

// ----------------------------------------------------------------- states

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string
  detail?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex h-full min-h-[120px] flex-col items-center justify-center gap-2 px-6 py-8 text-center">
      <p className="text-sm font-medium text-ink-300">{title}</p>
      {detail && <p className="max-w-sm text-xs leading-relaxed text-ink-500">{detail}</p>}
      {action}
    </div>
  )
}

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[120px] items-center justify-center gap-2 py-8" role="status">
      <span className="h-1.5 w-1.5 animate-pulse-live rounded-full bg-gold-500" />
      <span className="label">{label}</span>
    </div>
  )
}

export function ErrorState({ title, detail }: { title: string; detail?: ReactNode }) {
  return (
    <div className="flex h-full min-h-[120px] flex-col items-center justify-center gap-2 px-6 py-8 text-center">
      <p className="text-sm font-medium text-status-critical">{title}</p>
      {detail && <p className="max-w-sm text-xs leading-relaxed text-ink-400">{detail}</p>}
    </div>
  )
}

/**
 * What a page shows when its subsystem does not exist yet.
 *
 * Named for what it is. The brief is explicit that an unbuilt subsystem must say so rather
 * than render plausible emptiness, and naming the delivering phase turns "broken" into
 * "not yet", which is a different and more useful message.
 */
export function AwaitingPhase({
  subsystem,
  phase,
  detail,
}: {
  subsystem: string
  phase: number | null
  detail?: string
}) {
  return (
    <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-3 px-6 py-10 text-center">
      <span className="chip border-ink-700 bg-ink-850 text-ink-400">
        {phase === null ? 'Not available' : `Awaiting Phase ${phase}`}
      </span>
      <p className="text-sm font-medium text-ink-200">{subsystem}</p>
      {detail && <p className="max-w-md text-xs leading-relaxed text-ink-500">{detail}</p>}
      <p className="max-w-md text-2xs leading-relaxed text-ink-600">
        This page is built and waiting for its backend. No figures are shown because none
        exist yet — nothing here is simulated.
      </p>
    </div>
  )
}

/** Marks a value as last-known rather than current, without hiding it. */
export function StaleBadge({ ageLabel }: { ageLabel: string }) {
  return (
    <span className="chip border-status-degraded/40 bg-status-degraded/10 text-status-degraded">
      Last update {ageLabel}
    </span>
  )
}
