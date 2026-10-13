import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { setUnauthorizedHandler } from './api/http'
import { stopLive } from './api/live'
import { session } from './session'
import './style.css'

setUnauthorizedHandler(() => {
  // Страницы без входа (вход, инструкция) не уводят на вход. При открытии по прямой ссылке
  // маршрут ещё не установлен (401 приходит во время первого перехода) — смотрим адрес страницы
  if (router.currentRoute.value.meta.public || router.resolve(window.location.pathname).meta.public) return
  session.username = null
  stopLive()
  void router.replace({ path: '/login', query: { next: router.currentRoute.value.fullPath } })
})

createApp(App).use(router).mount('#app')
