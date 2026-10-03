<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const msg = ref('')
const healthLabel = { HEALTHY: '🟢 健康', DEGRADED: '🟡 降级', CIRCUIT_OPEN: '🔴 熔断' }

async function load() {
  msg.value = ''
  try { items.value = await api.sourceHealth() } catch (e) { msg.value = e.message }
}
const trend = ref(null)
// **对端实例探活**(2026-10-03):本机与远程库独立,微博/抖音/百度热榜归远程跑 ——
// 这个探活把"远端整机失联"与"那些源本身没数据"分开(用的是对端公开的 /healthz)。
const peer = ref(null)
async function loadPeer() {
  try { peer.value = (await api.get('/api/source-health/peer')).data } catch { peer.value = null }
}
const sigLabels = { wechat_quota: '微信读书额度耗尽', cookie_expired: 'Cookie 失效', xianyu_verify: '闲鱼滑块' }
async function loadTrend() {
  try {
    trend.value = (await api.get('/api/source-health/trend', { params: { days: 14 } })).data
  } catch { trend.value = null }
}
function dayFail(row) {
  let n = 0
  for (const k of Object.values(row.kinds || {})) n += (k.failed || 0)
  return n
}
onMounted(async () => { await load(); await loadTrend(); await loadPeer() })
</script>

<template>
  <div>
    <h2>数据源健康</h2>
    <p class="muted">每采集源三态:🟢 HEALTHY / 🟡 DEGRADED(新鲜度超标、24h 失败≥3)/ 🔴 CIRCUIT_OPEN(熔断/Cookie 失效)。判定信号:最近成功、24h 失败数、滑块/WAF 冷却、数据写入新鲜度。</p>
    <p v-if="peer && peer.configured" style="margin:8px 0;padding:8px 10px;background:rgba(127,127,127,.12);border-radius:6px">
      <b>对端实例(远程 hotspot)</b>:
      <span v-if="peer.online">🟢 在线 · v{{ peer.version }} · 响应 {{ peer.latency_ms }}ms</span>
      <span v-else style="color:#c00">🔴 失联 —— 微博/抖音/百度热榜归它跑,它挂了那些源会集体停更({{ peer.error }})</span>
      <span class="muted" style="font-size:12px"> · {{ peer.url }}</span>
    </p>
    <p v-else-if="peer" class="muted" style="font-size:12px">对端实例:未配置(设 <code>PEER_HEALTH_URL</code> 后可见远程在线状态)</p>
    <p v-if="msg" style="color:#c00">{{ msg }}</p>
    <table style="margin-top:12px">
      <thead><tr><th>数据源</th><th>健康</th><th>问题</th><th>最近成功</th><th>24h失败</th><th>数据新鲜度</th><th>采集间隔</th></tr></thead>
      <tbody>
        <tr v-for="s in items" :key="s.section">
          <td>{{ s.label }}</td>
          <td>{{ healthLabel[s.health] || s.health }}</td>
          <td style="max-width:380px">
            <span v-if="!s.problems.length" class="muted">—</span>
            <span v-for="p in s.problems" :key="p" style="display:block;color:#c00">· {{ p }}</span>
            <span v-if="s.last_fail_detail" class="muted" style="display:block;font-size:12px">最近失败: {{ s.last_fail_detail }}</span>
          </td>
          <td>{{ s.last_success_at ? s.last_success_at.slice(5, 16) : '从未' }}<span class="muted" v-if="s.last_success_age_h!==null"> ({{ s.last_success_age_h }}h前)</span></td>
          <td>{{ s.fails_24h }}</td>
          <td>{{ s.data_age_h === null ? '从未' : s.data_age_h + 'h 前' }}</td>
          <td>{{ s.interval_h }}h</td>
        </tr>
        <tr v-if="!items.length"><td colspan="7" class="muted">加载中…</td></tr>
      </tbody>
    </table>

    <div class="card" style="margin-top:14px" v-if="trend">
      <h3>近 14 天账号健康趋势</h3>
      <p>
        <span v-for="(v, k) in trend.signals" :key="k" style="margin-right:16px">
          {{ sigLabels[k] || k }} <b>{{ Object.values(v).reduce((a, b) => a + b, 0) }}</b> 次</span>
      </p>
      <table>
        <tr><th>日期</th><th>失败次数(全源)</th></tr>
        <tr v-for="row in trend.by_day" :key="row.date">
          <td>{{ row.date }}</td><td>{{ dayFail(row) }}</td>
        </tr>
      </table>
      <p class="empty">额度耗尽/滑块/Cookie 失效的历史频次——用于判断"该充值/该换 Cookie/该降频"的时机</p>
    </div>
  </div>
</template>
