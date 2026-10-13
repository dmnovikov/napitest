<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/http'
import { session } from '../session'

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
      <label>Оператор <input v-model="username" autocomplete="username" autofocus required /></label>
      <label>Пароль <input v-model="password" type="password" autocomplete="current-password" required /></label>
      <div v-if="error" class="err">{{ error }}</div>
      <button class="primary" type="submit" :disabled="busy">Войти</button>
      <router-link class="guide" to="/guide">Инструкция оператора</router-link>
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
  font-size: 14px;
}
</style>
