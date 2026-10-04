/**
 * Run frozen room diagnostic and capture results.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

async function runRoomDiagnostic() {
  console.log('[TEST] Launching browser for room diagnostic...\n');

  const browser = await chromium.launch({
    headless: true,
    args: ['--enable-webgl', '--use-gl=angle', '--enable-gpu', '--no-sandbox']
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  page.on('console', msg => {
    if (msg.text().includes('[ROOM_DIAG]') || msg.text().includes('[VISUAL]')) {
      console.log(msg.text());
    }
  });

  const url = 'http://localhost:8766/?render_test=room&hud=0';
  console.log(`[TEST] Loading ${url}`);

  try {
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });

    // Wait for diagnostic to complete
    console.log('[TEST] Waiting for room diagnostic to complete...');
    let results = null;
    for (let i = 0; i < 60; i++) {
      results = await page.evaluate(() => window.FROZEN_ROOM_RESULTS);
      if (results) break;
      await page.waitForTimeout(500);
    }

    if (!results) {
      console.log('\n=== ROOM DIAGNOSTIC FAILED: No results captured ===\n');
      await browser.close();
      process.exit(1);
    }

    // Capture canvas.toDataURL
    const canvasDataUrl = await page.evaluate(() => {
      const canvas = document.querySelector('#gl');
      return canvas ? canvas.toDataURL('image/png') : null;
    });

    // Save screenshot
    const screenshotDir = 'D:\\.Music\\visual\\debug\\screenshots';
    if (!fs.existsSync(screenshotDir)) {
      fs.mkdirSync(screenshotDir, { recursive: true });
    }

    if (canvasDataUrl && canvasDataUrl.startsWith('data:image')) {
      const base64 = canvasDataUrl.replace(/^data:image\/png;base64,/, '');
      fs.writeFileSync(path.join(screenshotDir, 'room_cam1.png'), Buffer.from(base64, 'base64'));
      console.log('\nSaved: screenshots/room_cam1.png');
    }

    // Generate report
    console.log('\n' + '='.repeat(70));
    console.log('                    ROOM DIAGNOSTIC REPORT');
    console.log('='.repeat(70) + '\n');

    console.log(`BUILD ID: ${results.buildInfo?.build_id || 'unknown'}`);
    console.log(`Camera: CAM_1 HERO FRONT\n`);

    console.log('--- SURFACE VISIBILITY ---');
    const surfaces = results.surfaces || {};
    for (const [name, data] of Object.entries(surfaces)) {
      const status = data.pass ? '✓ VISIBLE' : '✗ NOT VISIBLE';
      const size = data.width && data.height ? `${data.width}x${data.height}` : 'N/A';
      console.log(`${name.padEnd(12)}: ${status.padEnd(15)} ${data.count} px (${size})`);
    }

    console.log('\n--- COVERAGE METRICS ---');
    console.log(`Total colored pixels: ${results.totalColoredPixels}`);
    console.log(`Screen coverage: ${results.coveragePercent}%`);
    console.log(`Room bounding box: ${results.roomBbox?.width}x${results.roomBbox?.height}`);
    console.log(`Visible walls: ${results.visibleWalls}`);

    console.log('\n--- ACCEPTANCE CRITERIA ---');
    console.log(`Floor visible: ${results.floorVisible ? 'PASS' : 'FAIL'}`);
    console.log(`Back wall visible: ${results.backWallVisible ? 'PASS' : 'FAIL'}`);
    console.log(`Side walls visible: ${results.sideWallsVisible ? 'PASS' : 'FAIL'}`);
    console.log(`Substantial coverage (>15%): ${results.substantialCoverage ? 'PASS' : 'FAIL'}`);

    console.log('\n' + '='.repeat(70));
    console.log(`ROOM: ${results.allPass ? 'PASS ✓' : 'FAIL ✗'}`);
    console.log('='.repeat(70) + '\n');

    // Output raw JSON
    console.log('--- RAW DATA (JSON) ---');
    console.log(JSON.stringify(results, null, 2));

  } catch (err) {
    console.error('[TEST] Error:', err.message);
  }

  await browser.close();
}

runRoomDiagnostic().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
