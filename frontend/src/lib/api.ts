/**
 * The HTTP client.
 *
 * Thin on purpose: typed functions over `fetch`, no abstraction layer. The interesting
 * behaviour lives in two places.
 *
 * **Capability refusals are not errors.** The API answers 409 with a structured body when a
 * subsystem does not exist yet (originality, OBS). That is a *fact about the build*, not a
 * failure, so it is parsed into a value the caller can render rather than thrown — a page
 * showing "awaiting Phase 6" should not go through an error boundary to get there.
 *
 * **Everything else that fails, fails loudly.** A 500 is a real problem and the UI should say
 * so rather than quietly rendering an empty state, which is indistinguishable from a healthy
 * but idle station.
 */

import type {
  ActiveMarket,
  Alert,
  BufferState,
  Capability,
  ControlResult,
  EmergencyState,
  GenerationJob,
  LiveState,
  MarketHistory,
  MarketState,
  NowPlaying,
  QueueItem,
  SystemResources,
  TrackSummary,
} from './types'

const BASE = '/api'

/** A subsystem that is not available in this build, with the reason and the phase. */
export class CapabilityUnavailable extends Error {
  readonly capability: string
  readonly state: string
  readonly arrivesInPhase: number | null

  constructor(detail: {
    capability: string
    state: string
    detail: string
    arrives_in_phase: number | null
  }) {
    super(detail.detail)
    this.name = 'CapabilityUnavailable'
    this.capability = detail.capability
    this.state = detail.state
    this.arrivesInPhase = detail.arrives_in_phase
  }
}

/** The station is not attached to this API process. */
export class StationUnavailable extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'StationUnavailable'
  }
}

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}) },
      ...init,
    })
  } catch (cause) {
    // A network failure, not an HTTP one: the API is unreachable. Distinguished because the
    // UI's offline affordance is different from its error affordance.
    throw new ApiError(0, cause instanceof Error ? cause.message : 'The API is unreachable.')
  }

  if (response.ok) {
    return (await response.json()) as T
  }

  let detail: unknown = null
  try {
    detail = (await response.json())?.detail ?? null
  } catch {
    detail = null
  }

  if (response.status === 409 && detail && typeof detail === 'object' && 'capability' in detail) {
    throw new CapabilityUnavailable(detail as never)
  }
  if (response.status === 503) {
    throw new StationUnavailable(typeof detail === 'string' ? detail : 'No station attached.')
  }
  throw new ApiError(
    response.status,
    typeof detail === 'string' ? detail : `${response.status} ${response.statusText}`,
  )
}

export const api = {
  status: () => request<LiveState>('/status'),
  capabilities: () => request<Capability[]>('/capabilities'),

  marketCurrent: () => request<MarketState | null>('/market/current'),
  marketHistory: (window: string) =>
    request<MarketHistory>(`/market/history?window=${encodeURIComponent(window)}`),

  radioCurrent: () => request<NowPlaying | null>('/radio/current'),
  radioQueue: () => request<QueueItem[]>('/radio/queue'),
  radioBuffer: () => request<BufferState>('/radio/buffer'),
  radioEmergency: () => request<EmergencyState>('/radio/emergency'),
  radioHistory: (limit = 50) => request<HistoryEntry[]>(`/radio/history?limit=${limit}`),

  skip: () => request<ControlResult>('/radio/skip', { method: 'POST' }),
  lock: (trackId: string) =>
    request<ControlResult>(`/radio/queue/${encodeURIComponent(trackId)}/lock`, {
      method: 'POST',
    }),
  unlock: (trackId: string) =>
    request<ControlResult>(`/radio/queue/${encodeURIComponent(trackId)}/unlock`, {
      method: 'POST',
    }),

  markets: () => request<ActiveMarket>('/markets'),
  setMarketClosed: (symbol: string, closed: boolean) =>
    request<ActiveMarket>('/simulation/market-closure', {
      method: 'POST',
      body: JSON.stringify({ symbol, closed }),
    }),

  scenarios: () => request<ScenarioList>('/simulation/scenarios'),
  setScenario: (scenario: string) =>
    request<ScenarioList>('/simulation/regime', {
      method: 'POST',
      body: JSON.stringify({ scenario }),
    }),

  jobs: (params: { state?: string; limit?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.state) query.set('state', params.state)
    query.set('limit', String(params.limit ?? 50))
    return request<GenerationJob[]>(`/generation/jobs?${query}`)
  },
  jobCounts: () => request<{ by_state: Record<string, number>; total: number }>('/generation/counts'),
  providerStatus: () => request<ProviderStatus>('/generation/provider'),

  resources: () => request<SystemResources>('/system/resources'),

  library: (params: LibraryQuery = {}) => {
    const query = new URLSearchParams()
    if (params.search) query.set('search', params.search)
    if (params.genre) query.set('genre', params.genre)
    if (params.regime) query.set('regime', params.regime)
    if (params.symbol) query.set('symbol', params.symbol)
    if (params.state) query.set('state', params.state)
    query.set('offset', String(params.offset ?? 0))
    query.set('limit', String(params.limit ?? 50))
    return request<LibraryPage>(`/library/tracks?${query}`)
  },
  libraryTrack: (trackId: string) =>
    request<TrackDetail>(`/library/tracks/${encodeURIComponent(trackId)}`),

  analytics: (window: string, symbol?: string) => {
    const query = new URLSearchParams({ window })
    if (symbol) query.set('symbol', symbol)
    return request<Analytics>(`/analytics?${query}`)
  },

  originality: () => request<OriginalitySummary>('/originality/summary'),
  originalityRecent: (limit = 50) =>
    request<OriginalityResult[]>(`/originality/recent?limit=${limit}`),
  trackEvidence: (trackId: string) =>
    request<TrackEvidence>(`/originality/tracks/${encodeURIComponent(trackId)}`),
  obs: () => request<unknown>('/obs/status'),
}

