<script setup lang="ts">
defineProps<{ title: string; message: string; okText?: string; danger?: boolean; cancelText?: string; altText?: string }>()
defineEmits<{ (e: 'ok'): void; (e: 'cancel'): void; (e: 'alt'): void }>()
</script>

<template>
  <div class="overlay" @click.self="$emit('cancel')" @keydown.esc="$emit('cancel')">
    <div class="dialog" role="dialog" aria-modal="true">
      <h3>{{ title }}</h3>
      <p class="msg">{{ message }}</p>
      <div class="actions">
        <button @click="$emit('cancel')">{{ cancelText ?? 'Отмена' }}</button>
        <button v-if="altText" @click="$emit('alt')">{{ altText }}</button>
        <button :class="danger ? 'danger' : 'primary'" autofocus @click="$emit('ok')">
          {{ okText ?? 'Подтвердить' }}
        </button>
      </div>
    </div>
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
}
.dialog {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  min-width: min(380px, calc(100vw - 32px));
  box-sizing: border-box;
  max-width: min(560px, calc(100vw - 32px));
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
}
h3 {
  margin: 0 0 10px;
}
.msg {
  white-space: pre-line;
  line-height: 1.45;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}
</style>
