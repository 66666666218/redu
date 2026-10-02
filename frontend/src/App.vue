<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, clearToken } from './api'
import { toasts } from './toast'

const router = useRouter()
const role = ref('')
// **本实例的板块归属**(2026-10-02):两端部署各有独立库,本机不采微博/抖音/百度 ——
// 那些页面照常能打开、里面却是**3 天前的旧数据**(实测 73~82h),不标出来会被误以为在更新。
const sections = ref({})

function owned(path) {
  // 路径 → 板块;板块不在本实例管辖内 → false(未取到归属时不标,避免闪一下)
  const sec = { '/weibo': 'weibo', '/xianyu': 'xianyu', '/douhot': 'douhot', '/baidu': 'baidu' }[path]
  if (!sec || !sections.value[sec]) return true
  return !!sections.value[sec].owned
}

function navText(path, label) {
  return owned(path) ? label : label + '(远端)'
}

function logout() {
  clearToken()
  router.push('/login')
}
onMounted(async () => {
  if (localStorage.getItem('token')) {
    try { role.value = (await api.me()).role } catch {}
    try { sections.value = (await api.instance()).sections || {} } catch {}
  }
})
</script>

<template>
  <div v-if="router.currentRoute.value.meta.auth && !router.currentRoute.value.meta.screen" class="topbar">
    <div class="brand">🔥 热点监控平台</div>
    <nav>
      <router-link to="/">仪表盘</router-link>
      <router-link to="/weibo" :title="owned('/weibo') ? '微博' : '微博由远端实例采集,本机页面是旧数据'">{{ navText('/weibo', '微博') }}</router-link>
      <router-link to="/xianyu">闲鱼</router-link>
      <router-link to="/douhot" :title="owned('/douhot') ? '抖音' : '抖音由远端实例采集,本机页面是旧数据'">{{ navText('/douhot', '抖音') }}</router-link>
      <router-link to="/wechat">公众号监听</router-link>
      <router-link to="/baidu" :title="owned('/baidu') ? '百度' : '百度由远端实例采集,本机页面是旧数据'">{{ navText('/baidu', '百度') }}</router-link>
      <router-link to="/cookies">Cookie 管理</router-link>
      <router-link to="/schedule">采集频率</router-link>
      <router-link to="/alerts">预警设置</router-link>
      <router-link to="/members">群会员</router-link>
      <router-link to="/events">热点事件</router-link>
      <router-link to="/suggestions">热点建议</router-link>
      <router-link to="/resources">资源库</router-link>
      <router-link to="/cross">跨平台对标号</router-link>
      <router-link to="/xunlei">迅雷群组</router-link>
      <router-link to="/hotrank">多平台热榜</router-link>
      <router-link to="/health">数据源健康</router-link>
      <router-link v-if="role==='admin' || role==='operator'" to="/admin">管理后台</router-link>
      <a href="#" @click.prevent="logout">退出</a>
    </nav>
  </div>
  <router-view />
  <div class="toasts">
    <div v-for="t in toasts" :key="t.id" :class="['toast', t.type]">{{ t.msg }}</div>
  </div>
</template>
