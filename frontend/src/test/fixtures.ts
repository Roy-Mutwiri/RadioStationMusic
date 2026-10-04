/**
 * Fixtures shaped exactly like the API's responses.
 *
 * Built from the DTO types rather than hand-written objects, so a backend field that changes
 * shape breaks the fixture at compile time rather than silently making every test assert
 * against something the API no longer sends.
 */

import type {
  ActiveMarket,
  MarketAvailability,
  BufferState,
  EmergencyState,
  GenerationHealth,
  HealthComponent,
  LiveState,
  MarketState,
  NowPlaying,
  QueueItem,
  StationStatus,
} from '../lib/types'

export const marketFixture: MarketState = {
  symbol: 'XAUUSD',
  timestamp: '2026-10-02T12:00:00Z',
  price: 4012.5,
  price_change: 3.25,
  price_change_percent: 0.081,
  regime: 'bullish_breakout',
  regime_label: 'Bullish Breakout',
  regime_confidence: 0.89,
  regime_age_seconds: 900,
  direction: 'bullish',
  session: 'london',
  energy: 84,
  energy_velocity: 2.5,
  volatility: 81,
  trend_strength: 88,
  momentum: 76,
  compression: 12,
  feed_status: 'live',
  data_age_seconds: 1.2,
  is_stale: false,
  is_simulated: false,
}

export const nowPlayingFixture: NowPlaying = {
  track_id: 'TF-20261002-00042',
  title: 'Liquidity After Midnight',
  artist: 'tf01',
  tier: 'scheduled',
  is_station_id: false,
  genre: 'uk_trap',
  secondary_genre: 'dnb',
  bpm: 148,
  musical_key: 'F# minor',
  is_instrumental: false,
  vocal_style: 'rap',
  duration_seconds: 215,
  elapsed_seconds: 64,
  remaining_seconds: 151,
  progress: 64 / 215,
  planned_regime: 'bullish_breakout',
  planned_symbol: 'XAUUSD',
  planned_energy: 86,
  novelty_target: 0.82,
  transition_in: 'crossfade',
  origin: 'Scheduled queue',
  started_at: '2026-10-02T11:58:56Z',
  reason: {
    factors: [
      'critical priority (buffer empty)',
      'target energy 86 from market 84',
      'genre uk_trap',
      'relaxed: none',
      '148 BPM',
    ],
    market_regime: 'bullish_breakout',
    market_energy: 84,
    target_energy: 86,
    diversity_note: null,
    creative_temperature: 0.62,
  },
  output_peak: 0.71,
}

export function queueFixture(): QueueItem[] {
  return [
    {
      position: 0,
      track_id: 'TF-20261002-00043',
      title: 'Session Open Pressure',
      artist: 'tf02',
      genre: 'dnb',
      bpm: 174,
      energy: 88,
      planned_regime: 'bullish_breakout',
      planned_symbol: 'XAUUSD',
      lock: 'locked',
      lock_label: 'HARD',
      lock_reason: 'next on air',
      is_protected: true,
      readiness: 'ready',
      generation_state: 'ready',
      generation_progress: null,
      duration_seconds: 198,
      starts_in_seconds: 151,
    },
    {
      position: 1,
      track_id: 'TF-20261002-00044',
      title: 'Compression Study',
      artist: 'tf03',
      genre: 'deep_house',
      bpm: 122,
      energy: 54,
      planned_regime: 'normal_range',
      planned_symbol: 'XAUUSD',
      lock: 'semi_locked',
      lock_label: 'SOFT',
      lock_reason: null,
      is_protected: true,
      readiness: 'ready',
      generation_state: 'ready',
      generation_progress: null,
      duration_seconds: 240,
      starts_in_seconds: 349,
    },
    {
      position: 2,
      track_id: 'TF-20261002-00045',
      title: 'Range Drift',
      artist: 'tf04',
      genre: 'lofi',
      bpm: 84,
      energy: 28,
      planned_regime: 'quiet',
      planned_symbol: 'XAUUSD',
      lock: 'replaceable',
      lock_label: 'FLEXIBLE',
      lock_reason: null,
      is_protected: false,
      readiness: 'pending',
      generation_state: 'generating',
      generation_progress: 0.42,
      duration_seconds: 210,
      starts_in_seconds: 589,
    },
  ]
}

export const bufferFixture: BufferState = {
  level: 'healthy',
  trajectory: 'filling',
  reason: 'generating at 2.25x playback',
  ready_minutes: 47,
  pending_minutes: 9,
  minimum_minutes: 20,
  target_minutes: 45,
  maximum_minutes: 90,
  fill_ratio: 47 / 45,
  ready_tracks: 14,
  pending_tracks: 3,
  capacity_ratio: 2.25,
  trend_minutes_per_hour: 6.2,
  seconds_to_failure: null,
}

