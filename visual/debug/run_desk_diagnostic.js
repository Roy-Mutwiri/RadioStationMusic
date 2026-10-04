/**
 * Run frozen desk diagnostic and capture results.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

async function runDeskDiagnostic() {
  console.log('[TEST] Launching browser for desk diagnostic...\n');

  const browser = await chromium.launch({
    headless: true,
    args: ['--enable-webgl', '--use-gl=angle', '--enable-gpu', '--no-sandbox']
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  page.on('console', msg => {
    if (msg.text().includes('[DESK_DIAG]') || msg.text().includes('[VISUAL]')) {
      console.log(msg.text());
    }
  });

  const url = 'http://localhost:8766/?render_test=desk&hud=0';
  console.log(`[TEST] Loading ${url}`);

  try {
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });

    // Wait for diagnostic to complete
    console.log('[TEST] Waiting for desk diagnostic to complete...');
    let results = null;
    for (let i = 0; i < 60; i++) {
      results = await page.evaluate(() => window.FROZEN_DESK_RESULTS);
      if (results) break;
      await page.waitForTimeout(500);
    }

    if (!results) {
      console.log('\n=== DESK DIAGNOSTIC FAILED: No results captured ===\n');
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
      fs.writeFileSync(path.join(screenshotDir, 'desk_cam1.png'), Buffer.from(base64, 'base64'));
      console.log('\nSaved: screenshots/desk_cam1.png');
    }

    // Generate report
    console.log('\n' + '='.repeat(70));
    console.log('                    DESK DIAGNOSTIC REPORT');
    console.log('='.repeat(70) + '\n');

    console.log(`BUILD ID: ${results.buildInfo?.build_id || 'unknown'}`);
    console.log(`Camera: CAM_1 HERO FRONT\n`);

    console.log('--- ITEM VISIBILITY ---');
    const items = results.items || {};
    for (const [name, data] of Object.entries(items)) {
      const status = data.visible ? '✓ VISIBLE' : '✗ NOT VISIBLE';
      console.log(`${name.padEnd(14)}: ${status.padEnd(15)} ${data.pixels} px`);
    }

    console.log(`\nVisible items: ${results.visibleCount}/7`);

    console.log('\n--- ACCEPTANCE CRITERIA ---');
    console.log(`Desk surface visible: ${items.DESK?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`Main monitor visible: ${items.MON_MAIN?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`At least 2 secondary monitors: ${items.MON_SECONDARY?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`Keyboard visible: ${items.KEYBOARD?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`Mouse visible: ${items.MOUSE?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`Mug visible: ${items.MUG?.visible ? 'PASS' : 'FAIL'}`);
    console.log(`Notebook visible: ${items.NOTEBOOK?.visible ? 'PASS' : 'FAIL'}`);

    console.log('\n' + '='.repeat(70));
    console.log(`DESK: ${results.allPass ? 'PASS ✓' : 'FAIL ✗'}`);
    console.log('='.repeat(70) + '\n');

    // Output raw JSON
    console.log('--- RAW DATA (JSON) ---');
    console.log(JSON.stringify(results, null, 2));

  } catch (err) {
    console.error('[TEST] Error:', err.message);
  }

  await browser.close();
}

runDeskDiagnostic().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
