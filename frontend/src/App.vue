<script setup>
import { useRouter } from 'vue-router'
import { clearToken } from './api'

const router = useRouter()
function logout() {
  clearToken()
  router.push('/login')
}
</script>

<template>
  <div v-if="router.currentRoute.value.meta.auth" class="topbar">
    <div class="brand">🔥 热点监控平台</div>
    <nav>
      <router-link to="/">仪表盘</router-link>
      <router-link to="/cookies">Cookie 管理</router-link>
      <a href="#" @click.prevent="logout">退出</a>
    </nav>
  </div>
  <router-view />
</template>
