import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright drives the **real** Control Center against the **real** station.
 *
 * No mocked API. The whole point of these tests is that the UI reports a running station
 * faithfully, and a fixture server would make them a test of the fixture. `tradefix dev`
 * starts the station, the API and the built frontend in one process, which is exactly what
 * an operator runs.
 *
 * The three viewports are the ones the brief names. They are declared as projects rather
 * than resized mid-test so a failure says which resolution broke.
 */
export default defineConfig({
  testDir: './tests-e2e',
  outputDir: './test-results',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:8000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    colorScheme: 'dark',
  },
  projects: [
    { name: '1920x1080', use: { ...devices['Desktop Chrome'], viewport: { width: 1920, height: 1080 } } },
    { name: '1440x900', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: '1366x768', use: { ...devices['Desktop Chrome'], viewport: { width: 1366, height: 768 } } },
  ],
  webServer: {
    // Started from the repository root so the station finds its configuration and database.
    command: 'python -m tradefix_radio.cli.main dev --port 8000 --scenario violent_breakout',
    cwd: '..',
    url: 'http://127.0.0.1:8000/api/health',
    reuseExistingServer: true,
    // Generous: the station warms the market feed to a classified regime before serving.
    timeout: 180_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
