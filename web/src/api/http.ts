import type {
  AuditRow,
  BatchDetail,
  BatchRow,
  DeviceDetail,
  DeviceView,
  ImageHistoryRow,
  ImagesInfo,
  SdCardInfo,
  BoardType,
  Mode,
  PublicStatus,
  Summary,
} from './types'

export const API = '/api/v1'

export class HttpError extends Error {
  constructor(public status: number, message: string, public extra: unknown = null) {
    super(message)
  }
}

let onUnauthorized: () => void = () => {}
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(API + path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401) {
    onUnauthorized()
    throw new HttpError(401, 'Требуется вход')
  }
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!res.ok) {
    const d = data as { detail?: unknown; extra?: unknown } | null
    const msg = d && typeof d === 'object' && 'detail' in d ? String(d.detail) : `HTTP ${res.status}`
    throw new HttpError(res.status, msg, d && typeof d === 'object' ? (d.extra ?? null) : null)
  }
  return data as T
}

export const api = {
  login: (username: string, password: string) =>
    request<unknown>('POST', '/auth/login', { username, password }),
  logout: () => request<unknown>('POST', '/auth/logout', {}),
  setExpected: (count: number) => request<{ hidden?: number }>('PUT', '/expected', { count }),
  me: () => request<{ username: string }>('GET', '/auth/me'),
  state: () => request<{ devices: DeviceView[]; summary: Summary }>('GET', '/state'),
  startBatch: (body: {
    mode: Mode
    device_ids: number[]
    confirm: boolean
    kind: 'production' | 'golden'
    options?: {
      sdnand_pattern?: 'quick' | 'full'
      memtester?: 'quick' | 'full'
      net?: 'quick' | 'full'
      boot?: 'check' | 'skip'
      os?: string
      zero?: 'quick' | 'full'
      allow_retest?: boolean
      /** «Без проверки сети» */
      net_skip?: boolean
      /** Тип плат для всей партии (запоминается у плат); не задан — как определено у каждой */
      board?: BoardType
    }
  }) => request<{ warnings?: string[] }>('POST', '/batches', body),
  retryBoot: (device_ids: number[]) => request<{ jobs: number }>('POST', '/devices/bootcheck', { device_ids }),
  stop: (device_ids: number[]) => request<unknown>('POST', '/stop', { device_ids }),
  stopBatch: (id: number, device_ids: number[] = []) =>
    request<unknown>('POST', `/batches/${id}/stop`, { device_ids }),
  batches: () => request<BatchRow[]>('GET', '/batches'),
  batch: (id: number) => request<BatchDetail>('GET', `/batches/${id}`),
  device: (id: number) => request<DeviceDetail>('GET', `/devices/${id}`),
  refreshGrid: () =>
    request<{ hidden: number; kept_busy: string; reconnect: number }>('POST', '/devices/refresh', {}),
  identify: (id: number) => request<unknown>('POST', `/devices/${id}/identify`, {}),
  setGolden: (id: number, is_golden: boolean) =>
    request<unknown>('POST', `/devices/${id}/golden`, { is_golden }),
  audit: () => request<AuditRow[]>('GET', '/audit'),
  images: () => request<ImagesInfo & { history: ImageHistoryRow[] }>('GET', '/images'),
  addImage: (name: string, url: string, password: string) =>
    request<{ key: string }>('POST', '/images', { name, url, password }),
  editImage: (
    key: string,
    password: string,
    change: { url?: string; name?: string; visible?: boolean; move?: number },
  ) => request<unknown>('PUT', `/images/${encodeURIComponent(key)}`, { password, ...change }),
  setCustom: (url: string, password: string) => request<unknown>('POST', '/images/custom', { url, password }),
  setBoard: (device_ids: number[], board: BoardType | null) =>
    request<{ warnings: string[] }>('PUT', '/devices/board', { device_ids, board }),
  sdcard: () => request<SdCardInfo>('GET', '/sdcard'),
  publicStatus: () => request<PublicStatus>('GET', '/public'),
  sdRebuild: (password: string, url?: string) =>
    request<unknown>('POST', '/sdcard', { password, url: url || null }),
}

export const links = {
  sdDownload: `${API}/sdcard/download`,
  label: (id: number) => `${API}/devices/${id}/label`,
  batchCsv: (id: number) => `${API}/batches/${id}/export.csv`,
  batchJson: (id: number) => `${API}/batches/${id}/export.json`,
}
