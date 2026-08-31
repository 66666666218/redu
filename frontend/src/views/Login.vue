<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, setToken } from '../api'

const router = useRouter()
const login = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    const res = await api.login(login.value, password.value)
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
    <h2>登录</h2>
    <div v-if="error" class="error">{{ error }}</div>
    <input v-model="login" placeholder="邮箱 / 用户名" @keyup.enter="submit" />
    <input v-model="password" type="password" placeholder="密码" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">登录</button>
    <p class="empty">
      <router-link to="/forgot" style="color:var(--dim)">忘记密码?</router-link>
      &nbsp;·&nbsp;
      <router-link to="/register" style="color:var(--accent)">注册</router-link>
    </p>
  </div>
</template>
