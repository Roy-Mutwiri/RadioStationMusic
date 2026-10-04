/**
 * Investigate why the WebGL canvas appears black despite having content.
 */
const { chromium } = require('playwright');

async function investigate() {
  console.log('[TEST] Investigating canvas visibility...\n');

  const browser = await chromium.launch({
    headless: true,
    args: ['--enable-webgl', '--use-gl=angle', '--enable-gpu', '--no-sandbox']
  });

  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();

  page.on('console', msg => {
    if (msg.text().includes('[VISUAL]') || msg.text().includes('[FROZEN_DIAG]')) {
      console.log(msg.text());
    }
  });

  const url = 'http://localhost:8766/?render_test=world_marker&hud=0';
  await page.goto(url, { waitUntil: 'load', timeout: 30000 });

  // Wait for diagnostic to complete
  for (let i = 0; i < 60; i++) {
    const results = await page.evaluate(() => window.FROZEN_DIAG_RESULTS);
    if (results) break;
    await page.waitForTimeout(500);
  }

  // Investigate canvas properties
  const canvasInfo = await page.evaluate(() => {
    const canvas = document.querySelector('#gl');
    if (!canvas) return { error: 'Canvas #gl not found' };

    const rect = canvas.getBoundingClientRect();
    const style = window.getComputedStyle(canvas);

    // Check if canvas is covered by other elements
    const elementAtCenter = document.elementFromPoint(rect.left + rect.width/2, rect.top + rect.height/2);
    const elementAboveCanvas = elementAtCenter && elementAtCenter !== canvas ?
      { tagName: elementAtCenter.tagName, id: elementAtCenter.id, className: elementAtCenter.className } : null;

    // Get all elements with higher z-index
    const allElements = Array.from(document.querySelectorAll('*'));
    const elementsAbove = allElements.filter(el => {
      const elStyle = window.getComputedStyle(el);
      const elRect = el.getBoundingClientRect();
      return elStyle.position !== 'static' &&
             parseInt(elStyle.zIndex) > 0 &&
             elRect.width > 0 && elRect.height > 0 &&
             elRect.left < rect.right && elRect.right > rect.left &&
             elRect.top < rect.bottom && elRect.bottom > rect.top;
    }).map(el => ({
      tagName: el.tagName,
      id: el.id,
      className: el.className,
      zIndex: window.getComputedStyle(el).zIndex,
      rect: el.getBoundingClientRect()
    }));

    return {
      canvas: {
        width: canvas.width,
        height: canvas.height,
        clientWidth: canvas.clientWidth,
        clientHeight: canvas.clientHeight,
        offsetWidth: canvas.offsetWidth,
        offsetHeight: canvas.offsetHeight,
        rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
        style: {
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
          zIndex: style.zIndex,
          position: style.position,
          transform: style.transform,
          background: style.background,
          backgroundColor: style.backgroundColor
        }
      },
      elementAtCenter,
      elementsAbove: elementsAbove.slice(0, 10),
      bodyBackground: window.getComputedStyle(document.body).backgroundColor
    };
  });

  console.log('\n=== CANVAS INVESTIGATION ===\n');
  console.log('Canvas #gl:');
  console.log(JSON.stringify(canvasInfo.canvas, null, 2));
  console.log('\nElement at canvas center:', JSON.stringify(canvasInfo.elementAtCenter, null, 2));
  console.log('\nElements above canvas:', JSON.stringify(canvasInfo.elementsAbove, null, 2));
  console.log('\nBody background:', canvasInfo.bodyBackground);

  // Check WebGL context attributes
  const glInfo = await page.evaluate(() => {
    const canvas = document.querySelector('#gl');
    const gl = canvas?.getContext('webgl2');
    if (!gl) return { error: 'No WebGL2 context' };

    const attrs = gl.getContextAttributes();
    return {
      preserveDrawingBuffer: attrs.preserveDrawingBuffer,
      alpha: attrs.alpha,
      antialias: attrs.antialias,
      desynchronized: attrs.desynchronized,
      powerPreference: attrs.powerPreference,
      premultipliedAlpha: attrs.premultipliedAlpha,
      failIfMajorPerformanceCaveat: attrs.failIfMajorPerformanceCaveat,
      xrCompatible: attrs.xrCompatible
    };
  });

  console.log('\n=== WebGL Context Attributes ===');
  console.log(JSON.stringify(glInfo, null, 2));

  // Take a screenshot of just the canvas area
  await page.screenshot({
    path: 'D:\\.Music\\visual\\debug\\canvas_only.png',
    clip: { x: 0, y: 0, width: 1920, height: 1080 }
  });

  // Get the canvas image data directly
  const canvasDataUrl = await page.evaluate(() => {
    const canvas = document.querySelector('#gl');
    if (!canvas) return null;
    try {
      return canvas.toDataURL('image/png');
    } catch (e) {
      return 'Error: ' + e.message;
    }
  });

  if (canvasDataUrl && canvasDataUrl.startsWith('data:image')) {
    const fs = require('fs');
    const base64 = canvasDataUrl.replace(/^data:image\/png;base64,/, '');
    fs.writeFileSync('D:\\.Music\\visual\\debug\\canvas_todataurl.png', Buffer.from(base64, 'base64'));
    console.log('\nSaved canvas.toDataURL() to canvas_todataurl.png');
  } else {
    console.log('\nCanvas toDataURL result:', canvasDataUrl);
  }

  await browser.close();
}

investigate().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
