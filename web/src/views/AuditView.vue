<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/http'
import type { AuditRow } from '../api/types'
import { formatDateTime } from '../lib/format'

const rows = ref<AuditRow[]>([])
const error = ref<string | null>(null)

function show(d: unknown) {
  if (d === null || d === undefined) return ''
  return typeof d === 'string' ? d : JSON.stringify(d)
}

onMounted(async () => {
  try {
    rows.value = await api.audit()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <div>
    <h2>Журнал действий</h2>
    <div v-if="error" class="err">{{ error }}</div>
    <table class="tbl">
      <thead><tr><th>Время</th><th>Оператор</th><th>Действие</th><th>Подробности</th></tr></thead>
      <tbody>
        <tr v-for="(r, i) in rows" :key="i">
          <td style="white-space: nowrap">{{ formatDateTime(r.ts) }}</td>
          <td>{{ r.operator ?? '—' }}</td>
          <td>{{ r.action }}</td>
          <td class="mono small">{{ show(r.detail) }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="4" class="muted">Записей нет</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.small {
  font-size: 12px;
  word-break: break-all;
}
</style>
