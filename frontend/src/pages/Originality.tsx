/**
 * The Originality page (§6.16).
 *
 * Shows the evidence behind every approval and rejection: the QC checks with their measured
 * values, the similarity components that drove a verdict, and what mastering did. The design
 * principle is the same one that governs the API — **nothing here is a single verdict**. A
 * rejection without its component breakdown is an assertion an operator cannot check, and the
 * one question this page exists to answer is "why".
 *
 * The scope note is rendered verbatim from the server, at the top, always. §86 forbids
 * implying that fingerprinting establishes copyright uniqueness, and a page full of
 * similarity percentages implies exactly that unless it says otherwise in plain words. The
 * sentence comes from the component that owns the numbers rather than from this file, so the
 * claim and the data can never drift apart.
 */

import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { useState } from 'react'
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import {
  Chip,
  EmptyState,
  ErrorState,
  LoadingState,
  Metric,
  Panel,
} from '../components/primitives'
import { api, type OriginalityResult, type QcCheck, type SimilarityComponent } from '../lib/api'

const VERDICT_TONE = {
  approve: 'soft',
  review: 'warn',
  reject: 'danger',
} as const

const CHECK_TONE = {
  pass: 'soft',
  warn: 'warn',
  fail: 'danger',
} as const

function percent(value: number | null | undefined, digits = 0): string | null {
  return value === null || value === undefined ? null : `${(value * 100).toFixed(digits)}%`
}

function score(value: number | null | undefined): string | null {
  return value === null || value === undefined ? null : value.toFixed(3)
}

/** A measured value with its unit, or the explicit absence marker. Never a zero stand-in. */
function checkValue(check: QcCheck): string {
  if (check.value === null) return '—'
  const magnitude = Math.abs(check.value)
  const formatted =
    magnitude >= 100 ? check.value.toFixed(0) : magnitude >= 1 ? check.value.toFixed(2) : check.value.toFixed(4)
  return check.unit ? `${formatted} ${check.unit}` : formatted
}

/**
 * What each evidence class means, in a sentence an operator can act on.
 *
 * The raw enum values are precise and unhelpful on a dashboard; these are the same
 * distinctions in words. Kept beside the page rather than sent from the server because
 * they are presentation, and the server already sends the per-track reason verbatim.
 */
const EVIDENCE_LABELS: Record<string, string> = {
  style_only: 'Approved — similar style, not the same recording',
  no_production_history: 'Approved — cold start, nothing has aired to repeat',
  rotation_pressure: 'Rejected — too close to something aired very recently',
  definitive_duplicate: 'Rejected — exact content match',
  strong_recording_match: 'Rejected — fingerprint says the same recording',
  corroborated_recording_match: 'Rejected — same recording, confirmed by structure',
}

