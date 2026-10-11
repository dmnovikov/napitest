<script setup lang="ts">
import { computed } from 'vue'
import type { DeviceView } from '../api/types'
import StageBar from './StageBar.vue'
import { now } from '../lib/clock'
import { TILE_LABEL, currentStageLine, formatAgo, jobHasWarn, jobWarnings, tileClass, tileTag, tileTimer } from '../lib/format'

const props = defineProps<{ device: DeviceView; selected: boolean }>()
defineEmits<{ (e: 'pick', ev: MouseEvent): void; (e: 'open'): void }>()

const warns = computed(() => jobWarnings(props.device.job))
// Плата прошла всё: тесты, прошивку и загрузку с SD-NAND — толстая зелёная рамка
const complete = computed(() => props.device.tile === 'pass' && props.device.job?.mode === 'test_flash')
const passWarn = computed(() => props.device.tile === 'pass' && jobHasWarn(props.device.job))
const line = computed(() => {
  if (passWarn.value) return warns.value[0] ?? 'PASS, есть замечания'
  return currentStageLine(props.device.job, props.device.tile)
})
const lineTitle = computed(() => (passWarn.value ? warns.value.join('\n') || 'есть замечания' : line.value))
const timer = computed(() => tileTimer(props.device.job, now.value))
const failLine = computed(() => {
  const st = props.device.job?.stages ?? {}
  return Object.values(st).some((s) => s && (s.state === 'fail' || s.state === 'error'))
})
const title = computed(() => {
  const d = props.device
  return [
    `№${d.display_no} ${d.serial ?? ''}`,
    `cpuid: ${d.cpuid ?? '—'}`,
    `IP: ${d.ip ?? '—'}`,
    `Состояние: ${TILE_LABEL[d.tile] ?? d.tile}${complete.value ? ' — тесты, прошивка и загрузка пройдены' : ''}`,
  ].join('\n')
})
</script>

<template>
  <div
    class="tile"
    :class="[tileClass(device.tile), { selected, complete }]"
    :title="title"
    @click="$emit('pick', $event)"
    @dblclick="$emit('open')"
  >
    <div class="row head">
      <span class="no">{{ device.display_no }}</span>
      <span v-if="device.tested_before" class="seen" title="Плата уже проверялась, есть в базе">↻</span>
      <span class="tag">{{ tileTag(device) }}</span>
      <span class="badges">
        <span v-if="device.is_golden" class="badge golden" title="Golden sample">★</span>
        <span v-if="device.simulated" class="badge sim" title="Симулированный агент">SIM</span>
        <span v-if="device.tile === 'interrupted'" class="badge bang" title="Прервано">!</span>
        <span v-if="device.tile === 'incomplete'" class="badge bang" title="Не выполнено: тест запрещён, это не дефект платы">—</span>
      </span>
    </div>
    <StageBar :job="device.job" labels />
    <div class="row line" :class="{ failtxt: failLine, warntxt: passWarn }" :title="lineTitle">
      <span v-if="passWarn" class="warn-ico">⚠</span>{{ line }}
    </div>
    <div class="row foot">
      <span :class="{ countdown: timer.countdown }">{{ timer.text }}</span>
      <span v-if="device.tile === 'offline'" class="ago">{{ formatAgo(device.last_seen, now) }}</span>
      <span v-else-if="device.tile === 'awaiting_boot'" class="ago">ждём BOOT</span>
    </div>
  </div>
</template>

<style scoped>
.tile {
  background: var(--card);
  border: 3px solid var(--blue);
  border-radius: 8px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  cursor: pointer;
  user-select: none;
  min-height: 108px;
  box-sizing: border-box;
  transition: box-shadow 0.15s;
}
.tile:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}
.tile.selected {
  box-shadow: 0 0 0 3px var(--select);
  background: #eef4ff;
}
.tile-ready {
  border-color: var(--blue);
}
.tile-busy,
.tile-awaiting_boot {
  border-color: var(--yellow);
}
.tile-pass {
  border-color: var(--green);
}
.tile.complete {
  border-width: 6px;
  padding: 3px 5px;
  background: #e9f7ee;
}
.tile.complete.selected {
  background: #dcefe3;
}
.tile-fail {
  border-color: var(--red);
}
.tile-offline {
  border: 3px dashed #9aa0a8;
  background: #f6f7f8;
  opacity: 0.85;
}
.tile-interrupted,
.tile-incomplete {
  border-color: #8a8f98;
}
.row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.head .no {
  font-weight: 700;
  font-size: 16px;
}
.head .tag {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--muted);
}
.badges {
  margin-left: auto;
  display: flex;
  gap: 3px;
}
.seen {
  color: #6b7280;
  font-size: 13px;
  margin-left: -2px;
}
.badge {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 3px;
  line-height: 15px;
  font-weight: 700;
}
.badge.sim {
  background: #e3e6ea;
  color: #555;
}
.badge.golden {
  color: #b8860b;
  font-size: 13px;
}
.badge.bang {
  background: #8a8f98;
  color: #fff;
}
.line {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: block;
}
.warntxt {
  color: #8a5a00;
}
.warn-ico {
  color: #e09b00;
  margin-right: 3px;
}
.failtxt {
  color: var(--red);
  font-weight: 600;
}
.foot {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  justify-content: space-between;
}
.countdown {
  color: #9a7400;
  font-weight: 700;
}
.ago {
  font-family: var(--font);
  font-size: 11px;
}
</style>
