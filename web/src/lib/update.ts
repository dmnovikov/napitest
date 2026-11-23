import { ref } from 'vue'

/** Стенд обновили, а в браузере открыта прежняя страница: показываем подсказку «обновите страницу». */
export const updateReady = ref(false)

/** Путь главного скрипта из текста index.html (сборка даёт ему новое имя при каждом изменении). */
export function bundleOf(html: string): string {
  const m = /<script[^>]+src="([^"]*assets\/index-[^"]+\.js)"/.exec(html)
  return m ? m[1].replace(/^\.?\/?/, '/') : ''
}

function currentBundle(): string {
  const s = document.querySelector<HTMLScriptElement>('script[type="module"][src*="assets/index-"]')
  return s ? new URL(s.src, location.href).pathname : ''
}

export async function checkUpdate(): Promise<void> {
  const mine = currentBundle()
  if (!mine || updateReady.value) return // режим разработки или подсказка уже показана
  try {
    const res = await fetch('/', { cache: 'no-store' })
    if (!res.ok) return
    const theirs = bundleOf(await res.text())
    if (theirs && theirs !== mine) updateReady.value = true
  } catch {
    /* сервер перезапускается: проверим в следующий раз */
  }
}

let timer: number | undefined
export function startUpdateWatch(): void {
  if (timer !== undefined) return
  timer = window.setInterval(() => void checkUpdate(), 60000)
}
