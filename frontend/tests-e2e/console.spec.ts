/**
 * End-to-end: the Control Center against a running station.
 *
 * These are the Phase 5 acceptance gates expressed as tests. Nothing here is mocked — the
 * station is broadcasting, the market simulator is running, and the assertions are about
 * what the browser actually shows.
 *
 * Two things these deliberately do **not** assert: exact numbers, and exact text from the
 * director. The station is live, so the buffer is whatever it is and the track is whatever
 * the director chose. Asserting on those would produce a suite that fails for being right.
 * What is asserted is *shape and honesty* — that a figure is present and plausible, that an
 * unbuilt subsystem says so, that a disconnect does not blank the screen.
 */

import { expect, test, type Page } from '@playwright/test'

/** Waits for the socket's first frame, which is what replaces every loading state. */
async function waitForLiveState(page: Page) {
  await expect(page.getByTestId('connection-indicator').first()).toContainText('CONNECTED', {
    timeout: 30_000,
  })
  await expect(page.getByTestId('dashboard')).toBeVisible()
}

test.describe('dashboard', () => {
  test('loads and reports real runtime state', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    // Gate A: the dashboard shows real runtime state.
    await expect(page.getByTestId('now-playing')).toBeVisible()
    await expect(page.getByTestId('market-panel')).toBeVisible()
    await expect(page.getByTestId('buffer-panel')).toBeVisible()
    await expect(page.getByTestId('health-panel')).toBeVisible()
    await expect(page.getByTestId('queue-panel')).toBeVisible()

    // Gate B: Now Playing comes from the engine, so it names a real track.
    const title = page.getByTestId('now-playing').getByRole('heading', { level: 3 })
    await expect(title).not.toBeEmpty()

    // Gate G: health indicators come from the backend, with explanations.
    const playout = page.getByTestId('health-playout')
    await expect(playout).toBeVisible()
    await expect(playout).toContainText(/healthy|degraded|critical|recovering|offline/i)
  })

  test('the elapsed time advances from the position stream', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    const elapsed = page.getByTestId('elapsed')
    const first = await elapsed.textContent()
    // The position cadence is twice a second; three seconds is comfortably enough.
    await page.waitForTimeout(3_000)
    const second = await elapsed.textContent()
    expect(second).not.toEqual(first)
  })

  test('the buffer reports minutes, a level and a trend', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    const minutes = page.getByTestId('buffer-minutes')
    await expect(minutes).toBeVisible()
    expect(Number(await minutes.textContent())).not.toBeNaN()
    await expect(page.getByTestId('buffer-trend')).toContainText(/min\/hour|holding/)
  })

  test('the emergency tier is always visible', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)
    await expect(page.getByTestId('emergency-banner')).toContainText(
      /NORMAL|TIER 2 RESERVE|TIER 3 PROCEDURAL/,
    )
  })

  test('the why-this-track panel shows the director’s recorded factors', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    const toggle = page.getByTestId('why-toggle')
    // Emergency audio has no blueprint and therefore no panel; that is a valid state.
    if (await toggle.isVisible()) {
      await toggle.click()
      await expect(page.getByTestId('why-panel')).toContainText(
        /stored factors, not a generated explanation/i,
      )
    }
  })
})

test.describe('queue', () => {
  test('renders real lock levels', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    const rows = page.getByTestId('queue-row')
    await expect(rows.first()).toBeVisible({ timeout: 30_000 })

    // Gate C: the UI shows the §28 locks the runtime actually computed.
    const labels = await page.getByTestId('queue-row').locator('.chip').allTextContents()
    expect(labels.some((label) => /HARD|SOFT|FLEXIBLE|PINNED/.test(label))).toBe(true)
  })

  test('the head of the queue is protected from pinning', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)
    const firstRow = page.getByTestId('queue-row').first()
    await expect(firstRow).toBeVisible({ timeout: 30_000 })
    // Position-derived protection is shown, not discovered by a rejected action.
    await expect(firstRow.getByRole('button', { name: /pin/i })).toBeDisabled()
  })
})

test.describe('navigation', () => {
  const pages: { link: string; testId: string }[] = [
    { link: 'Market', testId: 'market-page' },
    { link: 'Radio', testId: 'radio-page' },
    { link: 'Generation', testId: 'generation-page' },
    { link: 'Library', testId: 'library-page' },
    { link: 'Originality', testId: 'originality-page' },
    { link: 'Analytics', testId: 'analytics-page' },
    { link: 'OBS', testId: 'obs-page' },
    { link: 'System', testId: 'system-page' },
    { link: 'Settings', testId: 'settings-page' },
  ]

  for (const entry of pages) {
    test(`navigates to ${entry.link}`, async ({ page }) => {
      await page.goto('/')
      await waitForLiveState(page)
      await page.getByRole('link', { name: entry.link, exact: true }).click()
      await expect(page.getByTestId(entry.testId)).toBeVisible()
    })
  }
})

