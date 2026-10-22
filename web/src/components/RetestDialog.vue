<script setup lang="ts">
import { computed } from 'vue'
import type { Mode, RetestItem } from '../api/types'
import { MODE_LABEL, formatDateTime } from '../lib/format'

// «Только прошивка» спрашивает не про тесты, а про уже прошитую SD-NAND; rest — сколько выбранных плат не прошивались
const props = defineProps<{ items: RetestItem[]; displayNo: (id: number) => number | string; mode: Mode; rest: number }>()
defineEmits<{ (e: 'ok'): void; (e: 'cancel'): void; (e: 'rest'): void }>()
const flash = computed(() => props.mode === 'flash_only')

function name(i: RetestItem): string {
  if (i.serial) return i.serial
  return i.cpuid ? `…${i.cpuid.slice(-4).toUpperCase()}` : '—'
}
</script>

<template>
  <div class="overlay" @click.self="$emit('cancel')">
    <div class="dialog" role="dialog" aria-modal="true">
      <h3>{{ flash ? 'Платы уже прошивались' : 'Платы уже проверялись' }}</h3>
      <p class="msg">
        {{ flash ? 'SD-NAND этих плат уже прошита. Прошить заново?' : 'Эти платы (по cpuid) уже есть в базе. Проверить их повторно?' }}
      </p>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>№</th><th>Плата</th><th>Последний прогон</th><th>Партия</th><th>Дата</th><th>Прогонов</th></tr>
          </thead>
          <tbody>
            <tr v-for="i in items" :key="i.device_id">
              <td>{{ displayNo(i.device_id) }}</td>
              <td class="mono">{{ name(i) }}</td>
              <td>
                {{ MODE_LABEL[i.last_mode] ?? i.last_mode }}
                <span class="st" :class="i.last_status">{{ i.last_status.toUpperCase() }}</span>
              </td>
              <td class="mono">{{ i.last_batch ?? '—' }}</td>
              <td>{{ formatDateTime(i.last_date) }}</td>
              <td class="num">{{ i.runs }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="actions">
        <button @click="$emit('cancel')">Отмена</button>
        <button v-if="rest > 0" @click="$emit('rest')">{{ flash ? 'Только непрошитые' : 'Только непроверенные' }} ({{ rest }})</button>
        <button class="primary" autofocus @click="$emit('ok')">{{ flash ? 'Прошить заново' : 'Проверить повторно' }}</button>
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
  z-index: 110;
}
.dialog {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  min-width: min(480px, calc(100vw - 32px));
  box-sizing: border-box;
  max-width: 760px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
}
h3 {
  margin: 0 0 8px;
}
.scroll {
  max-height: 50vh;
  overflow: auto;
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
th,
td {
  text-align: left;
  padding: 4px 8px;
  border-bottom: 1px solid #e5e7eb;
}
th {
  color: #6b7280;
  font-weight: 600;
}
.num {
  text-align: right;
}
.mono {
  font-family: ui-monospace, monospace;
}
.st {
  font-weight: 700;
  margin-left: 4px;
}
.st.pass {
  color: #16a34a;
}
.st.warn {
  color: var(--orange);
}
.st.fail,
.st.interrupted {
  color: #dc2626;
}
.st.offline {
  color: #6b7280;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
</style>