export const emergencyFixture: EmergencyState = {
  tier: 'scheduled',
  tier_label: 'NORMAL',
  is_degraded: false,
  reserve_minutes: 30,
  reserve_tracks_remaining: 10,
  tier2_activations: 0,
  tier3_activations: 1,
  seconds_in_tier2: 0,
  seconds_in_tier3: 60,
  recoveries: 1,
  last_reason: null,
}

export const generationFixture: GenerationHealth = {
  provider: 'mock',
  capacity_ratio: 2.25,
  latency_p50_seconds: 20,
  latency_p95_seconds: 21,
  samples: 40,
  completed: 173,
  failed: 0,
  retries: 0,
  timeouts: 0,
  cancelled: 0,
  in_flight: 1,
  pending_leases: 1,
  mean_audio_seconds: 45,
}

export const healthFixture: HealthComponent[] = [
  {
    name: 'playout',
    label: 'Playout',
    state: 'healthy',
    detail: 'On air, 160 tracks completed, no dead air.',
    metric: '120 min on air',
    checked_at: null,
  },
  {
    name: 'generator',
    label: 'Generator',
    state: 'degraded',
    detail: '2 failed and 1 timed out; buffer remains safe at 47 min.',
    metric: '2.25x capacity',
    checked_at: null,
  },
  {
    name: 'market_feed',
    label: 'Market Feed',
    state: 'healthy',
    detail: 'live feed, 1240 bars processed.',
    metric: '1s old',
    checked_at: null,
  },
]

export const statusFixture: StationStatus = {
  is_broadcasting: true,
  playout_state: 'playing',
  station_name: 'TRADE FIX RADIO',
  tagline: 'THE MARKET COMPOSES THE RADIO',
  version: '0.1.0',
  environment: 'development',
  mode: 'development',
  test_mode: false,
  started_at: '2026-10-02T10:00:00Z',
  uptime_seconds: 7_200,
  tracks_played: 160,
  tracks_generated: 173,
  seconds_on_air: 7_200,
  unintended_silence_seconds: 0,
  underruns: 0,
  audio_coverage: 1,
}

/**
 * Routing with gold on air and Bitcoin healthy in reserve — the ordinary weekday state.
 *
 * `feed_degraded` is false on both. The fixture for the interesting case (a silent feed on
 * a trading day) is built per-test with an override, because the thing worth asserting
 * there is that it renders differently from a closure.
 */
export const goldAvailabilityFixture: MarketAvailability = {
  symbol: 'XAUUSD',
  state: 'open',
  reason: 'trading; last tick 1s ago',
  feed_degraded: false,
  data_age_seconds: 1,
  feed_status: 'simulated',
  calendar_open: true,
  is_active: true,
  bars_processed: 240,
  last_price: 4012.5,
  assessed_at: '2026-10-02T12:00:00Z',
}

export const bitcoinAvailabilityFixture: MarketAvailability = {
  symbol: 'BTCUSD',
  state: 'open',
  reason: 'trading; last tick 1s ago',
  feed_degraded: false,
  data_age_seconds: 1,
  feed_status: 'simulated',
  calendar_open: null,
  is_active: false,
  bars_processed: 240,
  last_price: 103_400,
  assessed_at: '2026-10-02T12:00:00Z',
}

export const routingFixture: ActiveMarket = {
  active_symbol: 'XAUUSD',
  primary_symbol: 'XAUUSD',
  is_primary: true,
  has_active_market: true,
  active_since: '2026-10-02T10:00:00Z',
  switch_reason: 'initial_selection',
  switch_count: 0,
  pending_symbol: null,
  pending_seconds_remaining: null,
  symbols: [goldAvailabilityFixture, bitcoinAvailabilityFixture],
}

export function liveStateFixture(overrides: Partial<LiveState> = {}): LiveState {
  return {
    status: statusFixture,
    market: marketFixture,
    routing: routingFixture,
    now_playing: nowPlayingFixture,
    queue: queueFixture(),
    buffer: bufferFixture,
    emergency: emergencyFixture,
    generation: generationFixture,
    health: healthFixture,
    alerts: [],
    capabilities: [
      { capability: 'playout', state: 'ready', detail: 'The playout engine is running.', arrives_in_phase: null },
      { capability: 'simulation', state: 'ready', detail: 'Scenario controls are enabled.', arrives_in_phase: null },
      { capability: 'originality', state: 'planned', detail: 'Arrives with Phase 6.', arrives_in_phase: 6 },
      { capability: 'obs', state: 'planned', detail: 'Arrives with Phase 8.', arrives_in_phase: 8 },
      { capability: 'gpu', state: 'unavailable', detail: 'No NVIDIA GPU is visible.', arrives_in_phase: null },
    ],
    at: '2026-10-02T12:00:00Z',
    ...overrides,
  }
}
