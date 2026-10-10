/**
 * NOW PLAYING, and the panel that explains it.
 *
 * Two things here are deliberately not what a music dashboard usually does.
 *
 * **The level meter is real or absent.** It is driven by the playout engine's measured peak
 * sample, which the API sends with each position message. A decorative sine-wave "waveform"
 * would be the single most dishonest pixel in the console — it would animate happily through
 * total silence — so when the backend sends no peak, the meter renders an explicit "no
 * signal data" instead of moving.
 *
 * **"Why this track?" shows stored rationale only.** The director writes down its reasoning
 * as it decides; this renders that text. Nothing is inferred here and nothing is generated —
 * the brief rules out exposing hidden model reasoning, and the deterministic factors are both
 * checkable and more useful.
 *
 * The progress bar subscribes to `usePosition`, not `useLive`, so it updates twice a second
 * while the rest of the panel re-renders only when the track changes.
 */

import clsx from 'clsx'
import { memo, useState, useCallback } from 'react'

import { api } from '../lib/api'
import { ABSENT, decimal, duration, percent, titleCase } from '../lib/format'
import { usePosition } from '../lib/live'
import type { NowPlaying as NowPlayingData } from '../lib/types'
import { Chip, EmptyState, Panel } from './primitives'

/**
 * The output level, from the engine's real peak measurement.
 *
 * Rendered as discrete segments rather than a smooth bar because a segmented meter is what a
 * broadcast desk uses and because it reads accurately at a glance: you count lit segments
 * rather than estimate a width.
 */
const SEGMENTS = 24

