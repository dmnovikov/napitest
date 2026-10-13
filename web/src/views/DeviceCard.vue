<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import StageBar from '../components/StageBar.vue'
import { api, links } from '../api/http'
import { live } from '../api/live'
import type { BoardType, DeviceDetail, DeviceJob } from '../api/types'
import { now } from '../lib/clock'
import {
  BOARD_NAME,
  JOB_STATUS_LABEL,
  MODE_LABEL,
  STAGE_LONG,
  STATE_LABEL,
  TILE_LABEL,
  formatAgo,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatMeasurement,
  hasWarn,
  stageClass,
  tileClass,
} from '../lib/format'

const route = useRoute()
const id = computed(() => Number(route.params.id))
const detail = ref<DeviceDetail | null>(null)
const error = ref<string | null>(null)
const open = ref(new Set<string>())

const device = computed(() => live.devices.get(id.value) ?? detail.value?.device ?? null)

async function load() {
  try {
    detail.value = await api.device(id.value)
    error.value = null
    if (!open.value.size && detail.value.jobs.length) open.value = new Set([detail.value.jobs[0].job_id])
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

let timer: number | undefined
onMounted(() => {
  void load()
  timer = window.setInterval(load, 5000)
})
onUnmounted(() => clearInterval(timer))
watch(id, () => {
  open.value = new Set()
  void load()
})
watch(
  () => device.value?.job?.status,
  () => void load(),
)

function toggle(j: DeviceJob) {
  const s = new Set(open.value)
  if (s.has(j.job_id)) s.delete(j.job_id)
  else s.add(j.job_id)
  open.value = s
}

async function toggleGolden() {
  if (!device.value) return
  try {
    await api.setGolden(id.value, !device.value.is_golden)
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

const boardMsg = ref<string | null>(null)
async function setBoard(ev: Event) {
  const v = (ev.target as HTMLSelectElement).value
  boardMsg.value = null
  try {
    const r = await api.setBoard([id.value], v === 'auto' ? null : (v as BoardType))
    boardMsg.value = r.warnings.join('\n') || null
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function openLabel() {
  window.open(links.label(id.value), '_blank')
}

async function identify() {
  try {
    await api.identify(id.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function jobAsView(j: DeviceJob) {
  return {
    job_id: j.job_id,
    batch_id: j.batch_id,
    mode: j.mode,
    status: j.status,
    started_at: j.started_at,
    finished_at: j.finished_at,
    elapsed: null,
    boot_deadline: null,
    result: j.result,
    stages: Object.fromEntries(j.stages.map((s) => [s.stage, { state: s.state, detail: s.detail, t: s.t, warnings: s.warnings ?? null }])),
  }
}

function jobDuration(j: DeviceJob) {
  if (!j.started_at) return '—'
  const end = j.finished_at ? Date.parse(j.finished_at) : now.value
  return formatDuration((end - Date.parse(j.started_at)) / 1000)
}

const canLabel = computed(() => {
  const d = device.value
  return !!d?.serial && (d.tile === 'pass' || detail.value?.jobs.some((j) => j.result === 'pass' || j.status === 'pass'))
})
</script>

<template>
  <div class="card-page">
    <div class="crumbs"><router-link to="/">← Платы</router-link></div>
    <div v-if="error" class="err">{{ error }}</div>
    <div v-if="!device && !error" class="muted">Загрузка…</div>

    <template v-if="device">
      <div class="header" :class="tileClass(device.tile)">
        <h2>
          Плата №{{ device.display_no }}
          <span class="mono">{{ device.serial ?? 'без серийника' }}</span>
          <span v-if="device.is_golden" class="golden">★ golden sample</span>
          <span v-if="device.simulated" class="sim">SIM</span>
        </h2>
        <div class="status">{{ TILE_LABEL[device.tile] ?? device.tile }}</div>
        <div class="actions">
          <button :disabled="!canLabel" title="Только при PASS и наличии серийника" @click="openLabel">
            Этикетка
          </button>
          <button @click="toggleGolden">{{ device.is_golden ? 'Снять golden sample' : 'Сделать golden sample' }}</button>
          <button v-if="device.has_led" @click="identify">Идентифицировать</button>
        </div>
      </div>

      <div class="cols">
        <section class="box">
          <h3>Плата</h3>
          <table class="kv">
            <tr><th>Серийник</th><td class="mono">{{ device.serial ?? '—' }}</td></tr>
            <tr><th>cpuid</th><td class="mono">{{ device.cpuid ?? '—' }} <span class="muted">({{ device.id_source ?? '?' }})</span></td></tr>
            <tr>
              <th>Плата</th>
              <td>
                <b>{{ BOARD_NAME[device.board?.type ?? 'napi-c'] }}</b>
                <span class="muted">
                  <template v-if="device.board?.override"> — задан вручную, стенд определил:
                    {{ device.board.detected ? BOARD_NAME[device.board.detected] : 'не определён' }}</template>
                  <template v-else-if="device.board?.detected"> — определён по подключению</template>
                  <template v-else> — агент не сообщил подключение</template>
                  <template v-if="device.board?.uplink">
                    (связь: {{ device.board.uplink.iface }}<template v-if="device.board.uplink.bus === 'usb'">, USB-адаптер</template><template v-if="device.board.uplink.driver">, {{ device.board.uplink.driver }}</template>)
                  </template>
                </span>
                <select class="boardsel" :value="device.board?.override ?? 'auto'" @change="setBoard">
                  <option value="auto">авто</option>
                  <option value="napi-c">NAPI-C вручную</option>
                  <option value="napi-p">NAPI-P вручную</option>
                </select>
                <div v-if="boardMsg" class="boardwarn">{{ boardMsg }}</div>
                <div v-if="device.board?.type === 'napi-p'" class="muted">NAPI-P: сеть не проверяется</div>
              </td>
            </tr>
            <tr><th>IP</th><td class="mono">{{ device.ip ?? '—' }}</td></tr>
            <tr><th>MAC</th><td class="mono">{{ device.mac ?? '—' }} <span class="muted">({{ device.mac_source ?? '?' }})</span></td></tr>
            <tr><th>Связь</th><td>{{ device.online ? 'online' : 'нет связи' }}, последний heartbeat {{ formatAgo(device.last_seen, now) }}<span class="muted"> ({{ formatDateTime(device.last_seen) }})</span></td></tr>
            <tr><th>Ядро</th><td class="mono">{{ device.kernel ?? '—' }}</td></tr>
            <tr><th>Агент</th><td class="mono">{{ device.agent_version ?? '—' }}</td></tr>
            <tr><th>Уже проверялась</th><td>{{ device.tested_before ? 'да' : 'нет' }}</td></tr>
            <tr><th>Тестовая microSD</th><td class="mono">{{ device.test_sd_version ?? '—' }}
              <span v-if="detail?.sd_selfcheck" class="muted">· самопроверка: {{ detail.sd_selfcheck.state ?? '?' }} {{ detail.sd_selfcheck.detail ?? '' }}</span></td></tr>
            <tr><th>RAM</th><td>{{ formatBytes(device.ram_bytes) }}</td></tr>
          </table>
        </section>

        <section class="box">
          <h3>Текущее задание</h3>
          <template v-if="device.job">
            <table class="kv">
              <tr><th>Задание</th><td class="mono">{{ device.job.job_id }}</td></tr>
              <tr><th>Режим</th><td>{{ MODE_LABEL[device.job.mode] ?? device.job.mode }}</td></tr>
              <tr><th>Статус</th><td>{{ JOB_STATUS_LABEL[device.job.status] ?? device.job.status }}</td></tr>
              <tr><th>Старт</th><td>{{ formatDateTime(device.job.started_at) }}</td></tr>
              <tr v-if="device.job.boot_deadline"><th>BOOT до</th><td>{{ formatDateTime(device.job.boot_deadline) }}</td></tr>
            </table>
            <StageBar :job="device.job" labels />
            <table class="stages">
              <tr v-for="(s, name) in device.job.stages" :key="name">
                <th>{{ STAGE_LONG[name] ?? name }}</th>
                <td><span class="chip" :class="stageClass(s?.state)">{{ STATE_LABEL[s?.state ?? 'pending'] }}</span></td>
                <td>{{ typeof s?.pct === 'number' ? Math.round(s.pct) + ' %' : '' }}</td>
                <td class="mono">{{ typeof s?.t === 'number' ? formatDuration(s.t) : '' }}</td>
                <td>
                  {{ s?.detail ?? '' }}
                  <ul v-if="s?.warnings?.length" class="warns"><li v-for="(w, i) in s.warnings" :key="i">⚠ {{ w }}</li></ul>
                  <span v-else-if="hasWarn(s)" class="warns-inline">⚠ есть замечания</span>
                </td>
              </tr>
            </table>
          </template>
          <div v-else class="muted">Нет задания</div>
        </section>
      </div>

      <section class="box">
        <h3>Накопители</h3>
        <template v-if="detail?.inventory">
          <div :class="detail.inventory.ok ? 'ok' : 'bad'">
            SD-NAND: <b class="mono">{{ detail.inventory.target ?? 'не распознана' }}</b>;
            загрузка с: <b class="mono">{{ detail.inventory.boot_device ?? '—' }}</b>
            {{ detail.inventory.ok ? '' : ' — распознавание неоднозначно, запись запрещена' }}
          </div>
          <table class="grid-table">
            <thead>
              <tr><th>Имя</th><th>Тип</th><th>Размер</th><th>Контроллер</th><th>by-path</th><th>modalias</th><th>RO</th><th>Съёмн.</th><th>Загрузочный</th><th>Смонтировано</th></tr>
            </thead>
            <tbody>
              <tr v-for="dv in detail.inventory.devices" :key="dv.name" :class="{ target: dv.name === detail.inventory.target, boot: dv.is_boot }">
                <td class="mono">{{ dv.name }}</td>
                <td>{{ dv.type ?? '—' }}</td>
                <td>{{ formatBytes(dv.size_bytes) }}</td>
                <td class="mono">{{ dv.controller ?? '—' }}</td>
                <td class="mono">{{ dv.by_path ?? '—' }}</td>
                <td class="mono">{{ dv.modalias ?? '—' }}</td>
                <td>{{ dv.ro ? 'да' : 'нет' }}</td>
                <td>{{ dv.removable ? 'да' : 'нет' }}</td>
                <td>{{ dv.is_boot ? 'да' : 'нет' }}</td>
                <td class="mono">{{ dv.mounted?.length ? dv.mounted.join(', ') : '—' }}</td>
              </tr>
            </tbody>
          </table>
          <ul class="reasons">
            <li v-for="(r, i) in detail.inventory.reasons" :key="i">{{ r }}</li>
          </ul>
        </template>
        <div v-else class="muted">Нет данных inventory</div>
      </section>

      <section class="box">
        <h3>История прогонов</h3>
        <div v-if="!detail?.jobs.length" class="muted">Прогонов нет</div>
        <div v-for="j in detail?.jobs ?? []" :key="j.job_id" class="job">
          <div class="job-head" @click="toggle(j)">
            <span class="arrow">{{ open.has(j.job_id) ? '▾' : '▸' }}</span>
            <span>{{ formatDateTime(j.started_at) }}</span>
            <span>{{ MODE_LABEL[j.mode] ?? j.mode }}</span>
            <span class="chip" :class="stageClass(j.result === 'pass' || j.status === 'pass' ? 'pass' : j.status === 'fail' ? 'fail' : j.status === 'running' || j.status === 'awaiting_boot' ? 'running' : 'pending')">
              {{ JOB_STATUS_LABEL[j.status] ?? j.status }}
            </span>
            <span class="mono muted">{{ jobDuration(j) }}</span>
            <span class="bar"><StageBar :job="jobAsView(j)" /></span>
            <span class="muted">партия {{ j.batch_id ?? '—' }}</span>
          </div>
          <div v-if="open.has(j.job_id)" class="job-body">
            <div class="muted small">
              Задание <span class="mono">{{ j.job_id }}</span>
              <template v-if="j.board_type"> · плата <b>{{ BOARD_NAME[j.board_type] }}</b><template
                v-if="j.board_detected && j.board_detected !== j.board_type"> (вручную, стенд определил
                {{ BOARD_NAME[j.board_detected] }})</template><template v-if="j.uplink_iface">, связь
                {{ j.uplink_iface }}</template></template>
              <template v-if="j.os_name"> · ОС <b>{{ j.os_name }}</b></template>
              <template v-if="j.image_file"> · <span class="mono">{{ j.image_file }}</span></template>
              <template v-else-if="j.image_version"> · образ <span class="mono">{{ j.image_version }}</span></template>
              <template v-if="j.image_sha256"> · SHA-256 <span class="mono">{{ j.image_sha256 }}</span></template>
            </div>
            <div v-for="s in j.stages" :key="s.stage" class="stage">
              <div class="stage-head">
                <b>{{ STAGE_LONG[s.stage] ?? s.stage }}</b>
                <span class="chip" :class="stageClass(s.state)">{{ STATE_LABEL[s.state] ?? s.state }}</span>
                <span class="mono muted">{{ typeof s.t === 'number' ? formatDuration(s.t) : '' }}</span>
                <span>{{ s.detail ?? '' }}</span>
              </div>
              <ul v-if="s.warnings?.length" class="warns"><li v-for="(w, i) in s.warnings" :key="i">⚠ {{ w }}</li></ul>
              <table v-if="s.measurements && Object.keys(s.measurements).length" class="kv small">
                <tr v-for="(v, k) in s.measurements" :key="k"><th>{{ k }}</th><td class="mono">{{ formatMeasurement(v) }}</td></tr>
              </table>
              <pre v-if="s.log" class="log">{{ s.log }}</pre>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.card-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  background: var(--card);
  border-left: 6px solid var(--blue);
  border-radius: 8px;
  padding: 10px 14px;
}
.header.tile-busy,
.header.tile-awaiting_boot {
  border-left-color: var(--yellow);
}
.header.tile-pass {
  border-left-color: var(--green);
}
.header.tile-fail {
  border-left-color: var(--red);
}
.header.tile-offline,
.header.tile-interrupted {
  border-left-color: #8a8f98;
}
.header h2 {
  margin: 0;
  font-size: 20px;
}
.header .actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
.golden {
  color: #b8860b;
  font-size: 14px;
  margin-left: 8px;
}
.sim {
  background: #e3e6ea;
  font-size: 11px;
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 8px;
}
.cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.box {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 14px;
}
.box h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
.kv th {
  text-align: left;
  color: var(--muted);
  font-weight: 500;
  padding: 2px 14px 2px 0;
  vertical-align: top;
  white-space: nowrap;
}
.kv td {
  padding: 2px 0;
  word-break: break-all;
}
.stages {
  margin-top: 8px;
  width: 100%;
  font-size: 13px;
}
.stages th {
  text-align: left;
  width: 70px;
}
.stages td {
  padding: 2px 6px;
}
.chip {
  display: inline-block;
  padding: 0 7px;
  border-radius: 9px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  line-height: 17px;
}
.chip.st-pending,
.chip.st-skipped {
  background: #d4d7dd;
  color: #333;
}
.chip.st-running {
  background: var(--yellow);
  color: #333;
}
.chip.st-pass {
  background: var(--green);
}
.chip.st-fail {
  background: var(--red);
}
.chip.st-error {
  background: var(--orange);
}
.chip.st-interrupted {
  background: #8a8f98;
}
.grid-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  margin: 8px 0;
}
.grid-table th,
.grid-table td {
  border-bottom: 1px solid var(--line);
  padding: 4px 6px;
  text-align: left;
}
.grid-table tr.target {
  background: #eaf6ec;
}
.grid-table tr.boot {
  background: #f1f2f4;
}
.reasons {
  font-size: 13px;
  margin: 4px 0 0;
}
.ok {
  color: var(--green);
}
.bad {
  color: var(--red);
  font-weight: 600;
}
.job {
  border-top: 1px solid var(--line);
}
.job-head {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 6px 0;
  cursor: pointer;
  font-size: 13px;
}
.job-head .bar {
  width: 140px;
}
.job-body {
  padding: 4px 0 10px 20px;
}
.stage {
  margin-top: 8px;
}
.stage-head {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 13px;
}
.log {
  background: #1e2228;
  color: #d8dee6;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  margin: 6px 0 0;
}
.small {
  font-size: 12px;
}
.err {
  color: var(--red);
}
@media (max-width: 1100px) {
  .cols {
    grid-template-columns: 1fr;
  }
}
.warns {
  margin: 3px 0 4px;
  padding: 3px 8px;
  list-style: none;
  background: #fff6db;
  border-left: 3px solid #f4b400;
  color: #7a5200;
  font-size: 12px;
}
.warns-inline {
  color: #9a6a00;
  font-size: 12px;
}
.boardsel {
  margin-left: 8px;
  font: inherit;
  font-size: 13px;
}
.boardwarn {
  color: #8a4b00;
  font-weight: 600;
  white-space: pre-line;
}
</style>
