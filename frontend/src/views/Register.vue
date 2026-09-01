<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'

const router = useRouter()
const email = ref('')
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

const EMAIL_RE = /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/
const MIN_PASSWORD = 8

// 前端只做即时反馈,真正的校验在后端(app/auth.py::register_user)
function localError() {
  const mail = email.value.trim()
  if (!mail) return '请填写邮箱'
  if (!EMAIL_RE.test(mail)) return '邮箱格式不正确,请填写如 name@example.com'
  if (password.value.length < MIN_PASSWORD) return `密码至少 ${MIN_PASSWORD} 位`
  return ''
}

async function submit() {
  error.value = localError()
  if (error.value) return
  loading.value = true
  try {
    const res = await api.register(email.value.trim(), password.value, username.value.trim() || null)
    setToken(res.token)
    router.push('/')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth">
    <h2>邮箱注册</h2>
    <div v-if="error" class="error">{{ error }}</div>
    <input v-model="email" type="email" placeholder="邮箱" @keyup.enter="submit" />
    <input v-model="username" placeholder="用户名(可空,默认取邮箱前缀)" @keyup.enter="submit" />
    <input v-model="password" type="password" :placeholder="`密码(至少 ${MIN_PASSWORD} 位)`" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">{{ loading ? '注册中…' : '注册并登录' }}</button>
    <p class="empty">已有账号?<router-link to="/login" style="color:var(--accent)">去登录</router-link></p>
  </div>
</template>
