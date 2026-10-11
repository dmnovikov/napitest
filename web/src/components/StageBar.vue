<script setup lang="ts">
import type { JobView } from '../api/types'
import { STAGES } from '../api/types'
import { computed } from 'vue'
import { STAGE_LABEL, STAGE_LONG, STATE_LABEL, hasWarn, shownStages, stageClass } from '../lib/format'

const props = defineProps<{ job: JobView | null; labels?: boolean }>()
const shown = computed(() => shownStages(props.job))

function stateOf(name: (typeof STAGES)[number]) {
  return props.job?.stages?.[name]?.state ?? 'pending'
}
function titleOf(name: (typeof STAGES)[number]) {
  const s = props.job?.stages?.[name]
  const st = s?.state ?? 'pending'
  const warns = s?.warnings?.length ? '\n⚠ ' + s.warnings.join('\n⚠ ') : s?.warn ? '\n⚠ есть замечания' : ''
  return `${STAGE_LONG[name]}: ${STATE_LABEL[st] ?? st}${s?.detail ? ' — ' + s.detail : ''}${warns}`
}
function warnOf(name: (typeof STAGES)[number]) {
  return hasWarn(props.job?.stages?.[name])
}
</script>

<template>
  <div class="stagebar" :style="{ gridTemplateColumns: `repeat(${shown.length}, 1fr)` }">
    <div v-for="name in shown" :key="name" class="seg-wrap" :title="titleOf(name)">
      <div class="seg" :class="stageClass(stateOf(name))">
        <span v-if="stateOf(name) === 'interrupted'" class="bang">!</span>
        <span v-if="warnOf(name)" class="warn-dot" />
      </div>
      <div v-if="labels" class="seg-label">{{ STAGE_LABEL[name] }}</div>
    </div>
  </div>
</template>

<style scoped>
.stagebar {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 3px;
}
.seg {
  height: 10px;
  border-radius: 2px;
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.seg-label {
  font-size: 9px;
  color: var(--muted);
  text-align: center;
  margin-top: 1px;
  letter-spacing: 0.02em;
}
.warn-dot {
  position: absolute;
  top: 0;
  right: 0;
  width: 0;
  height: 0;
  border-style: solid;
  border-width: 0 8px 8px 0;
  border-color: transparent #f4b400 transparent transparent;
}
.bang {
  font-size: 9px;
  font-weight: 700;
  color: #fff;
  line-height: 1;
}
.st-pending {
  background: #d4d7dd;
}
.st-pass {
  background: var(--green);
}
.st-fail {
  background: var(--red);
}
.st-error {
  background: var(--orange);
}
.st-interrupted {
  background: #8a8f98;
}
.st-skipped {
  background: repeating-linear-gradient(
    -45deg,
    #eceef1 0 3px,
    #b9bdc4 3px 4px
  );
  outline: 1px solid #c5c9cf;
  outline-offset: -1px;
}
.st-running {
  background: linear-gradient(90deg, #f4c430 0%, #ffe48a 50%, #f4c430 100%);
  background-size: 200% 100%;
  animation: run 1.2s linear infinite;
}
@keyframes run {
  from {
    background-position: 200% 0;
  }
  to {
    background-position: 0 0;
  }
}
</style>
