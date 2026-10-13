<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/http'
import type { PublicStatus } from '../api/types'
import { MODE_LABEL, formatTime } from '../lib/format'
import { session } from '../session'

// Без входа: идут ли уже тесты (второй тестировщик должен это видеть до входа)
const status = ref<PublicStatus | null>(null)
let timer: number | undefined
async function loadStatus() {
  try {
    status.value = await api.publicStatus()
  } catch {
    status.value = null
  }
  timer = window.setTimeout(loadStatus, 10000)
}
onMounted(loadStatus)
onUnmounted(() => clearTimeout(timer))

const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const error = ref<string | null>(null)
const busy = ref(false)

async function submit() {
  busy.value = true
  error.value = null
  try {
    await api.login(username.value, password.value)
    const me = await api.me()
    session.username = me.username
    session.freshLogin = true
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await router.replace(next)
  } catch (e) {
    error.value = e instanceof Error && e.message !== 'Требуется вход' ? e.message : 'Неверное имя или пароль'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login">
    <form class="box" @submit.prevent="submit">
      <h2>NAPI Factory Test</h2>
      <div v-if="status?.active.length" class="busy">
        <b>⚠ Осторожно: идут тесты</b>
        <div v-for="a in status.active" :key="a.batch_id">
          {{ a.name }} — {{ MODE_LABEL[a.mode] ?? a.mode }}, плат: {{ a.boards }}, с {{ formatTime(a.started_at).slice(0, 5) }}
        </div>
      </div>
      <label>Оператор <input v-model="username" autocomplete="username" autofocus required /></label>
      <label>Пароль <input v-model="password" type="password" autocomplete="current-password" required /></label>
      <div v-if="error" class="err">{{ error }}</div>
      <button class="primary" type="submit" :disabled="busy">Войти</button>
      <router-link class="guide" to="/guide">Инструкция и образ microSD</router-link>
    </form>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  justify-content: center;
  padding-top: 12vh;
}
.box {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 24px 28px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 320px;
}
h2 {
  margin: 0 0 6px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--muted);
}
input {
  font-size: 15px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.guide {
  text-align: center;
  font-size: 15px;
  font-weight: 600;
  padding: 8px;
  border: 1px solid var(--blue);
  border-radius: 6px;
}
.guide:hover {
  background: #eef4ff;
  text-decoration: none;
}
.busy {
  background: #fff1c2;
  border: 1px solid #e3b23c;
  color: #6b4500;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.busy b {
  font-size: 15px;
}
</style>
