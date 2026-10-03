/**
 * Formatting helpers.
 *
 * Centralised because the same value must read identically everywhere — a duration in the
 * queue, in Now Playing and in the overlay is the same duration, and three components
 * formatting it three ways is how a dashboard starts looking amateur.
 *
 * The rule throughout: **`null` in, placeholder out.** Every function here returns a visible
 * em-dash for an absent value rather than "0" or "NaN". That is the UI half of §86's ban on
 * fabricated metrics — the backend declines to send a number it does not have, and these
 * refuse to invent one on its behalf.
 */

/** What an absent value looks like. One character, unmistakably "no data". */
export const ABSENT = '—'

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return ABSENT
  const total = Math.max(0, Math.round(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
  }
  return `${minutes}:${String(secs).padStart(2, '0')}`
}

/** "4 min", "2 h 11 min" — for spans a human thinks about in minutes, not seconds. */
export function humanDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return ABSENT
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

export function minutes(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return `${value.toFixed(digits)} min`
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

/** Signed, so a change reads as a change even before colour is applied. */
export function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}`
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return `${(value * 100).toFixed(digits)}%`
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return `${value.toFixed(digits)}×`
}

export function integer(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return Math.round(value).toLocaleString('en-US')
}

export function decimal(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT
  return value.toFixed(digits)
}

/** `bullish_breakout` → `Bullish Breakout`. */
export function titleCase(value: string | null | undefined): string {
  if (!value) return ABSENT
  return value
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

/** "42s ago", "3 min ago" — for staleness, where the unit matters more than precision. */
export function since(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined) return ABSENT
  const seconds = Math.max(0, Math.round(milliseconds / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const mins = Math.round(seconds / 60)
  if (mins < 60) return `${mins} min ago`
  return `${Math.round(mins / 60)} h ago`
}

export function clockTime(value: string | Date | null | undefined): string {
  if (!value) return ABSENT
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ABSENT
  return date.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function shortDate(value: string | Date | null | undefined): string {
  if (!value) return ABSENT
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ABSENT
  return date.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

/**
 * "starts in 4:12", or "now" for the head of the queue.
 *
 * Zero is rendered as "now" rather than "0:00" because the head of a queue is not four
 * minutes away by a rounding error — it is next, and saying so is more useful.
 */
export function startsIn(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ABSENT
  if (seconds < 1) return 'now'
  return duration(seconds)
}

/** A buffer trend, phrased the way an operator would say it. */
export function bufferTrend(minutesPerHour: number | null | undefined): string {
  if (
    minutesPerHour === null ||
    minutesPerHour === undefined ||
    !Number.isFinite(minutesPerHour)
  ) {
    return ABSENT
  }
  if (Math.abs(minutesPerHour) < 0.1) return 'holding'
  // A mock provider that renders a 30-second track in half a second measures §93 capacity at
  // 63x, which turns into "+3744.3 min/hour" — arithmetically correct and useless, and it
  // makes the panel look broken. Past an hour of buffer gained per hour the exact figure
  // stops carrying information; what matters is the direction and that it is comfortable.
  if (minutesPerHour > 60) return 'filling rapidly'
  if (minutesPerHour < -60) return 'draining rapidly'
  const sign = minutesPerHour > 0 ? '+' : ''
  return `${sign}${minutesPerHour.toFixed(1)} min/hour`
}
