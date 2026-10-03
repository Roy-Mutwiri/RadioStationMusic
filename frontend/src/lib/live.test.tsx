/**
 * Live-state store: the socket, the two cadences, and the offline story.
 *
 * Driven against a fake WebSocket rather than a real server. The behaviour under test is
 * entirely about *what the UI does when the socket misbehaves*, and a real server is both
 * harder to make misbehave on cue and slower.
 *
 * The property that matters most: a disconnect must not blank the interface. The brief is
 * explicit, and it is the difference between an operator glancing at a screen and trusting
 * it versus wondering whether the station died.
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LiveProvider, useLive, usePosition } from './live'
import { liveStateFixture } from '../test/fixtures'

/** A WebSocket the test drives directly. */
class FakeSocket {
  static instances: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  readonly url: string
  closed = false

  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  close() {
    this.closed = true
    this.onclose?.()
  }

  /** Simulate the server pushing a frame. */
  push(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
}

function Probe() {
  const { state, connection, isStale } = useLive()
  const position = usePosition()
  return (
    <div>
      <span data-testid="connection">{connection}</span>
      <span data-testid="stale">{isStale ? 'stale' : 'fresh'}</span>
      <span data-testid="title">{state?.now_playing?.title ?? 'none'}</span>
      <span data-testid="elapsed">{position ? Math.round(position.elapsed_seconds) : 'none'}</span>
      <span data-testid="renders" />
    </div>
  )
}

beforeEach(() => {
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket as never)
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function latest(): FakeSocket {
  const socket = FakeSocket.instances.at(-1)
  if (!socket) throw new Error('no socket was opened')
  return socket
}

describe('live provider', () => {
  it('opens a socket and reports connecting until it is open', () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    expect(screen.getByTestId('connection')).toHaveTextContent('connecting')
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('applies a state frame', async () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().push({ type: 'state', at: '2026-10-02T12:00:00Z', payload: liveStateFixture() })
    })
    expect(screen.getByTestId('connection')).toHaveTextContent('connected')
    expect(screen.getByTestId('title')).toHaveTextContent('Liquidity After Midnight')
  })

  it('applies position updates separately from the state frame', () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().push({
        type: 'position',
        at: '2026-10-02T12:00:01Z',
        payload: {
          track_id: 'TF-20261002-00042',
          elapsed_seconds: 91.4,
          remaining_seconds: 123.6,
          duration_seconds: 215,
          progress: 0.425,
          output_peak: 0.6,
        },
      })
    })
    expect(screen.getByTestId('elapsed')).toHaveTextContent('91')
    // A position message alone must not have to carry the station.
    expect(screen.getByTestId('title')).toHaveTextContent('none')
  })

  it('keeps the last known state when the socket drops', async () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().push({ type: 'state', at: '2026-10-02T12:00:00Z', payload: liveStateFixture() })
    })
    act(() => {
      latest().onclose?.()
    })

    // The brief's requirement, stated as an assertion: the interface does not blank.
    expect(screen.getByTestId('title')).toHaveTextContent('Liquidity After Midnight')
    expect(screen.getByTestId('connection')).toHaveTextContent('reconnecting')
    expect(screen.getByTestId('stale')).toHaveTextContent('stale')
  })

  it('reconnects on a backoff and recovers', async () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().onclose?.()
    })
    expect(FakeSocket.instances).toHaveLength(1)

    await act(async () => {
      vi.advanceTimersByTime(600)
    })
    expect(FakeSocket.instances.length).toBeGreaterThan(1)

    act(() => {
      latest().onopen?.()
      latest().push({ type: 'state', at: '2026-10-02T12:00:05Z', payload: liveStateFixture() })
    })
    await waitFor(() => {
      expect(screen.getByTestId('connection')).toHaveTextContent('connected')
      expect(screen.getByTestId('stale')).toHaveTextContent('fresh')
    })
  })

  it('declares itself offline after repeated failures', async () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    for (let attempt = 0; attempt < 5; attempt += 1) {
      act(() => {
        latest().onclose?.()
      })
      await act(async () => {
        vi.advanceTimersByTime(20_000)
      })
    }
    expect(screen.getByTestId('connection')).toHaveTextContent('offline')
  })

  it('survives a malformed frame without losing the socket', () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().push({ type: 'state', at: '2026-10-02T12:00:00Z', payload: liveStateFixture() })
      latest().onmessage?.({ data: 'not json at all' })
    })
    // One bad message drops one message, and nothing else.
    expect(screen.getByTestId('connection')).toHaveTextContent('connected')
    expect(screen.getByTestId('title')).toHaveTextContent('Liquidity After Midnight')
  })

  it('marks data stale when frames stop arriving even while the socket is open', async () => {
    render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    act(() => {
      latest().onopen?.()
      latest().push({ type: 'state', at: '2026-10-02T12:00:00Z', payload: liveStateFixture() })
    })
    expect(screen.getByTestId('stale')).toHaveTextContent('fresh')

    // A socket that is open but silent is the subtle failure: TCP is fine, the producer is
    // not. Age is measured from the last frame rather than from the connection.
    await act(async () => {
      vi.advanceTimersByTime(9_000)
    })
    expect(screen.getByTestId('stale')).toHaveTextContent('stale')
  })

  it('closes the socket on unmount', () => {
    const { unmount } = render(
      <LiveProvider url="ws://test/ws">
        <Probe />
      </LiveProvider>,
    )
    const socket = latest()
    unmount()
    expect(socket.closed).toBe(true)
  })
})
