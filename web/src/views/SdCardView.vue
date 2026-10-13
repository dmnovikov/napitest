<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PasswordDialog from '../components/PasswordDialog.vue'
import { api, links } from '../api/http'
import type { SdCardInfo } from '../api/types'
import { formatBytes, formatDateTime } from '../lib/format'

const info = ref<SdCardInfo | null>(null)
const loadError = ref<string | null>(null)
const message = ref<string | null>(null)
const dialog = ref(false)
const dialogError = ref<string | null>(null)
const busy = ref(false)
let timer: number | undefined

async function load() {
  try {
    info.value = await api.sdcard()
    loadError.value = null
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
  clearTimeout(timer)
  timer = window.setTimeout(load, info.value?.progress ? 2000 : 10000)
}
onMounted(load)
onUnmounted(() => clearTimeout(timer))

const img = computed(() => info.value?.image ?? null)
const agent = computed(() => info.value?.agent ?? null)
const building = computed(() => !!info.value?.progress)

async function rebuild(v: { url: string; password: string }) {
  busy.value = true
  dialogError.value = null
  try {
    await api.sdRebuild(v.password, v.url)
    dialog.value = false
    message.value = v.url ? 'Новая база принята: скачивается, затем образ соберётся заново' : 'Образ собирается заново'
    await load()
  } catch (e) {
    dialogError.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="sd">
    <h2>Тестовая microSD</h2>
    <p class="note">
      Один файл для всех тестовых microSD. Скачайте его и прошейте на карты через balenaEtcher (распаковывать не
      нужно). Плата определяется по чипу, поэтому все карты одинаковые. Программа на картах после этого обновляется
      сама, перешивать карты после обновлений стенда не нужно.
    </p>
    <div v-if="message" class="message" @click="message = null">{{ message }}</div>
    <div v-if="loadError" class="warnbox">{{ loadError }}</div>

    <div class="card main">
      <template v-if="img">
        <a class="download" :href="links.sdDownload" :download="img.file">Скачать образ microSD</a>
        <div class="facts">
          <div><span class="lbl">Файл:</span> <span class="mono">{{ img.file }}</span> · {{ formatBytes(img.size) }}
            <span class="muted">(на карте {{ formatBytes(img.image_size) }}, нужна карта от 8 ГБ)</span></div>
          <div><span class="lbl">Версия:</span> {{ img.version }} · собран {{ formatDateTime(img.built_at) }}</div>
          <div><span class="lbl">SHA-256 файла:</span> <span class="mono small">{{ img.sha256 }}</span></div>
          <div>
            <span class="lbl">Сервер:</span>
            <template v-if="img.server_url">карты подключаются к <span class="mono">{{ img.server_url }}</span></template>
            <template v-else>карты находят сервер в сети сами</template>
          </div>
        </div>
      </template>
      <div v-else-if="!building && !info?.error" class="muted">Образ ещё не собран.</div>

      <div v-if="info?.progress" class="progress">
        <div>{{ img ? 'Собирается новый образ (пока можно скачивать прежний)' : 'Собирается образ' }}:
          {{ info.progress.text }}<template v-if="info.progress.pct != null"> — {{ Math.round(info.progress.pct) }} %</template></div>
        <div v-if="info.progress.pct != null" class="bar"><span :style="{ width: Math.min(100, info.progress.pct) + '%' }" /></div>
      </div>
      <div v-if="info?.error && !building" class="warnbox">Образ не собран: {{ info.error }}</div>
    </div>

    <div class="card">
      <h3>Программа на картах</h3>
      <div v-if="agent">
        Версия на сервере: <span class="mono">{{ agent.version }}</span>.
        <template v-if="agent.online">
          <span :class="{ amber: agent.current < agent.online }">
            Обновлена на {{ agent.current }} из {{ agent.online }} карт на связи.
          </span>
          <template v-if="agent.current < agent.online">
            Остальные обновятся сами между партиями (около минуты, плата на это время пропадёт и вернётся).
          </template>
        </template>
        <template v-else>Плат на связи нет.</template>
      </div>
      <ul v-if="agent?.errors.length" class="errs">
        <li v-for="(e, i) in agent.errors" :key="i">Плата {{ e.no ?? '?' }}: {{ e.detail }}</li>
      </ul>
    </div>

    <div class="card">
      <h3>База образа (тестовая Armbian)</h3>
      <div v-if="info?.base" class="facts">
        <div><span class="lbl">Файл:</span> <span class="mono">{{ info.base.source ?? info.base.file }}</span>
          · {{ formatBytes(info.base.size) }}</div>
        <div v-if="info.base.url"><span class="lbl">Ссылка:</span> <span class="mono small">{{ info.base.url }}</span></div>
        <div><span class="lbl">Задана:</span> {{ formatDateTime(info.base.set_at) }}
          <template v-if="info.base.operator"> · {{ info.base.operator }}</template></div>
      </div>
      <p class="muted">
        Проверенная Armbian, на которой работает программа стенда. Меняется редко. Образ собирается заново сам, когда
        обновляется программа стенда.
      </p>
      <div>
        <button class="warn" :disabled="busy" @click="dialogError = null; dialog = true">Обновить образ microSD</button>
      </div>
    </div>

    <PasswordDialog
      v-if="dialog"
      title="Обновить образ microSD"
      message="Без ссылки образ соберётся заново из текущей базы. Со ссылкой сначала скачается новая база (образ всего диска тестовой Armbian). Пока новый образ собирается, скачивается прежний."
      ask-url
      url-optional
      url-label="Ссылка на новую базу (необязательно)"
      ok-text="Собрать образ"
      :error="dialogError"
      :busy="busy"
      @ok="rebuild"
      @cancel="dialog = false"
    />
  </div>
</template>

<style scoped>
.sd {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 900px;
}
h2,
h3 {
  margin: 0;
}
h3 {
  font-size: 16px;
}
.note {
  margin: 0;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.45;
}
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 14px;
}
.download {
  align-self: flex-start;
  background: var(--blue);
  color: #fff;
  text-decoration: none;
  font-size: 17px;
  font-weight: 600;
  padding: 10px 22px;
  border-radius: 8px;
}
.download:hover {
  filter: brightness(1.08);
}
.facts {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-wrap: anywhere;
}
.lbl,
.muted {
  color: var(--muted);
}
.muted {
  margin: 0;
}
.small {
  font-size: 12px;
}
.progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
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
.warnbox {
  padding: 8px 12px;
  border-radius: 6px;
  background: #fff6e0;
  border: 1px solid #f0d27a;
  color: #8a4b00;
}
.amber {
  color: #9a6700;
  font-weight: 600;
}
.errs {
  margin: 0;
  padding-left: 20px;
  color: #8a4b00;
}
.message {
  padding: 8px 12px;
  border-radius: 6px;
  background: #e8f4ea;
  border: 1px solid #bfe0c6;
  cursor: pointer;
}
</style>
