<script setup lang="ts">
import { computed } from 'vue'
import type { DeviceView, Summary } from '../api/types'
import { now } from '../lib/clock'
import { MODE_LABEL, boardsSummary, formatDuration, formatTime, secondsSince } from '../lib/format'

const props = defineProps<{ summary: Summary | null; devices: DeviceView[] }>()

const hhmm = (iso: string) => formatTime(iso).slice(0, 5)
const boards = computed(() => boardsSummary(props.devices))
// Прогноз окончания текущей партии (по длительности этапов прошлых проверок)
const eta = computed(() => {
  const b = props.summary?.batch
  const a = props.summary?.active?.find((x) => x.batch_id === b?.batch_id)
  if (!a?.eta_at) return null
  const soon = Date.parse(a.eta_at) - now.value < 60000
  // Истории ещё нет и считать не по чему: не показываем «не раньше <сейчас>»
  if (a.eta_partial && soon) return { text: 'прогноз после первых проверок', muted: true }
  return { text: `${a.eta_partial ? 'не раньше' : '~ в'} ${hhmm(a.eta_at)}`, muted: false }
})

// «Время на кофе»: сколько можно не подходить к стенду — до первой платы, которой нужен оператор
// (BOOTCHECK: вынуть microSD), иначе до конца партии
const coffee = computed(() => {
  const act = props.summary?.active ?? []
  if (!act.length) return null
  const ids = new Set(act.map((a) => a.batch_id))
  const mine = props.devices.filter((d) => d.job?.batch_id != null && ids.has(d.job.batch_id))
  if (mine.some((d) => d.tile === 'awaiting_boot' && d.online)) return { text: 'нужен оператор: выньте microSD', need: true }
  if (mine.some((d) => ['fail', 'warn', 'offline', 'interrupted'].includes(d.tile))) {
    return { text: 'нужен оператор: есть FAIL, WARN или плата без связи', need: true }
  }
  if (act.some((a) => a.eta_partial)) return { text: '☕ после первых проверок', need: false }
  const at = Math.min(...act.map((a) => Date.parse(a.operator_at ?? a.eta_at ?? '')).filter((t) => !Number.isNaN(t)))
  if (!Number.isFinite(at)) return null
  const min = Math.round((at - now.value) / 60000)
  if (min < 5) return { text: act.some((a) => a.mode !== 'test_only') ? '☕ не успеете — скоро понадобитесь' : '☕ не успеете — скоро конец', need: false }
  return { text: `☕ можно отойти на ~${min} мин`, need: false }
})

const elapsed = computed(() => {
  const b = props.summary?.batch
  if (!b?.started_at) return null
  if (b.finished_at) return (Date.parse(b.finished_at) - Date.parse(b.started_at)) / 1000
  return secondsSince(b.started_at, now.value)
})
const counters = computed(() => {
  const s = props.summary
  return [
    { k: 'Всего', v: s?.total, c: '', sub: s?.expected ? `на стенде ${s.expected}` : '' },
    { k: 'Online', v: s?.online, c: 'c-blue', sub: '' },
    { k: 'В работе', v: s?.busy, c: 'c-yellow', sub: '' },
    { k: 'PASS', v: s?.pass, c: 'c-green', sub: s?.pass_warn ? `из них ⚠ ${s.pass_warn}` : '' },
    ...(s?.boot_unconfirmed
      ? [{ k: 'BOOT ?', v: s.boot_unconfirmed, c: 'c-orange', sub: 'загрузка не подтверждена' }]
      : []),
    ...(s?.warn ? [{ k: 'WARN', v: s.warn, c: 'c-orange', sub: 'не прошла только сеть' }] : []),
    { k: 'FAIL', v: s?.fail, c: 'c-red', sub: '' },
    { k: 'Offline', v: s?.offline, c: 'c-gray', sub: '' },
    { k: 'Прервано', v: s?.interrupted, c: 'c-gray', sub: '' },
    { k: 'Не выполнено', v: s?.incomplete ?? 0, c: 'c-gray', sub: '' },
  ]
})
</script>

