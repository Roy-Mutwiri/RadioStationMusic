/**
 * The broadcast overlay — what viewers see, not what operators see.
 *
 * Designed at 1920×1080 and scaled to fit anything else, because an OBS Browser Source is a
 * fixed canvas and a responsive layout would reflow unpredictably inside it.
 *
 * Three rules separate this from the dashboard:
 *
 * **It is beautiful before it is informative.** A viewer is watching, not operating. Type is
 * large, the composition is mostly empty, and every operational number — buffer, capacity,
 * job states — is absent. The brief's warning against turning the stream into a terminal is
 * the main constraint here.
 *
 * **It never shows a price it does not have.** §21 applies on the stream more strictly than
 * anywhere else, because a viewer cannot check. A simulated feed shows the regime and the
 * energy and simply omits the number.
 *
 * **It survives a disconnect silently.** A viewer must not see "RECONNECTING". The last known
 * state stays on screen and the only acknowledgement is that the live dot stops pulsing.
 */

import clsx from 'clsx'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { decimal, duration, price, titleCase } from '../lib/format'
import { useLive, usePosition } from '../lib/live'
import type { LiveState, NowPlaying } from '../lib/types'

/** Layout modes, for a 24/7 stream where a static composition burns in and bores. */
const MODES = ['standard', 'market', 'track', 'minimal'] as const
export type OverlayMode = (typeof MODES)[number]

/**
 * Educational lines, rotated slowly under the brand.
 *
 * §14 and §32 constrain every one of these: they describe what the *station* does, never what
 * the market will do. No line may imply a price prediction, a guaranteed relationship or a
 * signal — so they are all statements about the mapping, which is a fact about the software.
 */
const EDUCATIONAL_LINES = [
  'Market energy drives tempo, density and instrumentation.',
  'Every track is generated for the conditions it plays in.',
  'A quiet market composes quiet music. Nothing here predicts price.',
  'Regime changes reshape the upcoming queue, never the current track.',
  'All music is original and generated locally.',
] as const

function useRotating<T>(items: readonly T[], intervalMs: number): T | undefined {
  const [index, setIndex] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setIndex((value) => (value + 1) % items.length), intervalMs)
    return () => clearInterval(id)
  }, [items.length, intervalMs])
  return items[index]
}

function CoverArt({ track }: { track: NowPlaying }) {
  let hash = 0
  for (let index = 0; index < track.track_id.length; index += 1) {
    hash = (hash * 31 + track.track_id.charCodeAt(index)) % 360
  }
  return (
    <div
      className="relative aspect-square w-[260px] shrink-0 overflow-hidden rounded-md
        border border-white/10 shadow-2xl"
      style={{
        // Brighter than the console's version: on a stream this sits over video and at
        // 14 % lightness it disappeared into the background entirely.
        background: `linear-gradient(145deg, hsl(${hash} 30% 26%), hsl(${(hash + 50) % 360} 26% 10%))`,
      }}
      aria-hidden="true"
    >
      <div
        className="absolute inset-0 opacity-70"
        style={{
          background: `repeating-linear-gradient(${hash % 180}deg, transparent 0 14px, rgba(201,162,39,0.16) 14px 16px)`,
        }}
      />
      <div className="absolute inset-0 bg-gradient-to-t from-black/50 to-transparent" />
      <div className="absolute inset-0 rounded-md ring-1 ring-inset ring-gold-500/25" />
    </div>
  )
}

/** A spectrum driven by the engine's real peak. Flat when there is no measurement. */
function Spectrum({ peak }: { peak: number | null }) {
  const bars = 56
  return (
    <div className="flex h-10 items-end gap-[3px]" aria-hidden="true">
      {Array.from({ length: bars }, (_, index) => {
        // A fixed envelope shaped by the measured peak: louder output lifts the whole
        // spectrum. It is driven by real amplitude, so silence is visibly silent.
        const centre = 1 - Math.abs(index - bars / 2) / (bars / 2)
        const height = peak === null ? 2 : Math.max(2, (peak * 100 * (0.35 + centre * 0.65)))
        return (
          <span
            key={index}
            className="w-full rounded-[1px] bg-gradient-to-t from-gold-500/30 to-gold-400/80
              transition-[height] duration-300 ease-out"
            style={{ height: `${Math.min(100, height)}%` }}
          />
        )
      })}
    </div>
  )
}

function Brand({ broadcasting }: { broadcasting: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-[26px] font-semibold uppercase tracking-[0.22em] text-white">
        Trade Fix Radio
      </span>
      <span
        className={clsx(
          'flex items-center gap-2 rounded-sm border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.18em]',
          broadcasting
            ? 'border-gold-500/50 bg-gold-500/15 text-gold-300'
            : 'border-white/15 text-white/40',
        )}
      >
        <span
          className={clsx(
            'h-1.5 w-1.5 rounded-full',
            broadcasting ? 'animate-pulse-live bg-gold-400' : 'bg-white/30',
          )}
        />
        Live
      </span>
    </div>
  )
}

