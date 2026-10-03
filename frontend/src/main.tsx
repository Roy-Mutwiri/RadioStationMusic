/**
 * Application entry.
 *
 * Two route trees under one provider. The console lives inside `Shell`; the overlay is
 * deliberately outside it — it shares the live socket and nothing else, because a viewer must
 * never see a scrollbar, a sidebar or a connection indicator.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { Shell } from './components/Shell'
import { LiveProvider } from './lib/live'
import { DashboardPage } from './pages/Dashboard'

// The dashboard is eager because it is the landing page; everything else loads on demand,
// which keeps the overlay's bundle down to what a stream machine actually needs.
const OverlayPage = lazy(() =>
  import('./pages/Overlay').then((module) => ({ default: module.OverlayPage })),
)
const OverlayPreviewPage = lazy(() =>
  import('./pages/Overlay').then((module) => ({ default: module.OverlayPreviewPage })),
)
const pages = () => import('./pages/pages')
const MarketPage = lazy(() => pages().then((m) => ({ default: m.MarketPage })))
const RadioPage = lazy(() => pages().then((m) => ({ default: m.RadioPage })))
const GenerationPage = lazy(() => pages().then((m) => ({ default: m.GenerationPage })))
const LibraryPage = lazy(() => pages().then((m) => ({ default: m.LibraryPage })))
const OriginalityPage = lazy(() => pages().then((m) => ({ default: m.OriginalityPage })))
const AnalyticsPage = lazy(() => pages().then((m) => ({ default: m.AnalyticsPage })))
const ObsPage = lazy(() => pages().then((m) => ({ default: m.ObsPage })))
const SystemPage = lazy(() => pages().then((m) => ({ default: m.SystemPage })))
const SettingsPage = lazy(() => pages().then((m) => ({ default: m.SettingsPage })))
const NotFoundPage = lazy(() => pages().then((m) => ({ default: m.NotFoundPage })))

import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Live state arrives over the socket; queries here are for the windowed and paginated
      // reads that genuinely need a request. Retrying them hard would hammer an API that is
      // already telling us it is in trouble.
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LiveProvider>
        <BrowserRouter>
          <Suspense
            fallback={
              <div className="flex h-screen items-center justify-center">
                <span className="label">Loading</span>
              </div>
            }
          >
            <Routes>
            <Route path="/overlay/live" element={<OverlayPage />} />
            <Route element={<Shell />}>
              <Route index element={<DashboardPage />} />
              <Route path="market" element={<MarketPage />} />
              <Route path="radio" element={<RadioPage />} />
              <Route path="generation" element={<GenerationPage />} />
              <Route path="library" element={<LibraryPage />} />
              <Route path="originality" element={<OriginalityPage />} />
              <Route path="analytics" element={<AnalyticsPage />} />
              <Route path="obs" element={<ObsPage />} />
              <Route path="system" element={<SystemPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="overlay" element={<OverlayPreviewPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </LiveProvider>
    </QueryClientProvider>
  </StrictMode>,
)