<template>
  <div class="panel">
    <div class="counters">
      <div v-for="c in counters" :key="c.k" class="counter" :class="c.c">
        <div class="v">{{ c.v ?? '—' }}</div>
        <div class="k">{{ c.k }}</div>
        <div v-if="c.sub" class="sub">{{ c.sub }}</div>
      </div>
    </div>
    <div class="info">
      <div>
        <span class="lbl">Партия:</span>
        <b>{{ summary?.batch?.name ?? '—' }}</b>
        <template v-if="summary?.batch">
          · {{ MODE_LABEL[summary.batch.mode] ?? summary.batch.mode }}
          <span v-if="summary.batch.kind === 'golden'" class="pill">проверка стенда</span>
          <span v-if="summary.batch.golden" class="pill">golden: {{ summary.batch.golden }}</span>
        </template>
      </div>
      <div>
        <span class="lbl">Оператор:</span> {{ summary?.batch?.operator ?? summary?.operator ?? '—' }}
        <span class="lbl">Старт:</span> {{ formatTime(summary?.batch?.started_at) }}
        <span class="lbl">Прошло:</span> <span class="mono">{{ formatDuration(elapsed) }}</span>
        <template v-if="eta">
          <span class="lbl">Закончится:</span>
          <b class="eta" :class="{ muted: eta.muted }" title="Прогноз по длительности этапов прошлых проверок; BOOTCHECK зависит от того, когда плату перезагрузят">{{ eta.text }}</b>
        </template>
        <span v-if="coffee" class="coffee" :class="{ need: coffee.need }" title="Сколько можно не подходить к стенду: до первой платы, которой нужен оператор (с BOOTCHECK — вынуть microSD), иначе до конца партии">{{ coffee.text }}</span>
      </div>
      <div>
        <span class="lbl">microSD:</span>
        <span class="mono">{{ summary?.versions.test_sd?.length ? summary.versions.test_sd.join(', ') : '—' }}</span>
        <span class="lbl">Образ:</span> <span class="mono">{{ summary?.versions.image ?? '—' }}</span>
      </div>
      <div v-if="summary?.net_stand" class="boards warn">
        ⚠ NET не прошли почти все платы партии — похоже на сеть стенда, а не на брак: проверьте коммутатор, кабель
        или Wi-Fi до сервера и повторите проверку (или запустите с галочкой «Без проверки сети»)
      </div>
      <div v-if="boards" class="boards" :class="boards.tone">{{ boards.tone === 'ok' ? '✓' : '⚠' }} {{ boards.text }}</div>
    </div>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  gap: 24px;
  align-items: center;
  flex-wrap: wrap;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 14px;
}
.counters {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
@media (max-width: 700px) {
  .panel {
    gap: 10px;
    padding: 8px 10px;
  }
  .counters {
    gap: 6px 4px;
  }
  .counter {
    min-width: 0;
    flex: 1 0 22%;
  }
  .counter .v {
    font-size: 18px;
  }
}
.counter {
  text-align: center;
  min-width: 56px;
}
.counter .v {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.counter .k {
  font-size: 11px;
  color: var(--muted);
}
.c-blue .v {
  color: var(--blue);
}
.c-yellow .v {
  color: #b58900;
}
.c-green .v {
  color: var(--green);
}
.c-orange .v {
  color: var(--orange);
}
.c-red .v {
  color: var(--red);
}
.c-gray .v {
  color: #7a808a;
}
.info {
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.lbl {
  color: var(--muted);
  margin-left: 8px;
}
.info > div > .lbl:first-child {
  margin-left: 0;
}
.pill {
  background: #eef0f3;
  border-radius: 10px;
  padding: 0 8px;
  font-size: 11px;
  margin-left: 6px;
}
.eta {
  color: #6b5200;
}
.coffee {
  margin-left: 10px;
  color: #6b4f2a;
}
.coffee.need {
  color: #8a4b00;
  font-weight: 600;
}
.eta.muted {
  color: var(--muted);
  font-weight: normal;
}
.boards {
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 4px;
  align-self: flex-start;
}
.boards.ok {
  color: #13692f;
  background: #e3f4e8;
}
.boards.warn {
  color: #6b4500;
  background: #fff1c2;
}
.sub {
  font-size: 10px;
  color: #9a6a00;
  white-space: nowrap;
}
</style>
