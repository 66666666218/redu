<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const router = useRouter()
const password = ref('')
const msg = ref('')
const ok = ref(false)
const loading = ref(false)

// 重置令牌优先从 location.hash 读(新邮件用 #token=,不进 Referer/日志);
// 兼容历史邮件的 ?token=,查询参数走 route.query。
function readToken() {
  const hash = (typeof location !== 'undefined' && location.hash) || ''
  const m = hash.match(/[#&]token=([^&]+)/)
  if (m) { try { return decodeURIComponent(m[1]) } catch { return m[1] } }
  return String(route.query.token || '')
}

async function submit() {
  loading.value = true
  msg.value = ''
  try {
    const res = await api.reset(readToken(), password.value)
    ok.value = true
    msg.value = res.message || '密码已重置'
  } catch (e) {
    msg.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth">
    <h2>设置新密码</h2>
    <div v-if="msg" :class="ok ? 'ok' : 'error'">{{ msg }}</div>
    <input v-model="password" type="password" placeholder="新密码" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">重置密码</button>
    <p class="empty" v-if="ok"><router-link to="/login" style="color:var(--accent)">去登录</router-link></p>
  </div>
</template>
