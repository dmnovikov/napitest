import { describe, expect, it } from 'vitest'
import {
  STAGE_LABEL,
  currentStageLine,
  previousText,
  shownStages,
  formatDuration,
  osLine,
  formatBytes,
  hasWarn,
  jobHasWarn,
  jobWarnings,
  stageClass,
  tileClass,
  tileTag,
  tileTimer,
} from './format'
import type { ImagesInfo, JobView, OsImage } from '../api/types'

const job = (over: Partial<JobView> = {}): JobView => ({
  job_id: 'j1',
  batch_id: 1,
  mode: 'test_flash',
  status: 'running',
  started_at: '2026-10-10T12:00:00Z',
  finished_at: null,
  elapsed: 0,
  boot_deadline: null,
  stages: {},
  result: null,
  ...over,
})

describe('stageClass', () => {
  it('maps known states', () => {
    expect(stageClass('pass')).toBe('st-pass')
    expect(stageClass('running')).toBe('st-running')
    expect(stageClass('skipped')).toBe('st-skipped')
    expect(stageClass('interrupted')).toBe('st-interrupted')
  })
  it('falls back to pending', () => {
    expect(stageClass(undefined)).toBe('st-pending')
    expect(stageClass('bogus')).toBe('st-pending')
  })
})

describe('tileClass', () => {
  it('offline is never fail', () => {
    expect(tileClass('offline')).toBe('tile-offline')
    expect(tileClass('fail')).toBe('tile-fail')
    expect(tileClass('nonsense')).toBe('tile-ready')
  })
})

describe('formatDuration', () => {
  it('formats mm:ss and h:mm:ss', () => {
    expect(formatDuration(0)).toBe('00:00')
    expect(formatDuration(134)).toBe('02:14')
    expect(formatDuration(3725)).toBe('1:02:05')
    expect(formatDuration(null)).toBe('--:--')
  })
})

describe('formatBytes', () => {
  it('uses binary units', () => {
    expect(formatBytes(536870912)).toBe('512.00 МиБ')
    expect(formatBytes(512)).toBe('512 Б')
  })
})

describe('tile helpers', () => {
  it('tag prefers serial', () => {
    expect(tileTag({ serial: 'NC-2610-0017', cpuid_tail: 'A3F2' })).toBe('0017')
    expect(tileTag({ serial: null, cpuid_tail: 'A3F2' })).toBe('A3F2')
  })
  it('stage line shows fail detail first', () => {
    const j = job({
      stages: { ram: { state: 'fail', detail: 'RAM: 2 err' }, net: { state: 'running' } },
    })
    expect(currentStageLine(j, 'busy')).toBe('RAM: 2 err')
  })
  it('stage line shows progress', () => {
    const j = job({ stages: { ram: { state: 'pass' }, sdnand: { state: 'running', pct: 43 } } })
    expect(currentStageLine(j, 'busy')).toBe('SD-NAND 43 %')
  })
  it('awaiting boot shows server instruction', () => {
    const j = job({ status: 'awaiting_boot', stages: { boot: { state: 'running', detail: 'ждём загрузки с SD-NAND' } } })
    expect(currentStageLine(j, 'awaiting_boot')).toBe('BOOTCHECK: ждём загрузки с SD-NAND')
    expect(currentStageLine(job({ status: 'awaiting_boot' }), 'awaiting_boot')).toBe('BOOTCHECK: ожидание загрузки')
  })
  it('previous result line', () => {
    const p = { job_id: 'j', status: 'pass', mode: 'test_flash' as const, finished_at: '2026-10-11T14:32:00', batch: 'B', detail: null, warnings_count: 1 }
    expect(previousText(p)).toBe('уже проверялась: PASS ⚠ · 11.10 14:32 · Тесты + прошивка')
    expect(previousText({ ...p, status: 'fail', detail: 'NET: tx 41' })).toBe(
      'уже проверялась: FAIL (NET: tx 41) · 11.10 14:32 · Тесты + прошивка',
    )
  })
  it('CHECKB shown only when part of the job', () => {
    expect(shownStages(null)).not.toContain('boot')
    expect(shownStages(job({ stages: { boot: { state: 'skipped' } } }))).not.toContain('boot')
    expect(shownStages(job({ stages: { boot: { state: 'pending' } } }))).toContain('boot')
    const unconfirmed = { state: 'skipped' as const, warnings: ['BOOTCHECK: загрузка не подтверждена'] }
    expect(shownStages(job({ stages: { boot: unconfirmed } }))).toContain('boot')
  })
  it('timer counts from start', () => {
    const now = Date.parse('2026-10-10T12:02:14Z')
    expect(tileTimer(job(), now).text).toBe('02:14')
  })
  it('timer counts down for awaiting_boot', () => {
    const now = Date.parse('2026-10-10T12:00:00Z')
    const t = tileTimer(job({ status: 'awaiting_boot', boot_deadline: '2026-10-10T12:10:00Z' }), now)
    expect(t).toEqual({ text: '10:00', countdown: true })
  })
})

