import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The dev server proxies to the API so the browser sees one origin, which means the
    // WebSocket works without CORS and the production and development paths are identical.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the chart library out. The broadcast overlay runs on a streaming machine
        // and uses no charts; without this it would download Recharts to render a track
        // title. Vendor is separated too so a UI change does not invalidate it.
        manualChunks: {
          charts: ['recharts'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
        },
      },
    },
  },
})
