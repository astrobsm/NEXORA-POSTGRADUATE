import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

import { readFileSync } from 'node:fs'

const { version } = JSON.parse(readFileSync('./package.json', 'utf-8'))

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    // Surfaced in the sync payload so the server can see which client build a
    // device is running when diagnosing a sync problem.
    __APP_VERSION__: JSON.stringify(version),
  },
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      // Dev-time proxy so the browser sees a single origin and the service worker
      // can cache API responses under the same scope as the app shell.
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://localhost:8000',
        changeOrigin: true,
      },
      '/health': { target: process.env.VITE_API_PROXY ?? 'http://localhost:8000' },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the heavy, rarely-changing dependencies so a routine app update
        // does not force clinicians on poor connections to re-download them.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
          data: ['@tanstack/react-query', 'dexie'],
        },
      },
    },
  },
})
