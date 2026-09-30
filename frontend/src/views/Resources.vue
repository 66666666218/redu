<template>
  <div>
    <h2>资源库 · 现成资源检索</h2>
    <p class="muted">
      对标号全历史盘链的沉淀:高共振 = 同一条链被多个号反复发(需求被验证);「已转存✓」=
      我方链已有,点开即用。爆款榜 = 近 24h 多号突然同发(跟得越早扩散越强)。
    </p>
    <p v-if="msg" :style="{ color: msg.includes('失败') ? '#c00' : '#080' }">{{ msg }}</p>

    <div class="card" style="margin-bottom:14px" v-if="summary">
      <span style="margin-right:18px">📦 总链 <b>{{ summary.total_links }}</b></span>
      <span style="margin-right:18px">🔥 多号验证 <b>{{ summary.multi_account }}</b></span>
      <span class="empty">近 {{ summary.days }} 天口径</span>
    </div>

    <div class="card" style="margin-bottom:14px">
      <div class="row" style="gap:10px;flex-wrap:wrap">
        <input v-model="q" placeholder="搜资源(如:花少 / 问卷 / 字体 / 剪辑软件),回车检索" style="flex:1;margin:0" @keyup.enter="search" />
        <button @click="search">检索</button>
        <button class="ghost" @click="loadAll">共振榜(全部)</button>
      </div>
    </div>

    <div class="card" style="margin-bottom:14px" v-if="viral.length">
      <h3>🚨 爆款资源(近 {{ viralHours }}h 多号同发)</h3>
      <table>
        <tr><th>号数</th><th>盘</th><th>资源</th><th>我方链</th><th>最近</th></tr>
        <tr v-for="r in viral" :key="'v' + r.pan_url">
          <td>×{{ r.accounts }}</td><td>{{ r.pan_type }}</td>
          <td style="max-width:340px">{{ r.titles[0] || '—' }}</td>
          <td><a v-if="r.my_link" :href="r.my_link" target="_blank">已转存✓ 点开</a><span v-else class="muted">未转存</span></td>
          <td class="muted">{{ r.last_seen.slice(0, 10) }}</td>
        </tr>
      </table>
    </div>

    <div class="card">
      <h3>{{ q ? '检索「' + q + '」' : '高共振资源榜' }}({{ items.length }})</h3>
      <table v-if="items.length">
        <tr><th>号数</th><th>盘</th><th>资源(标题样例)</th><th>我方链</th><th>最近</th><th>首次</th></tr>
        <tr v-for="r in items" :key="r.pan_url">
          <td>×{{ r.accounts }}</td><td>{{ r.pan_type }}</td>
          <td style="max-width:360px">
            <div>{{ r.titles[0] || '—' }}</div>
            <div class="muted" style="font-size:12px" v-if="r.titles[1]">{{ r.titles[1] }}</div>
          </td>
          <td style="font-size:12px">
            <a v-if="r.my_link" :href="r.my_link" target="_blank">已转存✓</a>
            <span v-else class="muted">未转存</span>
          </td>
          <td class="muted" style="font-size:12px">{{ r.last_seen.slice(0, 10) }}</td>
          <td class="muted" style="font-size:12px">{{ r.first_seen.slice(0, 10) }}</td>
        </tr>
      </table>
      <p v-else class="empty">{{ q ? '没有匹配——换个短词试试' : '暂无共振资源(需 ≥2 个号发过同一条链)' }}</p>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const q = ref('')
const items = ref([])
const viral = ref([])
const viralHours = ref(24)
const summary = ref(null)
const msg = ref('')

async function loadAll() {
  q.value = ''
  const r = await api.get('/api/wechat/resources')
  summary.value = r.data.summary
  items.value = r.data.items
}

async function search() {
  const r = await api.get('/api/wechat/resources', { params: { q: q.value.trim() } })
  summary.value = r.data.summary
  items.value = r.data.items
  if (q.value.trim() && !r.data.items.length) msg.value = '没有匹配的资源'
  else msg.value = ''
}

onMounted(async () => {
  try {
    await loadAll()
    const v = await api.get('/api/wechat/resources/viral')
    viral.value = v.data.items
    viralHours.value = v.data.hours
  } catch (e) {
    msg.value = '加载失败:' + (e?.response?.data?.detail || e.message || e)
  }
})
</script>