describe('warnings', () => {
  it('hasWarn by text or heartbeat flag', () => {
    expect(hasWarn(undefined)).toBe(false)
    expect(hasWarn({ warnings: [] })).toBe(false)
    expect(hasWarn({ warnings: null, warn: false })).toBe(false)
    expect(hasWarn({ warnings: ['TCP rx 36 Mbit/s'] })).toBe(true)
    expect(hasWarn({ warn: true })).toBe(true)
  })
  it('job-level ⚠ and prefixed texts', () => {
    expect(jobHasWarn(job())).toBe(false)
    expect(jobHasWarn(job({ warnings_count: 1 }))).toBe(true)
    const j = job({ stages: { net: { state: 'pass', warnings: ['TCP rx 36 Mbit/s'] } } })
    expect(jobHasWarn(j)).toBe(true)
    expect(jobWarnings(j)).toEqual(['NET: TCP rx 36 Mbit/s'])
    expect(jobWarnings(job({ stages: { net: { state: 'pass', warnings: ['NET: x'] } } }))).toEqual(['NET: x'])
  })
})

describe('incomplete', () => {
  it('блокировка предохранителем показывается причиной, а не FAIL', async () => {
    const { currentStageLine } = await import('./format')
    const job = {
      job_id: 'j', batch_id: 1, mode: 'test_only', status: 'incomplete', started_at: null, finished_at: null,
      elapsed: 1, boot_deadline: null, result: 'incomplete', detail: null,
      stages: { ram: { state: 'pass' }, net: { state: 'pass' },
        sdnand: { state: 'skipped', blocked: true, detail: 'SD: запись запрещена (profile not validated)' } },
    } as any
    expect(currentStageLine(job, 'incomplete')).toBe('SD: запись запрещена (profile not validated)')
  })
})

describe('osLine', () => {
  const img = (over: Partial<OsImage> = {}): OsImage => ({
    id: 1, url: 'https://x/a.img.xz', state: 'ready', detail: null, file: 'armbian-1.img.zst',
    source_file: 'Armbian_2026.09.img.xz', version: '2026.09', image_size: 1, image_sha256: 'abcdef0123456789',
    compressed_size: 1, source_sha256: null, operator: null, created_at: '', ready_at: '', ...over,
  })
  const info = (over: Partial<ImagesInfo['variants'][0]> = {}): ImagesInfo => ({
    menu: [], sync: true,
    variants: [{ key: 'armbian', name: 'Armbian', kind: 'url', visible: true, current: img(), next: null,
      cards: { ready: 2, online: 2 }, ...over }],
  })
  const armbian = { key: 'armbian', name: 'Armbian', kind: 'url' as const }

  it('ready on all cards', () => {
    expect(osLine(armbian, info())).toEqual({
      text: 'Armbian_2026.09.img.xz · sha abcdef012345 · на картах ✓', tone: 'ok' })
  })
  it('not yet on every card', () => {
    const l = osLine(armbian, info({ cards: { ready: 1, online: 3 } }))
    expect(l.tone).toBe('warn')
    expect(l.text).toContain('на картах 1 из 3, обновляются')
  })
  it('first download in progress', () => {
    const l = osLine(armbian, info({ current: null, next: img({ state: 'working', detail: 'скачивание 5 из 600 МБ', pct: 0.8 }) }))
    expect(l).toEqual({ text: 'образ готовится: скачивание 5 из 600 МБ, 0 %', tone: 'warn' })
  })
  it('error and builtins', () => {
    expect(osLine(armbian, info({ current: null, next: img({ state: 'error', detail: 'HTTP 404' }) })).tone).toBe('error')
    expect(osLine({ key: 'zero', name: 'Zeroimage', kind: 'zero' }, null, true).text).toContain('всю SD-NAND')
    expect(osLine({ key: 'custom', name: 'Custom', kind: 'custom' }, info()).text).toBe('укажите ссылку на образ клиента')
  })
})

