<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PasswordDialog from '../components/PasswordDialog.vue'
import { api } from '../api/http'
import { live } from '../api/live'
import type { ImageHistoryRow, OsVariant } from '../api/types'
import { formatBytes, formatDateTime, prepText } from '../lib/format'

const variants = computed<OsVariant[]>(() => live.summary?.images?.variants ?? [])
const regular = computed(() => variants.value.filter((v) => v.kind === 'url'))
const custom = computed(() => variants.value.find((v) => v.kind === 'custom') ?? null)
const sync = computed(() => live.summary?.images?.sync ?? false)

const history = ref<ImageHistoryRow[]>([])
const message = ref<string | null>(null)
let timer: number | undefined

async function loadHistory() {
  try {
    history.value = (await api.images()).history
  } catch {
    /* обновится в следующий раз */
  }
}
onMounted(() => {
  void loadHistory()
  timer = window.setInterval(loadHistory, 10000)
})
onUnmounted(() => clearInterval(timer))

// Любое изменение — через диалог с повторным вводом пароля
interface Action {
  title: string
  message?: string
  askName?: boolean
  askUrl?: boolean
  name?: string
  url?: string
  okText: string
  run: (v: { name: string; url: string; password: string }) => Promise<string>
}
const action = ref<Action | null>(null)
const actionError = ref<string | null>(null)
const actionBusy = ref(false)

function open(a: Action) {
  actionError.value = null
  action.value = a
}
async function confirm(v: { name: string; url: string; password: string }) {
  const a = action.value
  if (!a) return
  actionBusy.value = true
  actionError.value = null
  try {
    message.value = await a.run(v)
    action.value = null
    void loadHistory()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    actionBusy.value = false
  }
}

function addOs() {
  open({
    title: 'Добавить ОС',
    message: 'Новый пункт меню «ОС на SD-NAND». Образ скачается, проверится и разойдётся по картам между партиями.',
    askName: true,
    askUrl: true,
    okText: 'Добавить',
    run: async (v) => {
      await api.addImage(v.name, v.url, v.password)
      return `«${v.name}» добавлена, образ готовится`
    },
  })
}
function changeUrl(v: OsVariant) {
  open({
    title: `${v.name}: изменить ссылку`,
    message: 'Новый образ скачается и проверится. До тех пор шьётся прежний. Смена записывается в журнал.',
    askUrl: true,
    url: v.next?.url ?? v.current?.url ?? '',
    okText: 'Сменить образ',
    run: async (x) => {
      await api.editImage(v.key, x.password, { url: x.url })
      return `${v.name}: новая ссылка принята, образ готовится`
    },
  })
}
function rename(v: OsVariant) {
  open({
    title: `${v.name}: переименовать`,
    askName: true,
    name: v.name,
    okText: 'Сохранить',
    run: async (x) => {
      await api.editImage(v.key, x.password, { name: x.name })
      return `Переименовано: ${x.name}`
    },
  })
}
function toggle(v: OsVariant) {
  open({
    title: v.visible ? `${v.name}: убрать из меню` : `${v.name}: вернуть в меню`,
    message: v.visible
      ? 'Пункт пропадёт из меню запуска, образ будет удалён с карт. История прошивок сохраняется; вернуть можно здесь же.'
      : 'Пункт снова появится в меню запуска, образ разойдётся по картам между партиями.',
    okText: v.visible ? 'Убрать' : 'Вернуть',
    run: async (x) => {
      await api.editImage(v.key, x.password, { visible: !v.visible })
      return v.visible ? `${v.name} убрана из меню` : `${v.name} снова в меню`
    },
  })
}
function move(v: OsVariant, dir: number) {
  open({
    title: `${v.name}: ${dir < 0 ? 'выше' : 'ниже'} в меню`,
    message: 'Первый видимый пункт выбран в меню запуска по умолчанию.',
    okText: 'Переместить',
    run: async (x) => {
      await api.editImage(v.key, x.password, { move: dir })
      return 'Порядок изменён'
    },
  })
}

function cardsText(v: OsVariant): string {
  if (!v.cards) return ''
  const { ready, online } = v.cards
  if (!online) return 'нет плат на связи'
  if (ready >= online) return `на всех картах (${online})`
  return `на картах ${ready} из ${online}${sync.value ? ', обновляются' : ', обновятся после партии'}`
}
const STATE: Record<string, string> = {
  queued: 'в очереди',
  working: 'готовится',
  ready: 'готов',
  error: 'ошибка',
  replaced: 'заменён',
}
</script>

