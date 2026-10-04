/**
 * Run frozen character diagnostic across multiple cameras.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const CAMERAS = ['CAM_1', 'CAM_4', 'CAM_6'];

async function runCharacterDiagnostic() {
  console.log('[TEST] Launching browser for character diagnostic...\n');

  const browser = await chromium.launch({
    headless: true,
    args: ['--enable-webgl', '--use-gl=angle', '--enable-gpu', '--no-sandbox']
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const screenshotDir = 'D:\\.Music\\visual\\debug\\screenshots';
  if (!fs.existsSync(screenshotDir)) {
    fs.mkdirSync(screenshotDir, { recursive: true });
  }

  const allResults = {};

  for (const camera of CAMERAS) {
    console.log(`\n${'='.repeat(70)}`);
    console.log(`                    CHARACTER TEST - ${camera}`);
    console.log('='.repeat(70));

    const page = await context.newPage();

    page.on('console', msg => {
      if (msg.text().includes('[CHAR_DIAG]')) {
        console.log(msg.text());
      }
    });

    const url = `http://localhost:8766/?render_test=character&camera=${camera}&hud=0`;
    console.log(`Loading ${url}`);

    try {
      await page.goto(url, { waitUntil: 'load', timeout: 30000 });

      // Wait for diagnostic
      let results = null;
      for (let i = 0; i < 60; i++) {
        results = await page.evaluate(() => window.FROZEN_CHAR_RESULTS);
        if (results) break;
        await page.waitForTimeout(500);
      }

      if (!results) {
        console.log(`${camera}: FAILED - No results captured`);
        allResults[camera] = { allPass: false, error: 'No results' };
        await page.close();
        continue;
      }

      // Save screenshot
      const canvasDataUrl = await page.evaluate(() => {
        const canvas = document.querySelector('#gl');
        return canvas ? canvas.toDataURL('image/png') : null;
      });

      if (canvasDataUrl && canvasDataUrl.startsWith('data:image')) {
        const base64 = canvasDataUrl.replace(/^data:image\/png;base64,/, '');
        const filename = `character_${camera.toLowerCase()}.png`;
        fs.writeFileSync(path.join(screenshotDir, filename), Buffer.from(base64, 'base64'));
        console.log(`Saved: screenshots/${filename}`);
      }

      // Report
      console.log(`\n--- ${camera} RESULTS ---`);
      const parts = results.parts || {};
      for (const [name, data] of Object.entries(parts)) {
        const status = data.visible ? '✓ VISIBLE' : '✗ NOT VISIBLE';
        console.log(`${name.padEnd(12)}: ${status.padEnd(15)} ${data.pixels} px`);
      }
      console.log(`\nVisible: ${results.visibleCount}/5`);
      console.log(`Head visible: ${results.hasHead ? 'YES' : 'NO'}`);
      console.log(`Torso visible: ${results.hasTorso ? 'YES' : 'NO'}`);
      console.log(`${camera}: ${results.allPass ? 'PASS ✓' : 'FAIL ✗'}`);

      allResults[camera] = results;

    } catch (err) {
      console.error(`${camera} Error:`, err.message);
      allResults[camera] = { allPass: false, error: err.message };
    }

    await page.close();
  }

  await browser.close();

  // Summary
  console.log('\n' + '='.repeat(70));
  console.log('                    CHARACTER DIAGNOSTIC SUMMARY');
  console.log('='.repeat(70) + '\n');

  for (const camera of CAMERAS) {
    const result = allResults[camera];
    const status = result?.allPass ? 'PASS ✓' : 'FAIL ✗';
    console.log(`${camera}: ${status}`);
  }

  const allPass = Object.values(allResults).every(r => r.allPass);
  console.log(`\nOVERALL CHARACTER: ${allPass ? 'PASS ✓' : 'FAIL ✗'}`);

  // Output JSON
  console.log('\n--- RAW DATA ---');
  console.log(JSON.stringify(allResults, null, 2));
}

runCharacterDiagnostic().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
