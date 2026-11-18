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

/** Запуск прошивки: у части плат на карте только прежний образ (новый ещё качается). */
export interface OldImageInfo {
  name: string
  version: string | null
  file: string | null
  old: { no: number; version: string | null; file: string | null; pct: number | null }[]
  new: number[]
}

export function oldImageInfo(err: unknown): OldImageInfo | null {
  if (!err || typeof err !== 'object') return null
  const e = err as { status?: unknown; extra?: unknown }
  if (e.status !== 409 || !e.extra || typeof e.extra !== 'object' || Array.isArray(e.extra)) return null
  const info = (e.extra as { old_image?: unknown }).old_image
  if (!info || typeof info !== 'object' || !Array.isArray((info as OldImageInfo).old)) return null
  return info as OldImageInfo
}

export function oldImageText(i: OldImageInfo): string {
  const cur = `${i.name}${i.version ? ' ' + i.version : ''}`
  const old = i.old
    .map((o) => `№ ${o.no} — ${o.version ?? o.file ?? 'прежняя версия'}` + (o.pct != null ? ` (новый скачан на ${Math.round(o.pct)} %)` : ''))
    .join('\n')
  return (
    `Новый образ ${cur} скачан не на все карты.\n\nПрежним образом будут прошиты:\n${old}\n\n` +
    (i.new.length ? `Новым образом: № ${i.new.join(', ')}.\n\n` : 'Новым образом не будет прошита ни одна плата.\n\n') +
    'Версия образа по каждой плате попадёт в отчёт партии.'
  )
}
