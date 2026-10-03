/**
 * Screenshot capture for the design review.
 *
 * Not assertions — these produce the artefacts a human (and the Phase 5 report) looks at.
 * The brief is explicit that the UI must not be declared complete because tests pass, so
 * this exists to make the visual review possible rather than to replace it.
 *
 * Two mechanical checks run alongside the capture, because they are the failures a reviewer
 * is worst at spotting and a browser is best at: horizontal overflow, and text clipped by
 * its own container.
 */

import { expect, test, type Page } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const SHOT_DIR = 'screenshots'

const PAGES: { path: string; name: string; settle?: number }[] = [
  { path: '/', name: 'dashboard', settle: 2500 },
  { path: '/market', name: 'market', settle: 2000 },
  { path: '/radio', name: 'radio', settle: 1500 },
  { path: '/generation', name: 'generation', settle: 1500 },
  { path: '/library', name: 'library', settle: 1500 },
  { path: '/originality', name: 'originality', settle: 800 },
  { path: '/analytics', name: 'analytics', settle: 2000 },
  { path: '/obs', name: 'obs', settle: 800 },
  { path: '/system', name: 'system', settle: 1500 },
  { path: '/settings', name: 'settings', settle: 800 },
]

/** Elements whose content is wider or taller than the box drawn for it. */
async function clippedElements(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const offenders: string[] = []
    for (const element of Array.from(document.querySelectorAll('*'))) {
      const style = getComputedStyle(element)
      if (style.overflow === 'auto' || style.overflow === 'scroll') continue
      if (style.overflowX === 'auto' || style.overflowX === 'scroll') continue
      // `truncate` is a deliberate ellipsis, not a clip.
      if (element.classList.contains('truncate')) continue
      // `sr-only` is clipped to one pixel on purpose: it exists for screen readers and is
      // supposed to be invisible. Flagging it would make the check cry wolf on every page
      // with an accessible table header.
      if (element.classList.contains('sr-only')) continue
      // SVG elements report scroll geometry that does not mean what it does for HTML —
      // Recharts' axis labels register a few pixels of phantom overflow on every chart.
      if (element.namespaceURI !== 'http://www.w3.org/1999/xhtml') continue
      const horizontal = element.scrollWidth - element.clientWidth
      if (horizontal > 2 && element.clientWidth > 0) {
        offenders.push(
          `${element.tagName.toLowerCase()}.${Array.from(element.classList).slice(0, 2).join('.')}` +
            ` overflows by ${horizontal}px`,
        )
      }
    }
    return offenders.slice(0, 10)
  })
}

test.describe('screenshots', () => {
  for (const entry of PAGES) {
    test(`captures ${entry.name}`, async ({ page }, testInfo) => {
      const viewport = testInfo.project.name
      mkdirSync(join(SHOT_DIR, viewport), { recursive: true })

      await page.goto(entry.path)
      await page.waitForTimeout(entry.settle ?? 1000)
      await page.screenshot({
        path: join(SHOT_DIR, viewport, `${entry.name}.png`),
        fullPage: false,
      })

      // Mechanical checks a reviewer's eye is unreliable at.
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow, `${entry.name} scrolls horizontally by ${overflow}px`).toBeLessThanOrEqual(1)

      const clipped = await clippedElements(page)
      expect(clipped, `${entry.name} has clipped content: ${clipped.join('; ')}`).toEqual([])
    })
  }

  test('captures the overlay in every layout', async ({ page }, testInfo) => {
    // The overlay is designed at 1920×1080 and only reviewed there; capturing it at the
    // smaller console widths would produce artefacts of a size it never runs at.
    test.skip(testInfo.project.name !== '1920x1080', 'the overlay targets 1920×1080')

    mkdirSync(join(SHOT_DIR, 'overlay'), { recursive: true })
    for (const mode of ['standard', 'market', 'track', 'minimal']) {
      await page.goto(`/overlay/live?mode=${mode}`)
      await expect(page.getByTestId('overlay')).toBeVisible()
      await page.waitForTimeout(1500)
      await page.screenshot({ path: join(SHOT_DIR, 'overlay', `${mode}.png`) })

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow, `overlay/${mode} scrolls horizontally`).toBeLessThanOrEqual(1)
    }
  })
})
