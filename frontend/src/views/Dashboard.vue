<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'

const dash = ref({ weibo_trends: [], xianyu_hot: [], douhot_words: [] })
const analytics = ref({ total_want: 0, top_risers: [], categories: [], top_fallers: [], items: [] })
const watches = ref([])
const watchForm = ref({ list_type: 'word', keyword: '' })
const msg = ref('')
const busy = ref('')

// 智能排序:可能爆发的优先,其次按预测热度 / 环比涨幅,把最值得看的顶上来
const sortedWatches = computed(() => {
  return [...watches.value].sort((a, b) => {
    if (!!b.burst !== !!a.burst) return b.burst ? 1 : -1
    const fb = b.forecast_next ?? 0, fa = a.forecast_next ?? 0
    if (fb !== fa) return fb - fa
    return (b.growth ?? 0) - (a.growth ?? 0)
  })
})

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

// 分数格式化:过万显示"x.x万",否则显示原始整数(内容词分数跨度大,211 会显示成 0.0万)
function fmtScore(v) {
  if (v == null) return '—'
  if (Math.abs(v) >= 10000) return (v / 1e4).toFixed(1) + '万'
  return String(Math.round(v))
}

// 把热度序列画成迷你趋势线(实线=实际,虚线=预测外推),返回 {solid, fc, w, h}
function spark(series, forecast) {
  if (!series || series.length < 2) return ''
  const W = 132, H = 38
  // 预留一个槽位给预测点(若提供),横轴按 实际点数+预测1点 归一化
  const totalSlots = series.length + (forecast != null ? 1 : 0)
  const xs = i => (i / (totalSlots - 1)) * W
  const max = Math.max(...series, forecast ?? 0), min = Math.min(...series, forecast ?? 0)
  const range = (max - min) || 1
  const ys = v => H - ((v - min) / range) * (H - 4) - 2
  const solid = series.map((v, i) => `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(' ')
  let fc = ''
  if (forecast != null) {
    const last = series[series.length - 1]
    fc = `${xs(series.length - 1).toFixed(1)},${ys(last).toFixed(1)} ${xs(series.length).toFixed(1)},${ys(forecast).toFixed(1)}`
  }
  return { solid, fc, w: W, h: H }
}
function trendClass(label) {
  return label === '上升期' ? 'up' : (label === '回落期' ? 'down' : '')
}

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
        <h3>闲鱼 · 前100 虚拟商品</h3>
        <table><tr><th>标题</th><th>价</th><th>场次</th></tr>
          <tr v-for="(it,i) in dash.xianyu_hot.slice(0,30)" :key="it.item_id"><td>{{ i+1 }}. {{ it.title.slice(0,30) }}</td><td class="price">{{ it.price }}</td><td>{{ it.hit_keywords }}</td></tr>
        </table>
      </div>
      <div class="card" v-if="dash.douhot_words.length">
        <h3>抖音 · 内容词</h3>
        <table><tr><th>词</th><th>飙升</th><th>趋势</th></tr>
          <tr v-for="it in dash.douhot_words.slice(0,100)" :key="it.title"><td>{{ it.title }}</td><td class="price">{{ (it.score/1e4).toFixed(0) }}万</td><td :class="{up:it.trend_delta>0}">{{ it.trend_delta>0?'↑':(it.trend_delta<0?'↓':'—') }}</td></tr>
        </table>
      </div>

      <div class="card">
        <h3>闲鱼 · Top20 详情分析(想要数)  <span class="empty">昨日→今日</span></h3>
        <p class="empty">今日总想要 {{ analytics.total_want }} · 上榜 {{ analytics.count }} <span style="color:var(--dim)">(想要数受反爬限制,无则显示 0)</span></p>
        <h4 style="color:var(--dim);margin:8px 0">🔥 上升榜</h4>
        <table v-if="analytics.top_risers.length"><tr><th>标题</th><th>涨跌</th><th>涨跌%</th></tr>
          <tr v-for="it in analytics.top_risers.slice(0,8)" :key="it.item_id"><td>{{ it.title.slice(0,24) }}</td><td class="up">+{{ it.delta }}</td><td class="up">{{ pct(it.pct) }}</td></tr>
        </table>
        <div v-else class="empty">先"闲鱼深度采集"</div>
        <h4 style="color:var(--dim);margin:10px 0 4px">类目分布</h4>
        <div v-for="c in analytics.categories" :key="c.name" class="empty">· {{ c.name }} ×{{ c.count }}</div>
      </div>

      <div class="card">
        <h3>🤖 关键词监控 · 智能体</h3>
        <div class="row" style="gap:8px;margin-bottom:8px">
          <select v-model="watchForm.list_type" style="width:auto">
            <option v-for="t in listTypes" :key="t.key" :value="t.key">{{ t.label }}</option>
          </select>
          <input v-model="watchForm.keyword" placeholder="输入关键词(任意词,榜外也能查)" style="margin:0;flex:1" @keyup.enter="addWatch" />
          <button @click="addWatch">关注</button>
        </div>

        <div v-if="watches.length" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
          <div class="watch-card" v-for="w in sortedWatches" :key="w.keyword">
            <div class="row" style="justify-content:space-between">
              <b>{{ w.keyword }} <span v-if="w.burst" style="color:var(--down)">🔥爆发</span></b>
              <span class="badge" :class="trendClass(w.trend_label)">{{ w.trend_label || '关注中' }}</span>
            </div>
            <div class="row" style="gap:14px;margin:8px 0">
              <div><div class="empty" style="padding:0">当前分</div><b class="num">{{ fmtScore(w.last_score) }}</b></div>
              <div><div class="empty" style="padding:0">环比</div><b class="num" :class="{up:(w.growth||0)>0, down:(w.growth||0)<0}">{{ pct(w.growth) }}</b></div>
              <div><div class="empty" style="padding:0">预测下一轮</div><b class="num">{{ fmtScore(w.forecast_next) }}</b></div>
            </div>
            <svg v-if="w.series && w.series.length >= 2" :width="132" :height="38" class="spark">
              <polyline :points="spark(w.series, w.forecast_next).solid" fill="none" stroke="var(--neon)" stroke-width="2" stroke-linejoin="round"/>
              <polyline v-if="spark(w.series, w.forecast_next).fc" :points="spark(w.series, w.forecast_next).fc" fill="none" stroke="var(--neon-2)" stroke-width="2" stroke-dasharray="4,3" stroke-linejoin="round"/>
            </svg>
            <div class="empty" style="margin-top:6px;color:var(--dim)">置信度:{{ w.confidence || '—' }}</div>
            <div class="empty" style="margin-top:2px">{{ w.summary || '再采集一次积累更多数据' }}</div>
          </div>
        </div>
        <div v-else class="empty">输入任意关键词 → 关注 → 采集抖音:无论它在不在榜上都会定向查出热度,并预测走势</div>
      </div>
    </div>
  </div>
</template>
