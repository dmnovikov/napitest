import { describe, expect, it } from 'vitest'
import { HttpError } from '../api/http'
import { retestList } from './retest'

const item = {
  device_id: 51, serial: 'NC-2610-0001', cpuid: '9e098bbcae8b6599', last_job_id: 'j_1',
  last_mode: 'test_only', last_status: 'pass', last_batch: 'B-20261010-01', last_date: '2026-10-10T19:00:00Z', runs: 2,
}

describe('retestList', () => {
  it('достаёт список из 409 «уже проверялась»', () => {
    expect(retestList(new HttpError(409, 'часть плат уже проверялась', { retest: [item] }))).toEqual([item])
  })
  it('409 «активное задание» (extra — массив id) не путает с повтором', () => {
    expect(retestList(new HttpError(409, 'активное задание', [51]))).toBeNull()
  })
  it('другие ошибки — null', () => {
    expect(retestList(new HttpError(400, 'x', { retest: [item] }))).toBeNull()
    expect(retestList(new Error('x'))).toBeNull()
    expect(retestList(new HttpError(409, 'x', { retest: [] }))).toBeNull()
  })
})
