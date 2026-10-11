export type StageName = 'ram' | 'net' | 'sdnand' | 'flash' | 'boot'
export type StageState = 'pending' | 'running' | 'pass' | 'fail' | 'skipped' | 'error' | 'interrupted'
export type Mode = 'test_only' | 'test_flash' | 'flash_only'
export type Tile = 'ready' | 'busy' | 'awaiting_boot' | 'pass' | 'fail' | 'offline' | 'interrupted' | 'incomplete'
export type JobStatus =
  | 'queued' | 'running' | 'awaiting_boot' | 'pass' | 'fail' | 'interrupted' | 'cancelled' | 'offline' | 'incomplete'

export const STAGES: StageName[] = ['ram', 'net', 'sdnand', 'flash', 'boot']

export interface StageSnap {
  state: StageState
  t?: number | null
  pct?: number | null
  detail?: string | null
  /** Замечания: этап пройден, но есть особенности. */
  warnings?: string[] | null
  /** Признак замечаний в heartbeat-снимке (без текста). */
  warn?: boolean | null
  /** этап не выполнялся: запрещён предохранителем (не дефект платы) */
  blocked?: boolean | null
}

export interface JobView {
  job_id: string
  batch_id: number | null
  mode: Mode
  status: JobStatus
  started_at: string | null
  finished_at: string | null
  elapsed: number | null
  boot_deadline: string | null
  stages: Partial<Record<StageName, StageSnap>>
  result: string | null
  detail?: string | null
  warnings_count?: number | null
}

export interface DeviceView {
  device_id: number
  display_no: number
  cpuid: string | null
  cpuid_tail: string | null
  id_source: string | null
  serial: string | null
  online: boolean
  last_seen: string | null
  ip: string | null
  mac: string | null
  mac_source: string | null
  agent_version: string | null
  test_sd_version: string | null
  kernel: string | null
  ram_bytes: number | null
  has_led: boolean
  simulated: boolean
  is_golden: boolean
  tested_before?: boolean
  /** Итог прошлой проверки, если плату перезагрузили после него: квадрат «готова», но уже проверялась. */
  previous?: PreviousResult | null
  tile: Tile
  job: JobView | null
}

export interface BatchInfo {
  batch_id: number
  name: string
  mode: Mode
  kind: 'production' | 'golden'
  operator: string | null
  started_at: string | null
  finished_at: string | null
  golden: string | null
}

export interface Summary {
  total: number
  online: number
  busy: number
  pass: number
  fail: number
  offline: number
  interrupted: number
  incomplete?: number | null
  /** Из PASS — с замечаниями. */
  pass_warn?: number | null
  /** Сколько плат оператор поставил на стенд (0: не задано). */
  expected?: number | null
  batch: BatchInfo | null
  versions: { test_sd: string[]; image: string | null; server: string | null }
  operator: string | null
  server_time: string
}

export interface InventoryDevice {
  name: string
  type?: string | null
  size_bytes?: number | null
  controller?: string | null
  by_path?: string | null
  modalias?: string | null
  removable?: boolean | null
  ro?: boolean | null
  is_boot?: boolean | null
  mounted?: string[] | null
}

export interface Inventory {
  devices: InventoryDevice[]
  target: string | null
  boot_device: string | null
  reasons: string[]
  ok: boolean
}

export interface StageResult {
  stage: StageName
  state: StageState
  t: number | null
  detail: string | null
  measurements: Record<string, unknown> | null
  log: string | null
  warnings?: string[] | null
}

export interface DeviceJob {
  job_id: string
  batch_id: number | null
  mode: Mode
  status: JobStatus
  result: string | null
  started_at: string | null
  finished_at: string | null
  image_version: string | null
  image_sha256: string | null
  stages: StageResult[]
}

export interface DeviceDetail {
  device: DeviceView
  inventory: Inventory | null
  sd_selfcheck: { state?: string; detail?: string } | null
  jobs: DeviceJob[]
}

export interface BatchRow {
  batch_id: number
  name: string
  mode: Mode
  kind: 'production' | 'golden'
  operator: string | null
  started_at: string | null
  finished_at: string | null
  image_version: string | null
  test_sd_versions: string[] | string | null
  pass: number
  fail: number
  total: number
  golden: string | null
}

export interface BatchJob {
  job_id: string
  device_id: number
  serial: string | null
  cpuid: string | null
  status: JobStatus
  result: string | null
  stages: Partial<Record<StageName, { state: StageState; detail?: string | null; warnings?: string[] | null }>>
}

export interface BatchDetail {
  batch: BatchRow
  jobs: BatchJob[]
}

export interface AuditRow {
  ts: string
  operator: string | null
  action: string
  detail: unknown
}

/** Плата, которая уже проверялась (409 на POST /batches, extra.retest). */
export interface RetestItem {
  device_id: number
  serial: string | null
  cpuid: string | null
  last_job_id: string
  last_mode: Mode
  last_status: string
  last_batch: string | null
  last_date: string | null
  runs: number
}

export interface PreviousResult {
  job_id: string
  status: string
  mode: Mode
  finished_at: string | null
  batch: string | null
  detail: string | null
  warnings_count?: number
}
