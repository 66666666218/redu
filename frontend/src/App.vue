<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, clearToken } from './api'
import { toasts } from './toast'

const router = useRouter()
const role = ref('')

function logout() {
  clearToken()
  router.push('/login')
}
onMounted(async () => {
  if (localStorage.getItem('token')) {
    try { role.value = (await api.me()).role } catch {}
  }
})
</script>

<template>
  <div v-if="router.currentRoute.value.meta.auth && !router.currentRoute.value.meta.screen" class="topbar">
    <div class="brand">🔥 热点监控平台</div>
    <nav>
      <router-link to="/">仪表盘</router-link>
      <router-link to="/weibo">微博</router-link>
      <router-link to="/xianyu">闲鱼</router-link>
      <router-link to="/douhot">抖音</router-link>
      <router-link to="/wechat">公众号监听</router-link>
      <router-link to="/baidu">百度</router-link>
      <router-link to="/cookies">Cookie 管理</router-link>
      <router-link to="/schedule">采集频率</router-link>
      <router-link to="/alerts">预警设置</router-link>
      <router-link v-if="role==='admin' || role==='operator'" to="/admin">管理后台</router-link>
      <a href="#" @click.prevent="logout">退出</a>
    </nav>
  </div>
  <router-view />
  <div class="toasts">
    <div v-for="t in toasts" :key="t.id" :class="['toast', t.type]">{{ t.msg }}</div>
  </div>
</template>