export interface HistoryEntry {
  track_id: string
  genre: string
  secondary_genre: string | null
  bpm: number
  musical_key: string
  duration_seconds: number
  is_instrumental: boolean
  primary_topic: string | null
  persona_id: string | null
  energy_at_generation: number
  regime_at_generation: string
  played_at: string | null
}

export interface ScenarioList {
  applied: boolean
  scenario: string
  message: string
  available: string[]
}

export interface LibraryQuery {
  search?: string
  genre?: string
  regime?: string
  /** Omit for every market — there is no 'ALL' sentinel. */
  symbol?: string
  state?: string
  offset?: number
  limit?: number
}

export interface LibraryPage {
  items: TrackSummary[]
  total: number
  offset: number
  limit: number
}

/** The validated lyric the station composed, as stored (§17). */
export interface TrackLyrics {
  text: string
  format: string
  perspective: string
  primary_topic: string
  secondary_topic: string | null
  tradefix_mentions: number
  educational_intensity: number
  word_count: number
  concepts_used: string[]
  created_at: string
}

/**
 * What the generation provider was actually sent (§7.9, §7.10, §7.26).
 *
 * Kept beside the blueprint rather than folded into it, because the blueprint records the
 * station's *intent* and this records what the model was *told*. They are supposed to
 * agree; the whole reason this exists is that for every vocal track they silently did not.
 */
export interface ProviderSubmission {
  attempt: number
  provider: string
  model_identifier: string
  caption: string
  requested_lyrics: string | null
  provider_lyrics: string
  lyrics_modified: boolean
  lyric_notes: string[]
  instrumental: boolean
  profile: string
  inference_steps: number
  guidance_scale: number
  seed: number
  warnings: string[]
  /** A vocal track realised without words. The headline a detail panel should lead on. */
  vocals_downgraded: boolean
}

/** One role's audio, with the filesystem actually consulted (B5). */
export interface AudioFile {
  /**
   * `present` — a live row and the bytes are there.
   * `deleted` — retention reclaimed them on purpose. Expected, not a fault.
   * `missing` — a row says the file exists and it does not. A real defect.
   * `unknown` — no row at all; true of every pre-B5 track.
   */
  status: 'present' | 'deleted' | 'missing' | 'unknown'
  size_bytes: number | null
  format: string | null
  sample_rate: number | null
  channels: number | null
  created_at: string | null
  deleted_at: string | null
  retained_forever: boolean
}

export interface TrackDetail {
  summary: TrackSummary
  blueprint: Record<string, unknown> | null
  /** Keyed by file role: `raw`, `master`, and any others the track owns. */
  audio: Record<string, AudioFile>
  /** True only when some role is `present`. Was `bool(row_count)`, which was always false. */
  has_audio: boolean
  /** Null for a genuine instrumental, or when composition failed. */
  lyrics: TrackLyrics | null
  /** Null until the track has been generated at least once. */
  submission: ProviderSubmission | null
}

export interface Analytics {
  window: string
  tracks_generated: number
  tracks_played: number
  generation_failures: number
  emergency_activations: number
  average_market_energy: number | null
  average_radio_energy: number | null
  genre_distribution: { label: string; count: number }[]
  regime_distribution: { label: string; count: number }[]
  bpm_distribution: { label: number; count: number }[]
}

