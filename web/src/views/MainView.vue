<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import BoardTile from '../components/BoardTile.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import PasswordDialog from '../components/PasswordDialog.vue'
import RetestDialog from '../components/RetestDialog.vue'
import TopPanel from '../components/TopPanel.vue'
import { api, links } from '../api/http'
import { live } from '../api/live'
import type { BoardType, DeviceView, Mode, RetestItem } from '../api/types'
import { BOARD_NAME, MODE_LABEL, formatTime, osLine } from '../lib/format'
import { session } from '../session'
import { retestList } from '../lib/retest'

const router = useRouter()

const devices = computed<DeviceView[]>(() =>
  [...live.devices.values()].sort((a, b) => a.display_no - b.display_no || a.device_id - b.device_id),
)
const selected = ref(new Set<number>())
// Скрытые со стенда платы снимаются с выбора, иначе их не видно, а запуск их включит
watch(devices, (list) => {
  const shown = new Set(list.map((d) => d.device_id))
  if ([...selected.value].some((id) => !shown.has(id))) {
    selected.value = new Set([...selected.value].filter((id) => shown.has(id)))
  }
})
const lastPicked = ref<number | null>(null)
const fullPattern = ref(false)
const fullMemtester = ref(false)
const fullNet = ref(false)
const skipNet = ref(false)
const skipBoot = ref(false)
// Тип плат для всей партии: '' — как определено (или задано) у каждой платы
const batchBoard = ref<'' | BoardType>('')

// Меню «ОС для внутреннего накопителя»: по умолчанию первый пункт (Armbian); список ведёт сервер (страница «Образы»)
const osMenu = computed(() => live.summary?.images?.menu ?? [])
const osKey = ref('')
const zeroFull = ref(false)
watch(
  osMenu,
  (m) => {
    if (m.length && !m.some((i) => i.key === osKey.value)) osKey.value = m[0].key
  },
  { immediate: true },
)
const osItem = computed(() => osMenu.value.find((i) => i.key === osKey.value))
const osInfo = computed(() => osLine(osItem.value, live.summary?.images, zeroFull.value))
const osName = computed(() => {
  const i = osItem.value
  if (!i) return '—'
  if (i.kind === 'zero') return zeroFull.value ? 'Zeroimage (полная)' : 'Zeroimage'
  const v = live.summary?.images?.variants.find((x) => x.key === i.key)
  const ver = v?.current?.version ?? i.version
  return i.kind === 'custom' ? `Custom: ${v?.current?.source_file ?? '—'}` : `${i.name}${ver ? ' ' + ver : ''}`
})
const customDialog = ref(false)
const customError = ref<string | null>(null)
async function saveCustom(v: { url: string; password: string }) {
  customError.value = null
  try {
    await api.setCustom(v.url, v.password)
    customDialog.value = false
    say('Образ клиента скачивается и проверяется; затем он разойдётся по картам')
  } catch (e) {
    customError.value = e instanceof Error ? e.message : String(e)
  }
}
const busy = ref(false)
const message = ref<{ text: string; error: boolean; warn?: boolean } | null>(null)

const selectedDevices = computed(() => devices.value.filter((d) => selected.value.has(d.device_id)))
const anyLed = computed(() => selectedDevices.value.some((d) => d.has_led))
const batchId = computed(() => live.summary?.batch?.batch_id ?? null)
// Только что вошли, а на стенде уже идут тесты (возможно, другого тестировщика)
const active = computed(() => live.summary?.active ?? [])
const loginWarn = computed(() => session.freshLogin && active.value.length > 0)
watch(
  () => live.summary,
  (s) => {
    // Предупреждение показывается один раз после входа: если тестов нет — сразу снимаем флаг
    if (s && !(s.active ?? []).length) session.freshLogin = false
  },
)

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
  cancelText?: string
  /** Третья кнопка диалога */
  alt?: { text: string; run: () => void }
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
  } else if (s.has(d.device_id)) {
    // Щелчок добавляет плату к выбору или снимает её (на сенсорном экране нет Ctrl)
    s.delete(d.device_id)
  } else {
    s.add(d.device_id)
  }
  lastPicked.value = d.device_id
  selected.value = s
}

