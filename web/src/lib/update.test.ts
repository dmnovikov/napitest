import { describe, expect, it } from 'vitest'
import { bundleOf } from './update'

describe('update hint', () => {
  it('finds the main bundle in index.html', () => {
    const html = '<head><script type="module" crossorigin src="/assets/index-BM6iOR1J.js"></script></head>'
    expect(bundleOf(html)).toBe('/assets/index-BM6iOR1J.js')
    expect(bundleOf(html.replace('/assets', './assets'))).toBe('/assets/index-BM6iOR1J.js')
    expect(bundleOf('<script type="module" src="/src/main.ts"></script>')).toBe('')
  })
})
