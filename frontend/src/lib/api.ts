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

  resources: () => request<SystemResources>('/system/resources'),

  library: (params: LibraryQuery = {}) => {
    const query = new URLSearchParams()
    if (params.search) query.set('search', params.search)
    if (params.genre) query.set('genre', params.genre)
    if (params.regime) query.set('regime', params.regime)
    if (params.state) query.set('state', params.state)
    query.set('offset', String(params.offset ?? 0))
    query.set('limit', String(params.limit ?? 50))
    return request<LibraryPage>(`/library/tracks?${query}`)
  },
  libraryTrack: (trackId: string) =>
    request<TrackDetail>(`/library/tracks/${encodeURIComponent(trackId)}`),

  analytics: (window: string) => request<Analytics>(`/analytics?window=${window}`),

  originality: () => request<unknown>('/originality/summary'),
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

export interface TrackDetail {
  summary: TrackSummary
  blueprint: Record<string, unknown> | null
  has_audio: boolean
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
