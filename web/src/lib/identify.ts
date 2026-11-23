/** Сколько секунд горит светодиод по кнопке «Идентифицировать»: значение оператора помнит браузер. */
export const IDENTIFY_DEFAULT = 30
export const IDENTIFY_MIN = 5
export const IDENTIFY_MAX = 600
const KEY = 'napi.identifySeconds'

export function clampSeconds(v: unknown): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return IDENTIFY_DEFAULT
  return Math.min(IDENTIFY_MAX, Math.max(IDENTIFY_MIN, n))
}

export function loadSeconds(): number {
  try {
    const v = localStorage.getItem(KEY)
    return v === null ? IDENTIFY_DEFAULT : clampSeconds(v)
  } catch {
    return IDENTIFY_DEFAULT
  }
}

export function saveSeconds(v: number): void {
  try {
    localStorage.setItem(KEY, String(v))
  } catch {
    /* без хранилища значение живёт до перезагрузки страницы */
  }
}
