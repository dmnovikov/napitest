import type { DeviceView, JobView, Mode, StageName, StageSnap, StageState, Tile } from '../api/types'
import { STAGES } from '../api/types'

export const STAGE_LABEL: Record<StageName, string> = {
  ram: 'RAM',
  net: 'NET',
  sdnand: 'SD',
  flash: 'FLASH',
  boot: 'BOOT',
}

export const STAGE_LONG: Record<StageName, string> = {
  ram: 'RAM',
  net: 'NET',
  sdnand: 'SD-NAND',
  flash: 'FLASH',
  boot: 'BOOT',
}

export const MODE_LABEL: Record<Mode, string> = {
  test_only: 'Только тесты',
  test_flash: 'Тесты + прошивка',
  flash_only: 'Только прошивка',
}

export const STATE_LABEL: Record<StageState, string> = {
  pending: 'ожидает',
  running: 'идёт',
  pass: 'PASS',
  fail: 'FAIL',
  skipped: 'пропущен',
  error: 'ошибка',
  interrupted: 'прерван',
}

export const TILE_LABEL: Record<Tile, string> = {
  ready: 'готов',
  busy: 'в работе',
  awaiting_boot: 'ожидание загрузки',
  pass: 'PASS',
  fail: 'FAIL',
  offline: 'нет связи',
  interrupted: 'прервано',
  incomplete: 'не выполнено',
}

export const JOB_STATUS_LABEL: Record<string, string> = {
  queued: 'в очереди',
  running: 'выполняется',
  awaiting_boot: 'ожидание загрузки',
  pass: 'PASS',
  fail: 'FAIL',
  interrupted: 'прервано',
  cancelled: 'отменено',
  offline: 'нет связи',
  incomplete: 'не выполнено',
}

const KNOWN_STATES: StageState[] = ['pending', 'running', 'pass', 'fail', 'skipped', 'error', 'interrupted']

/** CSS-класс сегмента полоски этапов. */
export function stageClass(state: string | null | undefined): string {
  const s = (state ?? 'pending') as StageState
  return 'st-' + (KNOWN_STATES.includes(s) ? s : 'pending')
}

/** CSS-класс рамки квадрата. Нет связи никогда не даёт красный. */
export function tileClass(tile: Tile | string | null | undefined): string {
  switch (tile) {
    case 'ready':
    case 'busy':
    case 'awaiting_boot':
    case 'pass':
    case 'fail':
    case 'offline':
    case 'interrupted':
      return 'tile-' + tile
    default:
      return 'tile-ready'
  }
}

/** Длительность в секундах → «MM:SS» или «H:MM:SS». */
export function formatDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || !Number.isFinite(sec)) return '--:--'
  const neg = sec < 0
  let s = Math.floor(Math.abs(sec))
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  s -= m * 60
  const p = (n: number) => String(n).padStart(2, '0')
  const body = h > 0 ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`
  return neg ? '-' + body : body
}

/** ISO-время → локальное «ДД.ММ.ГГГГ ЧЧ:ММ:СС». */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** Сколько секунд прошло с момента iso до nowMs. */
export function secondsSince(iso: string | null | undefined, nowMs: number): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  return Math.max(0, (nowMs - t) / 1000)
}

/** «3 с назад», «5 мин назад». */
export function formatAgo(iso: string | null | undefined, nowMs: number): string {
  const s = secondsSince(iso, nowMs)
  if (s === null) return 'никогда'
  if (s < 60) return `${Math.floor(s)} с назад`
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`
  return `${Math.floor(s / 86400)} дн назад`
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 2)} ${units[i]}`
}

/** Хвост для квадрата: последние 4 символа серийника, иначе cpuid_tail. */
export function tileTag(d: Pick<DeviceView, 'serial' | 'cpuid_tail'>): string {
  if (d.serial) return d.serial.slice(-4)
  return d.cpuid_tail ?? '????'
}

/** Текст строки текущего этапа квадрата. */
export function currentStageLine(job: JobView | null, tile: Tile): string {
  if (!job) return tile === 'offline' ? '' : 'нет задания'
  const st = job.stages ?? {}
  for (const name of STAGES) {
    const s = st[name]
    if (s && (s.state === 'fail' || s.state === 'error')) {
      return s.detail ? s.detail : `${STAGE_LONG[name]}: ${STATE_LABEL[s.state]}`
    }
  }
  for (const name of STAGES) {
    const s = st[name]
    if (s && s.state === 'interrupted') return s.detail || `${STAGE_LONG[name]}: прерван`
  }
  if (job.status === 'incomplete') {
    // Тест не выполнялся (предохранитель), это не дефект платы: показываем причину без красного
    for (const name of STAGES) {
      const s = st[name]
      if (s && s.blocked) return s.detail || `${STAGE_LONG[name]}: не выполнен`
    }
    return job.detail || 'не выполнено'
  }
  // Текст ожидания BOOT задаёт сервер: что сделать оператору и что видно в сети
  if (job.status === 'awaiting_boot') return st.boot?.detail ? `BOOT: ${st.boot.detail}` : 'BOOT: ожидание загрузки'
  for (const name of STAGES) {
    const s = st[name]
    if (s && s.state === 'running') return runningText(name, s)
  }
  if (job.status === 'queued') return 'в очереди'
  return JOB_STATUS_LABEL[job.status] ?? job.status
}

function runningText(name: StageName, s: StageSnap): string {
  const pct = typeof s.pct === 'number' ? ` ${Math.round(s.pct)} %` : ''
  if (!pct && s.detail) return `${STAGE_LONG[name]}: ${s.detail}`
  return `${STAGE_LONG[name]}${pct}`
}

/** Таймер квадрата: время от старта, для awaiting_boot — сколько осталось до таймаута. */
export function tileTimer(job: JobView | null, nowMs: number): { text: string; countdown: boolean } {
  if (!job) return { text: '', countdown: false }
  if (job.status === 'awaiting_boot' && job.boot_deadline) {
    const left = (Date.parse(job.boot_deadline) - nowMs) / 1000
    return { text: formatDuration(Math.max(0, left)), countdown: true }
  }
  if (job.finished_at && job.started_at) {
    return {
      text: formatDuration((Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000),
      countdown: false,
    }
  }
  const s = secondsSince(job.started_at, nowMs)
  if (s !== null) return { text: formatDuration(s), countdown: false }
  return { text: formatDuration(job.elapsed), countdown: false }
}

export function formatMeasurement(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** Есть ли у этапа замечания (текст из события или флаг из heartbeat). */
export function hasWarn(
  s: { warnings?: string[] | null; warn?: boolean | null } | null | undefined,
): boolean {
  if (!s) return false
  return (Array.isArray(s.warnings) && s.warnings.length > 0) || s.warn === true
}

/** Все замечания задания по этапам: «NET: …». */
export function jobWarnings(job: JobView | null | undefined): string[] {
  if (!job) return []
  const out: string[] = []
  for (const name of STAGES) {
    const s = job.stages?.[name]
    for (const w of s?.warnings ?? []) out.push(w.startsWith(STAGE_LONG[name]) || w.startsWith(STAGE_LABEL[name]) ? w : `${STAGE_LONG[name]}: ${w}`)
  }
  return out
}

/** Показывать ли ⚠ у PASS-квадрата. */
export function jobHasWarn(job: JobView | null | undefined): boolean {
  if (!job) return false
  if ((job.warnings_count ?? 0) > 0) return true
  return STAGES.some((n) => hasWarn(job.stages?.[n]))
}
