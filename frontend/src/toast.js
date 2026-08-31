// 全局轻量 Toast
import { reactive } from 'vue'

export const toasts = reactive([])
let _id = 0

export function toast(msg, type = 'info') {
  const id = ++_id
  toasts.push({ id, msg, type })
  setTimeout(() => {
    const i = toasts.findIndex(t => t.id === id)
    if (i > -1) toasts.splice(i, 1)
  }, 3500)
}

export function toastOk(msg) { toast(msg, 'ok') }
export function toastError(msg) { toast(msg, 'error') }