function MarketStrip({ state, large }: { state: LiveState; large?: boolean }) {
  const market = state.market
  if (!market) return null
  return (
    <div className={clsx('flex items-end gap-10', large && 'gap-14')}>
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-white/40">XAUUSD</div>
        <div
          className={clsx(
            'font-mono tnum leading-none text-white',
            large ? 'text-6xl' : 'text-4xl',
          )}
        >
          {price(market.price)}
        </div>
        {market.price === null && (
          <div className="mt-1 text-[11px] text-white/35">
            {market.is_simulated ? 'Simulated session' : 'Awaiting live price'}
          </div>
        )}
      </div>
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-white/40">Regime</div>
        <div
          className={clsx(
            'font-semibold uppercase tracking-[0.1em] text-gold-300',
            large ? 'text-3xl' : 'text-xl',
          )}
        >
          {market.regime_label}
        </div>
      </div>
      <div className="min-w-[180px]">
        <div className="flex items-baseline justify-between">
          <span className="text-[11px] uppercase tracking-[0.18em] text-white/40">Energy</span>
          <span className="font-mono text-sm tnum text-white/80">
            {decimal(market.energy, 0)}
          </span>
        </div>
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-white/10">
          <div
            className="h-full rounded-full bg-gold-500 transition-[width] duration-1000"
            style={{ width: `${Math.max(0, Math.min(100, market.energy))}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function TrackBlock({
  track,
  elapsed,
  progress,
  large,
}: {
  track: NowPlaying
  elapsed: number
  progress: number
  large?: boolean
}) {
  return (
    <div className="min-w-0 flex-1">
      <div className="text-[11px] uppercase tracking-[0.18em] text-gold-500/70">Now playing</div>
      <h1
        className={clsx(
          'mt-1 truncate font-semibold leading-tight text-white',
          large ? 'text-6xl' : 'text-4xl',
        )}
        title={track.title}
      >
        {track.title}
      </h1>
      <div className={clsx('mt-2 truncate text-white/55', large ? 'text-2xl' : 'text-lg')}>
        {track.artist ?? 'Trade Fix Radio'}
      </div>
      <div className="mt-3 flex items-center gap-3 text-[13px] text-white/40">
        {track.genre && <span>{titleCase(track.genre)}</span>}
        {track.bpm !== null && (
          <>
            <span aria-hidden="true">•</span>
            <span className="font-mono tnum">{track.bpm} BPM</span>
          </>
        )}
        {track.musical_key && (
          <>
            <span aria-hidden="true">•</span>
            <span className="font-mono">{track.musical_key}</span>
          </>
        )}
      </div>
      <div className="mt-4 flex items-center gap-3">
        <span className="font-mono text-xs tnum text-white/50">{duration(elapsed)}</span>
        <div className="h-[3px] flex-1 overflow-hidden rounded-full bg-white/10">
          <div
            className="h-full rounded-full bg-gold-500 transition-[width] duration-500 ease-linear"
            style={{ width: `${Math.min(100, progress * 100)}%` }}
          />
        </div>
        <span className="font-mono text-xs tnum text-white/50">
          {duration(track.duration_seconds)}
        </span>
      </div>
    </div>
  )
}

export function OverlayPage() {
  const { state, connection } = useLive()
  const position = usePosition()
  const [params] = useSearchParams()
  const line = useRotating(EDUCATIONAL_LINES, 18_000)

  const requested = params.get('mode') as OverlayMode | null
  const mode: OverlayMode = MODES.includes(requested as OverlayMode)
    ? (requested as OverlayMode)
    : 'standard'

  // A viewer must never be shown connection plumbing. On a drop the last frame stays on
  // screen; the only tell is the live dot, which stops pulsing.
  const broadcasting = (state?.status.is_broadcasting ?? false) && connection === 'connected'

  if (!state) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-black">
        <div className="text-center">
          <div className="text-[26px] font-semibold uppercase tracking-[0.22em] text-white/80">
            Trade Fix Radio
          </div>
          <div className="mt-2 text-[12px] uppercase tracking-[0.2em] text-gold-500/60">
            The market composes the radio
          </div>
        </div>
      </div>
    )
  }

  const track = state.now_playing
  const live =
    position && track && position.track_id === track.track_id
      ? position
      : track
        ? {
            elapsed_seconds: track.elapsed_seconds,
            progress: track.progress,
            output_peak: track.output_peak,
          }
        : null

  return (
    <div
      className="relative h-screen w-screen overflow-hidden bg-black text-white"
      data-testid="overlay"
      data-mode={mode}
    >
      {/* A slow gold wash from the lower left, so the composition has a light source and the
          lower third reads against video behind it. */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(120% 90% at 8% 105%, rgba(201,162,39,0.14) 0%, transparent 55%),' +
            'radial-gradient(90% 70% at 100% 0%, rgba(255,255,255,0.05) 0%, transparent 60%)',
        }}
      />

      <header className="absolute left-0 right-0 top-0 flex items-center justify-between px-14 py-10">
        <Brand broadcasting={broadcasting} />
        {mode !== 'minimal' && state.market && (
          <div className="text-right">
            <div className="text-[11px] uppercase tracking-[0.18em] text-white/40">Session</div>
            <div className="text-lg text-white/70">{titleCase(state.market.session)}</div>
          </div>
        )}
      </header>

      {mode === 'market' ? (
        <div className="absolute inset-x-0 bottom-0 px-14 pb-14">
          <MarketStrip state={state} large />
          {track && live && (
            <div className="mt-10 flex items-end gap-8 border-t border-white/10 pt-8">
              <TrackBlock
                track={track}
                elapsed={live.elapsed_seconds}
                progress={live.progress}
              />
              <Spectrum peak={live.output_peak ?? null} />
            </div>
          )}
        </div>
      ) : mode === 'track' ? (
        <div className="absolute inset-0 flex items-center justify-center px-14">
          {track && live ? (
            <div className="flex w-full max-w-[1400px] items-center gap-14">
              <CoverArt track={track} />
              <TrackBlock
                track={track}
                elapsed={live.elapsed_seconds}
                progress={live.progress}
                large
              />
            </div>
          ) : null}
        </div>
      ) : mode === 'minimal' ? (
        <div className="absolute inset-x-0 bottom-0 px-14 pb-14">
          {track && live && (
            <div className="flex items-end justify-between gap-10">
              <TrackBlock
                track={track}
                elapsed={live.elapsed_seconds}
                progress={live.progress}
              />
              {state.market && (
                <div className="shrink-0 text-right">
                  <div className="font-semibold uppercase tracking-[0.1em] text-gold-300">
                    {state.market.regime_label}
                  </div>
                  <div className="font-mono text-3xl tnum text-white">
                    {price(state.market.price)}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="absolute inset-x-0 bottom-0 px-14 pb-20">
          <div className="flex items-end gap-10">
            {track && <CoverArt track={track} />}
            {track && live && (
              <TrackBlock
                track={track}
                elapsed={live.elapsed_seconds}
                progress={live.progress}
              />
            )}
            <div className="shrink-0 space-y-7">
              <MarketStrip state={state} />
              <Spectrum peak={live?.output_peak ?? null} />
            </div>
          </div>
        </div>
      )}

      <footer className="absolute inset-x-0 bottom-0 flex items-center justify-between px-14 pb-6">
        <span className="text-[11px] uppercase tracking-[0.22em] text-gold-500/50">
          The market composes the radio
        </span>
        {mode !== 'minimal' && line && (
          <span className="text-[11px] text-white/30">{line}</span>
        )}
      </footer>
    </div>
  )
}

/** A preview harness so an operator can check each layout before putting it on air. */
export function OverlayPreviewPage() {
  const [mode, setMode] = useState<OverlayMode>('standard')
  return (
    <div className="space-y-3 p-4">
      <div className="flex items-center gap-2">
        <span className="label">Overlay layout</span>
        {MODES.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setMode(option)}
            aria-pressed={mode === option}
            className={clsx(
              'rounded-sm px-2 py-0.5 text-2xs font-medium uppercase tracking-label',
              mode === option
                ? 'bg-gold-500/15 text-gold-300'
                : 'text-ink-500 hover:bg-ink-850 hover:text-ink-300',
            )}
          >
            {option}
          </button>
        ))}
        <a
          href={`/overlay/live?mode=${mode}`}
          target="_blank"
          rel="noreferrer"
          className="ml-auto text-2xs text-gold-400 hover:underline"
        >
          Open full size
        </a>
      </div>
      <div className="overflow-hidden rounded-panel border border-ink-800">
        {/* Scaled to a 1920×1080 canvas so the preview is dimensionally honest. */}
        <div style={{ width: 1920, height: 1080, transform: 'scale(0.52)', transformOrigin: 'top left' }}>
          <iframe
            title={`Overlay preview — ${mode}`}
            src={`/overlay/live?mode=${mode}`}
            style={{ width: 1920, height: 1080, border: 0 }}
          />
        </div>
        <div style={{ height: 1080 * 0.52 - 1080 }} />
      </div>
    </div>
  )
}