const LevelMeter = memo(function LevelMeter({ peak }: { peak: number | null }) {
  if (peak === null) {
    return (
      <div className="flex h-6 items-center text-2xs text-ink-600" data-testid="level-meter-absent">
        No signal data from the sink
      </div>
    )
  }
  const lit = Math.round(Math.max(0, Math.min(1, peak)) * SEGMENTS)
  return (
    <div
      className="flex h-6 items-end gap-[2px]"
      data-testid="level-meter"
      role="meter"
      aria-valuenow={Math.round(peak * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="Output level"
    >
      {Array.from({ length: SEGMENTS }, (_, index) => {
        const on = index < lit
        // The top fifth is the clipping zone; it turns amber rather than red because the
        // engine already guarantees no clipping, so this is information, not an alarm.
        const hot = index > SEGMENTS * 0.8
        return (
          <span
            key={index}
            className={clsx(
              'w-full rounded-[1px] transition-colors duration-150',
              on ? (hot ? 'bg-status-degraded' : 'bg-gold-500') : 'bg-ink-800',
            )}
            style={{ height: `${35 + (index / SEGMENTS) * 65}%` }}
          />
        )
      })}
    </div>
  )
})

const ProgressBar = memo(function ProgressBar({ fallback }: { fallback: NowPlayingData }) {
  // Prefers the high-frequency position message and falls back to the structural frame, so
  // the bar is correct immediately on load and smooth thereafter.
  const position = usePosition()
  const live =
    position && position.track_id === fallback.track_id
      ? position
      : {
          elapsed_seconds: fallback.elapsed_seconds,
          remaining_seconds: fallback.remaining_seconds,
          duration_seconds: fallback.duration_seconds,
          progress: fallback.progress,
          output_peak: fallback.output_peak,
        }

  return (
    <div className="space-y-2">
      <LevelMeter peak={live.output_peak ?? null} />
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-ink-800"
        role="progressbar"
        aria-valuenow={Math.round(live.progress * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Track progress"
      >
        <div
          className="h-full bg-gold-500 transition-[width] duration-500 ease-linear"
          style={{ width: `${Math.min(100, live.progress * 100)}%` }}
        />
      </div>
      <div className="flex justify-between font-mono text-xs tnum">
        <span className="text-ink-200" data-testid="elapsed">
          {duration(live.elapsed_seconds)}
        </span>
        <span className="text-ink-500">-{duration(live.remaining_seconds)}</span>
      </div>
    </div>
  )
})

/**
 * Cover art, generated from the track id.
 *
 * Deterministic from the id, so a track always looks the same — and visibly abstract, so it
 * is never mistaken for real artwork the station does not have. Artwork generation is not in
 * any phase of the plan; this is a placeholder that admits to being one.
 */
function CoverArt({ trackId, genre }: { trackId: string; genre: string | null }) {
  let hash = 0
  for (let index = 0; index < trackId.length; index += 1) {
    hash = (hash * 31 + trackId.charCodeAt(index)) % 360
  }
  return (
    <div
      className="relative aspect-square w-full shrink-0 overflow-hidden rounded-sm border border-ink-800"
      style={{
        background: `linear-gradient(145deg, hsl(${hash} 22% 12%), hsl(${(hash + 40) % 360} 18% 7%))`,
      }}
      aria-hidden="true"
    >
      <div
        className="absolute inset-0 opacity-50"
        style={{
          background: `repeating-linear-gradient(${hash % 180}deg, transparent 0 7px, rgba(201,162,39,0.07) 7px 8px)`,
        }}
      />
      <div className="absolute inset-x-0 bottom-0 p-2">
        <div className="truncate font-mono text-2xs uppercase tracking-label text-gold-500/70">
          {genre ? titleCase(genre) : 'Trade Fix Radio'}
        </div>
      </div>
    </div>
  )
}

function WhyThisTrack({ track }: { track: NowPlayingData }) {
  const [open, setOpen] = useState(false)
  const reason = track.reason

  if (!reason) {
    return (
      <p className="text-2xs leading-relaxed text-ink-500">
        {track.tier === 'scheduled'
          ? 'No recorded rationale for this track.'
          : `${titleCase(track.tier)} audio has no blueprint — it is emergency cover, not programming.`}
      </p>
    )
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 text-left"
        data-testid="why-toggle"
      >
        <span className="label text-gold-500/80">Why this track?</span>
        <span className="font-mono text-2xs text-ink-500">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="mt-2 space-y-2" data-testid="why-panel">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1">
            <div>
              <div className="label">Market regime</div>
              <div className="text-xs text-ink-200">{titleCase(reason.market_regime)}</div>
            </div>
            <div>
              <div className="label">Energy</div>
              <div className="font-mono text-xs tnum text-ink-200">
                {decimal(reason.market_energy, 0)} → {decimal(reason.target_energy, 0)}
              </div>
            </div>
          </div>

          <div>
            <div className="label mb-1">Programming response</div>
            <ul className="space-y-0.5">
              {reason.factors.map((factor, index) => (
                <li
                  key={`${factor}-${index}`}
                  className="flex gap-1.5 text-2xs leading-relaxed text-ink-300"
                >
                  <span aria-hidden="true" className="mt-[5px] h-1 w-1 shrink-0 rounded-full bg-gold-500/60" />
                  <span>{factor}</span>
                </li>
              ))}
            </ul>
          </div>

          {reason.creative_temperature !== null && (
            <div className="flex items-baseline justify-between border-t hairline pt-1.5">
              <span className="label">Creative temperature</span>
              <span className="font-mono text-2xs tnum text-ink-300">
                {decimal(reason.creative_temperature, 2)}
              </span>
            </div>
          )}
          <p className="border-t hairline pt-1.5 text-2xs leading-relaxed text-ink-600">
            Recorded by the director when it planned this track. These are the system's own
            stored factors, not a generated explanation.
          </p>
        </div>
      )}
    </div>
  )
}

function PlayPauseButton({ isPaused }: { isPaused: boolean }) {
  const [loading, setLoading] = useState(false)

  const handleClick = useCallback(async () => {
    setLoading(true)
    try {
      if (isPaused) {
        await api.resume()
      } else {
        await api.pause()
      }
    } finally {
      setLoading(false)
    }
  }, [isPaused])

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={loading}
      className={clsx(
        'flex h-10 w-10 items-center justify-center rounded-full border transition-colors',
        isPaused
          ? 'border-gold-500 bg-gold-500/20 text-gold-400 hover:bg-gold-500/30'
          : 'border-ink-700 bg-ink-800 text-ink-300 hover:bg-ink-700 hover:text-ink-100',
        loading && 'opacity-50 cursor-not-allowed'
      )}
      aria-label={isPaused ? 'Resume playback' : 'Pause playback'}
      title={isPaused ? 'Resume' : 'Pause'}
    >
      {isPaused ? (
        <svg className="h-5 w-5 ml-0.5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M8 5v14l11-7z" />
        </svg>
      ) : (
        <svg className="h-5 w-5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
        </svg>
      )}
    </button>
  )
}

function DuckingButton({ duckingEnabled }: { duckingEnabled: boolean }) {
  const [loading, setLoading] = useState(false)
  const [enabled, setEnabled] = useState(duckingEnabled)

  const handleClick = useCallback(async () => {
    setLoading(true)
    try {
      await api.toggleDucking()
      setEnabled(!enabled)
    } finally {
      setLoading(false)
    }
  }, [enabled])

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={loading}
      className={clsx(
        'flex h-8 w-8 items-center justify-center rounded-full border transition-colors',
        enabled
          ? 'border-blue-500 bg-blue-500/20 text-blue-400 hover:bg-blue-500/30'
          : 'border-ink-700 bg-ink-800 text-ink-500 hover:bg-ink-700 hover:text-ink-300',
        loading && 'opacity-50 cursor-not-allowed'
      )}
      aria-label={enabled ? 'Disable voice ducking' : 'Enable voice ducking'}
      title={enabled ? 'Voice ducking ON (click to disable)' : 'Voice ducking OFF (click to enable)'}
    >
      <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24">
        <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.91-3c-.49 0-.9.36-.98.85C16.52 14.2 14.47 16 12 16s-4.52-1.8-4.93-4.15a.998.998 0 00-.98-.85c-.61 0-1.09.54-1 1.14.49 3 2.89 5.35 5.91 5.78V20c0 .55.45 1 1 1s1-.45 1-1v-2.08c3.02-.43 5.42-2.78 5.91-5.78.1-.6-.39-1.14-1-1.14z" />
      </svg>
    </button>
  )
}

