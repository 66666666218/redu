<template>
  <div>
    <h2>跨平台对标号 · 同类资源号发现</h2>
    <p class="muted">
      拿公众号监控到的网盘资源去别的平台找同类号:知乎按<b>资源词</b>搜内容、
      B站按<b>行业词</b>搜用户(号名/签名明写网盘即收录)。
      低频跑(默认周一/四 09:00),每个请求之间有限速——<b>别连点「立即发现」</b>。
    </p>
    <p v-if="msg" :style="{ color: msg.includes('失败') ? '#c00' : '#080' }">{{ msg }}</p>

    <div class="card" style="margin-bottom:14px">
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <select v-model="platform" style="margin:0;width:160px" @change="load">
          <option value="">全部平台</option>
          <option value="zhihu">知乎</option>
          <option value="bilibili">B站</option>
        </select>
        <span class="muted">共 {{ items.length }} 个号</span>
        <button :disabled="running" @click="discover">
          {{ running ? '发现中(限速约 30 秒)…' : '立即发现一轮' }}
        </button>
        <button class="ghost" @click="load">刷新</button>
      </div>
    </div>

    <div class="card">
      <table v-if="items.length">
        <tr><th>平台</th><th>账号</th><th>命中词</th><th>内容 / 签名</th><th>盘链</th><th>发现</th></tr>
        <tr v-for="a in items" :key="a.platform + a.uid">
          <td>{{ platName(a.platform) }}</td>
          <td>
            <a v-if="a.url" :href="a.url" target="_blank" rel="noopener">{{ a.name }}</a>
            <span v-else>{{ a.name }}</span>
          </td>
          <td class="muted" style="font-size:12px">{{ a.hit_keyword || '—' }}</td>
          <td style="max-width:340px;font-size:12px">{{ a.snippet || '—' }}</td>
          <td style="font-size:12px">
            <a v-if="a.pan_link" :href="a.pan_link" target="_blank" rel="noopener">有链 ✓</a>
            <span v-else class="muted">—</span>
          </td>
          <td class="muted" style="font-size:12px">{{ String(a.discovered_at || '').slice(0, 10) }}</td>
        </tr>
      </table>
      <p v-else class="empty">
        还没有收录——点「立即发现一轮」,或等定时任务(默认周一/四 09:00)
      </p>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const items = ref([])
const platform = ref('')
const msg = ref('')
const running = ref(false)

const PLAT_NAMES = { zhihu: '知乎', bilibili: 'B站' }
const platName = (p) => PLAT_NAMES[p] || p

async function load() {
  try {
    const r = await api.get('/api/cross/accounts',
      platform.value ? { params: { platform: platform.value } } : {})
    items.value = r.data.items
    msg.value = ''
  } catch (e) {
    msg.value = '加载失败:' + (e?.response?.data?.detail || e.message || e)
  }
}

async function discover() {
  running.value = true
  msg.value = '发现中…(请求之间有限速,请勿关闭页面)'
  try {
    const r = await api.post('/api/cross/discover')
    const d = r.data || {}
    msg.value = `完成:命中 ${d.found ?? 0}、新增 ${d.new ?? 0}` +
      (d.status && d.status !== 'ok' ? `(状态:${d.status})` : '')
    await load()
  } catch (e) {
    msg.value = '发现失败:' + (e?.response?.data?.detail || e.message || e)
  } finally {
    running.value = false
  }
}

onMounted(load)
</script>