// Тип платы для выделенных (если автоопределение ошиблось); «авто» — снова по подключению
async function setBoardSelected(ev: Event) {
  const el = ev.target as HTMLSelectElement
  const v = el.value
  el.value = ''
  const ids = [...selected.value]
  if (!v || !ids.length) return
  try {
    const r = await api.setBoard(ids, v === 'auto' ? null : (v as BoardType))
    if (r.warnings.length) say(r.warnings.join('\n'), false, true)
    else say(`Тип платы для выбранных (${ids.length}): ${v === 'auto' ? 'по подключению' : BOARD_NAME[v]}`)
  } catch (e) {
    say(e instanceof Error ? e.message : String(e), true)
  }
}

function boardsLine(ids: number[]): string {
  if (batchBoard.value) {
    return `Тип плат для партии: ${BOARD_NAME[batchBoard.value]} (запомнится у плат).` +
      (batchBoard.value === 'napi-p' ? ' Сеть не проверяется.\n' : '\n')
  }
  const p = devices.value.filter((d) => ids.includes(d.device_id) && d.board?.type === 'napi-p').length
  if (!p) return ''
  return p === ids.length ? 'Все платы NAPI-P: сеть не проверяется.\n' : `Из них NAPI-P: ${p} — у них сеть не проверяется.\n`
}

function selectAllOnline() {
  selected.value = new Set(devices.value.filter((d) => d.online).map((d) => d.device_id))
}
function clearSelection() {
  selected.value = new Set()
}