export type { Alert }


// ---------------------------------------------------------------- originality

export interface OriginalitySummary {
  library_size: number
  evaluated_count: number
  verdict_counts: Record<string, number>
  novelty_histogram: number[]
  fingerprint_provider: string
  fingerprint_detail: string
  /** The scope the station is willing to claim. Rendered verbatim, never paraphrased. */
  scope_note: string

  /** PRODUCTION_RADIO tracks available to compare against. */
  production_references: number
  /**
   * True when nothing has aired, so graded novelty has no basis.
   *
   * Rendered explicitly. The alternative is a 100% approval rate that reads as quality
   * and is actually an empty library.
   */
  cold_start: boolean
  /** Dispositions of candidates that entered REVIEW. */
  resolution_counts: Record<string, number>
  /** Which class of evidence decided them. */
  evidence_counts: Record<string, number>
  resolver_version: string | null
}

export interface SimilarityComponent {
  track_id: string
  score: number
  components: Record<string, number>
  is_exact_audio: boolean
  is_exact_lyrics: boolean
  blueprint_threshold: number | null
  blueprint_is_recent: boolean | null
  detail: string
}

export interface OriginalityResult {
  track_id: string
  verdict: 'approve' | 'review' | 'reject'
  novelty_score: number
  max_similarity: number
  threshold: number
  closest_track_id: string | null
  deciding_component: string | null
  compared_against: number
  comparisons: SimilarityComponent[]
  evaluated_at: string
}

export interface QcCheck {
  name: string
  status: 'pass' | 'warn' | 'fail'
  value: number | null
  unit: string
  threshold: string
  reason: string
}

export interface QcResult {
  stage: 'raw' | 'mastered'
  status: 'pass' | 'warn' | 'fail'
  summary: string
  passed_count: number
  warned_count: number
  failed_count: number
  analysis_backend: string
  elapsed_seconds: number
  evaluated_at: string
  checks: QcCheck[]
}

export interface MasteringRecord {
  outcome: 'mastered' | 'skipped' | 'failed'
  target_lufs: number
  measured_lufs_before: number | null
  measured_lufs_after: number | null
  true_peak_dbtp: number | null
  true_peak_ceiling_dbtp: number | null
  peak_constrained: boolean
  gain_applied_db: number | null
  trimmed_seconds: number
  duration_after: number | null
  elapsed_seconds: number
  detail: string
  mastered_at: string
}

export interface AudioFeaturesRecord {
  duration_seconds: number
  sample_rate: number
  channels: number
  peak: number
  rms: number
  crest_factor: number
  integrated_lufs: number | null
  spectral_centroid: number | null
  tempo: number | null
  musical_key: string | null
  silence_ratio: number
  clipped_sample_ratio: number
  backend: string
  computed_at: string
}

export interface FingerprintRecord {
  canonical_sha256: string | null
  file_sha256: string | null
  provider: string | null
  provider_version: string | null
  embedding_version: number | null
  tempo: number | null
  musical_key: string | null
  blueprint_signature: string | null
  computed_at: string | null
}

export interface LyricFingerprintRecord {
  content_hash: string
  word_count: number
  unique_word_ratio: number
  internal_repetition: number
  tradefix_mentions: number
  line_count: number
  computed_at: string
}

export interface TrackEvidence {
  track_id: string
  qc_results: QcResult[]
  features: AudioFeaturesRecord | null
  originality: OriginalityResult | null
  mastering: MasteringRecord | null
  fingerprint: FingerprintRecord | null
  lyrics: LyricFingerprintRecord | null
}


// -------------------------------------------------------------- provider

export interface ProviderStatus {
  provider: string
  /** `unavailable` | `loading` | `ready` | `generating` | `unloading` | `failed` */
  status: string
  model: string
  lm_model: string | null
  version: string | null
  loaded: boolean
  load_seconds: number | null
  last_success_at: string | null
  last_error: string | null
  last_error_at: string | null
  generations: number
  failures: number
  oom_events: number
  latency_p50_seconds: number | null
  latency_p95_seconds: number | null
  current_track_id: string | null
  current_elapsed_seconds: number | null
  vram_total_mb: number | null
  vram_used_mb: number | null
  vram_free_mb: number | null
  peak_vram_mb: number | null
  gpu_temperature_c: number | null
  /** False for ACE-Step: it reports pending/done, so a percentage would be invented. */
  supports_progress: boolean
}
