/**
 * The live-state store: one WebSocket, two cadences, and an honest offline story.
 *
 * Three requirements from the brief shape this file, and they pull against each other:
 *
 * 1. *"WebSocket disconnection should not blank the whole interface. Retain the last known
 *    values and clearly mark them stale."*
 * 2. *"Do not rerender the whole dashboard on every audio-position event."*
 * 3. *"Differentiate server state and UI state. Avoid giant global stores."*
 *
 * The resolution is two contexts over one socket. `LiveStateContext` holds the structural
 * frame and changes every couple of seconds; `PositionContext` holds only audio progress and
 * changes twice a second. A component subscribes to whichever it needs, so the progress bar
 * re-renders four times a second and the queue table does not re-render at all.
 *
 * Staleness is a first-class value rather than an absence. On disconnect the last frame is
 * *kept* and flagged — an operator looking away for thirty seconds should come back to the
 * station they left with a clear "reconnecting" marker, not to a blank page that looks like
 * the station died.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import type { ConnectionState, LiveEnvelope, LiveState, PositionUpdate } from './types'

/** Reconnection backoff, in milliseconds. Capped so a long outage still recovers promptly. */
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000] as const

/**
 * How long a frame may go unrefreshed before the UI calls it stale, in milliseconds.
 *
 * Three state pushes. Long enough that one dropped frame is not an alarm, short enough that
 * an operator is never looking at a minute-old buffer reading believing it is current.
 */
const STALE_AFTER_MS = 7000

interface LiveContextValue {
  state: LiveState | null
  connection: ConnectionState
  /** True when the socket is down and the values on screen are the last known ones. */
  isStale: boolean
  /** Milliseconds since the last frame, or null before the first one. */
  ageMs: number | null
  /** Forces an immediate reconnect attempt, for the connection indicator's retry. */
  reconnect: () => void
}

const LiveStateContext = createContext<LiveContextValue | null>(null)
const PositionContext = createContext<PositionUpdate | null>(null)

export function useLive(): LiveContextValue {
  const value = useContext(LiveStateContext)
  if (!value) throw new Error('useLive must be used inside <LiveProvider>')
  return value
}

/**
 * Audio progress, isolated.
 *
 * Deliberately a separate context so that subscribing to the progress bar does not subscribe
 * to the station. A component calling this re-renders twice a second; one calling `useLive`
 * re-renders every two seconds. Mixing them would make the second rate win for everything.
 */
export function usePosition(): PositionUpdate | null {
  return useContext(PositionContext)
}

function socketUrl(): string {
  if (typeof window === 'undefined') return ''
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}

export function LiveProvider({
  children,
  url,
}: {
  children: ReactNode
  /** Overridable for tests, which drive a fake socket rather than a real server. */
  url?: string
}) {
  const [state, setState] = useState<LiveState | null>(null)
  const [position, setPosition] = useState<PositionUpdate | null>(null)
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closedByUs = useRef(false)

  const connect = useCallback(() => {
    if (typeof WebSocket === 'undefined') return
    const target = url ?? socketUrl()
    if (!target) return

    closedByUs.current = false
    // Once we have given up, stay given up until a connection actually succeeds. Without
    // this, every retry flipped the indicator back to "reconnecting" and an operator looking
    // at a station that had been unreachable for ten minutes saw a hopeful amber label.
    setConnection((current) =>
      current === 'offline'
        ? 'offline'
        : attemptRef.current === 0
          ? 'connecting'
          : 'reconnecting',
    )

    let socket: WebSocket
    try {
      socket = new WebSocket(target)
    } catch {
      scheduleReconnect()
      return
    }
    socketRef.current = socket

    socket.onopen = () => {
      attemptRef.current = 0
      setConnection('connected')
    }

    socket.onmessage = (event) => {
      let envelope: LiveEnvelope
      try {
        envelope = JSON.parse(event.data as string) as LiveEnvelope
      } catch {
        // A malformed frame is the server's problem and must not kill the socket: dropping
        // one message leaves the UI showing the previous state, which is correct.
        return
      }
      if (envelope.type === 'state') {
        setState(envelope.payload)
        setReceivedAt(Date.now())
        setNow(Date.now())
      } else if (envelope.type === 'position') {
        setPosition(envelope.payload)
      }
    }

    socket.onerror = () => {
      // Errors always precede a close; the close handler owns reconnection so this does not
      // schedule a second attempt.
    }

    socket.onclose = () => {
      socketRef.current = null
      if (closedByUs.current) return
      setConnection((current) => (current === 'offline' ? 'offline' : 'reconnecting'))
      scheduleReconnect()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url])

  const scheduleReconnect = useCallback(() => {
    const index = Math.min(attemptRef.current, BACKOFF_MS.length - 1)
    const delay = BACKOFF_MS[index] ?? 15000
    attemptRef.current += 1
    if (attemptRef.current > 3) setConnection('offline')
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => connect(), delay)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect])

  const reconnect = useCallback(() => {
    attemptRef.current = 0
    if (timerRef.current) clearTimeout(timerRef.current)
    socketRef.current?.close()
    connect()
  }, [connect])

  useEffect(() => {
    connect()
    return () => {
      closedByUs.current = true
      if (timerRef.current) clearTimeout(timerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect])

  // A slow tick purely so "last update 42s ago" counts up while the socket is down. One
  // second is the coarsest interval that still reads as live.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const ageMs = receivedAt === null ? null : Math.max(0, now - receivedAt)
  const isStale =
    connection !== 'connected' || (ageMs !== null && ageMs > STALE_AFTER_MS)

  const live = useMemo<LiveContextValue>(
    () => ({ state, connection, isStale, ageMs, reconnect }),
    [state, connection, isStale, ageMs, reconnect],
  )

  return (
    <LiveStateContext.Provider value={live}>
      <PositionContext.Provider value={position}>{children}</PositionContext.Provider>
    </LiveStateContext.Provider>
  )
}

/** Test seam: render children against a fixed frame, with no socket involved. */
export function StaticLiveProvider({
  children,
  value,
  position = null,
}: {
  children: ReactNode
  value: Partial<LiveContextValue> & { state: LiveState | null }
  position?: PositionUpdate | null
}) {
  const live: LiveContextValue = {
    connection: 'connected',
    isStale: false,
    ageMs: 0,
    reconnect: () => {},
    ...value,
  }
  return (
    <LiveStateContext.Provider value={live}>
      <PositionContext.Provider value={position}>{children}</PositionContext.Provider>
    </LiveStateContext.Provider>
  )
}
