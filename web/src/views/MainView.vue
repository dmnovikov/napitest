<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import BoardTile from '../components/BoardTile.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import RetestDialog from '../components/RetestDialog.vue'
import TopPanel from '../components/TopPanel.vue'
import { api, links } from '../api/http'
import { live } from '../api/live'
import type { DeviceView, Mode, RetestItem } from '../api/types'
import { MODE_LABEL } from '../lib/format'
import { retestList } from '../lib/retest'

const router = useRouter()

const devices = computed<DeviceView[]>(() =>
  [...live.devices.values()].sort((a, b) => a.display_no - b.display_no || a.device_id - b.device_id),
)
const selected = ref(new Set<number>())
const lastPicked = ref<number | null>(null)
const fullPattern = ref(false)
const fullMemtester = ref(false)
const fullNet = ref(false)
const busy = ref(false)
const message = ref<{ text: string; error: boolean } | null>(null)

const selectedDevices = computed(() => devices.value.filter((d) => selected.value.has(d.device_id)))
const anyLed = computed(() => selectedDevices.value.some((d) => d.has_led))
const batchId = computed(() => live.summary?.batch?.batch_id ?? null)

// «Плат на стенде»: оператор вводит, сколько поставил; недостающие показываются пустыми местами
const expected = computed(() => live.summary?.expected ?? 0)
const onlineCount = computed(() => devices.value.filter((d) => d.online).length)
const missing = computed(() => Math.max(0, expected.value - onlineCount.value))
const placeholders = computed(() => Math.max(0, expected.value - devices.value.length))
// Поле показывает текущее значение сервера и сохраняется само (Enter или уход из поля)
const expectedInput = ref<string>('')
const expectedFocused = ref(false)
watch(
  expected,
  (v) => {
    if (!expectedFocused.value) expectedInput.value = v ? String(v) : ''
  },
  { immediate: true },
)
function saveExpected() {
  const raw = String(expectedInput.value ?? '').trim()
  const n = raw === '' ? 0 : Number(raw)
  if (n === expected.value) return
  if (!Number.isInteger(n) || n < 0 || n > 500) {
    say('Число плат: целое от 0 до 500', true)
    return
  }
  void guarded(async () => {
    await api.setExpected(n)
    say(n ? `На стенде ожидается плат: ${n}` : 'Ожидаемое число плат не задано')
  })
}
function shortageText(): string {
  if (!missing.value) return ''
  return (
    `Подключено ${onlineCount.value} из ${expected.value} плат на стенде. ` +
    `Не видно ${missing.value}: проверьте питание, Ethernet и тестовую microSD.\n`
  )
}

interface Pending {
  title: string
  message: string
  okText: string
  danger: boolean
  run: () => Promise<void>
}
const pending = ref<Pending | null>(null)

// Предупреждение «платы уже проверялись»: промис решается кнопкой диалога
const retest = ref<{ items: RetestItem[]; resolve: (ok: boolean) => void } | null>(null)
function askRetest(items: RetestItem[]): Promise<boolean> {
  return new Promise((resolve) => {
    retest.value = { items, resolve }
  })
}
function answerRetest(ok: boolean) {
  const r = retest.value
  retest.value = null
  r?.resolve(ok)
}
function displayNo(id: number): number | string {
  return live.devices.get(id)?.display_no ?? id
}

function pick(d: DeviceView, ev: MouseEvent) {
  const s = new Set(selected.value)
  if (ev.shiftKey && lastPicked.value !== null) {
    const ids = devices.value.map((x) => x.device_id)
    const a = ids.indexOf(lastPicked.value)
    const b = ids.indexOf(d.device_id)
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a]
      for (const id of ids.slice(lo, hi + 1)) s.add(id)
    }
  } else if (ev.ctrlKey || ev.metaKey) {
    if (s.has(d.device_id)) s.delete(d.device_id)
    else s.add(d.device_id)
  } else if (s.size === 1 && s.has(d.device_id)) {
    s.clear()
  } else {
    s.clear()
    s.add(d.device_id)
  }
  lastPicked.value = d.device_id
  selected.value = s
}

function selectAllOnline() {
  selected.value = new Set(devices.value.filter((d) => d.online).map((d) => d.device_id))
}
function clearSelection() {
  selected.value = new Set()
}

function say(text: string, error = false) {
  message.value = { text, error }
  if (!error) setTimeout(() => message.value?.text === text && (message.value = null), 5000)
}

async function guarded(fn: () => Promise<void>) {
  busy.value = true
  try {
    await fn()
  } catch (e) {
    say(e instanceof Error ? e.message : String(e), true)
  } finally {
    busy.value = false
  }
}

function ask(p: Pending) {
  pending.value = p
}
async function confirmOk() {
  const p = pending.value
  pending.value = null
  if (p) await guarded(p.run)
}

function needSelection(): number[] | null {
  const ids = [...selected.value]
  if (!ids.length) {
    say('Выберите платы (клик, Ctrl/Shift+клик или «Выбрать все online»)', true)
    return null
  }
  return ids
}

