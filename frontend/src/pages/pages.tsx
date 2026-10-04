/**
 * Pages 2–10.
 *
 * Collected in one module because they share a shape: read from the live frame or a query,
 * render a table or a set of panels, and handle the four states the brief requires —
 * loading, empty, offline, partial failure. Splitting them across ten files would multiply
 * the imports without separating any concerns.
 *
 * The capability-gated pages (Originality, OBS) are built to their final design and render
 * `AwaitingPhase` instead of data. That is the brief's instruction taken literally: design
 * the intended page, then refuse to populate it with anything invented.
 */

import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  EnergyTimeline,
  MarketPanel,
  MarketRoutingPanel,
  type TimelineWindow,
} from '../components/MarketPanel'
import { GenerationStrip, HealthPanel } from '../components/HealthPanel'
import { NowPlayingPanel } from '../components/NowPlaying'
import { BufferPanel, EmergencyBanner, QueuePanel } from '../components/QueuePanel'
import {
  AwaitingPhase,
  Button,
  Chip,
  EmptyState,
  ErrorState,
  LoadingState,
  Metric,
  Meter,
  Panel,
  Row,
} from '../components/primitives'
import type { AudioFile } from '../lib/api'
import { api, CapabilityUnavailable } from '../lib/api'
import {
  ABSENT,
  clockTime,
  decimal,
  duration,
  humanDuration,
  integer,
  percent,
  ratio,
  shortDate,
  titleCase,
} from '../lib/format'
import { useLive } from '../lib/live'
import type { Capability } from '../lib/types'

function useCapability(name: string): Capability | null {
  const { state } = useLive()
  return state?.capabilities.find((entry) => entry.capability === name) ?? null
}

function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-end justify-between gap-4 pb-1">
      <div>
        <h1 className="text-base font-semibold tracking-tight text-ink-100">{title}</h1>
        {subtitle && <p className="mt-0.5 text-2xs text-ink-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}

// ----------------------------------------------------------------- 2. Market

/**
 * The simulation panel.
 *
 * Only rendered when the capability says the run mode allows it, and always behind a visible
 * warning. §72 is categorical that simulation must never look like live trading, so the
 * controls sit inside a marked region rather than beside the real market readings.
 */
function SimulationPanel() {
  const capability = useCapability('simulation')
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const scenarios = useQuery({
    queryKey: ['scenarios'],
    queryFn: () => api.scenarios(),
    enabled: capability?.state === 'ready',
  })

  if (!capability || capability.state !== 'ready') {
    return (
      <Panel title="Simulation" data-testid="simulation-panel">
        <EmptyState
          title="Scenario controls unavailable"
          detail={capability?.detail ?? 'This run mode does not expose the market simulator.'}
        />
      </Panel>
    )
  }

  async function apply(scenario: string) {
    setBusy(scenario)
    try {
      const result = await api.setScenario(scenario)
      setMessage(result.message)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'The scenario could not be applied.')
    } finally {
      setBusy(null)
    }
  }

  const available = scenarios.data?.available ?? []

  return (
    <Panel
      title="Simulation"
      data-testid="simulation-panel"
      action={
        <span className="chip border-gold-500/40 bg-gold-500/10 text-gold-300">
          Simulation mode
        </span>
      }
    >
      <p className="mb-3 text-2xs leading-relaxed text-ink-500">
        These controls drive the real market simulator, and the station reacts to the result
        exactly as it would to a live feed. Nothing here is connected to a broker.
      </p>
      <div className="flex flex-wrap gap-1.5">
        {available.map((scenario) => (
          <Button
            key={scenario}
            onClick={() => void apply(scenario)}
            disabled={busy !== null}
            data-testid={`scenario-${scenario}`}
          >
            {titleCase(scenario)}
          </Button>
        ))}
      </div>
      {message && (
        <p className="mt-3 border-t hairline pt-2 text-2xs text-ink-400" role="status">
          {message}
        </p>
      )}
    </Panel>
  )
}

function RegimeExplanation() {
  const { state } = useLive()
  const market = state?.market
  if (!market) return null

  // The contributing factors, as the regime engine weighs them. Stored numbers from the
  // current state — not an explanation generated after the fact.
  const factors = [
    { label: 'Trend strength', value: market.trend_strength },
    { label: 'Volatility', value: market.volatility },
    { label: 'Momentum', value: market.momentum },
    { label: 'Compression', value: market.compression },
    { label: 'Energy', value: market.energy },
  ]

  return (
    <Panel title="Regime explanation" data-testid="regime-explanation">
      <div className="mb-3 flex items-baseline justify-between">
        <span className="text-sm font-medium text-ink-100">{market.regime_label}</span>
        <span className="font-mono text-sm tnum text-gold-400">
          {decimal(market.regime_confidence * 100, 0)}%
        </span>
      </div>
      <div className="space-y-2">
        {factors.map((factor) => (
          <div key={factor.label}>
            <div className="flex items-baseline justify-between">
              <span className="label">{factor.label}</span>
              <span className="font-mono text-xs tnum text-ink-300">{decimal(factor.value, 0)}</span>
            </div>
            <Meter value={factor.value} max={100} tone="neutral" label={factor.label} />
          </div>
        ))}
      </div>
      <p className="mt-3 border-t hairline pt-2 text-2xs leading-relaxed text-ink-600">
        These are the feature values the regime engine scored. The classification is a
        weighted reading of them, not a separate judgement.
      </p>
    </Panel>
  )
}


