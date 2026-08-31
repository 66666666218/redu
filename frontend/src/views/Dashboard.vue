<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const dash = ref({ weibo_trends: [], xianyu_hot: [], douhot_words: [] })
const analytics = ref({ total_want: 0, top_risers: [], categories: [], top_fallers: [], items: [] })
const watches = ref([])
const watchForm = ref({ list_type: 'word', keyword: '' })
const msg = ref('')
const busy = ref('')

const platforms = [
  { key: 'weibo', label: '微博' },
  { key: 'xianyu', label: '闲鱼' },
  { key: 'douhot', label: '抖音' }
]
const listTypes = [
  { key: 'word', label: '内容词榜' }, { key: 'search', label: '搜索榜' },
  { key: 'video', label: '视频榜' }, { key: 'topic', label: '话题榜' },
  { key: 'subscribe', label: '我的订阅' }
]

async function load() {
  try { dash.value = await api.dashboard() } catch (e) { msg.value = e.message }
}
async function loadAnalytics() {
  try { analytics.value = await api.xianyuAnalytics() } catch (e) { /* 暂无数据 */ }
}
async function loadWatches() {
  try { watches.value = await api.douhotWatchAnalytics() } catch (e) { /* */ }
}
async function collect(key, label) {
  busy.value = key
  msg.value = ''
  try {
    const r = await api.collect(key)
    msg.value = `${label} 采集完成:${r.count} 条${r.rising ? `,判涨 ${r.rising.length}` : ''}`
    await load(); await loadWatches()
  } catch (e) { msg.value = `${label} 采集失败:${e.message}` } finally { busy.value = '' }
}
async function collectDeep() {
  busy.value = 'xydeep'
  msg.value = ''
  try {
    await api.xianyuCollectDeep()
    msg.value = '闲鱼深度采集完成'
    await loadAnalytics()
  } catch (e) { msg.value = '深度采集失败:' + e.message } finally { busy.value = '' }
}
async function addWatch() {
  if (!watchForm.value.keyword.trim()) return
  try {
    await api.douhotWatchAdd(watchForm.value.list_type, watchForm.value.keyword.trim())
    watchForm.value.keyword = ''
    await loadWatches()
  } catch (e) { msg.value = e.message }
}
function pct(v) { return v == null ? '—' : (v * 100).toFixed(1) + '%' }

onMounted(async () => { await load(); await loadAnalytics(); await loadWatches() })
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:16px">
      <button v-for="p in platforms" :key="p.key" :disabled="busy === p.key" @click="collect(p.key, p.label)">
        {{ busy === p.key ? '采集中…' : '采集' + p.label }}
      </button>
      <button :disabled="busy === 'xydeep'" @click="collectDeep">{{ busy === 'xydeep' ? '采集中…' : '闲鱼深度采集' }}</button>
      <button class="ghost" @click="load();loadAnalytics()">刷新</button>
      <span class="empty">{{ msg }}</span>
    </div>

    <div class="grid">
      <div class="card" v-if="dash.weibo_trends.length">
        <h3>微博 · 上涨趋势</h3>
        <table><tr><th>关键词</th><th>增长率</th></tr>
          <tr v-for="t in dash.weibo_trends" :key="t.keyword"><td>{{ t.keyword }}</td><td class="up">{{ (t.growth*100).toFixed(1) }}%</td></tr>
        </table>
      </div>
      <div class="card" v-if="dash.xianyu_hot.length">
        <h3>闲鱼 · 热榜</h3>
        <table><tr><th>标题</th><th>价</th><th>场次</th></tr>
          <tr v-for="(it,i) in dash.xianyu_hot.slice(0,15)" :key="it.item_id"><td>{{ i+1 }}. {{ it.title.slice(0,30) }}</td><td class="price">{{ it.price }}</td><td>{{ it.hit_keywords }}</td></tr>
        </table>
      </div>
      <div class="card" v-if="dash.douhot_words.length">
        <h3>抖音 · 内容词</h3>
        <table><tr><th>词</th><th>飙升</th><th>趋势</th></tr>
          <tr v-for="it in dash.douhot_words.slice(0,15)" :key="it.title"><td>{{ it.title }}</td><td class="price">{{ (it.score/1e4).toFixed(0) }}万</td><td :class="{up:it.trend_delta>0}">{{ it.trend_delta>0?'↑':(it.trend_delta<0?'↓':'—') }}</td></tr>
        </table>
      </div>

      <div class="card">
        <h3>闲鱼 · 深度分析(想要数)  <span class="empty">昨日→今日</span></h3>
        <p class="empty">今日总想要 {{ analytics.total_want }} · 上榜 {{ analytics.count }}</p>
        <h4 style="color:var(--dim);margin:8px 0">🔥 上升榜</h4>
        <table v-if="analytics.top_risers.length"><tr><th>标题</th><th>涨跌</th><th>涨跌%</th></tr>
          <tr v-for="it in analytics.top_risers.slice(0,8)" :key="it.item_id"><td>{{ it.title.slice(0,24) }}</td><td class="up">+{{ it.delta }}</td><td class="up">{{ pct(it.pct) }}</td></tr>
        </table>
        <div v-else class="empty">先"闲鱼深度采集"</div>
        <h4 style="color:var(--dim);margin:10px 0 4px">类目分布</h4>
        <div v-for="c in analytics.categories" :key="c.name" class="empty">· {{ c.name }} ×{{ c.count }}</div>
      </div>

      <div class="card">
        <h3>热点宝 · 关键词监控</h3>
        <div class="row" style="gap:8px;margin-bottom:8px">
          <select v-model="watchForm.list_type" style="width:auto">
            <option v-for="t in listTypes" :key="t.key" :value="t.key">{{ t.label }}</option>
          </select>
          <input v-model="watchForm.keyword" placeholder="输入关键词" style="margin:0;flex:1" @keyup.enter="addWatch" />
          <button @click="addWatch">关注</button>
        </div>
        <table v-if="watches.length"><tr><th>关键词</th><th>最新分</th><th>排名</th><th>涨幅</th><th>样本</th></tr>
          <tr v-for="w in watches" :key="w.keyword"><td>{{ w.keyword }}</td><td>{{ (w.last_score/1e4).toFixed(1) }}万</td><td>{{ w.rank_now || '未上榜' }}</td><td :class="{up:(w.growth||0)>0}">{{ w.growth ? (w.growth*100).toFixed(1)+'%' : '—' }}</td><td>{{ w.points }}</td></tr>
        </table>
        <div v-else class="empty">关注一个关键词,采集抖音后看趋势(词须在当前榜内才会记录)</div>
      </div>
    </div>
  </div>
</template>
