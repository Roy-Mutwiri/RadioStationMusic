/**
 * Fetch and display scene data from the visual server.
 */
const https = require('http');

const options = {
  hostname: 'localhost',
  port: 8766,
  path: '/api/visual/scene',
  method: 'GET'
};

const req = https.request(options, (res) => {
  let data = '';
  res.on('data', chunk => data += chunk);
  res.on('end', () => {
    const scene = JSON.parse(data);

    console.log('=== ROOM ===');
    console.log(`Dimensions: ${scene.room[0]}x${scene.room[1]}x${scene.room[2]} mm`);

    console.log('\n=== BOXES (Furniture) ===');
    for (const box of scene.boxes) {
      const [x0, y0, z0] = box.min;
      const [x1, y1, z1] = box.max;
      console.log(`${box.id}: [${x0},${y0},${z0}] to [${x1},${y1},${z1}] (${box.label})`);
    }

    console.log('\n=== QUADS (Monitors/Panels) ===');
    for (const quad of scene.quads) {
      console.log(`${quad.id}: centre=[${quad.centre}], ${quad.width}x${quad.height}, role=${quad.role || 'panel'}`);
    }

    console.log('\n=== CAMERAS ===');
    for (const cam of scene.cameras) {
      console.log(`${cam.id}: ${cam.name}, vfov=${cam.vfov}`);
    }
  });
});

req.on('error', (e) => console.error('Error:', e.message));
req.end();
