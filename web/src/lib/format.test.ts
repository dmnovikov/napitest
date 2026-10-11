import { describe, expect, it } from 'vitest'
import {
  currentStageLine,
  formatDuration,
  formatBytes,
  hasWarn,
  jobHasWarn,
  jobWarnings,
  stageClass,
  tileClass,
  tileTag,
  tileTimer,
} from './format'
import type { JobView } from '../api/types'

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
    expect(currentStageLine(j, 'awaiting_boot')).toBe('BOOT: ждём загрузки с SD-NAND')
    expect(currentStageLine(job({ status: 'awaiting_boot' }), 'awaiting_boot')).toBe('BOOT: ожидание загрузки')
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
