/**
 * Types mirroring the API's DTOs.
 *
 * Hand-written rather than generated, deliberately. The API's OpenAPI schema is available and
 * a generator would keep these in sync automatically — but it would also import every field
 * the backend happens to expose, and the point of this file is that the UI consumes a
 * *narrow, named* contract. When the backend adds a field the UI does not use, nothing here
 * should change.
 *
 * The one rule that matters: **optional means absent, not zero.** Every `| null` below is a
 * value the backend declined to supply, and the component rendering it must show the absence
 * rather than substituting a default. A `0` where the backend sent `null` is a fabricated
 * metric, which §86 forbids.
 */

export type HealthState =
  | 'healthy'
  | 'degraded'
  | 'critical'
  | 'recovering'
  | 'offline'

export type BufferLevelName = 'healthy' | 'low' | 'critical' | 'empty'
export type TrajectoryName = 'filling' | 'holding' | 'draining' | 'stalled'
export type TierName = 'scheduled' | 'emergency_reserve' | 'procedural'
export type LockName = 'locked' | 'semi_locked' | 'replaceable' | 'operator_pinned'
export type LockLabel = 'HARD' | 'SOFT' | 'FLEXIBLE' | 'PINNED'
export type CapabilityState = 'ready' | 'degraded' | 'planned' | 'unavailable'

export interface Capability {
  capability: string
  state: CapabilityState
  detail: string
  arrives_in_phase: number | null
}

export interface MarketState {
  symbol: string
  timestamp: string
  /** Null when the feed is simulated or stale. §21 — never substitute a last-known value. */
  price: number | null
  price_change: number | null
  price_change_percent: number | null
  regime: string
  regime_label: string
  regime_confidence: number
  regime_age_seconds: number
  direction: string
  session: string
  energy: number
  energy_velocity: number
  volatility: number
  trend_strength: number
  momentum: number
  compression: number
  feed_status: string
  data_age_seconds: number
  is_stale: boolean
  is_simulated: boolean
}

/** One configured market's availability (routing V1). */
export interface MarketAvailability {
  symbol: string
  /** `open` | `closed` | `stale` | `unavailable` | `unknown`. */
  state: string
  /** A sentence naming why, e.g. "no tick for 412s". */
  reason: string
  /**
   * True when the data path looks broken rather than the market being shut.
   *
   * Rendered separately from `state` on purpose: a closed market needs no action and a
   * silent feed on a trading day does, and one combined badge would hide the difference
   * the whole routing subsystem exists to preserve.
   */
  feed_degraded: boolean
  data_age_seconds: number | null
  feed_status: string
  calendar_open: boolean | null
  is_active: boolean
  bars_processed: number
  last_price: number | null
  assessed_at: string
}

/** Which market the station is planning against, and why. */
export interface ActiveMarket {
  /** The live symbol, or `NO_ACTIVE_MARKET` when neither is usable. */
  active_symbol: string
  primary_symbol: string
  is_primary: boolean
  has_active_market: boolean
  active_since: string | null
  switch_reason: string | null
  switch_count: number
  /** A switch being confirmed but not yet made — the hysteresis window. */
  pending_symbol: string | null
  pending_seconds_remaining: number | null
  symbols: MarketAvailability[]
}

export interface MarketPoint {
  at: string
  market_energy: number
  radio_energy: number | null
  regime: string
  price: number | null
}

export interface MarketHistory {
  window: string
  points: MarketPoint[]
  regime_changes: { index: number; at: string; regime: string }[]
}

export interface ProgrammingReason {
  /** The director's own recorded rationale. Never generated text. */
  factors: string[]
  market_regime: string
  market_energy: number
  target_energy: number
  diversity_note: string | null
  creative_temperature: number | null
}

export interface NowPlaying {
  track_id: string
  title: string
  artist: string | null
  tier: TierName
  is_station_id: boolean
  genre: string | null
  secondary_genre: string | null
  bpm: number | null
  musical_key: string | null
  is_instrumental: boolean | null
  vocal_style: string | null
  duration_seconds: number
  elapsed_seconds: number
  remaining_seconds: number
  progress: number
  planned_regime: string | null
  /** Which market this was planned against. Null for items planned before routing. */
  planned_symbol: string | null
  planned_energy: number | null
  novelty_target: number | null
  transition_in: string | null
  origin: string
  started_at: string | null
  reason: ProgrammingReason | null
  output_peak: number | null
}

