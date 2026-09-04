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
const searchKw = ref('')
const appliedKw = ref('')

async function loadList(t) {
  active.value = t; loading.value = true
  searchKw.value = ''; appliedKw.value = ''   // 切换 tab 时清除搜索,避免串榜
  try { const r = await api.douhotList(t); list.value = r.items || [] }
  catch (e) { list.value = []; toastError(e.message) } finally { loading.value = false }
}
async function searchList() {
  const kw = searchKw.value.trim()
  if (!kw) return
  loading.value = true
  try {
    const r = await api.douhotList(active.value, kw)
    list.value = r.items || []
    appliedKw.value = kw
  } catch (e) { list.value = []; toastError(e.message) } finally { loading.value = false }
}
async function clearSearch() {
  searchKw.value = ''; appliedKw.value = ''
  await loadList(active.value)
}
// 把搜索框当前词一键加入底部"关键词监控"(用当前 tab 的榜单类型),采集后即记录趋势
async function saveSearchWatch() {
  const kw = (searchKw.value.trim() || appliedKw.value).trim()
  if (!kw) { toastError('请先输入要关注的关键词'); return }
  try {
    await api.watchAdd('douhot', kw, active.value)
    toastOk(`已关注「${kw}」(${tabs.find(x=>x.key===active.value)?.label}),采集后自动记录趋势`)
    await loadWatches()
  } catch (e) { toastError(e.message) }
}
async function removeWatch(w) {
  try {
    await api.watchDel(w.section || 'douhot', w.list_type || 'word', w.keyword)
    toastOk(`已取消关注「${w.keyword}」`)
    await loadWatches()
  } catch (e) { toastError(e.message) }
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
      <div class="row" style="gap:8px;margin-bottom:10px">
        <input v-model="searchKw" placeholder="输入关键词,按词搜该榜热度(榜外也能查)" style="margin:0;flex:1" @keyup.enter="searchList" />
        <button @click="searchList">搜索</button>
        <button @click="saveSearchWatch" title="把当前词存为长期监控,采集后记录趋势">📌 关注</button>
        <button v-if="appliedKw" class="ghost" @click="clearSearch">清除</button>
      </div>
      <h3>{{ tabs.find(x=>x.key===active)?.label }}{{ appliedKw ? ` · 「${appliedKw}」` : '' }} · {{ list.length }} 条</h3>
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
          <!-- 榜单定向搜索类:逐条展示各相关主题趋势 -->
          <div v-if="w.entries && w.entries.length" style="margin:6px 0;font-size:12px">
            <div class="row" v-for="e in w.entries" :key="e.title" style="justify-content:space-between;gap:6px">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">└ {{ e.title.slice(0,18) }}</span>
              <span class="num">{{ fmt(e.last_score) }} <span :class="tclass(e.trend_label)">{{ pct(e.growth) }}</span></span>
            </div>
          </div>
          <div v-else class="empty">{{ w.summary || '再采集一次积累数据' }}</div>
          <button class="ghost" style="margin-top:6px;font-size:12px" @click="removeWatch(w)">取消关注</button>
        </div>
      </div>
      <div v-else class="empty">输入任意关键词关注,采集后会定向查热度并预测走势</div>
    </div>
  </div>
</template>
