import { reactive } from 'vue'
import type { DeviceView, Summary } from './types'

export const live = reactive({
  devices: new Map<number, DeviceView>(),
  summary: null as Summary | null,
  connected: false,
  /** Смещение часов сервера относительно браузера, мс (server - local). */
  clockSkew: 0,
})

let ws: WebSocket | null = null
let retry = 0
let timer: number | undefined
let stopped = true

function applySummary(s: Summary) {
  live.summary = s
  const t = Date.parse(s.server_time)
  if (!Number.isNaN(t)) live.clockSkew = t - Date.now()
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${proto}//${location.host}/ws/ui`)
  ws.onopen = () => {
    live.connected = true
    retry = 0
  }
  ws.onmessage = (ev) => {
    let msg: { type: string; devices?: DeviceView[]; summary?: Summary }
    try {
      msg = JSON.parse(ev.data)
    } catch {
      return
    }
    if (msg.type === 'snapshot') {
      live.devices = new Map((msg.devices ?? []).map((d) => [d.device_id, d]))
      if (msg.summary) applySummary(msg.summary)
    } else if (msg.type === 'devices') {
      for (const d of msg.devices ?? []) live.devices.set(d.device_id, d)
    } else if (msg.type === 'summary' && msg.summary) {
      applySummary(msg.summary)
    }
  }
  ws.onclose = () => {
    live.connected = false
    ws = null
    if (stopped) return
    const delay = Math.min(10000, 500 * 2 ** retry)
    retry++
    timer = window.setTimeout(connect, delay)
  }
  ws.onerror = () => ws?.close()
}

export function startLive() {
  if (!stopped) return
  stopped = false
  connect()
}

export function stopLive() {
  stopped = true
  if (timer) clearTimeout(timer)
  ws?.close()
}

/** «Текущее» время с поправкой на часы сервера. */
export function nowMs(): number {
  return Date.now() + live.clockSkew
}