test.describe('honesty', () => {
  test('unbuilt subsystems say which phase delivers them', async ({ page }) => {
    await page.goto('/originality')
    await expect(page.getByTestId('originality-page')).toBeVisible()
    await expect(page.getByText('Awaiting Phase 6')).toBeVisible()
    await expect(page.getByText(/nothing here is simulated/i)).toBeVisible()

    await page.goto('/obs')
    await expect(page.getByText('Awaiting Phase 8')).toBeVisible()
    // Gate H: no fake green connection. Asserted as "every source says not connected"
    // rather than "the word connected is absent", which the phrase *not connected*
    // trivially fails.
    await expect(page.getByTestId('obs-page')).toContainText('not connected')
    await expect(page.getByTestId('obs-page').getByText(/^connected$/i)).toHaveCount(0)
  })

  test('a simulated feed is marked everywhere', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)
    // §72: persistent, in the chrome, on every page.
    await expect(page.getByText('Simulation mode').first()).toBeVisible()
  })

  test('an absent GPU reports absence rather than zeroes', async ({ page }) => {
    await page.goto('/system')
    await expect(page.getByTestId('system-page')).toBeVisible()
    const gpu = page.getByTestId('gpu-panel')
    await expect(gpu).toContainText(/No GPU visible|Device/)
  })
})

test.describe('simulation controls', () => {
  test('a scenario change flows through the backend', async ({ page }) => {
    // Gate D: the operator's button reaches the real simulator.
    await page.goto('/market')
    await expect(page.getByTestId('market-page')).toBeVisible()

    const panel = page.getByTestId('simulation-panel')
    await expect(panel).toBeVisible()

    const button = page.getByTestId('scenario-flat')
    await expect(button).toBeVisible({ timeout: 20_000 })
    await button.click()

    await expect(panel.getByRole('status')).toContainText(/now running the 'flat' scenario/i)
  })
})

test.describe('connection', () => {
  test('a dropped socket keeps the last state and marks it stale', async ({ page }) => {
    // The socket is closed from inside the page rather than by cutting the network.
    //
    // Both `context.setOffline` and CDP's `emulateNetworkConditions` leave an
    // already-established loopback WebSocket connected, so the first two versions of this
    // test asserted against a socket that had never dropped and measured nothing. Recording
    // the instances and closing one drives the real `onclose` → backoff → reconnect path,
    // which is the behaviour under test.
    await page.addInitScript(() => {
      const Native = window.WebSocket
      const sockets: WebSocket[] = []
      ;(window as unknown as { __sockets: WebSocket[] }).__sockets = sockets
      class Recording extends Native {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols)
          sockets.push(this as unknown as WebSocket)
        }
      }
      window.WebSocket = Recording as unknown as typeof WebSocket
    })

    await page.goto('/')
    await waitForLiveState(page)

    const title = await page
      .getByTestId('now-playing')
      .getByRole('heading', { level: 3 })
      .textContent()

    // Gate E: drop the socket and confirm the interface does not blank.
    await page.evaluate(() => {
      const sockets = (window as unknown as { __sockets: WebSocket[] }).__sockets
      sockets.at(-1)?.close()
    })

    await expect(page.getByTestId('connection-indicator').first()).toContainText(
      /RECONNECTING|OFFLINE/,
      { timeout: 15_000 },
    )
    // The last known values stay on screen — the whole point of the requirement.
    await expect(page.getByTestId('now-playing').getByRole('heading', { level: 3 })).toHaveText(
      title ?? '',
    )
    await expect(page.getByTestId('dashboard')).toBeVisible()
    await expect(page.getByTestId('queue-panel')).toBeVisible()

    // And it recovers on its own, without a reload.
    await expect(page.getByTestId('connection-indicator').first()).toContainText('CONNECTED', {
      timeout: 60_000,
    })
  })
})

test.describe('overlay', () => {
  test('renders the broadcast surface without console chrome', async ({ page }) => {
    await page.goto('/overlay/live')
    await expect(page.getByTestId('overlay')).toBeVisible({ timeout: 30_000 })

    await expect(page.getByText('Trade Fix Radio')).toBeVisible()
    await expect(page.getByText('The market composes the radio')).toBeVisible()
    // A viewer must never see operator chrome.
    await expect(page.getByRole('navigation')).toHaveCount(0)
    await expect(page.getByTestId('connection-indicator')).toHaveCount(0)
  })

  test('supports its layout modes', async ({ page }) => {
    for (const mode of ['standard', 'market', 'track', 'minimal']) {
      await page.goto(`/overlay/live?mode=${mode}`)
      const overlay = page.getByTestId('overlay')
      await expect(overlay).toBeVisible({ timeout: 30_000 })
      await expect(overlay).toHaveAttribute('data-mode', mode)
    }
  })
})

test.describe('accessibility', () => {
  test('is keyboard navigable with visible focus', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)

    await page.keyboard.press('Tab')
    const focused = await page.evaluate(() => {
      const element = document.activeElement
      return element ? element.tagName : null
    })
    expect(focused).not.toBeNull()
    expect(['A', 'BUTTON', 'INPUT']).toContain(focused)
  })

  test('status is carried by text, not colour alone', async ({ page }) => {
    await page.goto('/')
    await waitForLiveState(page)
    const health = page.getByTestId('health-playout')
    await expect(health).toContainText(/HEALTHY|DEGRADED|CRITICAL|RECOVERING|OFFLINE/)
  })
})

test.describe('layout', () => {
  test('does not scroll horizontally at any target width', async ({ page }) => {
    for (const path of ['/', '/market', '/radio', '/generation', '/library', '/system']) {
      await page.goto(path)
      await page.waitForTimeout(800)
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow, `${path} overflows horizontally by ${overflow}px`).toBeLessThanOrEqual(1)
    }
  })
})