async function startBatch(mode: Mode, ids: number[], kind: 'production' | 'golden' = 'production') {
  const body = {
    mode,
    device_ids: ids,
    confirm: true,
    kind,
    options: {
      sdnand_pattern: (fullPattern.value ? 'full' : 'quick') as 'quick' | 'full',
      memtester: (fullMemtester.value ? 'full' : 'quick') as 'quick' | 'full',
      net: (fullNet.value ? 'full' : 'quick') as 'quick' | 'full',
    },
  }
  try {
    await api.startBatch(body)
  } catch (e) {
    const items = kind === 'production' ? retestList(e) : null
    if (!items) throw e
    busy.value = false
    const ok = await askRetest(items)
    busy.value = true
    if (!ok) {
      say(`Не запущено: ${MODE_LABEL[mode]} — платы уже проверялись`, true)
      return
    }
    await api.startBatch({ ...body, options: { ...body.options, allow_retest: true } })
  }
  say(`Запущено: ${MODE_LABEL[mode]}, плат: ${ids.length}`)
}

function launch(mode: Mode) {
  const ids = needSelection()
  if (!ids) return
  if (mode === 'test_only') {
    if (!missing.value) return void guarded(() => startBatch(mode, ids))
    ask({
      title: MODE_LABEL[mode],
      message: `${shortageText()}Запустить на выбранных (${ids.length})?`,
      okText: 'Запустить',
      danger: false,
      run: () => startBatch(mode, ids),
    })
    return
  }
  const warn =
    mode === 'flash_only'
      ? 'Тесты не выполняются. Внутренняя SD-NAND будет полностью перезаписана.'
      : 'После PASS всех тестов внутренняя SD-NAND будет полностью перезаписана.'
  ask({
    title: MODE_LABEL[mode],
    message: `${shortageText()}Плат выбрано: ${ids.length}.\n${warn}\nПродолжить?`,
    okText: 'Запустить',
    danger: true,
    run: () => startBatch(mode, ids),
  })
}

function stop() {
  const ids = [...selected.value]
  if (!ids.length && batchId.value === null) {
    say('Нет активной партии и не выбраны платы', true)
    return
  }
  const scope = ids.length ? `выбранных плат (${ids.length})` : `всей партии ${live.summary?.batch?.name ?? ''}`
  ask({
    title: 'Остановить',
    message:
      `Остановить задания ${scope}?\n` +
      'До начала записи задание отменяется. Если запись SD-NAND уже идёт, она прерывается, ' +
      'плата получает статус interrupted и требует повторной прошивки.',
    okText: 'Остановить',
    danger: true,
    run: async () => {
      if (ids.length) await api.stop(ids)
      else await api.stopBatch(batchId.value as number, [])
      say('Команда остановки отправлена')
    },
  })
}

function repeat() {
  const sel = selectedDevices.value
  if (!sel.length) return void needSelection()
  const groups = new Map<Mode, number[]>()
  const skipped: number[] = []
  for (const d of sel) {
    const m = d.job?.mode
    if (!m) {
      skipped.push(d.display_no)
      continue
    }
    groups.set(m, [...(groups.get(m) ?? []), d.device_id])
  }
  if (!groups.size) {
    say('У выбранных плат нет предыдущих заданий', true)
    return
  }
  const destructive = [...groups.keys()].some((m) => m !== 'test_only')
  const desc = [...groups.entries()].map(([m, ids]) => `${MODE_LABEL[m]}: ${ids.length}`).join('\n')
  const run = async () => {
    for (const [m, ids] of groups) await startBatch(m, ids)
    if (skipped.length) say(`Пропущены платы без заданий: ${skipped.join(', ')}`, true)
  }
  if (!destructive) return void guarded(run)
  ask({
    title: 'Повторить для выбранных',
    message: `${desc}\nВключены режимы с перезаписью SD-NAND. Продолжить?`,
    okText: 'Повторить',
    danger: true,
    run,
  })
}

function identify() {
  const ids = selectedDevices.value.filter((d) => d.has_led).map((d) => d.device_id)
  if (!ids.length) return
  void guarded(async () => {
    for (const id of ids) await api.identify(id)
    say(`Идентификация: ${ids.length} плат(ы) мигают 10 с`)
  })
}

function benchCheck() {
  let ids = [...selected.value]
  if (!ids.length) ids = devices.value.filter((d) => d.is_golden).map((d) => d.device_id)
  if (!ids.length) {
    say('Выберите эталонную плату или отметьте golden sample в карточке платы', true)
    return
  }
  void guarded(() => startBatch('test_only', ids, 'golden'))
}

function exportCsv() {
  if (batchId.value === null) {
    say('Нет текущей партии', true)
    return
  }
  window.open(links.batchCsv(batchId.value), '_blank')
}
</script>

