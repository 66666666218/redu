<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { toastOk, toastError } from '../toast'

const props = defineProps({ platform: { type: String, required: true } })
const platform = props.platform
const labels = { weibo: '微博热搜', baidu: '百度热搜', xianyu: '闲鱼热榜' }
const label = labels[platform] || platform
const view = ref({ count: 0, items: [] })
const watches = ref([])
const watchKeyword = ref('')
const busy = ref(false)

async function load() {
  try { view.value = await api.platformView(platform) } catch (e) { toastError(e.message) }
}
async function loadWatches() {
  try { watches.value = await api.watchAnalytics(platform) } catch { /* 暂无 */ }
}
async function addWatch() {
  if (!watchKeyword.value.trim()) return
  try { await api.watchAdd(platform, watchKeyword.value.trim()); watchKeyword.value=''; await loadWatches() }
  catch (e) { toastError(e.message) }
}
async function collect() {
  busy.value = true
  try {
    const r = await api.collect(platform)
    toastOk(`${label}采集完成:${r.count} 条` + (r.rising ? `,判涨 ${r.rising.length}` : ''))
    await load(); await loadWatches()
  } catch (e) { toastError(`${label}采集失败:${e.message}`) } finally { busy.value = false }
}

function fmt(v) { return v == null ? '—' : (Math.abs(v) >= 10000 ? (v/1e4).toFixed(1)+'万' : String(Math.round(v))) }
function pct(v) { return v == null ? '—' : (v*100).toFixed(1)+'%' }
function tclass(l) { return l === '上升期' ? 'up' : (l === '回落期' ? 'down' : '') }

onMounted(async () => { await load(); await loadWatches() })
</script>

<template>
  <div class="page">
    <div class="row" style="justify-content:space-between;margin-bottom:14px">
      <h2 style="margin:0">{{ label }}</h2>
      <div class="row" style="gap:8px">
        <button :disabled="busy" @click="collect">{{ busy ? '采集中…' : '采集' }}</button>
        <button class="ghost" @click="load">刷新</button>
      </div>
    </div>

    <div class="card">
      <h3>榜单 · 趋势预测</h3>
      <div v-if="view.items.length" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
        <div class="watch-card" v-for="(it, idx) in view.items" :key="it.name">
          <div class="row" style="justify-content:space-between">
            <b><span class="num" style="color:var(--dim)">{{ idx+1 }}.</span> {{ it.name }}</b>
            <span class="badge" :class="tclass(it.trend_label)">{{ it.trend_label }}</span>
          </div>
          <div class="row" style="gap:14px;margin:8px 0">
            <div><div class="empty" style="padding:0">热度</div><b class="num">{{ fmt(it.score) }}</b></div>
            <div><div class="empty" style="padding:0">环比</div><b class="num" :class="tclass(it.trend_label)">{{ pct(it.growth) }}</b></div>
            <div><div class="empty" style="padding:0">预测</div><b class="num">{{ fmt(it.forecast_next) }}</b></div>
          </div>
          <div class="empty" style="font-size:12px">
            <span v-if="it.hot" style="color:var(--down)">🔥热点</span>
            <span v-if="it.burst" style="color:var(--down)">可能爆发</span>
            <span v-if="!it.hot && !it.burst">未爆发</span>
            <span> · 样本 {{ it.points }}</span>
          </div>
        </div>
      </div>
      <div v-else class="empty">暂无数据,点击"采集"获取(需采集 2 轮以上才有趋势预测)</div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>🤖 关键词监控({{ label }})</h3>
      <div class="row" style="gap:8px;margin-bottom:8px">
        <input v-model="watchKeyword" placeholder="添加要监控的关键词(出现在榜内才会记录)" style="margin:0;flex:1" @keyup.enter="addWatch" />
        <button @click="addWatch">关注</button>
      </div>
      <div v-if="watches.length" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
        <div class="watch-card" v-for="w in watches" :key="w.keyword">
          <div class="row" style="justify-content:space-between"><b>{{ w.keyword }} <span v-if="w.burst" style="color:var(--down)">🔥</span></b>
            <span class="badge" :class="tclass(w.trend_label)">{{ w.trend_label }}</span></div>
          <div class="row" style="gap:14px;margin:8px 0">
            <div><div class="empty" style="padding:0">热度</div><b class="num">{{ fmt(w.last_score) }}</b></div>
            <div><div class="empty" style="padding:0">环比</div><b class="num" :class="tclass(w.trend_label)">{{ pct(w.growth) }}</b></div>
            <div><div class="empty" style="padding:0">预测</div><b class="num">{{ fmt(w.forecast_next) }}</b></div>
          </div>
          <div class="empty">{{ w.summary || '再采集一次积累数据' }}</div>
        </div>
      </div>
      <div v-else class="empty">该板块加个关键词,采集后会记录它每次的热度并预测走势(词须出现在榜内)</div>
    </div>
  </div>
</template>
