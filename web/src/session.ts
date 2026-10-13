import { reactive } from 'vue'

/** freshLogin: только что вошли — главная один раз показывает, идут ли уже тесты на стенде. */
export const session = reactive({ username: null as string | null, freshLogin: false })
