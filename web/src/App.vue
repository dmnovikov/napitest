<script setup lang="ts">
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api/http'
import { live, startLive, stopLive } from './api/live'
import { session } from './session'

const router = useRouter()
const route = useRoute()

watch(
  () => session.username,
  (u) => (u ? startLive() : stopLive()),
  { immediate: true },
)

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
    <header v-if="route.path !== '/login'" class="nav">
      <span class="brand">NAPI Factory Test</span>
      <router-link to="/" exact-active-class="active">Платы</router-link>
      <router-link to="/batches" active-class="active">История партий</router-link>
      <router-link to="/audit" active-class="active">Журнал</router-link>
      <span class="spacer" />
      <span class="conn" :class="live.connected ? 'on' : 'off'">
        <span class="dot" />{{ live.connected ? 'сервер на связи' : 'нет связи с сервером' }}
      </span>
      <span class="user">{{ session.username }}</span>
      <button class="link" @click="logout">Выйти</button>
    </header>
    <main class="content"><router-view /></main>
  </div>
</template>
