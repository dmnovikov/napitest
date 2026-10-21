<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, links } from '../api/http'
import type { BatchRow } from '../api/types'
import { MODE_LABEL, formatDateTime } from '../lib/format'

const rows = ref<BatchRow[]>([])
const error = ref<string | null>(null)

function sdv(v: BatchRow['test_sd_versions']) {
  if (!v) return '—'
  return Array.isArray(v) ? v.join(', ') || '—' : v
}

onMounted(async () => {
  try {
    rows.value = await api.batches()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <div>
    <h2>История партий</h2>
    <div v-if="error" class="err">{{ error }}</div>
    <table class="tbl">
      <thead>
        <tr>
          <th>Партия</th><th>Старт</th><th>Окончание</th><th>Оператор</th><th>Режим</th><th>Тип</th>
          <th>microSD</th><th>ОС / образ</th><th>Плат</th><th>PASS</th><th>WARN</th><th>FAIL</th><th>Golden</th><th>Экспорт</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in rows" :key="b.batch_id">
          <td><router-link :to="`/batches/${b.batch_id}`">{{ b.name }}</router-link></td>
          <td>{{ formatDateTime(b.started_at) }}</td>
          <td>{{ formatDateTime(b.finished_at) }}</td>
          <td>{{ b.operator ?? '—' }}</td>
          <td>{{ MODE_LABEL[b.mode] ?? b.mode }}</td>
          <td>{{ b.kind === 'golden' ? 'проверка стенда' : 'производство' }}</td>
          <td class="mono">{{ sdv(b.test_sd_versions) }}</td>
          <td>{{ b.os ?? b.image_version ?? '—' }}</td>
          <td>{{ b.total }}</td>
          <td class="pass">{{ b.pass }}</td>
          <td class="res-warn">{{ b.warn ?? 0 }}</td>
          <td class="fail">{{ b.fail }}</td>
          <td>{{ b.golden ?? '—' }}</td>
          <td>
            <a :href="links.batchCsv(b.batch_id)">CSV</a> ·
            <a :href="links.batchJson(b.batch_id)">JSON</a>
          </td>
        </tr>
        <tr v-if="!rows.length"><td colspan="13" class="muted">Партий нет</td></tr>
      </tbody>
    </table>
  </div>
</template>
