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
  /** Что шили на SD-NAND (меню «ОС для внутреннего накопителя»). */
  os_name?: string | null
  /** Прогноз: сколько секунд ещё идти заданию (только у незавершённых). */
  eta_s?: number | null
  /** Для части этапов ещё нет истории: прогноз занижен. */
  eta_partial?: boolean | null
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
  /** Тип платы: определён по интерфейсу связи с сервером или задан вручную. */
  board?: BoardInfo
}

export type BoardType = 'napi-c' | 'napi-p'

export interface BoardInfo {
  /** По этому типу проверяется плата. */
  type: BoardType
  /** Что определил стенд по интерфейсу связи (null — агент не сообщил). */
  detected: BoardType | null
  /** Задан вручную (null — автоопределение). */
  override: BoardType | null
  uplink: { iface: string; bus: string | null; driver: string | null; mac: string | null } | null
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
  /** Меню «ОС для внутреннего накопителя» и образы. */
  images?: ImagesInfo | null
  /** Партии, у которых ещё идут задания. */
  active?: ActiveBatch[] | null
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
  os_name?: string | null
  image_file?: string | null
  image_url?: string | null
  board_type?: BoardType | null
  board_detected?: BoardType | null
  uplink_iface?: string | null
  stages: StageResult[]
}

export interface DeviceDetail {
  device: DeviceView
  inventory: Inventory | null
  sd_selfcheck: { state?: string; detail?: string } | null
  jobs: DeviceJob[]
}

export type OsKind = 'url' | 'zero' | 'sd' | 'custom'

export interface MenuItem {
  key: string
  name: string
  kind: OsKind
  version?: string | null
}

/** Образ, подготовленный по ссылке (или подготовка новой ссылки). */
export interface OsImage {
  id: number
  url: string
  state: 'queued' | 'working' | 'ready' | 'error' | 'replaced'
  detail: string | null
  pct?: number | null
  file: string | null
  source_file: string | null
  version: string | null
  image_size: number | null
  image_sha256: string | null
  compressed_size: number | null
  source_sha256: string | null
  operator: string | null
  created_at: string
  ready_at: string | null
}

export interface OsVariant {
  key: string
  name: string
  kind: 'url' | 'custom'
  visible: boolean
  current: OsImage | null
  next: OsImage | null
  /** Сколько плат на связи уже имеют текущий образ на microSD. */
  cards?: { ready: number; online: number } | null
}

export interface ImagesInfo {
  menu: MenuItem[]
  variants: OsVariant[]
  /** Карты сейчас могут скачивать образы (нет активной партии). */
  sync: boolean
  agent?: AgentInfo
}

/** Программа на картах (агент): версия на сервере и сколько карт на связи уже с ней. */
export interface AgentInfo {
  version: string
  current: number
  online: number
  errors: { no: number | null; detail: string | null }[]
}

export interface SdCardInfo {
  image: {
    file: string
    sha256: string
    size: number
    image_size: number
    version: string
    agent_version: string
    base_sha256: string
    base_source: string | null
    server_url: string | null
    built_at: string
  } | null
  base: {
    file: string
    source: string | null
    url: string | null
    sha256: string
    size: number
    version?: string | null
    set_at: string
    operator: string | null
  } | null
  agent_version: string
  server_url: string | null
  progress: { stage: string; pct: number | null; text: string } | null
  error: string | null
  /** Агент или настройки новее собранного образа: идёт пересборка. */
  stale: boolean
  agent: AgentInfo
}

export interface ImageHistoryRow extends OsImage {
  variant: string
  key: string
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
  os?: string | null
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
  board_type?: BoardType | null
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

/** Партия с незавершёнными заданиями: предупреждение «идут тесты» и прогноз окончания. */
export interface ActiveBatch {
  batch_id: number
  name: string
  mode: Mode
  kind: 'production' | 'golden'
  started_at: string | null
  operator: string | null
  boards: number
  eta_at: string | null
  /** Для части этапов ещё нет истории: окончание не раньше eta_at. */
  eta_partial?: boolean
}

/** Без входа: что идёт на стенде и образ microSD для инструкции. */
export interface PublicStatus {
  active: ActiveBatch[]
  sd_image: { file: string; size: number; version: string; built_at: string; sha256: string } | null
}
