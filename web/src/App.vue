<script setup lang="ts">
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api/http'
import { live, startLive, stopLive } from './api/live'
import { session } from './session'
import { checkUpdate, startUpdateWatch, updateReady } from './lib/update'

const router = useRouter()
const route = useRoute()

watch(
  () => session.username,
  (u) => (u ? startLive() : stopLive()),
  { immediate: true },
)

// Стенд обновили: раз в минуту и сразу после возврата связи с сервером сверяем версию страницы
startUpdateWatch()
watch(
  () => live.connected,
  (on) => {
    if (on) void checkUpdate()
  },
)
function reloadPage() {
  location.reload()
}

async function logout() {
  try {
    await api.logout()
  } catch {
    /* сессия уже недействительна */
  }
  session.username = null
  await router.replace('/login')
}
</script>

<template>
  <div class="app">
    <header v-if="route.path !== '/login' && session.username" class="nav">
      <span class="brand">NAPI Factory Test</span>
      <router-link to="/" exact-active-class="active">Платы</router-link>
      <router-link to="/batches" active-class="active">История партий</router-link>
      <router-link to="/images" active-class="active">Образы</router-link>
      <router-link to="/sdcard" active-class="active">microSD</router-link>
      <router-link to="/guide" active-class="active">Инструкция</router-link>
      <router-link to="/audit" active-class="active">Журнал</router-link>
      <span class="spacer" />
      <span class="conn" :class="live.connected ? 'on' : 'off'">
        <span class="dot" />{{ live.connected ? 'сервер на связи' : 'нет связи с сервером' }}
      </span>
      <span class="user">{{ session.username }}</span>
      <button class="link" @click="logout">Выйти</button>
    </header>
    <div v-if="updateReady" class="update-hint">
      Стенд обновлён — обновите страницу, чтобы увидеть изменения.
      <button @click="reloadPage">Обновить</button>
    </div>
    <main class="content"><router-view /></main>
  </div>
</template>

<style scoped>
.update-hint {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 16px;
  font-weight: 600;
  color: #1d4f91;
  background: #e3edfa;
  border-bottom: 1px solid #b9cfee;
}
</style>