export function OriginalityPage() {
  const [selected, setSelected] = useState<string | null>(null)

  const summary = useQuery({ queryKey: ['originality', 'summary'], queryFn: api.originality })
  const recent = useQuery({
    queryKey: ['originality', 'recent'],
    queryFn: () => api.originalityRecent(50),
  })

  if (summary.isError) {
    return (
      <div className="space-y-4 p-4" data-testid="originality-page">
        <ErrorState
          title="Originality is unavailable"
          detail={summary.error instanceof Error ? summary.error.message : undefined}
        />
      </div>
    )
  }

  const data = summary.data
  const verdicts = data?.verdict_counts ?? {}
  const histogram = (data?.novelty_histogram ?? []).map((count, index) => ({
    bucket: `${index * 10}–${index * 10 + 10}`,
    count,
  }))
  const evaluated = data?.evaluated_count ?? 0

  return (
    <div className="space-y-4 p-4" data-testid="originality-page">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-100">Originality</h1>
          <p className="mt-0.5 text-2xs text-ink-500">
            Internal duplication prevention — not a copyright guarantee
          </p>
        </div>
        {data && (
          <Chip tone="neutral" title={data.fingerprint_detail}>
            fingerprint: {data.fingerprint_provider}
          </Chip>
        )}
      </header>

      {/* Rendered verbatim and above the numbers, never collapsed behind a tooltip. */}
      {data && (
        <p
          className="rounded-sm border hairline bg-ink-900/60 p-3 text-2xs leading-relaxed text-ink-400"
          data-testid="originality-scope"
        >
          {data.scope_note}
        </p>
      )}

      {data?.cold_start && (
        /*
         * Said plainly, because the alternative is an approval rate near 100% that reads
         * as quality and is actually an empty library. Graded novelty compares against
         * tracks that have aired; with none, there is nothing a new track could repeat.
         */
        <p
          className="rounded-sm border border-status-degraded/30 bg-status-degraded/5 p-3
            text-2xs leading-relaxed text-status-degraded"
          data-testid="originality-cold-start"
        >
          <strong>Novelty history: COLD START.</strong> No track has aired on the
          production station yet, so there are 0 reference recordings for creative
          similarity. Approvals below reflect that absence, not a judgement about quality.
          Duplicate detection is unaffected — it compares against every track regardless
          of whether it aired.
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Metric label="Library size" value={data?.library_size ?? null} size="lg" />
        <Metric label="Evaluated" value={evaluated || null} size="lg" />
        <Metric
          label="Approved"
          value={verdicts.approve ?? null}
          size="lg"
          tone="gold"
          hint={evaluated ? percent((verdicts.approve ?? 0) / evaluated) ?? undefined : undefined}
        />
        <Metric label="Review" value={verdicts.review ?? null} size="lg" />
        <Metric
          label="Rejected"
          value={verdicts.reject ?? null}
          size="lg"
          hint={evaluated ? percent((verdicts.reject ?? 0) / evaluated) ?? undefined : undefined}
        />
      </div>

      {data && Object.keys(data.evidence_counts).length > 0 && (
        <Panel
          title="How REVIEW candidates were resolved"
          data-testid="review-resolution"
          action={
            data.resolver_version ? (
              <span className="font-mono text-2xs text-ink-500">
                resolver {data.resolver_version}
              </span>
            ) : undefined
          }
        >
          <p className="mb-3 text-2xs leading-relaxed text-ink-500">
            A REVIEW verdict means the combined score could not decide. The resolver then
            asks what <em>kind</em> of similarity it was: two lo-fi tracks at the same
            tempo share a genre, not a recording.
          </p>
          <div className="space-y-1">
            {Object.entries(data.evidence_counts)
              .sort((a, b) => b[1] - a[1])
              .map(([evidence, count]) => (
                <Row
                  key={evidence}
                  label={EVIDENCE_LABELS[evidence] ?? evidence}
                  value={String(count)}
                />
              ))}
          </div>
        </Panel>
      )}

      <Panel title="Novelty distribution">
        {summary.isLoading ? (
          <LoadingState />
        ) : evaluated === 0 ? (
          <EmptyState
            title="Nothing evaluated yet"
            detail="The distribution appears once tracks have been through post-production."
          />
        ) : (
          <div className="h-44" data-testid="novelty-histogram">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={histogram} margin={{ top: 4, right: 4, bottom: 0, left: -24 }}>
                <XAxis
                  dataKey="bucket"
                  tick={{ fontSize: 10, fill: 'rgb(var(--ink-500))' }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fontSize: 10, fill: 'rgb(var(--ink-500))' }}
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip
                  contentStyle={{
                    background: 'rgb(var(--ink-900))',
                    border: '1px solid rgb(var(--ink-700))',
                    borderRadius: 2,
                    fontSize: 11,
                  }}
                  formatter={(value: number) => [`${value} track(s)`, 'count']}
                  labelFormatter={(label: string) => `novelty ${label}%`}
                />
                <Bar dataKey="count" radius={[1, 1, 0, 0]}>
                  {histogram.map((entry, index) => (
                    <Cell
                      key={entry.bucket}
                      // Low novelty is the end worth noticing, so it is the end that is
                      // coloured. A uniformly gold chart would say nothing.
                      fill={index < 3 ? 'rgb(var(--status-degraded))' : 'rgb(var(--gold-500))'}
                      fillOpacity={index < 3 ? 0.8 : 0.55}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Panel>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <Panel title="Recent evaluations" bodyClassName="p-0">
          {recent.isLoading ? (
            <div className="p-4">
              <LoadingState />
            </div>
          ) : !recent.data?.length ? (
            <div className="p-4">
              <EmptyState
                title="No evaluations recorded"
                detail="Every generated track is evaluated before it can become READY."
              />
            </div>
          ) : (
            <div className="max-h-[32rem] overflow-y-auto">
              <table className="w-full text-2xs">
                <thead className="sticky top-0 bg-ink-900/95 text-ink-500">
                  <tr className="border-b hairline">
                    <th className="px-3 py-1.5 text-left font-medium">Track</th>
                    <th className="px-3 py-1.5 text-left font-medium">Verdict</th>
                    <th className="px-3 py-1.5 text-right font-medium">Novelty</th>
                    <th className="px-3 py-1.5 text-left font-medium">Closest</th>
                    <th className="px-3 py-1.5 text-left font-medium">Driven by</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data.map((row) => (
                    <EvaluationRow
                      key={`${row.track_id}-${row.evaluated_at}`}
                      row={row}
                      selected={selected === row.track_id}
                      onSelect={() => setSelected(row.track_id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <EvidencePanel trackId={selected} />
      </div>
    </div>
  )
}

function EvaluationRow({
  row,
  selected,
  onSelect,
}: {
  row: OriginalityResult
  selected: boolean
  onSelect: () => void
}) {
  return (
    <tr
      onClick={onSelect}
      className={clsx(
        'cursor-pointer border-b hairline transition-colors',
        selected ? 'bg-gold-500/10' : 'hover:bg-ink-850',
      )}
    >
      <td className="px-3 py-1.5 font-mono text-ink-200">{row.track_id}</td>
      <td className="px-3 py-1.5">
        <Chip tone={VERDICT_TONE[row.verdict]}>{row.verdict}</Chip>
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-ink-200">
        {row.novelty_score.toFixed(2)}
      </td>
      <td className="px-3 py-1.5 font-mono text-ink-400">{row.closest_track_id ?? '—'}</td>
      <td className="px-3 py-1.5 text-ink-400">
        {row.deciding_component?.replace(/_/g, ' ') ?? '—'}
      </td>
    </tr>
  )
}

function EvidencePanel({ trackId }: { trackId: string | null }) {
  const evidence = useQuery({
    queryKey: ['originality', 'evidence', trackId],
    queryFn: () => api.trackEvidence(trackId as string),
    enabled: trackId !== null,
  })

  if (trackId === null) {
    return (
      <Panel title="Evidence">
        <EmptyState
          title="Select a track"
          detail="Every check, component score and mastering measurement behind its verdict."
        />
      </Panel>
    )
  }
  if (evidence.isLoading) {
    return (
      <Panel title="Evidence">
        <LoadingState />
      </Panel>
    )
  }
  if (evidence.isError || !evidence.data) {
    return (
      <Panel title="Evidence">
        <ErrorState
          title="No record for this track"
          detail={evidence.error instanceof Error ? evidence.error.message : undefined}
        />
      </Panel>
    )
  }

  const { qc_results, originality, mastering, fingerprint, lyrics, features } = evidence.data

  return (
    <Panel title={`Evidence — ${trackId}`} bodyClassName="space-y-4 p-4">
      {originality && (
        <section>
          <h3 className="label mb-2">Similarity components</h3>
          <p className="mb-2 text-2xs text-ink-500">
            Compared against {originality.compared_against.toLocaleString()} track(s). Each
            component is scored separately; the verdict follows the strongest, not an average.
          </p>
          {originality.comparisons.length === 0 ? (
            <p className="text-2xs text-ink-500">
              Nothing to compare against — this was the first track in the library.
            </p>
          ) : (
            <div className="space-y-2">
              {originality.comparisons.map((comparison) => (
                <ComparisonRow key={comparison.track_id} comparison={comparison} />
              ))}
            </div>
          )}
        </section>
      )}

      {qc_results.map((result) => (
        <section key={`${result.stage}-${result.evaluated_at}`}>
          <h3 className="label mb-2 flex items-center gap-2">
            QC — {result.stage}
            <Chip tone={CHECK_TONE[result.status]}>{result.status}</Chip>
            <span className="font-normal normal-case tracking-normal text-ink-500">
              {result.passed_count} passed · {result.warned_count} warned ·{' '}
              {result.failed_count} failed
            </span>
          </h3>
          <table className="w-full text-2xs">
            <tbody>
              {result.checks
                // Failures first, then warnings: the reason someone opened this panel.
                .slice()
                .sort((a, b) => rank(a.status) - rank(b.status))
                .map((check) => (
                  <tr key={check.name} className="border-b hairline last:border-0">
                    <td className="py-1 pr-2 align-top">
                      <Chip tone={CHECK_TONE[check.status]}>{check.status}</Chip>
                    </td>
                    <td className="py-1 pr-2 align-top font-mono text-ink-300">
                      {check.name}
                    </td>
                    <td className="py-1 pr-2 align-top text-right tabular-nums text-ink-200">
                      {checkValue(check)}
                    </td>
                    <td className="py-1 pr-2 align-top text-ink-500">{check.threshold}</td>
                    <td className="py-1 align-top text-ink-400">{check.reason}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      ))}

      {mastering && (
        <section>
          <h3 className="label mb-2">Mastering</h3>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="Target" value={mastering.target_lufs.toFixed(1)} unit="LUFS" size="sm" />
            <Metric
              label="Achieved"
              value={mastering.measured_lufs_after?.toFixed(1) ?? null}
              unit="LUFS"
              size="sm"
              hint={mastering.peak_constrained ? 'peak-constrained' : undefined}
            />
            <Metric
              label="True peak"
              value={mastering.true_peak_dbtp?.toFixed(1) ?? null}
              unit="dBTP"
              size="sm"
              hint={
                mastering.true_peak_ceiling_dbtp !== null
                  ? `ceiling ${mastering.true_peak_ceiling_dbtp.toFixed(1)}`
                  : undefined
              }
            />
            <Metric
              label="Gain applied"
              value={mastering.gain_applied_db?.toFixed(1) ?? null}
              unit="dB"
              size="sm"
            />
          </div>
          <p className="mt-2 text-2xs text-ink-400">{mastering.detail}</p>
        </section>
      )}

      <section className="grid gap-3 sm:grid-cols-2">
        {features && (
          <div>
            <h3 className="label mb-2">Measured</h3>
            <dl className="space-y-1 text-2xs">
              <Row label="Duration" value={`${features.duration_seconds.toFixed(1)} s`} />
              <Row label="Format" value={`${features.sample_rate} Hz · ${features.channels} ch`} />
              <Row label="Tempo" value={features.tempo ? `${features.tempo.toFixed(1)} BPM` : '—'} />
              <Row label="Key" value={features.musical_key ?? '—'} />
              <Row label="Crest factor" value={features.crest_factor.toFixed(2)} />
              <Row label="Analysis backend" value={features.backend} />
            </dl>
          </div>
        )}
        {(fingerprint || lyrics) && (
          <div>
            <h3 className="label mb-2">Identity</h3>
            <dl className="space-y-1 text-2xs">
              {fingerprint && (
                <>
                  <Row
                    label="Audio hash"
                    value={fingerprint.canonical_sha256?.slice(0, 16) ?? '—'}
                    mono
                  />
                  <Row
                    label="Fingerprint"
                    value={
                      fingerprint.provider
                        ? `${fingerprint.provider} v${fingerprint.provider_version ?? '?'}`
                        : '—'
                    }
                  />
                  <Row label="Embedding version" value={String(fingerprint.embedding_version ?? '—')} />
                </>
              )}
              {lyrics && (
                <>
                  <Row label="Lyric words" value={String(lyrics.word_count)} />
                  <Row label="Distinct words" value={percent(lyrics.unique_word_ratio) ?? '—'} />
                  <Row
                    label="Internal repetition"
                    value={percent(lyrics.internal_repetition) ?? '—'}
                  />
                </>
              )}
            </dl>
          </div>
        )}
      </section>
    </Panel>
  )
}

function rank(status: QcCheck['status']): number {
  return { fail: 0, warn: 1, pass: 2 }[status]
}

function ComparisonRow({ comparison }: { comparison: SimilarityComponent }) {
  const parts = Object.entries(comparison.components)
    .filter(([, value]) => value > 0)
    .sort(([, a], [, b]) => b - a)
  return (
    <div className="rounded-sm border hairline bg-ink-900/40 p-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-2xs text-ink-200">{comparison.track_id}</span>
        <span className="tabular-nums text-2xs text-ink-300">
          overall {score(comparison.score)}
        </span>
      </div>
      <p className="mt-0.5 text-2xs text-ink-500">{comparison.detail}</p>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {parts.length === 0 ? (
          <span className="text-2xs text-ink-600">no component scored above zero</span>
        ) : (
          parts.map(([name, value]) => (
            <Chip
              key={name}
              tone={value >= 0.9 ? 'danger' : value >= 0.7 ? 'warn' : 'flexible'}
              title={`${name}: ${value.toFixed(4)}`}
            >
              {name.replace(/_/g, ' ')} {value.toFixed(2)}
            </Chip>
          ))
        )}
        {comparison.is_exact_audio && <Chip tone="danger">identical audio</Chip>}
        {comparison.is_exact_lyrics && <Chip tone="danger">identical lyrics</Chip>}
        {comparison.blueprint_is_recent && <Chip tone="warn">recent window</Chip>}
      </div>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-ink-500">{label}</dt>
      <dd className={clsx('text-ink-200', mono && 'font-mono')}>{value}</dd>
    </div>
  )
}