function say(text: string, error = false, warn = false) {
  message.value = { text, error, warn }
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
function confirmAlt() {
  const p = pending.value
  pending.value = null
  p?.alt?.run()
}

/** Выбранные платы. Ничего не выбрано — окно: выбрать нужные или сразу все из pool (затем retry). */
function needSelection(
  retry: () => void,
  pool: DeviceView[] = devices.value.filter((d) => d.online),
  what = 'Все платы на связи',
  noneText = 'Не выбрано ни одной платы: щёлкните по нужным плиткам',
): number[] | null {
  const ids = [...selected.value]
  if (ids.length) return ids
  if (!pool.length) {
    say(noneText, false, true)
    return null
  }
  ask({
    title: 'Не выбрано ни одной платы',
    message: 'Выберите нужные платы щелчком по плитке или запустите сразу на всех.',
    okText: `${what} (${pool.length})`,
    cancelText: 'Выбрать нужные',
    danger: false,
    run: async () => {
      selected.value = new Set(pool.map((d) => d.device_id))
      queueMicrotask(retry)
    },
  })
  return null
}

/** На плате уже идёт задание (тесты, прошивка или ожидание BOOTCHECK): новую партию на ней не запускаем. */
function inWork(d: DeviceView): boolean {
  return d.tile === 'busy' || d.tile === 'awaiting_boot'
}

function skippedNos(ds: DeviceView[]): string {
  return ds.map((d) => d.display_no).join(', ')
}

async function startBatch(mode: Mode, ids: number[], kind: 'production' | 'golden' = 'production', note = '') {
  const body = {
    mode,
    device_ids: ids,
    confirm: true,
    kind,
    options: {
      sdnand_pattern: (fullPattern.value ? 'full' : 'quick') as 'quick' | 'full',
      memtester: (fullMemtester.value ? 'full' : 'quick') as 'quick' | 'full',
      net: (fullNet.value ? 'full' : 'quick') as 'quick' | 'full',
      boot: (skipBoot.value ? 'skip' : 'check') as 'check' | 'skip',
      ...(skipNet.value ? { net_skip: true } : {}),
      ...(batchBoard.value ? { board: batchBoard.value } : {}),
      ...(mode === 'test_only' ? {} : { os: osKey.value, zero: (zeroFull.value ? 'full' : 'quick') as 'quick' | 'full' }),
    },
  }
  let warnings: string[] = []
  try {
    warnings = (await api.startBatch(body))?.warnings ?? []
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
    warnings = (await api.startBatch({ ...body, options: { ...body.options, allow_retest: true } }))?.warnings ?? []
  }
  // Выбор снимается: следующие платы оператор отмечает с нуля
  selected.value = new Set()
  const text = `Запущено: ${MODE_LABEL[mode]}, плат: ${ids.length}${note}`
  if (warnings.length) say(`${text}\n${warnings.join('\n')}`, false, true)
  else say(text)
}

function launch(mode: Mode) {
  const free = devices.value.filter((d) => d.online && !inWork(d))
  const picked = needSelection(() => launch(mode), free, 'Все свободные платы на связи',
    'Свободных плат на связи нет: на всех уже идёт задание')
  if (!picked) return
  // Среди выбранных могут быть платы, где задание уже идёт: их пропускаем, а не отказываем всей партии
  const skipped = selectedDevices.value.filter(inWork)
  const ids = picked.filter((id) => !skipped.some((d) => d.device_id === id))
  if (!ids.length) {
    say(`На выбранных платах (${skippedNos(skipped)}) задание уже идёт: выберите свободные платы`, false, true)
    return
  }
  const note = skipped.length ? `; уже проверяются, пропущены: ${skippedNos(skipped)}` : ''
  const noteLine = skipped.length ? `\nУже проверяются, пропущены: ${skippedNos(skipped)}.` : ''
  if (mode === 'test_only') {
    if (!missing.value) return void guarded(() => startBatch(mode, ids, 'production', note))
    ask({
      title: MODE_LABEL[mode],
      message: `${shortageText()}${boardsLine(ids)}Запустить на выбранных (${ids.length})?${noteLine}`,
      okText: 'Запустить',
      danger: false,
      run: () => startBatch(mode, ids, 'production', note),
    })
    return
  }
  if (osInfo.value.tone === 'error') {
    say(`ОС для внутреннего накопителя «${osItem.value?.name ?? '—'}»: ${osInfo.value.text}`, true)
    return
  }
  const zero = osItem.value?.kind === 'zero'
  const warn =
    mode === 'flash_only'
      ? 'Тесты не выполняются. Внутренняя SD-NAND будет перезаписана.'
      : 'После PASS всех тестов внутренняя SD-NAND будет перезаписана.'
  const boot =
    skipBoot.value || zero
      ? 'Загрузка с SD-NAND проверяться не будет.'
      : 'BOOTCHECK: после прошивки выключите плату, выньте microSD и включите (ожидание до 10 мин).'
  ask({
    title: MODE_LABEL[mode],
    message:
      `${shortageText()}Плат выбрано: ${ids.length}.${noteLine}\n${boardsLine(ids)}ОС для внутреннего накопителя (SD-NAND): ${osName.value}\n(${osInfo.value.text})\n` +
      `${warn}\n${boot}\nПродолжить?`,
    okText: 'Запустить',
    danger: true,
    run: () => startBatch(mode, ids, 'production', note),
  })
}

function stop() {
  const ids = [...selected.value]
  if (!ids.length && batchId.value === null) {
    say('Нет активной партии и не выбраны платы', true)
    return
  }
  const a = active.value.find((x) => x.batch_id === batchId.value)
  const batchText = a
    ? `ВСЕЙ партии ${a.name}: ${MODE_LABEL[a.mode] ?? a.mode}, плат: ${a.boards}, запущена в ${formatTime(a.started_at).slice(0, 5)}`
    : `всей партии ${live.summary?.batch?.name ?? ''}`
  const others = active.value.filter((x) => x.batch_id !== batchId.value)
  const scope = ids.length ? `выбранных плат (${ids.length})` : batchText
  ask({
    title: 'Остановить',
    message:
      `Остановить задания ${scope}?\n` +
      (!ids.length && active.value.length > 1
        ? `Другие идущие партии (${others.map((x) => x.name).join(', ')}) не останавливаются.\n`
        : '') +
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

// «Только BOOTCHECK»: заново ждать загрузку с SD-NAND у прошитых плат, где она не подтвердилась
function bootcheck() {
  const pool = devices.value.filter((d) => d.tile === 'boot_unconfirmed')
  const ids = needSelection(bootcheck, pool, 'Все с неподтверждённой загрузкой')
  if (!ids) return
  void guarded(async () => {
    const r = await api.retryBoot(ids)
    say(`BOOTCHECK заново, плат: ${r.jobs}. Выключите плату, выньте microSD и включите (ожидание до 10 мин)`)
  })
}

function identify() {
  const ids = selectedDevices.value.filter((d) => d.has_led).map((d) => d.device_id)
  if (!ids.length) return
  void guarded(async () => {
    for (const id of ids) await api.identify(id)
    say(`Идентификация: у ${ids.length} плат(ы) светодиод горит непрерывно 10 с`)
  })
}

function benchCheck() {
  let ids = [...selected.value]
  let note = ''
  if (!ids.length) {
    // Без выбора — все эталоны на связи; эталон без связи в партию не попадает (иначе партия ждёт его)
    const golden = devices.value.filter((d) => d.is_golden)
    const off = golden.filter((d) => !d.online)
    ids = golden.filter((d) => d.online).map((d) => d.device_id)
    if (off.length) note = `. Эталоны без связи не проверяются: ${off.map((d) => d.display_no).join(', ')}`
    if (!ids.length) {
      say(
        off.length
          ? `Эталонные платы без связи: ${off.map((d) => d.display_no).join(', ')}. Подключите или выберите плату`
          : 'Выберите эталонную плату или отметьте golden sample в карточке платы',
        true,
      )
      return
    }
  }
  void guarded(async () => {
    await startBatch('test_only', ids, 'golden')
    if (note) say(`Проверка стенда запущена, плат: ${ids.length}${note}`, false, true)
  })
}

function refreshGrid() {
  void guarded(async () => {
    const r = await api.refreshGrid()
    const parts = [`скрыто плат без связи: ${r.hidden}`, `переподключаются: ${r.reconnect}`]
    if (r.kept_busy) parts.push(`не скрыты (есть задание, нажмите «Остановить»): ${r.kept_busy}`)
    say(parts.join('; '), false, !!r.kept_busy)
  })
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
    <div v-if="loginWarn" class="message warn active" @click="session.freshLogin = false">
      <b>⚠ Осторожно: на стенде уже идут тесты.</b>
      <div v-for="a in active" :key="a.batch_id">
        Партия {{ a.name }} — {{ MODE_LABEL[a.mode] ?? a.mode }}, плат: {{ a.boards }}, запущена в
        {{ formatTime(a.started_at).slice(0, 5) }}{{ a.operator ? ` (${a.operator})` : '' }}
      </div>
      <div>Не выбирайте эти платы и не нажимайте «Остановить» без выделения. Щёлкните, чтобы скрыть.</div>
    </div>
    <TopPanel :summary="live.summary" :devices="devices" />

    <div class="toolbar">
      <div class="group">
        <button class="primary" :disabled="busy" @click="launch('test_only')">Только тесты</button>
        <button class="warn" :disabled="busy" @click="launch('test_flash')">Тесты + прошивка</button>
        <button class="warn" :disabled="busy" @click="launch('flash_only')">Только прошивка</button>
        <button class="danger" :disabled="busy" @click="stop">Остановить</button>
      </div>
      <div class="group">
        <button
          :disabled="busy"
          title="Заново ждать загрузку с SD-NAND у прошитых плат, где она не подтвердилась (не успели вынуть microSD)"
          @click="bootcheck"
        >
          Только BOOTCHECK
        </button>
        <button v-if="anyLed" :disabled="busy" @click="identify">Идентифицировать</button>
        <button :disabled="busy" @click="benchCheck">Проверка стенда</button>
        <button :disabled="batchId === null" @click="exportCsv">Экспорт CSV</button>
      </div>
      <div class="group selrow">
        <button @click="selectAllOnline">Выбрать все онлайн</button>
        <button :disabled="!selected.size" @click="clearSelection">Снять выбор</button>
        <button
          :disabled="busy"
          title="Убрать со стенда платы без связи (их итоги остаются в истории) и заново опросить подключённые платы: IP, MAC, тип платы"
          @click="refreshGrid"
        >
          Скрыть офлайн, обнаружить онлайн платы
        </button>
        <span class="selcount" :class="{ some: selected.size }">выбрано: {{ selected.size }}</span>
        <label v-if="selected.size" class="chk" title="Если стенд определил тип платы неверно. «Авто» — снова по подключению: встроенный Ethernet — NAPI-C, USB-адаптер — NAPI-P.">
          Тип платы:
          <select value="" @change="setBoardSelected">
            <option value="" disabled>задать…</option>
            <option value="auto">авто</option>
            <option value="napi-c">NAPI-C</option>
            <option value="napi-p">NAPI-P</option>
          </select>
        </label>
        <span class="hint">щелчок по плате — выбрать или снять · Shift+щелчок — все подряд</span>
      </div>
      <div class="group opts">
        <label class="chk" title="Запись и сверка всей SD-NAND (3,9 ГБ). Без галочки: 3 участка по 64 МБ, ~40 с. В «Тесты + прошивка» SD-NAND проверяется целиком всегда.">
          <input v-model="fullPattern" type="checkbox" /> Тотальная проверка SD-NAND (+12 мин, режим «Только тесты»)
        </label>
        <label class="chk" title="memtester на 256 МБ. Без галочки: 64 МБ, ~2 мин. Все 512 МБ ОЗУ при каждой загрузке проверяет ядро (memtest).">
          <input v-model="fullMemtester" type="checkbox" /> Тотальная проверка памяти (+9 мин)
        </label>
        <label class="chk" title="Дополнительно TCP iperf3 по 10 с в каждую сторону: реальная пропускная способность. Без галочки: UDP 90 Мбит/с по 3 с — потери и ошибки порта.">
          <input v-model="fullNet" type="checkbox" :disabled="skipNet" /> Тотальная проверка сети (+30 с, нагружает сеть)
        </label>
        <label class="chk" title="Этап NET пропускается (серым «NET пропущен»): например, пока сеть стенда до сервера временно плохая (Wi-Fi). Пропуск записывается в историю и CSV.">
          <input v-model="skipNet" type="checkbox" /> Без проверки сети
        </label>
        <label class="chk" title="Без BOOTCHECK плата получает итог сразу после прошивки: не нужно выключать её, вынимать microSD и ждать до 10 мин. Записанный образ всё равно сверяется контрольной суммой.">
          <input v-model="skipBoot" type="checkbox" /> Не проверять загрузку с SD-NAND (BOOTCHECK)
        </label>
        <label class="chk" title="Если стенд определил тип неверно (например, NAPI-C подключили через USB-адаптер). Выбор запоминается у плат партии; NAPI-P — сеть не проверяется.">
          Тип плат в партии:
          <select v-model="batchBoard">
            <option value="">как определено</option>
            <option value="napi-c">все NAPI-C</option>
            <option value="napi-p">все NAPI-P</option>
          </select>
        </label>
      </div>
      <div class="group os">
        <label class="ossel">
          ОС для внутреннего накопителя:
          <select v-model="osKey">
            <option v-for="m in osMenu" :key="m.key" :value="m.key">{{ m.name }}</option>
          </select>
        </label>
        <label v-if="osItem?.kind === 'zero'" class="chk" title="Без галочки: нули в первые 64 МБ, секунды. С галочкой: вся SD-NAND, несколько минут.">
          <input v-model="zeroFull" type="checkbox" /> полный
        </label>
        <button v-if="osItem?.kind === 'custom'" :disabled="busy" @click="customError = null; customDialog = true">
          Ссылка на образ клиента…
        </button>
        <span class="osline" :class="osInfo.tone">{{ osInfo.text }}</span>
        <router-link to="/images" class="oslink">Образы</router-link>
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

    <div v-if="message" class="message" :class="{ error: message.error, warn: message.warn }" @click="message = null">
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
      :cancel-text="pending.cancelText"
      :alt-text="pending.alt?.text"
      @ok="confirmOk"
      @alt="confirmAlt"
      @cancel="pending = null"
    />
    <PasswordDialog
      v-if="customDialog"
      title="Custom: образ клиента"
      message="Одноразовый образ: в список «Образы» не сохраняется. Сервер скачает его, проверит, что это образ всего диска, и раздаст на карты между партиями. Прежний Custom удаляется."
      ask-url
      ok-text="Подготовить"
      :error="customError"
      @ok="saveCustom"
      @cancel="customDialog = false"
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
.selcount.some {
  color: var(--select-strong);
  font-weight: 700;
}
.selrow {
  flex-basis: 100%;
  padding: 6px 8px;
  background: #f3f6fb;
  border-radius: 6px;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}
.opts {
  flex-basis: 100%;
  gap: 4px 18px;
}
.opts .chk {
  margin-left: 0;
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
.os {
  font-size: 13px;
}
.ossel {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.ossel select {
  font: inherit;
  font-weight: 400;
  padding: 5px 8px;
  border-radius: 6px;
  border: 1px solid #c9cdd3;
  background: #fff;
  max-width: 260px;
}
.osline {
  color: var(--muted);
  overflow-wrap: anywhere;
}
.osline.warn {
  color: #9a6700;
}
.osline.error {
  color: #8a4b00;
  font-weight: 600;
}
.oslink {
  font-size: 13px;
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
.message.active {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 15px;
}
.message.warn {
  background: #fff6e0;
  border-color: #f0d27a;
  color: #8a4b00;
  white-space: pre-line;
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