describe('boardBadge', () => {
  it('shows detected type and marks manual mismatch', async () => {
    const { boardBadge } = await import('./format')
    const usb = { iface: 'enx00e04c680001', bus: 'usb', driver: 'r8152', mac: null }
    expect(boardBadge({ type: 'napi-p', detected: 'napi-p', override: null, uplink: usb })).toEqual({
      text: 'NAPI-P',
      manual: false,
      tone: 'auto',
      title: 'NAPI-P: распознан по подключению (enx00e04c680001 (r8152))',
    })
    const m = boardBadge({ type: 'napi-c', detected: 'napi-p', override: 'napi-c', uplink: usb })
    expect(m?.text).toBe('NAPI-C✎')
    expect(m?.title).toContain('Стенд определил: NAPI-P')
    expect(m?.tone).toBe('manual')
    expect(boardBadge({ type: 'napi-c', detected: null, override: null, uplink: null })?.text).toBe('тип ?')
    expect(boardBadge(undefined)).toBeNull()
  })

  it('summarises recognition for the top panel', async () => {
    const { boardsSummary } = await import('./format')
    const c = { type: 'napi-c' as const, detected: 'napi-c' as const, override: null, uplink: null }
    const p = { type: 'napi-p' as const, detected: 'napi-p' as const, override: null, uplink: null }
    const u = { type: 'napi-c' as const, detected: null, override: null, uplink: null }
    expect(boardsSummary([{ online: true, display_no: 1, board: c }, { online: true, display_no: 2, board: p }]))
      .toEqual({ text: 'Тип платы распознан у всех: NAPI-C: 1, NAPI-P: 1', tone: 'ok' })
    const w = boardsSummary([{ online: true, display_no: 1, board: c }, { online: true, display_no: 3, board: u }])
    expect(w?.tone).toBe('warn')
    expect(w?.text).toContain('не распознан: 1 из 2 (№3)')
    expect(boardsSummary([])).toBeNull()
  })
})

describe('etaText', () => {
  it('shows remaining minutes for active jobs only', async () => {
    const { etaText } = await import('./format')
    const j = (status: string, eta_s: number, eta_partial = false) => ({ status, eta_s, eta_partial }) as never
    expect(etaText(j('running', 430))).toBe('осталось ~8 мин')
    expect(etaText(j('running', 40))).toBe('осталось <1 мин')
    expect(etaText(j('running', 300, true))).toBe('осталось ≥5 мин')
    expect(etaText(j('pass', 300))).toBe('')
  })
})

describe('remarks 2026-10-21', () => {
  it('SD-NAND stage is labelled NAND, not SD (confused with the test microSD)', () => {
    expect(STAGE_LABEL.sdnand).toBe('NAND')
  })
  it('NET fail on a flashed board says it is flashed', () => {
    const j = job({ status: 'fail', stages: { net: { state: 'fail', detail: 'NET: UDP tx 48 Mbit/s<81' }, flash: { state: 'pass' } } })
    expect(currentStageLine(j, 'fail')).toBe('NET: UDP tx 48 Mbit/s<81 · прошита')
    const k = job({ status: 'fail', stages: { net: { state: 'fail', detail: 'NET: x' }, flash: { state: 'skipped' } } })
    expect(currentStageLine(k, 'fail')).toBe('NET: x')
  })
  it('stand network tile is grey, not red', () => {
    expect(tileClass('net_stand')).toBe('tile-net_stand')
  })
})