<template>
  <div class="images">
    <div class="head">
      <h2>Образы ОС для SD-NAND</h2>
      <button class="primary" @click="addOs">Добавить ОС</button>
    </div>
    <p class="note">
      Меню «ОС на SD-NAND» при запуске: эти ОС по порядку (первая — по умолчанию), затем встроенные
      Zeroimage и «Образ на microSD». Образ скачивается по ссылке один раз, проверяется (образ всего диска,
      не больше SD-NAND) и раздаётся на тестовые microSD между партиями. Изменения — с повторным вводом пароля.
    </p>
    <div v-if="message" class="message" @click="message = null">{{ message }}</div>

    <div v-for="(v, i) in regular" :key="v.key" class="card" :class="{ hidden: !v.visible }">
      <div class="title">
        <b>{{ v.name }}</b>
        <span v-if="i === 0 && v.visible" class="pill">по умолчанию</span>
        <span v-if="!v.visible" class="pill gray">убрана из меню</span>
        <span class="spacer" />
        <button :disabled="i === 0" title="Выше" @click="move(v, -1)">↑</button>
        <button :disabled="i === regular.length - 1" title="Ниже" @click="move(v, 1)">↓</button>
        <button @click="rename(v)">Переименовать</button>
        <button class="warn" @click="changeUrl(v)">Изменить ссылку</button>
        <button @click="toggle(v)">{{ v.visible ? 'Убрать' : 'Вернуть' }}</button>
      </div>
      <div v-if="v.current" class="cur">
        <div><span class="lbl">Файл:</span> <span class="mono">{{ v.current.source_file }}</span>
          · {{ formatBytes(v.current.image_size) }}</div>
        <div><span class="lbl">SHA-256 образа:</span> <span class="mono small">{{ v.current.image_sha256 }}</span></div>
        <div><span class="lbl">Ссылка:</span> <a class="mono small" :href="v.current.url" target="_blank">{{ v.current.url }}</a></div>
        <div>
          <span class="lbl">Готов:</span> {{ formatDateTime(v.current.ready_at) }}
          <template v-if="v.current.operator"> · задал {{ v.current.operator }}</template>
          · <span :class="{ amber: v.cards && v.cards.ready < v.cards.online }">{{ cardsText(v) }}</span>
        </div>
      </div>
      <div v-else-if="!v.next" class="cur muted">Образ не задан</div>
      <div v-if="v.next" class="next" :class="{ failed: v.next.state === 'error' }">
        <div>
          <span class="lbl">{{ v.current ? 'Новая ссылка:' : 'Образ:' }}</span>
          <span class="mono small">{{ v.next.url }}</span>
        </div>
        <div>{{ prepText(v.next) }}</div>
        <div v-if="v.next.state === 'working' && v.next.pct != null" class="bar">
          <span :style="{ width: Math.min(100, v.next.pct) + '%' }" />
        </div>
      </div>
    </div>

    <div class="card builtin">
      <div class="title"><b>Zeroimage</b><span class="pill gray">встроенная</span></div>
      <div class="muted">Нули в первые 64 МБ (загрузчик и таблица разделов) или, с галочкой «полный», на всю SD-NAND. Файла нет, BOOTCHECK не выполняется.</div>
    </div>
    <div class="card builtin">
      <div class="title"><b>Образ на microSD</b><span class="pill gray">встроенная</span></div>
      <div class="muted">Образ, записанный на тестовую microSD при её сборке ({{ live.summary?.versions.image ?? '—' }}). Не обновляется.</div>
    </div>
    <div class="card builtin">
      <div class="title"><b>Custom (образ клиента)</b><span class="pill gray">одноразовый</span></div>
      <div v-if="custom?.current" class="cur">
        <div><span class="lbl">Сейчас:</span> <span class="mono">{{ custom.current.source_file }}</span>
          · {{ formatBytes(custom.current.image_size) }} · {{ cardsText(custom) }}</div>
        <div><span class="lbl">SHA-256:</span> <span class="mono small">{{ custom.current.image_sha256 }}</span></div>
      </div>
      <div v-if="custom?.next" class="next" :class="{ failed: custom.next.state === 'error' }">
        {{ prepText(custom.next) }}
      </div>
      <div class="muted">Ссылка задаётся в меню запуска (пункт Custom) и в этот список не сохраняется.</div>
    </div>

    <h3>История образов</h3>
    <div class="tblwrap">
      <table class="tbl">
        <thead><tr><th>Задан</th><th>ОС</th><th>Файл</th><th>Состояние</th><th>Кто</th></tr></thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td style="white-space: nowrap">{{ formatDateTime(h.created_at) }}</td>
            <td>{{ h.variant }}</td>
            <td class="mono small">{{ h.source_file }}</td>
            <td>{{ STATE[h.state] ?? h.state }}<template v-if="h.state === 'error' && h.detail">: {{ h.detail }}</template></td>
            <td>{{ h.operator ?? '—' }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="5" class="muted">Записей нет</td></tr>
        </tbody>
      </table>
    </div>

    <PasswordDialog
      v-if="action"
      :title="action.title"
      :message="action.message"
      :ask-name="action.askName"
      :ask-url="action.askUrl"
      :name="action.name"
      :url="action.url"
      :ok-text="action.okText"
      :error="actionError"
      :busy="actionBusy"
      @ok="confirm"
      @cancel="action = null"
    />
  </div>
</template>

<style scoped>
.images {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 1100px;
}
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.head h2 {
  margin: 0;
}
.note {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
}
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}
.card.hidden {
  opacity: 0.65;
}
.title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 14px;
}
.spacer {
  flex: 1;
}
.pill {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
  background: #e3effd;
  color: #1d4f9c;
}
.pill.gray {
  background: #eceef1;
  color: #5b616b;
}
.cur,
.next {
  display: flex;
  flex-direction: column;
  gap: 3px;
  overflow-wrap: anywhere;
}
.next {
  border-left: 3px solid var(--yellow);
  padding-left: 8px;
}
.next.failed {
  border-left-color: #b0b5bd;
  color: #8a4b00;
  font-weight: 600;
}
.lbl {
  color: var(--muted);
}
.small {
  font-size: 12px;
}
.amber {
  color: #9a6700;
  font-weight: 600;
}
.bar {
  height: 6px;
  background: #eceef1;
  border-radius: 3px;
  overflow: hidden;
  max-width: 420px;
}
.bar span {
  display: block;
  height: 100%;
  background: var(--blue);
}
.message {
  padding: 8px 12px;
  border-radius: 6px;
  background: #e8f4ea;
  border: 1px solid #bfe0c6;
  cursor: pointer;
}
.muted {
  color: var(--muted);
}
.tblwrap {
  overflow-x: auto;
}
h3 {
  margin: 8px 0 0;
  font-size: 16px;
}
</style>