export function MarketPage() {
  const { state } = useLive()
  const simulation = useCapability('simulation')
  const [window, setWindow] = useState<TimelineWindow>('1h')
  const history = useQuery({
    queryKey: ['market-history', window],
    queryFn: () => api.marketHistory(window),
    refetchInterval: 15_000,
  })

  return (
    <div className="space-y-4 p-4" data-testid="market-page">
      <PageHeader
        title="Market Lab"
        subtitle={
          state?.routing
            ? state.routing.active_symbol + ' as the station reads it'
            : 'The market as the station reads it'
        }
      />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <MarketPanel market={state?.market ?? null} />
        <RegimeExplanation />
      </div>
      <MarketRoutingPanel
        routing={state?.routing ?? null}
        canSimulate={simulation?.state === 'ready'}
      />
      <EnergyTimeline history={history.data ?? null} window={window} onWindowChange={setWindow} />
      <SimulationPanel />
    </div>
  )
}

// ----------------------------------------------------------------- 3. Radio

export function RadioPage() {
  const { state } = useLive()
  const history = useQuery({ queryKey: ['radio-history'], queryFn: () => api.radioHistory(40) })

  if (!state) {
    return (
      <div className="p-4">
        <Panel title="Radio">
          <LoadingState />
        </Panel>
      </div>
    )
  }

  return (
    <div className="space-y-4 p-4" data-testid="radio-page">
      <PageHeader title="Radio" subtitle="Programming, buffer and recent history" />
      <EmergencyBanner emergency={state.emergency} />
      <NowPlayingPanel track={state.now_playing} />

      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <QueuePanel queue={state.queue} />
        <div className="space-y-4">
          <BufferPanel buffer={state.buffer} />
          <Panel title="Emergency reserve" data-testid="reserve-panel">
            {state.emergency ? (
              <div className="space-y-0.5">
                <Row label="Current tier">{state.emergency.tier_label}</Row>
                <Row label="Reserve">{decimal(state.emergency.reserve_minutes, 0)} min</Row>
                <Row label="Reserve tracks">{state.emergency.reserve_tracks_remaining}</Row>
                <Row label="Tier 2 activations">{state.emergency.tier2_activations}</Row>
                <Row label="Tier 3 activations">{state.emergency.tier3_activations}</Row>
                <Row label="Time on Tier 3">
                  {humanDuration(state.emergency.seconds_in_tier3)}
                </Row>
                <Row label="Recoveries">{state.emergency.recoveries}</Row>
              </div>
            ) : (
              <EmptyState title="No emergency data" />
            )}
          </Panel>
        </div>
      </div>

      <Panel title="Recent history" data-testid="history-panel" bodyClassName="p-0">
        {history.isLoading ? (
          <LoadingState />
        ) : history.data && history.data.length > 0 ? (
          <div className="max-h-[340px] overflow-auto">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-ink-900">
                <tr className="border-b border-ink-800 text-left">
                  <th scope="col" className="label px-3 py-2 font-medium">Played</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Track</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Genre</th>
                  <th scope="col" className="label px-2 py-2 text-right font-medium">BPM</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Key</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Regime</th>
                  <th scope="col" className="label px-2 py-2 text-right font-medium">Energy</th>
                  <th scope="col" className="label px-3 py-2 text-right font-medium">Length</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((entry) => (
                  <tr key={entry.track_id} className="border-b hairline hover:bg-ink-850/60">
                    <td className="px-3 py-1.5 font-mono text-2xs tnum text-ink-500">
                      {clockTime(entry.played_at)}
                    </td>
                    <td className="px-2 py-1.5 font-mono text-2xs text-ink-300">
                      {entry.track_id}
                    </td>
                    <td className="px-2 py-1.5 text-ink-200">{titleCase(entry.genre)}</td>
                    <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                      {entry.bpm}
                    </td>
                    <td className="px-2 py-1.5 text-ink-300">{entry.musical_key}</td>
                    <td className="px-2 py-1.5 text-ink-400">
                      {titleCase(entry.regime_at_generation)}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                      {decimal(entry.energy_at_generation, 0)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono tnum text-ink-400">
                      {duration(entry.duration_seconds)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="Nothing has aired yet"
            detail="History fills as tracks complete. A freshly started station has none."
          />
        )}
      </Panel>
    </div>
  )
}

// ----------------------------------------------------------------- 4. Generation

const JOB_STATE_TONE: Record<string, 'neutral' | 'gold' | 'warn' | 'danger' | 'soft'> = {
  planned: 'neutral',
  queued: 'neutral',
  leased: 'gold',
  generating: 'gold',
  generated: 'soft',
  retry_pending: 'warn',
  failed: 'danger',
  cancelled: 'neutral',
  abandoned: 'warn',
}

export function GenerationPage() {
  const { state } = useLive()
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: () => api.jobs({ limit: 60 }),
    refetchInterval: 5_000,
  })
  const counts = useQuery({
    queryKey: ['job-counts'],
    queryFn: () => api.jobCounts(),
    refetchInterval: 10_000,
  })

  return (
    <div className="space-y-4 p-4" data-testid="generation-page">
      <PageHeader
        title="Generation"
        subtitle="Job leases, retries and §93 capacity"
      />

      <GenerationStrip generation={state?.generation ?? null} />

      <ProviderPanel />

      <Panel title="Job states" data-testid="job-counts">
        {counts.isLoading ? (
          <LoadingState />
        ) : (
          <div className="flex flex-wrap gap-4">
            {Object.entries(counts.data?.by_state ?? {}).map(([name, value]) => (
              <Metric key={name} label={titleCase(name)} value={value} size="sm" />
            ))}
            {Object.keys(counts.data?.by_state ?? {}).length === 0 && (
              <p className="text-2xs text-ink-500">No jobs have been created yet.</p>
            )}
          </div>
        )}
      </Panel>

      <Panel title="Job monitor" data-testid="job-monitor" bodyClassName="p-0">
        {jobs.isLoading ? (
          <LoadingState />
        ) : jobs.data && jobs.data.length > 0 ? (
          <div className="max-h-[520px] overflow-auto">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-ink-900">
                <tr className="border-b border-ink-800 text-left">
                  <th scope="col" className="label px-3 py-2 font-medium">State</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Job</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Track</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Priority</th>
                  <th scope="col" className="label px-2 py-2 text-right font-medium">Attempt</th>
                  <th scope="col" className="label px-2 py-2 text-right font-medium">Age</th>
                  <th scope="col" className="label px-2 py-2 font-medium">Lease</th>
                  <th scope="col" className="label px-3 py-2 font-medium">Error</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((job) => (
                  <tr key={job.job_id} className="border-b hairline hover:bg-ink-850/60">
                    <td className="px-3 py-1.5">
                      <Chip tone={JOB_STATE_TONE[job.state] ?? 'neutral'}>
                        {job.state.toUpperCase()}
                      </Chip>
                    </td>
                    <td className="px-2 py-1.5 font-mono text-2xs text-ink-500">
                      {job.job_id.slice(0, 14)}
                    </td>
                    <td className="px-2 py-1.5 font-mono text-2xs text-ink-300">{job.track_id}</td>
                    <td className="px-2 py-1.5 text-ink-400">{titleCase(job.priority)}</td>
                    <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                      {job.attempt}/{job.max_attempts}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tnum text-ink-400">
                      {humanDuration(job.age_seconds)}
                    </td>
                    <td className="px-2 py-1.5 font-mono text-2xs text-ink-500">
                      {job.lease_owner
                        ? `${job.lease_owner.slice(0, 8)} · ${
                            job.lease_expires_in_seconds === null
                              ? ABSENT
                              : `${job.lease_expires_in_seconds.toFixed(0)}s`
                          }`
                        : ABSENT}
                    </td>
                    <td className="max-w-[220px] px-3 py-1.5">
                      {job.error_kind ? (
                        <span className="truncate text-2xs text-status-degraded" title={job.error_message ?? ''}>
                          {job.error_kind}
                        </span>
                      ) : (
                        <span className="text-ink-700">{ABSENT}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="No generation jobs"
            detail="Jobs appear as the scheduler plans tracks."
          />
        )}
      </Panel>

      <PostProductionPanel />

      <Panel title="Manual generation" data-testid="manual-generation">
        <AwaitingPhase
          subsystem="Generation Lab"
          phase={7}
          detail="Manual test generation needs a provider that can be driven outside the station's own scheduling loop. The mock provider is wired to the runtime only; the lab arrives with ACE-Step in Phase 7."
        />
      </Panel>
    </div>
  )
}



/**
 * Live generation-provider state (§7.25).
 *
 * Shows what is really there: model, load state, measured VRAM against the card's real
 * total, the in-flight track and how long it has been running, and p50/p95 latency once
 * there are samples to compute them from.
 *
 * **No percentage bar.** §7.25: *"Do not display fake percent complete if ACE-Step does not
 * expose trustworthy progress. An indeterminate progress state is better."* ACE-Step does
 * emit a progress number, but it is coarse — measured, it sits at 0.1 for the whole LM phase
 * and then jumps to 1.0 — so a bar would spend most of a 45-second generation claiming 10%.
 * The elapsed seconds are real and are what is shown instead.
 */
/**
 * Audio presence for one role, told truthfully (B5).
 *
 * This replaced `has_audio ? 'Yes' : 'No'`, which rendered "No" for all 247 tracks
 * because it counted `track_files` rows and nothing ever wrote one — including for
 * tracks whose masters were sitting on disk the whole time. "No" was also hiding three
 * different situations: reclaimed on purpose, genuinely lost, and never recorded. An
 * operator needs to tell those apart, so each gets its own word and its own colour.
 */
function AudioState({ file }: { file?: AudioFile }): JSX.Element {
  if (!file || file.status === 'unknown') {
    return <span className="text-ink-600">Not recorded</span>
  }
  if (file.status === 'missing') {
    return (
      <span className="text-status-bad" title="A database row names this file and it is not on disk.">
        Missing file
      </span>
    )
  }
  if (file.status === 'deleted') {
    return (
      <span className="text-ink-500" title="Reclaimed by retention. The metadata is kept.">
        Reclaimed{file.deleted_at ? ` · ${file.deleted_at.slice(0, 10)}` : ''}
      </span>
    )
  }
  const mb = file.size_bytes ? `${(file.size_bytes / 1e6).toFixed(1)} MB` : null
  return (
    <span className="text-status-ok">
      Present
      {mb ? <span className="text-ink-500"> · {mb}</span> : null}
      {file.format ? <span className="text-ink-500"> · {file.format}</span> : null}
      {file.retained_forever ? <span className="text-ink-500"> · pinned</span> : null}
    </span>
  )
}

function ProviderPanel() {
  const provider = useQuery({
    queryKey: ['provider-status'],
    queryFn: api.providerStatus,
    refetchInterval: 3_000,
    retry: false,
  })

  if (provider.isError) {
    return (
      <Panel title="Provider" data-testid="provider-panel">
        <ErrorState
          title="No provider attached"
          detail={provider.error instanceof Error ? provider.error.message : undefined}
        />
      </Panel>
    )
  }
  if (provider.isLoading || !provider.data) {
    return (
      <Panel title="Provider" data-testid="provider-panel">
        <LoadingState />
      </Panel>
    )
  }

  const p = provider.data
  const busy = p.status === 'generating'
  const vramPercent =
    p.vram_total_mb && p.vram_used_mb ? (p.vram_used_mb / p.vram_total_mb) * 100 : null

  return (
    <Panel
      title="Provider"
      data-testid="provider-panel"
      action={
        <div className="flex items-center gap-2">
          <Chip tone={PROVIDER_TONE[p.status] ?? 'neutral'}>{p.status.toUpperCase()}</Chip>
          {p.oom_events > 0 && <Chip tone="warn">{p.oom_events} OOM</Chip>}
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        <Metric label="Provider" value={p.provider} size="sm" />
        <Metric label="Model" value={p.model} size="sm" hint={p.lm_model ?? undefined} />
        <Metric
          label="VRAM"
          value={
            p.vram_used_mb && p.vram_total_mb
              ? `${(p.vram_used_mb / 1024).toFixed(1)} / ${(p.vram_total_mb / 1024).toFixed(1)} GB`
              : null
          }
          size="sm"
          hint={vramPercent !== null ? `${vramPercent.toFixed(0)}% used` : undefined}
        />
        <Metric
          label="Generated"
          value={p.generations || null}
          size="sm"
          hint={p.failures ? `${p.failures} failed` : undefined}
        />
        <Metric
          label="Latency p50 / p95"
          value={
            p.latency_p50_seconds !== null
              ? `${p.latency_p50_seconds.toFixed(0)}s / ${
                  p.latency_p95_seconds?.toFixed(0) ?? '—'
                }s`
              : null
          }
          size="sm"
          hint={p.latency_p50_seconds === null ? 'no completed generations yet' : undefined}
        />
      </div>

      {busy && p.current_track_id && (
        <div className="mt-3 border-t hairline pt-3">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-2xs text-ink-500">Current job</span>
            <span className="font-mono text-2xs text-ink-200">{p.current_track_id}</span>
          </div>
          <div className="mt-1 flex items-baseline justify-between gap-3">
            <span className="text-2xs text-ink-500">Elapsed</span>
            <span className="tabular-nums text-2xs text-gold-400">
              {p.current_elapsed_seconds !== null
                ? `${p.current_elapsed_seconds.toFixed(0)}s`
                : '—'}
            </span>
          </div>
          {/* Indeterminate, deliberately. See the component docstring. */}
          <div
            className="mt-2 h-0.5 w-full overflow-hidden rounded-full bg-ink-800"
            role="progressbar"
            aria-label="Generation in progress"
            aria-busy="true"
          >
            <div className="h-full w-1/3 animate-pulse rounded-full bg-gold-500/60" />
          </div>
          <p className="mt-1 text-2xs text-ink-600">
            ACE-Step does not report fine-grained progress; elapsed time is shown instead of
            an invented percentage.
          </p>
        </div>
      )}

      {p.last_error && (
        <p className="mt-3 border-t hairline pt-2 text-2xs text-status-degraded">
          Last error: {p.last_error}
        </p>
      )}
    </Panel>
  )
}

const PROVIDER_TONE: Record<string, 'neutral' | 'gold' | 'warn' | 'danger' | 'soft'> = {
  ready: 'soft',
  generating: 'gold',
  loading: 'warn',
  unloading: 'warn',
  unavailable: 'neutral',
  failed: 'danger',
}

/**
 * Post-production outcomes, on the Generation page (§6.17).
 *
 * Here rather than only on the Originality page because this is where the question gets
 * asked. An operator watching generation sees jobs completing and expects tracks; §6.14 says
 * generator success is not a playable track, and without this panel the gap between "60 jobs
 * completed" and "12 tracks queued" is invisible. The link to the full evidence lives on the
 * Originality page; what belongs here is the *rate* and the reasons.
 */
function PostProductionPanel() {
  const summary = useQuery({
    queryKey: ['originality', 'summary'],
    queryFn: api.originality,
    refetchInterval: 15_000,
    retry: false,
  })

  if (summary.isError) {
    // 409 with its reason when the pipeline is not attached — rendered as the capability
    // notice it is, not as a failure.
    return (
      <Panel title="Post-production" data-testid="post-production">
        <AwaitingPhase
          subsystem="Post-production"
          phase={6}
          detail={summary.error instanceof Error ? summary.error.message : undefined}
        />
      </Panel>
    )
  }

  const verdicts = summary.data?.verdict_counts ?? {}
  const evaluated = summary.data?.evaluated_count ?? 0
  const rejected = (verdicts.reject ?? 0) + (verdicts.review ?? 0)

  return (
    <Panel
      title="Post-production"
      data-testid="post-production"
      action={
        <Link to="/originality" className="text-2xs text-gold-400 hover:text-gold-300">
          evidence →
        </Link>
      }
    >
      {summary.isLoading ? (
        <LoadingState />
      ) : evaluated === 0 ? (
        <EmptyState
          title="Nothing validated yet"
          detail="Every generated track is checked before it can become READY."
        />
      ) : (
        <>
          <div className="flex flex-wrap gap-4">
            <Metric label="Validated" value={evaluated} size="sm" />
            <Metric label="Approved" value={verdicts.approve ?? 0} size="sm" tone="gold" />
            <Metric label="Rejected" value={rejected} size="sm" />
            <Metric
              label="Approval rate"
              value={`${Math.round(((verdicts.approve ?? 0) / evaluated) * 100)}%`}
              size="sm"
            />
          </div>
          <p className="mt-3 border-t hairline pt-2 text-2xs leading-relaxed text-ink-600">
            A completed generation job is not a playable track. Every render is checked for
            audio faults, compared against the library and mastered before it can be queued.
          </p>
        </>
      )}
    </Panel>
  )
}

// ----------------------------------------------------------------- 5. Library


/**
 * Market filter for the library and analytics pages.
 *
 * Driven from the routing frame rather than from a hard-coded pair, so configuring a third
 * fallback adds a button instead of a code change. Absent when routing is not attached —
 * a filter with one option is a control that lies about having a choice.
 *
 * "All markets" is the *absence* of a filter, not a value sent to the server. The API has
 * no ALL sentinel to get wrong.
 */
function MarketFilter({
  value,
  onChange,
}: {
  value: string | null
  onChange: (symbol: string | null) => void
}) {
  const { state } = useLive()
  const symbols = state?.routing?.symbols.map((entry) => entry.symbol) ?? []
  if (symbols.length < 2) return null

  return (
    <div className="flex items-center gap-1" data-testid="market-filter">
      <span className="label mr-1">Market</span>
      <Button onClick={() => onChange(null)} disabled={value === null}>
        All
      </Button>
      {symbols.map((symbol) => (
        <Button
          key={symbol}
          onClick={() => onChange(symbol)}
          disabled={value === symbol}
          data-testid={'market-filter-' + symbol}
        >
          {symbol}
        </Button>
      ))}
    </div>
  )
}

export function LibraryPage() {
  const [search, setSearch] = useState('')
  const [symbol, setSymbol] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const limit = 40

  const library = useQuery({
    queryKey: ['library', search, symbol, offset],
    queryFn: () =>
      api.library({ search: search || undefined, symbol: symbol ?? undefined, offset, limit }),
  })
  const detail = useQuery({
    queryKey: ['library-track', selected],
    queryFn: () => api.libraryTrack(selected as string),
    enabled: selected !== null,
  })

  return (
    <div className="space-y-4 p-4" data-testid="library-page">
      <PageHeader
        title="Track Library"
        subtitle="Everything the station has planned or played"
        action={
          <MarketFilter
            value={symbol}
            onChange={(next) => {
              setSymbol(next)
              setOffset(0)
            }}
          />
        }
      />

      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Panel
          title="Tracks"
          bodyClassName="p-0"
          action={
            <input
              type="search"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value)
                setOffset(0)
              }}
              placeholder="Search title or track id"
              aria-label="Search the library"
              className="w-56 rounded-sm border border-ink-700 bg-ink-850 px-2 py-1 text-xs
                text-ink-200 placeholder:text-ink-600 focus:border-gold-500/50"
            />
          }
        >
          {library.isLoading ? (
            <LoadingState />
          ) : library.isError ? (
            <ErrorState title="The library could not be read" detail={String(library.error)} />
          ) : library.data && library.data.items.length > 0 ? (
            <>
              <div className="max-h-[560px] overflow-auto">
                <table className="w-full border-collapse text-xs">
                  <thead className="sticky top-0 bg-ink-900">
                    <tr className="border-b border-ink-800 text-left">
                      <th scope="col" className="label px-3 py-2 font-medium">Title</th>
                      <th scope="col" className="label px-2 py-2 font-medium">Genre</th>
                      <th scope="col" className="label px-2 py-2 text-right font-medium">BPM</th>
                      <th scope="col" className="label px-2 py-2 font-medium">Regime</th>
                      <th scope="col" className="label px-2 py-2 text-right font-medium">Novelty</th>
                      <th scope="col" className="label px-2 py-2 text-right font-medium">Played</th>
                      <th scope="col" className="label px-3 py-2 font-medium">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {library.data.items.map((track) => (
                      <tr
                        key={track.track_id}
                        onClick={() => setSelected(track.track_id)}
                        className={clsx(
                          'cursor-pointer border-b hairline hover:bg-ink-850/60',
                          selected === track.track_id && 'bg-gold-500/[0.07]',
                        )}
                      >
                        <td className="max-w-[240px] px-3 py-1.5">
                          <div className="truncate text-ink-200">{track.title}</div>
                          <div className="truncate font-mono text-2xs text-ink-600">
                            {track.track_id}
                          </div>
                        </td>
                        <td className="px-2 py-1.5 text-ink-300">{titleCase(track.genre)}</td>
                        <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                          {track.bpm ?? ABSENT}
                        </td>
                        <td className="px-2 py-1.5 text-ink-400">
                          {titleCase(track.planned_regime)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono tnum text-ink-400">
                          {/* Null until Phase 6 measures it. Never a stand-in value. */}
                          {track.novelty_score === null ? ABSENT : percent(track.novelty_score, 0)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono tnum text-ink-300">
                          {track.play_count}
                        </td>
                        <td className="px-3 py-1.5 text-ink-500">{shortDate(track.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center justify-between border-t hairline px-3 py-2">
                <span className="text-2xs text-ink-500">
                  {library.data.total} track{library.data.total === 1 ? '' : 's'}
                </span>
                <div className="flex gap-1.5">
                  <Button
                    onClick={() => setOffset(Math.max(0, offset - limit))}
                    disabled={offset === 0}
                  >
                    Previous
                  </Button>
                  <Button
                    onClick={() => setOffset(offset + limit)}
                    disabled={offset + limit >= library.data.total}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <EmptyState
              title="The library is empty"
              detail="Tracks appear here as the director plans them and the generator renders them."
            />
          )}
        </Panel>

        <Panel title="Track detail" data-testid="track-detail">
          {selected === null ? (
            <EmptyState title="No track selected" detail="Choose a row to see its blueprint." />
          ) : detail.isLoading ? (
            <LoadingState />
          ) : detail.data ? (
            <div className="space-y-3">
              <div>
                <h3 className="text-sm font-semibold text-ink-100">{detail.data.summary.title}</h3>
                <p className="font-mono text-2xs text-ink-600">{detail.data.summary.track_id}</p>
              </div>
              <div className="space-y-0.5">
                <Row label="Artist">{detail.data.summary.artist ?? ABSENT}</Row>
                <Row label="Genre">{titleCase(detail.data.summary.genre)}</Row>
                <Row label="BPM">{detail.data.summary.bpm ?? ABSENT}</Row>
                <Row label="Key">{detail.data.summary.musical_key ?? ABSENT}</Row>
                <Row label="Duration">{duration(detail.data.summary.duration_seconds)}</Row>
                <Row label="State">{titleCase(detail.data.summary.state)}</Row>
                <Row label="Regime">{titleCase(detail.data.summary.planned_regime)}</Row>
                <Row label="Energy">{decimal(detail.data.summary.planned_energy, 0)}</Row>
                <Row label="Master">
                  <AudioState file={detail.data.audio?.master} />
                </Row>
                <Row label="Raw">
                  <AudioState file={detail.data.audio?.raw} />
                </Row>
              </div>
              <details className="border-t hairline pt-2">
                <summary className="label cursor-pointer text-gold-500/80">
                  Stored blueprint
                </summary>
                <pre className="mt-2 max-h-[320px] overflow-auto rounded-sm bg-ink-950 p-2 font-mono text-2xs leading-relaxed text-ink-400">
                  {JSON.stringify(detail.data.blueprint, null, 2)}
                </pre>
              </details>
            </div>
          ) : (
            <ErrorState title="Track not found" />
          )}
        </Panel>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------- 7. Analytics

const ANALYTICS_WINDOWS = ['24h', '7d', '30d', 'all'] as const

export function AnalyticsPage() {
  const [window, setWindow] = useState<(typeof ANALYTICS_WINDOWS)[number]>('24h')
  const [symbol, setSymbol] = useState<string | null>(null)
  const analytics = useQuery({
    queryKey: ['analytics', window, symbol],
    queryFn: () => api.analytics(window, symbol ?? undefined),
  })

  const cards = [
    { label: 'Tracks generated', value: integer(analytics.data?.tracks_generated) },
    { label: 'Tracks played', value: integer(analytics.data?.tracks_played) },
    { label: 'Generation failures', value: integer(analytics.data?.generation_failures) },
    { label: 'Emergency activations', value: integer(analytics.data?.emergency_activations) },
    { label: 'Avg market energy', value: decimal(analytics.data?.average_market_energy, 0) },
    { label: 'Avg radio energy', value: decimal(analytics.data?.average_radio_energy, 0) },
  ]

  return (
    <div className="space-y-4 p-4" data-testid="analytics-page">
      <PageHeader
        title="Analytics"
        subtitle={
          symbol
            ? 'Counted from the database, ' + symbol + ' only. Nothing here is estimated.'
            : 'Counted from the database. Nothing here is estimated.'
        }
        action={
          <div className="flex items-center gap-3">
            <MarketFilter value={symbol} onChange={setSymbol} />
          <div className="flex gap-0.5" role="group" aria-label="Time range">
            {ANALYTICS_WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setWindow(option)}
                aria-pressed={window === option}
                className={clsx(
                  'rounded-sm px-2 py-0.5 text-2xs font-medium uppercase tracking-label',
                  window === option
                    ? 'bg-gold-500/15 text-gold-300'
                    : 'text-ink-500 hover:bg-ink-850 hover:text-ink-300',
                )}
              >
                {option}
              </button>
            ))}
          </div>
          </div>
        }
      />

      <div className="grid grid-cols-6 gap-3">
        {cards.map((card) => (
          <Panel key={card.label} bodyClassName="px-3 py-2.5">
            <Metric label={card.label} value={card.value} />
          </Panel>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Distribution
          title="Genre distribution"
          data={analytics.data?.genre_distribution ?? []}
          loading={analytics.isLoading}
        />
        <Distribution
          title="Market regime distribution"
          data={analytics.data?.regime_distribution ?? []}
          loading={analytics.isLoading}
        />
      </div>
      <Distribution
        title="BPM distribution"
        data={(analytics.data?.bpm_distribution ?? []).map((row) => ({
          label: String(row.label),
          count: row.count,
        }))}
        loading={analytics.isLoading}
      />
    </div>
  )
}

function Distribution({
  title,
  data,
  loading,
}: {
  title: string
  data: { label: string; count: number }[]
  loading: boolean
}) {
  return (
    <Panel title={title} bodyClassName="p-2">
      {loading ? (
        <LoadingState />
      ) : data.length === 0 ? (
        <EmptyState
          title="No data in this window"
          detail="Nothing has been generated in the selected range."
        />
      ) : (
        <div className="h-[220px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -26 }}>
              <CartesianGrid stroke="#1b202b" vertical={false} />
              <XAxis
                dataKey="label"
                tick={{ fill: '#6b7383', fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: '#232935' }}
                interval="preserveStartEnd"
                tickFormatter={(value: string) => titleCase(value).slice(0, 12)}
              />
              <YAxis
                tick={{ fill: '#6b7383', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                width={40}
                allowDecimals={false}
              />
              <Tooltip
                cursor={{ fill: 'rgba(255,255,255,0.03)' }}
                contentStyle={{
                  background: '#0c0e12',
                  border: '1px solid #232935',
                  borderRadius: 4,
                  fontSize: 11,
                }}
              />
              <Bar dataKey="count" radius={[2, 2, 0, 0]}>
                {data.map((entry, index) => (
                  <Cell key={entry.label} fill={index % 2 === 0 ? '#c9a227' : '#8a6f1c'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  )
}

// ----------------------------------------------------------------- 8. OBS

export function ObsPage() {
  const capability = useCapability('obs')
  const sources = [
    'TF_NOW_PLAYING_TITLE',
    'TF_NOW_PLAYING_ARTIST',
    'TF_NOW_PLAYING_GENRE',
    'TF_NOW_PLAYING_BPM',
    'TF_MARKET_REGIME',
    'TF_MARKET_ENERGY',
    'TF_TRACK_ART',
  ]

  return (
    <div className="space-y-4 p-4" data-testid="obs-page">
      <PageHeader title="OBS" subtitle="Stream control and source mapping" />
      <Panel title="Connection">
        <AwaitingPhase
          subsystem="OBS websocket integration"
          phase={capability?.arrives_in_phase ?? 8}
          detail={capability?.detail}
        />
      </Panel>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Planned source mappings">
          <ul className="space-y-1">
            {sources.map((source) => (
              <li
                key={source}
                className="flex items-center justify-between border-b hairline py-1 last:border-0"
              >
                <span className="font-mono text-2xs text-ink-300">{source}</span>
                <span className="text-2xs text-ink-600">not connected</span>
              </li>
            ))}
          </ul>
        </Panel>
        <Panel title="Broadcast overlay">
          <p className="text-2xs leading-relaxed text-ink-400">
            The overlay is built and working now — it does not depend on OBS integration.
            Point an OBS Browser Source at it at 1920×1080.
          </p>
          <code className="mt-2 block rounded-sm bg-ink-950 px-2 py-1.5 font-mono text-2xs text-gold-300">
            {typeof window === 'undefined' ? '/overlay/live' : `${window.location.origin}/overlay/live`}
          </code>
          <a
            href="/overlay/live"
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-block text-2xs text-gold-400 underline-offset-2 hover:underline"
          >
            Open the overlay in a new tab
          </a>
        </Panel>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------- 9. System

export function SystemPage() {
  const { state } = useLive()
  const resources = useQuery({
    queryKey: ['resources'],
    queryFn: () => api.resources(),
    refetchInterval: 5_000,
  })
  const r = resources.data

  return (
    <div className="space-y-4 p-4" data-testid="system-page">
      <PageHeader title="System" subtitle="Host, process and runtime figures" />

      <div className="grid grid-cols-4 gap-3">
        <Panel bodyClassName="px-3 py-2.5">
          <Metric label="CPU" value={decimal(r?.cpu_percent, 0)} unit="%" />
        </Panel>
        <Panel bodyClassName="px-3 py-2.5">
          <Metric
            label="Memory"
            value={r ? `${decimal(r.memory_used_mb! / 1024, 1)}` : null}
            unit={r?.memory_total_mb ? `/ ${(r.memory_total_mb / 1024).toFixed(0)} GB` : undefined}
          />
        </Panel>
        <Panel bodyClassName="px-3 py-2.5">
          <Metric
            label="Process RSS"
            value={r ? decimal(r.process_memory_mb, 0) : null}
            unit="MB"
          />
        </Panel>
        <Panel bodyClassName="px-3 py-2.5">
          <Metric
            label="Event loop lag"
            value={r ? decimal(r.event_loop_lag_ms, 2) : null}
            unit="ms"
            tone={(r?.event_loop_lag_ms ?? 0) > 50 ? 'down' : 'default'}
          />
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="GPU" data-testid="gpu-panel">
          {r?.gpu_name ? (
            <div className="space-y-0.5">
              <Row label="Device">{r.gpu_name}</Row>
              <Row label="Utilisation">{decimal(r.gpu_percent, 0)}%</Row>
              <Row label="VRAM">
                {decimal(r.vram_used_mb, 0)} / {decimal(r.vram_total_mb, 0)} MB
              </Row>
              <Row label="Temperature">{decimal(r.gpu_temperature_c, 0)} °C</Row>
            </div>
          ) : (
            <EmptyState
              title="No GPU visible"
              detail="No NVIDIA device is available to this process. The mock provider does not need one, and no figures are shown rather than zeroes."
            />
          )}
        </Panel>

        <Panel title="Runtime" data-testid="runtime-panel">
          <div className="space-y-0.5">
            <Row label="Process uptime">{humanDuration(r?.process_uptime_seconds)}</Row>
            <Row label="Active tasks">{integer(r?.active_tasks)}</Row>
            <Row label="Database size">
              {r?.database_size_mb === null || r?.database_size_mb === undefined
                ? ABSENT
                : `${decimal(r.database_size_mb, 1)} MB`}
            </Row>
            <Row label="Disk free">
              {r?.disk_free_gb === null || r?.disk_free_gb === undefined
                ? ABSENT
                : `${decimal(r.disk_free_gb, 0)} GB`}
            </Row>
            <Row label="Queue depth">{integer(state?.queue.length)}</Row>
            <Row label="Buffer">{decimal(state?.buffer?.ready_minutes, 0)} min</Row>
            <Row label="Pending leases">{integer(state?.generation?.pending_leases)}</Row>
            <Row label="Generation p50">
              {state?.generation?.latency_p50_seconds === null ||
              state?.generation?.latency_p50_seconds === undefined
                ? ABSENT
                : `${decimal(state.generation.latency_p50_seconds, 0)}s`}
            </Row>
            <Row label="Generation p95">
              {state?.generation?.latency_p95_seconds === null ||
              state?.generation?.latency_p95_seconds === undefined
                ? ABSENT
                : `${decimal(state.generation.latency_p95_seconds, 0)}s`}
            </Row>
            <Row label="Capacity">{ratio(state?.generation?.capacity_ratio)}</Row>
          </div>
        </Panel>
      </div>

      <HealthPanel components={state?.health ?? []} />

      <Panel title="Watchdog">
        <AwaitingPhase
          subsystem="Process supervision and chaos recovery"
          phase={9}
          detail="Watchdog state, restart history and chaos-test results arrive with Phase 9."
        />
      </Panel>
    </div>
  )
}

// ----------------------------------------------------------------- 10. Settings

export function SettingsPage() {
  const { state } = useLive()
  const status = state?.status

  return (
    <div className="space-y-4 p-4" data-testid="settings-page">
      <PageHeader title="Settings" subtitle="Resolved configuration for this process" />

      <Panel title="Runtime">
        <div className="space-y-0.5">
          <Row label="Mode">{status?.mode ?? ABSENT}</Row>
          <Row label="Environment">{status?.environment ?? ABSENT}</Row>
          <Row label="Version">{status?.version ?? ABSENT}</Row>
          <Row label="Started">{status?.started_at ? clockTime(status.started_at) : ABSENT}</Row>
        </div>
      </Panel>

      <Panel title="Capabilities" data-testid="capabilities-panel">
        <ul className="divide-y divide-ink-800/80">
          {(state?.capabilities ?? []).map((capability) => (
            <li key={capability.capability} className="flex items-start justify-between gap-4 py-2">
              <div className="min-w-0">
                <div className="text-xs font-medium text-ink-200">
                  {titleCase(capability.capability)}
                </div>
                <p className="mt-0.5 text-2xs leading-snug text-ink-500">{capability.detail}</p>
              </div>
              <Chip
                tone={
                  capability.state === 'ready'
                    ? 'soft'
                    : capability.state === 'planned'
                      ? 'neutral'
                      : 'warn'
                }
                className="shrink-0"
              >
                {capability.state === 'planned' && capability.arrives_in_phase
                  ? `Phase ${capability.arrives_in_phase}`
                  : capability.state.toUpperCase()}
              </Chip>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Editing configuration">
        <p className="text-2xs leading-relaxed text-ink-400">
          Settings are resolved at startup from defaults, the mode overlay, the environment and
          any explicit overrides. Editing them from the browser would need a write path with
          validation, restart-impact analysis and secret masking — none of which exists yet, and
          a form that silently failed to apply would be worse than none.
        </p>
        <p className="mt-2 text-2xs leading-relaxed text-ink-600">
          Use <code className="font-mono text-ink-400">tradefix config</code> to see the resolved
          configuration with secrets masked, and <code className="font-mono text-ink-400">.env</code>{' '}
          to change it.
        </p>
      </Panel>
    </div>
  )
}

export function NotFoundPage() {
  return (
    <div className="p-4">
      <Panel title="Not found">
        <EmptyState title="No such page" detail="Pick a section from the left." />
      </Panel>
    </div>
  )
}

export { CapabilityUnavailable }
