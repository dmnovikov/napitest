<script setup lang="ts">
import { ref } from 'vue'

// Ответственное действие с образами ОС: повторный ввод пароля входа (+ ссылка / название, если нужны)
const props = defineProps<{
  title: string
  message?: string
  askName?: boolean
  askUrl?: boolean
  name?: string
  url?: string
  okText?: string
  error?: string | null
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'ok', v: { name: string; url: string; password: string }): void
  (e: 'cancel'): void
}>()

const name = ref(props.name ?? '')
const url = ref(props.url ?? '')
const password = ref('')

function submit() {
  if (props.busy) return
  emit('ok', { name: name.value.trim(), url: url.value.trim(), password: password.value })
}
</script>

<template>
  <div class="overlay" @click.self="$emit('cancel')" @keydown.esc="$emit('cancel')">
    <form class="dialog" role="dialog" aria-modal="true" @submit.prevent="submit">
      <h3>{{ title }}</h3>
      <p v-if="message" class="msg">{{ message }}</p>
      <label v-if="askName">
        Название
        <input v-model="name" type="text" placeholder="например, Debian" autofocus />
      </label>
      <label v-if="askUrl">
        Ссылка на образ (.img, .img.xz, .img.zst, .img.gz)
        <input v-model="url" type="url" placeholder="https://download.napilinux.ru/…" :autofocus="!askName" />
      </label>
      <label>
        Пароль входа (подтверждение)
        <input v-model="password" type="password" autocomplete="current-password" :autofocus="!askName && !askUrl" />
      </label>
      <p v-if="error" class="err">{{ error }}</p>
      <div class="actions">
        <button type="button" @click="$emit('cancel')">Отмена</button>
        <button type="submit" class="warn" :disabled="busy || !password || (askUrl && !url) || (askName && !name)">
          {{ okText ?? 'Подтвердить' }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(20, 25, 35, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 16px;
}
.dialog {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  width: 100%;
  max-width: 560px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
h3 {
  margin: 0;
}
.msg {
  white-space: pre-line;
  line-height: 1.45;
  margin: 0;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--muted);
}
input {
  font: inherit;
  font-size: 14px;
  padding: 7px 9px;
  border: 1px solid #c9cdd3;
  border-radius: 6px;
  color: var(--text);
  width: 100%;
}
.err {
  margin: 0;
  color: #8a4b00;
  font-weight: 600;
  font-size: 13px;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 6px;
}
</style>
