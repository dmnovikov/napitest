import type { RetestItem } from '../api/types'

/** Список уже проверявшихся плат из ошибки 409 запуска партии, иначе null. */
export function retestList(err: unknown): RetestItem[] | null {
  if (!err || typeof err !== 'object') return null
  const e = err as { status?: unknown; extra?: unknown }
  if (e.status !== 409 || !e.extra || typeof e.extra !== 'object' || Array.isArray(e.extra)) return null
  const list = (e.extra as { retest?: unknown }).retest
  if (!Array.isArray(list) || !list.length) return null
  return list as RetestItem[]
}
