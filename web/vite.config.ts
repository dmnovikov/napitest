import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const target = 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target: target.replace('http', 'ws'), ws: true },
    },
  },
  test: { environment: 'node' },
})
