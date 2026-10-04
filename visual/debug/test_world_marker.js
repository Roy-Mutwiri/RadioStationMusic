/**
 * Automated GPU diagnostic test for world_marker rendering.
 * Runs in headless Chromium to capture console output and screenshots.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE_URL = 'http://localhost:8766';
const OUTPUT_DIR = path.join(__dirname);

async function runTest(testName, url) {
  console.log(`\n========== ${testName} ==========`);

  // Launch with GPU enabled - use headless: false to verify visual output
  const useHeadless = process.env.HEADLESS !== 'false';
  const browser = await chromium.launch({
    headless: useHeadless,
    args: [
      '--enable-webgl',
      '--use-gl=angle',
      '--enable-gpu',
      '--no-sandbox',
      '--disable-setuid-sandbox'
    ]
  });
  if (!useHeadless) console.log('Running in headed mode for visual verification');
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  const consoleMessages = [];
  page.on('console', msg => {
    const text = msg.text();
    consoleMessages.push({ type: msg.type(), text });
    // Print GPU_AUDIT and IDENTITY_TEST messages immediately
    if (text.includes('GPU_AUDIT') || text.includes('IDENTITY_TEST') ||
        text.includes('WORLD_MARKER') || text.includes('MARKER') ||
        text.includes('PIXEL_READBACK')) {
      console.log(`[CONSOLE] ${text}`);
    }
  });

  page.on('pageerror', err => {
    console.error(`[PAGE_ERROR] ${err.message}`);
  });

  try {
    // Use 'load' instead of 'networkidle' because WebSocket keeps connection alive
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });

    // Wait for render loop to run several frames
    await page.waitForTimeout(3000);

    // Take screenshot
    const screenshotPath = path.join(OUTPUT_DIR, `${testName}.png`);
    await page.screenshot({ path: screenshotPath });
    console.log(`Screenshot saved: ${screenshotPath}`);

    // Get pixel data from center of canvas
    const pixelData = await page.evaluate(() => {
      const canvas = document.getElementById('gl');
      if (!canvas) return { error: 'No canvas found' };

      const gl = canvas.getContext('webgl2');
      if (!gl) return { error: 'No WebGL2 context' };

      const cx = Math.floor(canvas.width / 2);
      const cy = Math.floor(canvas.height / 2);

      const pixels = new Uint8Array(4);
      gl.readPixels(cx, canvas.height - cy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);

      // Also sample a grid
      const samples = [];
      for (let y = 0; y < canvas.height; y += Math.floor(canvas.height / 10)) {
        for (let x = 0; x < canvas.width; x += Math.floor(canvas.width / 10)) {
          const p = new Uint8Array(4);
          gl.readPixels(x, canvas.height - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, p);
          if (p[0] > 30 || p[1] > 40 || p[2] > 50) {
            samples.push({ x, y, r: p[0], g: p[1], b: p[2] });
          }
        }
      }

      return {
        center: { r: pixels[0], g: pixels[1], b: pixels[2], a: pixels[3] },
        canvasSize: { width: canvas.width, height: canvas.height },
        coloredPixels: samples.length,
        samples: samples.slice(0, 10)  // First 10 colored pixels
      };
    });

    console.log('\nPixel Analysis:');
    console.log(`  Canvas size: ${pixelData.canvasSize?.width}x${pixelData.canvasSize?.height}`);
    console.log(`  Center pixel: RGB(${pixelData.center?.r}, ${pixelData.center?.g}, ${pixelData.center?.b})`);
    console.log(`  Colored pixels found: ${pixelData.coloredPixels}`);

    if (pixelData.samples?.length > 0) {
      console.log('  Sample colored pixels:');
      pixelData.samples.forEach(s => {
        console.log(`    (${s.x}, ${s.y}): RGB(${s.r}, ${s.g}, ${s.b})`);
      });
    }

    // Determine pass/fail
    const centerIsColored = pixelData.center &&
      (pixelData.center.r > 50 || pixelData.center.g > 50 || pixelData.center.b > 50);
    const hasColoredPixels = pixelData.coloredPixels > 0;

    const result = {
      test: testName,
      pass: centerIsColored || hasColoredPixels,
      centerPixel: pixelData.center,
      coloredPixelCount: pixelData.coloredPixels,
      consoleMessages: consoleMessages.filter(m =>
        m.text.includes('GPU_AUDIT') || m.text.includes('IDENTITY') ||
        m.text.includes('MARKER') || m.text.includes('ERROR') ||
        m.text.includes('NULL') || m.text.includes('FAIL')
      )
    };

    console.log(`\nRESULT: ${result.pass ? 'PASS ✓' : 'FAIL ✗'}`);

    return result;

  } catch (error) {
    console.error(`Test error: ${error.message}`);
    return { test: testName, pass: false, error: error.message };
  } finally {
    await browser.close();
  }
}

async function main() {
  console.log('World Marker GPU Diagnostic Tests');
  console.log('==================================\n');

  const results = {};

  // Test 1: Identity matrix test (most important)
  results.identity = await runTest('world_marker_identity',
    `${BASE_URL}/?render_test=world_marker_identity&hud=0`);

  // Test 2: Full world marker test
  results.world_marker = await runTest('world_marker',
    `${BASE_URL}/?render_test=world_marker&hud=0`);

  // Test 3: Clip-space test (known working baseline)
  results.room_clip = await runTest('room_clip',
    `${BASE_URL}/?render_test=room_clip&hud=0`);

  // Summary
  console.log('\n\n========== SUMMARY ==========');
  console.log(`IDENTITY TEST:     ${results.identity?.pass ? 'PASS ✓' : 'FAIL ✗'}`);
  console.log(`WORLD_MARKER TEST: ${results.world_marker?.pass ? 'PASS ✓' : 'FAIL ✗'}`);
  console.log(`ROOM_CLIP TEST:    ${results.room_clip?.pass ? 'PASS ✓' : 'FAIL ✗'}`);

  // Root cause analysis
  console.log('\n========== ROOT CAUSE ANALYSIS ==========');
  if (results.room_clip?.pass && !results.identity?.pass) {
    console.log('DIAGNOSIS: World shader path is broken (identity fails but clip works)');
    console.log('SUSPECT: Vertex attribute, shader compilation, or uniform location issue');
  } else if (results.identity?.pass && !results.world_marker?.pass) {
    console.log('DIAGNOSIS: Matrix calculation or upload is wrong (identity works but world fails)');
    console.log('SUSPECT: viewProj matrix values, column/row major mismatch, or camera data');
  } else if (!results.room_clip?.pass) {
    console.log('DIAGNOSIS: Base WebGL rendering is broken');
    console.log('SUSPECT: Canvas, context, or fundamental GL state');
  } else if (results.world_marker?.pass) {
    console.log('DIAGNOSIS: All tests pass - rendering should be working');
  }

  // Save full results
  const outputPath = path.join(OUTPUT_DIR, 'WORLD_MARKER_GPU_AUDIT.json');
  fs.writeFileSync(outputPath, JSON.stringify(results, null, 2));
  console.log(`\nFull results saved to: ${outputPath}`);
}

main().catch(console.error);
