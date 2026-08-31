<script setup>
import { ref } from 'vue'
import { api } from '../api'

const email = ref('')
const msg = ref('')
const loading = ref(false)

async function submit() {
  loading.value = true
  msg.value = ''
  try {
    const res = await api.forgot(email.value)
    msg.value = res.message || '若邮箱已注册,重置邮件已发送'
  } catch (e) {
    msg.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth">
    <h2>找回密码</h2>
    <div v-if="msg" class="ok">{{ msg }}</div>
    <input v-model="email" placeholder="注册邮箱" @keyup.enter="submit" />
    <button :disabled="loading" @click="submit">发送重置邮件</button>
    <p class="empty"><router-link to="/login" style="color:var(--accent)">← 返回登录</router-link></p>
  </div>
</template>
