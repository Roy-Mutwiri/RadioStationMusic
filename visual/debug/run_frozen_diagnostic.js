/**
 * Automated frozen diagnostic runner using Playwright.
 * Captures the full diagnostic report from window.FROZEN_DIAG_RESULTS.
 */
const { chromium } = require('playwright');

async function runDiagnostic() {
  console.log('[TEST] Launching browser for frozen diagnostic...\n');

  const browser = await chromium.launch({
    headless: true,
    args: [
      '--enable-webgl',
      '--use-gl=angle',
      '--enable-gpu',
      '--no-sandbox',
      '--disable-setuid-sandbox'
    ]
  });

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });

  const page = await context.newPage();

  // Collect console messages
  const consoleMessages = [];
  page.on('console', msg => {
    consoleMessages.push(`[${msg.type()}] ${msg.text()}`);
    if (msg.text().includes('[FROZEN_DIAG]') || msg.text().includes('[VISUAL]')) {
      console.log(msg.text());
    }
  });

  // Navigate to the world_marker test
  const url = 'http://localhost:8766/?render_test=world_marker&hud=0';
  console.log(`[TEST] Loading ${url}`);

  try {
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });

    // Wait for the frozen diagnostic to complete
    console.log('[TEST] Waiting for frozen diagnostic to complete...');

    // Poll for results
    let results = null;
    for (let i = 0; i < 60; i++) {
      results = await page.evaluate(() => window.FROZEN_DIAG_RESULTS);
      if (results) break;
      await page.waitForTimeout(500);
    }

    if (!results) {
      console.log('\n=== DIAGNOSTIC FAILED: No results captured ===\n');
      console.log('Console messages:');
      consoleMessages.forEach(m => console.log('  ' + m));
      await browser.close();
      process.exit(1);
    }

    // Generate the required report format
    console.log('\n' + '='.repeat(70));
    console.log('                  FROZEN DIAGNOSTIC REPORT');
    console.log('='.repeat(70) + '\n');

    console.log(`AUTHORITATIVE VISUAL SERVER: port=${results.server?.port || 8766}, PID=${results.server?.pid || 'unknown'}`);
    console.log(`BUILD ID: ${results.buildId || 'unknown'}`);
    console.log(`served_build_matches_disk: ${results.buildId === '2026-10-04-frozen-diagnostic' ? 'YES' : 'NO'}`);
    console.log(`\nnormal RAF active during diagnostic: ${results.rafStopped ? 'NO (stopped)' : 'YES (bug!)'}`);
    console.log(`clear_calls_after_marker: ${results.clearCallsAfterDraw || 0}`);

    console.log('\n--- MARKER BOUNDING BOXES ---');
    if (results.markers) {
      for (const [name, marker] of Object.entries(results.markers)) {
        const bbox = marker.bbox;
        const size = bbox ? `${bbox.width}x${bbox.height}` : 'NOT FOUND';
        const pass = bbox && bbox.width >= 100 && bbox.height >= 100 ? 'PASS' : 'FAIL';
        console.log(`${name} marker bbox: ${size} [${pass}]`);
        if (bbox) {
          console.log(`    position: (${bbox.x}, ${bbox.y}) to (${bbox.x + bbox.width}, ${bbox.y + bbox.height})`);
          console.log(`    color: RGB(${marker.color?.r || '?'}, ${marker.color?.g || '?'}, ${marker.color?.b || '?'})`);
        }
      }
    }

    console.log('\n--- VISIBILITY ---');
    console.log(`WebGL framebuffer has colored pixels: ${results.webglHasPixels ? 'YES' : 'NO'}`);
    console.log(`Total colored pixels: ${results.totalColoredPixels || 0}`);
    console.log(`Readback mirror created: ${results.mirrorCreated ? 'YES' : 'NO'}`);

    // Take screenshot
    const screenshotPath = 'D:\\.Music\\visual\\debug\\frozen_diagnostic_screenshot.png';
    await page.screenshot({ path: screenshotPath, fullPage: false });
    console.log(`\nScreenshot saved: ${screenshotPath}`);

    // Determine overall result
    const markersPass = results.markers &&
      Object.values(results.markers).every(m => m.bbox && m.bbox.width >= 100 && m.bbox.height >= 100);
    const clearPass = (results.clearCallsAfterDraw || 0) === 0;
    const pixelsPass = (results.totalColoredPixels || 0) > 10000;

    console.log('\n' + '='.repeat(70));
    if (markersPass && clearPass && pixelsPass) {
      console.log('OVERALL RESULT: PASS');
      console.log('ROOT CAUSE: N/A - markers are rendering correctly');
      console.log('FIX: N/A');
    } else {
      console.log('OVERALL RESULT: FAIL');
      if (!markersPass) {
        console.log('ROOT CAUSE: Markers are smaller than 100x100 pixels');
      } else if (!clearPass) {
        console.log('ROOT CAUSE: Something is clearing the framebuffer after marker draw');
      } else if (!pixelsPass) {
        console.log('ROOT CAUSE: Insufficient colored pixels in framebuffer');
      }
      console.log('FIX: Debug required - check console output above');
    }
    console.log('='.repeat(70) + '\n');

    // Output raw JSON for debugging
    console.log('--- RAW DIAGNOSTIC DATA (JSON) ---');
    console.log(JSON.stringify(results, null, 2));

  } catch (err) {
    console.error('[TEST] Error:', err.message);
    console.log('\nConsole messages captured:');
    consoleMessages.slice(-20).forEach(m => console.log('  ' + m));
  }

  await browser.close();
}

runDiagnostic().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
