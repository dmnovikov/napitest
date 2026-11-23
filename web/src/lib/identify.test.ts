import { describe, expect, it } from 'vitest'
import { clampSeconds } from './identify'

describe('identify seconds', () => {
  it('clamps to 5..600, default 30', () => {
    expect(clampSeconds(45)).toBe(45)
    expect(clampSeconds(1)).toBe(5)
    expect(clampSeconds(9999)).toBe(600)
    expect(clampSeconds('')).toBe(5)
    expect(clampSeconds('abc')).toBe(30)
  })
})
