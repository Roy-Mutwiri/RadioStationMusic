import '@testing-library/jest-dom/vitest'

// jsdom has no ResizeObserver, and Recharts' ResponsiveContainer needs one. A stub is
// enough: chart tests assert on the data and the surrounding chrome, not on pixel layout.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as never

// Nor does it have matchMedia, which Recharts touches during layout.
globalThis.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as never
