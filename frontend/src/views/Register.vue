<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    const res = await api.register(username.value, password.value)
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
    <h2>注册</h2>
    <div v-if="error" class="error">{{ error }}</div>
    <input v-model="username" placeholder="用户名" @keyup.enter="submit" />
    <input v-model="password" type="password" placeholder="密码" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">注册并登录</button>
    <p class="empty">已有账号?<router-link to="/login" style="color:var(--accent)">去登录</router-link></p>
  </div>
</template>
