<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import { toastOk, toastError } from '../toast'

const tabs = [
  { key: 'word', label: '内容词榜' }, { key: 'search', label: '搜索榜' },
  { key: 'video', label: '视频榜' }, { key: 'topic', label: '话题榜' },
  { key: 'subscribe', label: '我的订阅' },
]
const active = ref('word')
const list = ref([])
const loading = ref(false)
const busy = ref(false)
const watches = ref([])
const watchForm = ref({ list_type: 'word', keyword: '' })

async function loadList(t) {
  active.value = t; loading.value = true
  try { const r = await api.douhotList(t); list.value = r.items || [] }
  catch (e) { list.value = []; toastError(e.message) } finally { loading.value = false }
}
async function loadWatches() {
  try { watches.value = await api.douhotWatchAnalytics() } catch {}
}
async function collect() {
  busy.value = true
  try {
    const r = await api.collect('douhot')
    toastOk(`抖音采集完成:${r.count} 条` + (r.rising ? `,判涨 ${r.rising.length}` : ''))
    await loadList(active.value); await loadWatches()
  } catch (e) { toastError('抖音采集失败:' + e.message) } finally { busy.value = false }
}
async function addWatch() {
  if (!watchForm.value.keyword.trim()) return
  try { await api.douhotWatchAdd(watchForm.value.list_type, watchForm.value.keyword.trim()); watchForm.value.keyword=''; await loadWatches() }
  catch (e) { toastError(e.message) }
}

const burstCount = computed(() => watches.value.filter(w => w.burst).length)

function fmt(v) { return v == null ? '—' : (Math.abs(v) >= 10000 ? (v/1e4).toFixed(1)+'万' : String(Math.round(v))) }
function pct(v) { return v == null ? '—' : (v*100).toFixed(1)+'%' }
function tclass(l) { return l === '上升期' ? 'up' : (l === '回落期' ? 'down' : '') }

onMounted(async () => { await loadList('word'); await loadWatches() })
</script>

<template>
  <div class="page">
    <div class="row" style="justify-content:space-between;margin-bottom:14px">
      <h2 style="margin:0">抖音热点 · 智能体</h2>
      <div class="row" style="gap:8px">
        <button :disabled="busy" @click="collect">{{ busy ? '采集中…' : '采集' }}</button>
        <button class="ghost" @click="loadList(active);loadWatches()">刷新</button>
      </div>
    </div>

    <!-- 热点宝式 tab -->
    <div class="row" style="gap:6px;margin-bottom:14px;flex-wrap:wrap">
      <button v-for="t in tabs" :key="t.key" :class="active===t.key ? '' : 'ghost'" @click="loadList(t.key)">{{ t.label }}</button>
    </div>

    <div class="card" v-if="!loading">
      <h3>{{ tabs.find(x=>x.key===active)?.label }} · {{ list.length }} 条</h3>
      <table><tr><th>#</th><th>词</th><th>分</th></tr>
        <tr v-for="(it, idx) in list" :key="it.title+idx">
          <td class="num">{{ idx+1 }}</td><td>{{ it.title }}</td><td class="price num">{{ fmt(it.score) }}</td>
        </tr>
      </table>
      <div v-if="!list.length" class="empty">暂无(需先采集,或该榜为空)</div>
    </div>
    <div v-else class="empty">加载中…</div>

    <!-- 关键词监控智能体 -->
    <div class="card" style="margin-top:16px">
      <h3>🤖 关键词监控(爆点 {{ burstCount }})</h3>
      <div class="row" style="gap:8px;margin-bottom:8px">
        <select v-model="watchForm.list_type" style="width:auto">
          <option v-for="t in tabs" :key="t.key" :value="t.key">{{ t.label }}</option>
        </select>
        <input v-model="watchForm.keyword" placeholder="任意词(榜外也能查)" style="margin:0;flex:1" @keyup.enter="addWatch" />
        <button @click="addWatch">关注</button>
      </div>
      <div v-if="watches.length" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
        <div class="watch-card" v-for="w in watches" :key="w.keyword">
          <div class="row" style="justify-content:space-between"><b>{{ w.keyword }} <span v-if="w.burst" style="color:var(--down)">🔥</span></b>
            <span class="badge" :class="tclass(w.trend_label)">{{ w.trend_label }}</span></div>
          <div class="row" style="gap:14px;margin:8px 0">
            <div><div class="empty" style="padding:0">当前</div><b class="num">{{ fmt(w.last_score) }}</b></div>
            <div><div class="empty" style="padding:0">环比</div><b class="num" :class="tclass(w.trend_label)">{{ pct(w.growth) }}</b></div>
            <div><div class="empty" style="padding:0">预测</div><b class="num">{{ fmt(w.forecast_next) }}</b></div>
          </div>
          <div class="empty">{{ w.summary || '再采集一次积累数据' }}</div>
        </div>
      </div>
      <div v-else class="empty">输入任意关键词关注,采集后会定向查热度并预测走势</div>
    </div>
  </div>
</template>
