import { ref } from 'vue'
import { nowMs } from '../api/live'

/** Общий тикающий «сейчас» (мс) для таймеров квадратов. */
export const now = ref(nowMs())
setInterval(() => {
  now.value = nowMs()
}, 1000)