export interface QueueItem {
  position: number
  track_id: string
  title: string
  artist: string | null
  genre: string | null
  bpm: number | null
  energy: number | null
  planned_regime: string | null
  /** Which market this was planned against. Null for items planned before routing. */
  planned_symbol: string | null
  lock: LockName
  lock_label: LockLabel
  lock_reason: string | null
  is_protected: boolean
  readiness: 'pending' | 'ready' | 'unavailable'
  generation_state: string
  generation_progress: number | null
  duration_seconds: number
  starts_in_seconds: number | null
}

export interface BufferState {
  level: BufferLevelName
  trajectory: TrajectoryName
  reason: string
  ready_minutes: number
  pending_minutes: number
  minimum_minutes: number
  target_minutes: number
  maximum_minutes: number
  fill_ratio: number
  ready_tracks: number
  pending_tracks: number
  capacity_ratio: number
  trend_minutes_per_hour: number
  /** Only present when the buffer is actually shrinking. */
  seconds_to_failure: number | null
}

export interface EmergencyState {
  tier: TierName
  tier_label: 'NORMAL' | 'TIER 2 RESERVE' | 'TIER 3 PROCEDURAL'
  is_degraded: boolean
  reserve_minutes: number
  reserve_tracks_remaining: number
  tier2_activations: number
  tier3_activations: number
  seconds_in_tier2: number
  seconds_in_tier3: number
  recoveries: number
  last_reason: string | null
}

export interface HealthComponent {
  name: string
  label: string
  state: HealthState
  detail: string
  metric: string | null
  checked_at: string | null
}

export interface GenerationHealth {
  provider: string
  capacity_ratio: number
  latency_p50_seconds: number | null
  latency_p95_seconds: number | null
  samples: number
  completed: number
  failed: number
  retries: number
  timeouts: number
  cancelled: number
  in_flight: number
  pending_leases: number
  mean_audio_seconds: number | null
}

export interface GenerationJob {
  job_id: string
  track_id: string
  state: string
  priority: string
  provider: string
  attempt: number
  max_attempts: number
  age_seconds: number
  lease_owner: string | null
  lease_expires_in_seconds: number | null
  generation_seconds: number | null
  error_kind: string | null
  error_message: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface SystemResources {
  cpu_percent: number | null
  memory_used_mb: number | null
  memory_total_mb: number | null
  disk_free_gb: number | null
  disk_total_gb: number | null
  gpu_name: string | null
  gpu_percent: number | null
  vram_used_mb: number | null
  vram_total_mb: number | null
  gpu_temperature_c: number | null
  process_uptime_seconds: number
  process_memory_mb: number
  event_loop_lag_ms: number | null
  active_tasks: number
  database_size_mb: number | null
}

export interface Alert {
  key: string
  severity: 'info' | 'warning' | 'critical'
  message: string
  raised_at: string
  remediation: string | null
}

export interface StationStatus {
  is_broadcasting: boolean
  playout_state: string
  station_name: string
  tagline: string
  version: string
  environment: string
  mode: string
  started_at: string | null
  uptime_seconds: number
  tracks_played: number
  tracks_generated: number
  seconds_on_air: number
  unintended_silence_seconds: number
  underruns: number
  audio_coverage: number | null
}

export interface TrackSummary {
  track_id: string
  title: string
  artist: string | null
  genre: string | null
  secondary_genre: string | null
  bpm: number | null
  musical_key: string | null
  duration_seconds: number | null
  is_instrumental: boolean | null
  state: string
  planned_regime: string | null
  /** Which market this was planned against. Null for items planned before routing. */
  planned_symbol: string | null
  planned_energy: number | null
  /** Phase 6 populates this. Null for every track generated before the originality engine. */
  novelty_score: number | null
  play_count: number
  created_at: string | null
  last_played_at: string | null
}

export interface LiveState {
  status: StationStatus
  market: MarketState | null
  /** Null only when no routing subsystem is attached. */
  routing: ActiveMarket | null
  now_playing: NowPlaying | null
  queue: QueueItem[]
  buffer: BufferState | null
  emergency: EmergencyState | null
  generation: GenerationHealth | null
  health: HealthComponent[]
  alerts: Alert[]
  capabilities: Capability[]
  at: string
}

/** The small, frequent message. Separate so a progress tick re-renders almost nothing. */
export interface PositionUpdate {
  track_id: string
  elapsed_seconds: number
  remaining_seconds: number
  duration_seconds: number
  progress: number
  output_peak: number | null
}

export type LiveEnvelope =
  | { type: 'state'; at: string; payload: LiveState }
  | { type: 'position'; at: string; payload: PositionUpdate }

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline'

export interface ControlResult {
  applied: boolean
  message: string
  track_id: string | null
  lock: string | null
}
