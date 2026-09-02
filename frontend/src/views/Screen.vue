<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

const dash = ref({ douhot_words: [], weibo_trends: [], xianyu_hot: [] })
const agent = ref({ weibo: [], xianyu: [] })
const watches = ref([])
const now = ref(new Date())
let timer = null

// 大屏每分钟自动刷新
async function load() {
  try { dash.value = await api.dashboard() } catch {}
  try { agent.value = await api.platformAgent() } catch {}
  try { watches.value = await api.douhotWatchAnalytics() } catch {}
}
function tick() { now.value = new Date() }

const allBurst = computed(() => {
  const rows = [
    ...watches.value.filter(w => w.burst).map(w => ({ title: w.keyword, label: '关注', ...w })),
    ...agent.value.weibo.filter(w => w.burst).map(w => ({ title: w.title, label: '微博', ...w })),
    ...agent.value.xianyu.filter(w => w.burst).map(w => ({ title: w.title, label: '闲鱼', ...w })),
  ]
  return rows.sort((a, b) => (b.forecast_next || 0) - (a.forecast_next || 0)).slice(0, 8)
})

function pct(v) { return v == null ? '—' : (v * 100).toFixed(1) + '%' }
function fmtScore(v) {
  if (v == null) return '—'
  if (Math.abs(v) >= 10000) return (v / 1e4).toFixed(1) + '万'
  return String(Math.round(v))
}
function spark(series, forecast) {
  if (!series || series.length < 2) return ''
  const W = 240, H = 56, totalSlots = series.length + (forecast != null ? 1 : 0)
  const xs = i => (i / (totalSlots - 1)) * W
  const max = Math.max(...series, forecast ?? 0), min = Math.min(...series, forecast ?? 0)
  const range = (max - min) || 1
  const ys = v => H - ((v - min) / range) * (H - 4) - 2
  const solid = series.map((v, i) => `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(' ')
  let fc = ''
  if (forecast != null) {
    fc = `${xs(series.length - 1).toFixed(1)},${ys(series[series.length - 1]).toFixed(1)} ${xs(series.length).toFixed(1)},${ys(forecast).toFixed(1)}`
  }
  return { solid, fc, W, H }
}
function trendClass(l) { return l === '上升期' ? 'up' : (l === '回落期' ? 'down' : '') }

const topWords = computed(() => dash.value.douhot_words || [])
const stats = computed(() => {
  const burst = watches.value.filter(w => w.burst).length +
    agent.value.weibo.filter(w => w.burst).length + agent.value.xianyu.filter(w => w.burst).length
  return { watches: watches.value.length, burst, top: topWords.value.length, alerts: dash.value.weibo_trends.length }
})

onMounted(async () => {
  await load()
  timer = setInterval(() => { load(); tick() }, 60000)
  tick()
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="screen">
    <header class="screen-head">
      <div class="screen-brand">🔥 热点监控 <span class="dim">· 智能体大屏</span></div>
      <div class="screen-time">{{ now.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) }}</div>
    </header>

    <div class="screen-stats">
      <div class="tile"><div class="tile-num">{{ stats.watches }}</div><div class="tile-lab">关注词</div></div>
      <div class="tile hot"><div class="tile-num">{{ stats.burst }}</div><div class="tile-lab">🔥 爆发</div></div>
      <div class="tile"><div class="tile-num">{{ stats.top }}</div><div class="tile-lab">抖音内容词</div></div>
      <div class="tile"><div class="tile-num">{{ stats.alerts }}</div><div class="tile-lab">上涨趋势</div></div>
    </div>

    <div class="screen-grid">
      <section class="panel">
        <div class="panel-title">🔥 爆发榜</div>
        <div class="burst-list" v-if="allBurst.length">
          <div class="burst-item" v-for="b in allBurst" :key="b.label+b.title">
            <div class="burst-head">
              <span class="burst-kw">{{ b.title }}</span>
              <span class="badge">{{ b.label }}</span>
              <span class="burst-g" :class="trendClass(b.trend_label)">{{ pct(b.growth) }}</span>
              <span class="burst-f">→ {{ fmtScore(b.forecast_next) }}</span>
            </div>
            <svg v-if="spark(b.series, b.forecast_next)" :width="spark(b.series,b.forecast_next).W" :height="spark(b.series,b.forecast_next).H" class="screen-spark">
              <polyline :points="spark(b.series,b.forecast_next).solid" fill="none" stroke="var(--neon)" stroke-width="3" stroke-linejoin="round"/>
              <polyline v-if="spark(b.series,b.forecast_next).fc" :points="spark(b.series,b.forecast_next).fc" fill="none" stroke="var(--neon-2)" stroke-width="3" stroke-dasharray="6,4"/>
            </svg>
          </div>
        </div>
        <div v-else class="empty">关注一个关键词并多轮采集后,这里会展示预测爆发的词</div>
      </section>

      <section class="panel">
        <div class="panel-title">📈 微博 / 闲鱼 预测</div>
        <div class="mini-grid">
          <div class="mini" v-for="s in [['微博',agent.weibo],['闲鱼',agent.xianyu]]" :key="s[0]">
            <div class="mini-lab">{{ s[0] }}</div>
            <div class="mini-item" v-for="w in s[1].slice(0,6)" :key="s[0]+w.title">
              <span class="mini-kw">{{ w.title.slice(0,14) }}</span>
              <span class="mini-v" :class="trendClass(w.trend_label)">{{ pct(w.growth) }}</span>
            </div>
            <div v-if="!s[1].length" class="empty">暂无</div>
          </div>
        </div>
      </section>
    </div>

    <div class="screen-grid" style="margin-top:16px">
      <section class="panel">
        <div class="panel-title">🔥 抖音内容词 Top</div>
        <div class="ticker">
          <span class="ticker-item" v-for="t in topWords.slice(0,30)" :key="t.title">{{ t.title }} <b class="num">{{ (t.score/1e4).toFixed(0) }}万</b></span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.screen { min-height:100vh; background:var(--bg); color:var(--txt); padding:24px; position:relative; z-index:2; font-size:18px }
.screen-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px }
.screen-brand { font-size:30px; font-weight:800; background:linear-gradient(90deg,var(--neon),var(--neon-2)); -webkit-background-clip:text; background-clip:text; color:transparent }
.dim { color:var(--dim); font-weight:400; font-size:20px }
.screen-time { font-family:var(--mono); font-size:30px; color:var(--neon) }

.screen-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:20px }
.tile { background:linear-gradient(160deg,var(--card),var(--card-2)); border:1px solid var(--line); border-radius:14px; padding:20px; text-align:center }
.tile.hot { border-color:rgba(255,92,122,.4); box-shadow:0 0 20px rgba(255,92,122,.12) }
.tile-num { font-size:52px; font-weight:800; font-family:var(--mono) }
.tile.hot .tile-num { color:var(--down) }
.tile-lab { color:var(--dim); margin-top:4px; font-size:16px }

.screen-grid { display:grid; grid-template-columns:1.4fr 1fr; gap:16px }
.panel { background:linear-gradient(160deg,var(--card),var(--card-2)); border:1px solid var(--line); border-radius:14px; padding:20px }
.panel-title { font-size:20px; color:var(--dim); margin-bottom:14px; letter-spacing:.5px }

.burst-list { display:flex; flex-direction:column; gap:14px }
.burst-item { background:rgba(3,6,12,.5); border:1px solid var(--line); border-radius:10px; padding:12px }
.burst-head { display:flex; align-items:center; gap:14px; }
.burst-kw { font-size:22px; font-weight:700 }
.burst-g { font-family:var(--mono); font-size:22px; margin-left:auto }
.burst-f { font-family:var(--mono); color:var(--dim); font-size:18px }
.screen-spark { display:block; margin-top:6px; width:100%; height:auto }

.mini-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px }
.mini-lab { color:var(--dim); margin-bottom:8px; font-size:16px }
.mini-item { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--line) }
.mini-kw { max-width:60%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.mini-v { font-family:var(--mono) }

.ticker { display:flex; flex-wrap:wrap; gap:12px }
.ticker-item { background:rgba(3,6,12,.5); border:1px solid var(--line); border-radius:8px; padding:8px 14px; font-size:17px }
.ticker-item b { color:var(--neon); margin-left:6px }

@media (max-width:1100px){ .screen-grid{grid-template-columns:1fr} .screen-stats{grid-template-columns:repeat(2,1fr)} }
</style>
