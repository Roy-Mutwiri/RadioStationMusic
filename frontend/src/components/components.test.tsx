/**
 * Component tests.
 *
 * Weighted towards the things that would be *dishonest* if they broke, rather than towards
 * coverage. A queue table that renders the wrong title is a bug; a buffer panel that renders
 * "0 min" when the backend sent `null` is a lie, and those get the most attention here.
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { AlertRail, GenerationStrip, HealthPanel } from './HealthPanel'
import { MarketPanel } from './MarketPanel'
import { NowPlayingPanel } from './NowPlaying'
import { BufferPanel, EmergencyBanner, QueuePanel } from './QueuePanel'
import { AwaitingPhase, Metric, StatusChip } from './primitives'
import {
  bufferFixture,
  emergencyFixture,
  generationFixture,
  healthFixture,
  marketFixture,
  nowPlayingFixture,
  queueFixture,
} from '../test/fixtures'

describe('status primitives', () => {
  it('never encodes health in colour alone', () => {
    // Accessibility requirement, and also what makes a greyscale screenshot readable.
    render(<StatusChip state="critical" />)
    expect(screen.getByText('CRITICAL')).toBeInTheDocument()
  })

  it('renders an absent metric as absent rather than as zero', () => {
    render(<Metric label="GPU temperature" value={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })

  it('names the phase that delivers an unbuilt subsystem', () => {
    render(<AwaitingPhase subsystem="Originality engine" phase={6} detail="Arrives later." />)
    expect(screen.getByText('Awaiting Phase 6')).toBeInTheDocument()
    expect(screen.getByText(/nothing here is simulated/i)).toBeInTheDocument()
  })
})

describe('now playing', () => {
  it('shows the track, its musical identity and where it came from', () => {
    render(<NowPlayingPanel track={nowPlayingFixture} />)
    expect(screen.getByText('Liquidity After Midnight')).toBeInTheDocument()
    expect(screen.getByText('148 BPM')).toBeInTheDocument()
    expect(screen.getByText('F# minor')).toBeInTheDocument()
    expect(screen.getByText('Scheduled queue')).toBeInTheDocument()
  })

  it('reveals the director’s recorded reasoning on request', async () => {
    const user = userEvent.setup()
    render(<NowPlayingPanel track={nowPlayingFixture} />)

    expect(screen.queryByTestId('why-panel')).not.toBeInTheDocument()
    await user.click(screen.getByTestId('why-toggle'))

    const panel = screen.getByTestId('why-panel')
    expect(within(panel).getByText('genre uk_trap')).toBeInTheDocument()
    expect(within(panel).getByText(/stored factors, not a generated explanation/i)).toBeInTheDocument()
  })

  it('shows a real level meter when the sink reported a peak', () => {
    render(<NowPlayingPanel track={nowPlayingFixture} />)
    expect(screen.getByTestId('level-meter')).toBeInTheDocument()
  })

  it('refuses to animate a meter with no measurement behind it', () => {
    // The single most dishonest pixel available to a music dashboard is a decorative
    // waveform that keeps moving through silence.
    render(<NowPlayingPanel track={{ ...nowPlayingFixture, output_peak: null }} />)
    expect(screen.queryByTestId('level-meter')).not.toBeInTheDocument()
    expect(screen.getByTestId('level-meter-absent')).toBeInTheDocument()
  })

  it('explains that emergency audio has no blueprint', () => {
    render(
      <NowPlayingPanel
        track={{ ...nowPlayingFixture, tier: 'procedural', reason: null, origin: 'Procedural synthesis (Tier 3)' }}
      />,
    )
    expect(screen.getByText(/emergency cover, not programming/i)).toBeInTheDocument()
  })

  it('has an empty state rather than a blank panel', () => {
    render(<NowPlayingPanel track={null} />)
    expect(screen.getByText('Nothing on air')).toBeInTheDocument()
  })
})

describe('market panel', () => {
  it('shows the regime as the headline fact', () => {
    render(<MarketPanel market={marketFixture} />)
    expect(screen.getByTestId('regime-badge')).toHaveTextContent('Bullish Breakout')
    expect(screen.getByTestId('market-energy')).toHaveTextContent('84')
  })

  it('withholds a stale price and says why', () => {
    render(
      <MarketPanel
        market={{ ...marketFixture, price: null, price_change: null, is_stale: true, feed_status: 'stale' }}
      />,
    )
    expect(screen.queryByText('4,012.50')).not.toBeInTheDocument()
    expect(screen.getByText(/price withheld while stale/i)).toBeInTheDocument()
  })

  it('says a simulated feed has no real price rather than showing one', () => {
    render(<MarketPanel market={{ ...marketFixture, price: null, is_simulated: true }} />)
    expect(screen.getByText(/simulated feed/i)).toBeInTheDocument()
  })

  it('tells the operator the radio keeps broadcasting when the feed dies', () => {
    render(<MarketPanel market={null} />)
    expect(screen.getByText('Market feed offline')).toBeInTheDocument()
    expect(screen.getByText(/radio keeps broadcasting/i)).toBeInTheDocument()
  })
})

describe('queue', () => {
  it('renders every lock level with its §28 label', () => {
    render(<QueuePanel queue={queueFixture()} />)
    const rows = screen.getAllByTestId('queue-row')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]!).getByText('HARD')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('SOFT')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('FLEXIBLE')).toBeInTheDocument()
  })

  it('shows generation progress for a track still being made', () => {
    render(<QueuePanel queue={queueFixture()} />)
    expect(screen.getByText('GENERATING 42%')).toBeInTheDocument()
  })

  it('disables pinning for a slot the queue already protects', () => {
    // The constraint is visible before the operator tries it, rather than discovered by a
    // rejected action.
    render(<QueuePanel queue={queueFixture()} />)
    const rows = screen.getAllByTestId('queue-row')
    expect(within(rows[0]!).getByRole('button', { name: /pin/i })).toBeDisabled()
    expect(within(rows[2]!).getByRole('button', { name: /pin/i })).toBeEnabled()
  })

  it('tells the operator when nothing is queued and why that may be fine', () => {
    render(<QueuePanel queue={[]} />)
    expect(screen.getByText('Nothing queued')).toBeInTheDocument()
  })
})

describe('buffer', () => {
  it('leads with minutes, because minutes are what the station runs out of', () => {
    render(<BufferPanel buffer={bufferFixture} />)
    expect(screen.getByTestId('buffer-minutes')).toHaveTextContent('47')
    expect(screen.getByText('HEALTHY')).toBeInTheDocument()
    expect(screen.getByTestId('buffer-trend')).toHaveTextContent('+6.2 min/hour')
  })

  it('hides the failure countdown while the buffer is not shrinking', () => {
    render(<BufferPanel buffer={bufferFixture} />)
    expect(screen.queryByTestId('buffer-failure-warning')).not.toBeInTheDocument()
  })

  it('shows the failure countdown the moment it becomes real', () => {
    render(
      <BufferPanel
        buffer={{
          ...bufferFixture,
          level: 'low',
          trajectory: 'draining',
          ready_minutes: 12,
          trend_minutes_per_hour: -12.4,
          seconds_to_failure: 1860,
          capacity_ratio: 0.4,
        }}
      />,
    )
    const warning = screen.getByTestId('buffer-failure-warning')
    expect(warning).toHaveTextContent('Critical in ~31 min')
    expect(screen.getByTestId('buffer-trend')).toHaveTextContent('-12.4 min/hour')
  })
})

describe('emergency banner', () => {
  it('stays quiet when the scheduled queue is carrying the output', () => {
    render(<EmergencyBanner emergency={emergencyFixture} />)
    expect(screen.getByTestId('emergency-banner')).toHaveTextContent('NORMAL')
  })

  it('becomes unmissable on an emergency tier, and says it recovers itself', () => {
    render(
      <EmergencyBanner
        emergency={{
          ...emergencyFixture,
          tier: 'procedural',
          tier_label: 'TIER 3 PROCEDURAL',
          is_degraded: true,
          last_reason: 'scheduled queue and reserve are both unavailable',
        }}
      />,
    )
    const banner = screen.getByTestId('emergency-banner')
    expect(banner).toHaveTextContent('TIER 3 PROCEDURAL')
    expect(banner).toHaveTextContent(/returns automatically/i)
  })
})

describe('health', () => {
  it('gives every component a state and an explanation', () => {
    render(<HealthPanel components={healthFixture} />)
    const generator = screen.getByTestId('health-generator')
    expect(within(generator).getByText('DEGRADED')).toBeInTheDocument()
    // The brief's standard: the row says what is wrong *and* whether the broadcast is at risk.
    expect(within(generator).getByText(/buffer remains safe at 47 min/i)).toBeInTheDocument()
  })

  it('pairs every alert with something to do about it', () => {
    render(
      <AlertRail
        alerts={[
          {
            key: 'buffer',
            severity: 'critical',
            message: 'Buffer is critical at 1.2 minutes.',
            raised_at: '2026-10-02T12:00:00Z',
            remediation: 'Check the generator; experimentation is already withheld.',
          },
        ]}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Check the generator')
  })

  it('warns in words when capacity falls below playback rate', () => {
    render(<GenerationStrip generation={{ ...generationFixture, capacity_ratio: 0.6 }} />)
    expect(screen.getByText(/generating slower than playback/i)).toBeInTheDocument()
  })

  it('shows unmeasured latency as absent, not as zero seconds', () => {
    render(
      <GenerationStrip
        generation={{ ...generationFixture, latency_p50_seconds: null, latency_p95_seconds: null }}
      />,
    )
    const strip = screen.getByTestId('generation-strip')
    expect(within(strip).getAllByText('—')).toHaveLength(2)
  })
})