<template>
  <div class="main">
    <TopPanel :summary="live.summary" />

    <div class="toolbar">
      <div class="group">
        <button class="primary" :disabled="busy" @click="launch('test_only')">Только тесты</button>
        <button class="warn" :disabled="busy" @click="launch('test_flash')">Тесты + прошивка</button>
        <button class="warn" :disabled="busy" @click="launch('flash_only')">Только прошивка</button>
        <button class="danger" :disabled="busy" @click="stop">Остановить</button>
      </div>
      <div class="group">
        <button :disabled="busy || !selected.size" @click="repeat">Повторить для выбранных</button>
        <button v-if="anyLed" :disabled="busy" @click="identify">Идентифицировать</button>
        <button :disabled="busy" @click="benchCheck">Проверка стенда</button>
        <button :disabled="batchId === null" @click="exportCsv">Экспорт CSV</button>
      </div>
      <div class="group">
        <button @click="selectAllOnline">Выбрать все online</button>
        <button :disabled="!selected.size" @click="clearSelection">Снять выбор</button>
        <span class="selcount">выбрано: {{ selected.size }}</span>
        <label class="chk" title="Запись и сверка всей SD-NAND (3,9 ГБ). Без галочки: 3 участка по 64 МБ, ~40 с. В «Тесты + прошивка» SD-NAND проверяется целиком всегда.">
          <input v-model="fullPattern" type="checkbox" /> Тотальная проверка SD-NAND (+12 мин, режим «Только тесты»)
        </label>
        <label class="chk" title="memtester на 256 МБ. Без галочки: 64 МБ, ~2 мин. Все 512 МБ ОЗУ при каждой загрузке проверяет ядро (memtest).">
          <input v-model="fullMemtester" type="checkbox" /> Тотальная проверка памяти (+9 мин)
        </label>
        <label class="chk" title="Дополнительно TCP iperf3 по 10 с в каждую сторону: реальная пропускная способность. Без галочки: UDP 90 Мбит/с по 3 с — потери и ошибки порта.">
          <input v-model="fullNet" type="checkbox" /> Тотальная проверка сети (+30 с, нагружает сеть)
        </label>
      </div>
      <div class="group expected">
        <span>Плат на стенде:</span>
        <input
          v-model="expectedInput"
          type="number"
          inputmode="numeric"
          min="0"
          max="500"
          placeholder="—"
          @focus="expectedFocused = true"
          @blur="expectedFocused = false; saveExpected()"
          @keydown.enter="($event.target as HTMLInputElement).blur()"
        />
        <span v-if="expected" :class="{ short: missing }">подключено {{ onlineCount }} из {{ expected }}</span>
      </div>
    </div>

    <div v-if="message" class="message" :class="{ error: message.error }" @click="message = null">
      {{ message.text }}
    </div>

    <div v-if="!devices.length && !expected" class="empty">
      Платы не подключены. Вставьте тестовые microSD, подключите Ethernet и питание.
    </div>
    <div class="grid" :class="{ few: devices.length + placeholders <= 12 }">
      <BoardTile
        v-for="d in devices"
        :key="d.device_id"
        :device="d"
        :selected="selected.has(d.device_id)"
        @pick="pick(d, $event)"
        @open="router.push(`/device/${d.device_id}`)"
      />
      <div v-for="n in placeholders" :key="'slot' + n" class="slot" title="Плата не подключилась">
        <span>нет платы</span>
        <small>питание, Ethernet, microSD?</small>
      </div>
    </div>

    <ConfirmDialog
      v-if="pending"
      :title="pending.title"
      :message="pending.message"
      :ok-text="pending.okText"
      :danger="pending.danger"
      @ok="confirmOk"
      @cancel="pending = null"
    />
    <RetestDialog
      v-if="retest"
      :items="retest.items"
      :display-no="displayNo"
      @ok="answerRetest(true)"
      @cancel="answerRetest(false)"
    />
  </div>
</template>

<style scoped>
.main {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 22px;
  align-items: center;
}
.group {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.selcount {
  font-size: 13px;
  color: var(--muted);
  margin-left: 4px;
}
.chk {
  font-size: 13px;
  margin-left: 10px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}
@media (min-width: 1700px) {
  .grid:not(.few) {
    grid-template-columns: repeat(10, 1fr);
  }
}
/* мало плат: крупные квадраты фиксированной ширины, не растянутые на весь экран */
.grid.few {
  grid-template-columns: repeat(auto-fill, minmax(210px, 250px));
}
.slot {
  border: 3px dashed #c3c8cf;
  border-radius: 8px;
  min-height: 108px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  color: #8a8f98;
  font-size: 14px;
}
.slot small {
  font-size: 11px;
}
.expected {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.expected input {
  width: 56px;
}
.expected .short {
  color: #9a6700;
  font-weight: 600;
}
.message {
  padding: 8px 12px;
  border-radius: 6px;
  background: #e8f4ea;
  border: 1px solid #bfe0c6;
  font-size: 14px;
  cursor: pointer;
}
.message.error {
  background: #fdecec;
  border-color: #f2c0c0;
  color: #9b1c1c;
}
.empty {
  color: var(--muted);
  padding: 30px;
  text-align: center;
}
</style>