export function NowPlayingPanel({
  track,
  playoutState,
}: {
  track: NowPlayingData | null
  playoutState?: string
}) {
  const isPaused = playoutState === 'paused'

  if (!track) {
    return (
      <Panel title="Now playing" data-testid="now-playing">
        <EmptyState
          title="Nothing on air"
          detail="The playout engine has no current item. If the station is running, this resolves within one block."
        />
      </Panel>
    )
  }

  const tierTone = track.tier === 'scheduled' ? 'neutral' : 'warn'

  return (
    <Panel
      title="Now playing"
      data-testid="now-playing"
      action={
        <div className="flex items-center gap-1.5">
          {track.is_station_id && <Chip tone="gold">Station ID</Chip>}
          <Chip tone={tierTone} title={track.origin}>
            {track.origin}
          </Chip>
        </div>
      }
    >
      <div className="flex gap-4">
        <div className="w-[104px] shrink-0 space-y-2">
          <CoverArt trackId={track.track_id} genre={track.genre} />
          <div className="flex items-center justify-center gap-2">
            <PlayPauseButton isPaused={isPaused} />
            <DuckingButton duckingEnabled={false} />
          </div>
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div className="min-w-0">
            <h3 className="truncate text-lg font-semibold leading-tight text-ink-100" title={track.title}>
              {track.title}
            </h3>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-ink-400">
              <span className="truncate">{track.artist ?? 'No persona'}</span>
              <span aria-hidden="true">·</span>
              <span className="font-mono text-2xs text-ink-500">{track.track_id}</span>
            </div>
          </div>

          <ProgressBar fallback={track} />

          <div className="flex flex-wrap items-center gap-1.5">
            {track.genre && <Chip>{titleCase(track.genre)}</Chip>}
            {track.secondary_genre && <Chip tone="flexible">{titleCase(track.secondary_genre)}</Chip>}
            {track.bpm !== null && <Chip>{track.bpm} BPM</Chip>}
            {track.musical_key && <Chip>{track.musical_key}</Chip>}
            <Chip tone="flexible">
              {track.is_instrumental === null
                ? ABSENT
                : track.is_instrumental
                  ? 'Instrumental'
                  : (track.vocal_style ?? 'Vocal')}
            </Chip>
            {track.transition_in && <Chip tone="flexible">{titleCase(track.transition_in)} in</Chip>}
          </div>

          <div className="grid grid-cols-3 gap-x-4 border-t hairline pt-2">
            <div>
              <div className="label">Planned regime</div>
              <div className="text-xs text-ink-200">
                {titleCase(track.planned_regime)}
                {track.planned_symbol && (
                  // The market this track was planned against, beside the regime rather
                  // than in the header: after a switch the track on air may still be the
                  // previous market's, and the header would say otherwise.
                  <span className="ml-1.5 font-mono text-2xs text-ink-500">
                    {track.planned_symbol}
                  </span>
                )}
              </div>
            </div>
            <div>
              <div className="label">Planned energy</div>
              <div className="font-mono text-xs tnum text-ink-200">
                {decimal(track.planned_energy, 0)}
              </div>
            </div>
            <div>
              <div className="label">Novelty target</div>
              <div className="font-mono text-xs tnum text-ink-200">
                {percent(track.novelty_target, 0)}
              </div>
            </div>
          </div>

          <div className="border-t hairline pt-2">
            <WhyThisTrack track={track} />
          </div>
        </div>
      </div>
    </Panel>
  )
}
