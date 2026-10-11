import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const target = 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [vue()],
  base: '/',
  // Сборка идёт в dist-next: работающий сервер раздаёт web/dist, туда она попадает только при установке
  // (scripts/deploy-web.sh), а не после каждой сборки
  build: { outDir: process.env.NAPI_WEB_OUT || 'dist-next', emptyOutDir: true },
  server: {
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target: target.replace('http', 'ws'), ws: true },
    },
  },
  test: { environment: 'node' },
})
