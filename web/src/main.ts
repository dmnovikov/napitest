import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { setUnauthorizedHandler } from './api/http'
import { stopLive } from './api/live'
import { session } from './session'
import './style.css'

setUnauthorizedHandler(() => {
  if (router.currentRoute.value.path === '/login') return
  session.username = null
  stopLive()
  void router.replace({ path: '/login', query: { next: router.currentRoute.value.fullPath } })
})

createApp(App).use(router).mount('#app')
