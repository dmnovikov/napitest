<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import StageBar from '../components/StageBar.vue'
import { api, links } from '../api/http'
import type { BatchDetail, BatchJob, JobView } from '../api/types'
import { JOB_STATUS_LABEL, MODE_LABEL, STAGE_LONG, formatDateTime, BOARD_NAME } from '../lib/format'

const route = useRoute()
const id = computed(() => Number(route.params.id))
const data = ref<BatchDetail | null>(null)
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    data.value = await api.batch(id.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})

function asView(j: BatchJob): JobView {
  return {
    job_id: j.job_id,
    batch_id: id.value,
    mode: data.value?.batch.mode ?? 'test_only',
    status: j.status,
    started_at: null,
    finished_at: null,
    elapsed: null,
    boot_deadline: null,
    result: j.result,
    stages: j.stages,
  }
}

function failDetails(j: BatchJob) {
  return Object.entries(j.stages)
    .filter(([, s]) => s && (s.state === 'fail' || s.state === 'error' || s.state === 'interrupted'))
    .map(([k, s]) => s?.detail || `${STAGE_LONG[k as keyof typeof STAGE_LONG] ?? k}: ${s?.state}`)
    .join('; ')
}

function warnText(j: BatchJob) {
  return Object.entries(j.stages)
    .flatMap(([k, s]) => (s?.warnings ?? []).map((w) => `${STAGE_LONG[k as keyof typeof STAGE_LONG] ?? k}: ${w}`))
    .join('\n')
}

function isPass(j: BatchJob) {
  return j.status === 'pass' || j.result === 'pass'
}
function sdv(v: unknown) {
  return Array.isArray(v) ? v.join(', ') : (v as string) ?? '—'
}
</script>

<template>
  <div>
    <div class="crumbs"><router-link to="/batches">← История партий</router-link></div>
    <div v-if="error" class="err">{{ error }}</div>
    <template v-if="data">
      <h2>Партия {{ data.batch.name }}</h2>
      <table class="kv">
        <tr><th>Режим</th><td>{{ MODE_LABEL[data.batch.mode] ?? data.batch.mode }} {{ data.batch.kind === 'golden' ? '(проверка стенда)' : '' }}</td></tr>
        <tr><th>Оператор</th><td>{{ data.batch.operator ?? '—' }}</td></tr>
        <tr><th>Время</th><td>{{ formatDateTime(data.batch.started_at) }} — {{ formatDateTime(data.batch.finished_at) }}</td></tr>
        <tr><th>Версии</th><td>microSD <span class="mono">{{ sdv(data.batch.test_sd_versions) }}</span>, образ <span class="mono">{{ data.batch.image_version ?? '—' }}</span></td></tr>
        <tr v-if="data.batch.os"><th>ОС для внутреннего накопителя</th><td>{{ data.batch.os }}</td></tr>
        <tr><th>Итог</th><td>плат {{ data.batch.total }}, <span class="pass">PASS {{ data.batch.pass }}</span>, <span class="res-warn">WARN {{ data.batch.warn ?? 0 }}</span>, <span class="fail">FAIL {{ data.batch.fail }}</span>, golden: {{ data.batch.golden ?? '—' }}</td></tr>
        <tr><th>Экспорт</th><td><a :href="links.batchCsv(data.batch.batch_id)">CSV</a> · <a :href="links.batchJson(data.batch.batch_id)">JSON</a></td></tr>
      </table>
      <table class="tbl">
        <thead>
          <tr><th>Серийник</th><th>cpuid</th><th>Плата</th><th>Этапы</th><th>Статус</th><th>Причина</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="j in data.jobs" :key="j.job_id">
            <td class="mono"><router-link :to="`/device/${j.device_id}`">{{ j.serial ?? '—' }}</router-link></td>
            <td class="mono">{{ j.cpuid ?? '—' }}</td>
            <td>{{ j.board_type ? BOARD_NAME[j.board_type] : '—' }}</td>
            <td style="width: 150px"><StageBar :job="asView(j)" /></td>
            <td :class="isPass(j) ? 'pass' : j.status === 'warn' ? 'res-warn' : j.status === 'fail' ? 'fail' : ''">{{ JOB_STATUS_LABEL[j.status] ?? j.status }}</td>
            <td>
              {{ failDetails(j) }}
              <span v-if="warnText(j)" class="warn" :title="warnText(j)">⚠</span>
            </td>
            <td><a v-if="(isPass(j) || j.status === 'warn') && j.serial" :href="links.label(j.device_id)" target="_blank">Этикетка</a></td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.warn {
  color: #e09b00;
  cursor: help;
  font-size: 15px;
}
.kv {
  margin-bottom: 14px;
}
.kv th {
  text-align: left;
  color: var(--muted);
  font-weight: 500;
  padding: 2px 14px 2px 0;
}
</style>
