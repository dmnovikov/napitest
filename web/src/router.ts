import { createRouter, createWebHistory } from 'vue-router'
import MainView from './views/MainView.vue'
import { api } from './api/http'
import { session } from './session'

export const router = createRouter({
  history: createWebHistory('/'),
  routes: [
    { path: '/', component: MainView },
    { path: '/login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
    { path: '/device/:id', component: () => import('./views/DeviceCard.vue') },
    { path: '/batches', component: () => import('./views/BatchesView.vue') },
    { path: '/batches/:id', component: () => import('./views/BatchDetailView.vue') },
    { path: '/audit', component: () => import('./views/AuditView.vue') },
    { path: '/images', component: () => import('./views/ImagesView.vue') },
    { path: '/sdcard', component: () => import('./views/SdCardView.vue') },
    { path: '/guide', component: () => import('./views/GuideView.vue'), meta: { public: true } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  if (to.meta.public) {
    if (!session.username && to.path !== '/login') {
      // Инструкция открывается и без входа; если вход уже есть, показываем обычное меню
      api.me().then((me) => (session.username = me.username), () => {})
    }
    return true
  }
  if (session.username) return true
  try {
    const me = await api.me()
    session.username = me.username
    return true
  } catch {
    return { path: '/login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  }
})
