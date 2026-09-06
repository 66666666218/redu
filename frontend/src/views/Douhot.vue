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
const watchForm = ref({ list_type: 'word', keyword: '', filter_keyword: '', date_window: 1 })
const searchKw = ref('')
const searchFilter = ref('')
const searchWindow = ref(1)
const appliedKw = ref('')
const searching = ref(false)   // 手动搜索状态:true 时显示搜索结果 list,而非"自动跟随监控词"的 activeWatch
// 监控时段(小时):1/24/72/168 → 近1小时/近1天/近3天/近7天
const windows = [
  { v: 1, label: '近1小时' }, { v: 24, label: '近1天' },
  { v: 72, label: '近3天' }, { v: 168, label: '近7天' },
]
function winLabel(v) { return (windows.find(x => x.v === v) || windows[1]).label }

async function loadList(t) {
  active.value = t; loading.value = true
  searchKw.value = ''; searchFilter.value = ''; appliedKw.value = ''; searching.value = false   // 切换 tab 时清除搜索,避免串榜
  await loadWatches()                          // 先刷新关注,确保拿到该榜关键词
  // 热点宝式:该榜设了"逐条类"关键词(话题/搜索/视频)→ 自动按该词搜索显示,而非默认榜
  const aw = watches.value.find(w => w.list_type === t && ['search', 'video', 'topic'].includes(t))
  const kw = aw?.keyword, fk = aw?.filter_keyword || ''
  const dw = kw ? (aw?.date_window || 1) : searchWindow.value   // 有关键词用它的时段,否则用搜索框时段
  try {
    const r = await api.douhotList(t, kw || '', fk, dw)
    list.value = r.items || []
    if (kw && r.items) appliedKw.value = kw
  }
  catch (e) { list.value = []; toastError(e.message) } finally { loading.value = false }
}
async function searchList() {
  const kw = searchKw.value.trim()
  if (!kw) return
  searching.value = true   // 手动搜索优先于"自动跟随监控词"
  loading.value = true
  try {
    const r = await api.douhotList(active.value, kw, searchFilter.value.trim(), searchWindow.value)
    list.value = r.items || []
    appliedKw.value = kw
  } catch (e) { list.value = []; toastError(e.message) } finally { loading.value = false }
}
async function clearSearch() {
  searchKw.value = ''; searchFilter.value = ''; appliedKw.value = ''; searching.value = false
  await loadList(active.value)
}
// 把搜索框当前词一键加入底部"关键词监控"(用当前 tab 的榜单类型),采集后即记录趋势
async function saveSearchWatch() {
  const kw = (searchKw.value.trim() || appliedKw.value).trim()
  if (!kw) { toastError('请先输入要关注的关键词'); return }
  const fk = searchFilter.value.trim()
  const dw = searchWindow.value
  try {
    await api.watchAdd('douhot', kw, active.value, fk, dw)
    toastOk(`已关注「${kw}」(${tabs.find(x=>x.key===active.value)?.label})${fk ? `,只监控含「${fk}」的` : ''},采集后自动记录趋势`)
    await loadWatches()
  } catch (e) { toastError(e.message) }
}
async function removeWatch(w) {
  try {
    await api.watchDel(w.section || 'douhot', w.list_type || 'word', w.keyword, w.filter_keyword || '')
    toastOk(`已取消关注「${w.keyword}」`)
    await loadWatches()
  } catch (e) { toastError(e.message) }
}
async function changeWatchWindow(w, v) {
  try {
    await api.watchUpdate('douhot', w.list_type || 'word', w.keyword, w.filter_keyword || '', v)
    toastOk(`「${w.keyword}」观测时段已改为 ${winLabel(v)}`)
    await loadWatches()
  } catch (e) { toastError(e.message); await loadWatches() }
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
  try {
    await api.douhotWatchAdd(watchForm.value.list_type, watchForm.value.keyword.trim(), watchForm.value.filter_keyword.trim(), watchForm.value.date_window)
    watchForm.value.keyword = ''; watchForm.value.filter_keyword = ''
    await loadWatches()
  }
  catch (e) { toastError(e.message) }
}

const burstCount = computed(() => watches.value.filter(w => w.burst).length)
// 当前 tab 的"逐条类"关键词(话题/搜索/视频,有 entries)→ 有关键词就展示它的主题表;内容词单值走默认榜
const activeWatch = computed(() => watches.value.find(w => w.list_type === active.value && w.entries && w.entries.length))

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
      <div class="row" style="gap:8px;margin-bottom:10px;flex-wrap:wrap">
        <input v-model="searchKw" placeholder="输入关键词,按词搜该榜热度(榜外也能查)" style="margin:0;flex:1;min-width:140px" @keyup.enter="searchList" />
        <input v-model="searchFilter" placeholder="过滤词(可空,只留含该词的)" style="margin:0;flex:.6;min-width:120px" @keyup.enter="searchList" />
        <select v-model="searchWindow" style="width:auto" title="统计时段">
          <option v-for="op in windows" :key="op.v" :value="op.v">{{ op.label }}</option>
        </select>
        <button @click="searchList">搜索</button>
        <button @click="saveSearchWatch" title="把当前词存为长期监控,采集后记录趋势">📌 关注</button>
        <button v-if="appliedKw" class="ghost" @click="clearSearch">清除</button>
      </div>

      <!-- 该榜有关键词在监控 → 展示这个词的主题表(每榜一表);手动搜索时改用搜索结果 -->
      <template v-if="!searching && activeWatch">
        <h3>📌 {{ activeWatch.keyword }}({{ tabs.find(x=>x.key===active)?.label }} · {{ winLabel(activeWatch.date_window || 1) }} · {{ activeWatch.trend_overview }})</h3>
        <div v-if="activeWatch.entries && activeWatch.entries.length" style="max-height:420px;overflow-y:auto">
          <table><tr><th>#</th><th>主题</th><th>得分</th><th>环比</th><th>趋势</th><th>预测</th></tr>
            <tr v-for="(e, ei) in activeWatch.entries" :key="e.title + ei">
              <td class="num">{{ e.rank_now }}</td><td><span v-if="e.burst" style="color:var(--down)">🔴</span> {{ e.title.slice(0,24) }}</td>
              <td class="num">{{ fmt(e.last_score) }}</td>
              <td class="num" :class="tclass(e.trend_label)">{{ pct(e.growth) }}</td>
              <td :class="tclass(e.trend_label)">{{ e.trend_label }}</td>
              <td class="num">{{ fmt(e.forecast_next) }}</td>
            </tr>
          </table>
        </div>
        <div v-else class="empty">关注中…… 采集后记录(每条主题需≥2轮采集才有趋势)</div>
      </template>

      <!-- 非手动搜索且无监控词 → 默认榜;手动搜索 → 显示搜索到的 list -->
      <template v-else>
        <h3>{{ tabs.find(x=>x.key===active)?.label }}{{ appliedKw ? ` · 「${appliedKw}」` : '' }} · {{ list.length }} 条</h3>
        <table><tr><th>#</th><th>词</th><th>分</th><th>趋势</th></tr>
          <tr v-for="(it, idx) in list" :key="it.title+idx">
            <td class="num">{{ idx+1 }}</td><td>{{ it.title }}</td><td class="price num">{{ fmt(it.score) }}</td>
            <td :class="tclass(it.trend_label)">{{ it.trend_growth != null ? (it.trend_label + ' ' + pct(it.trend_growth)) : '—' }}</td>
          </tr>
        </table>
        <div v-if="!list.length" class="empty">暂无(需先采集,或该榜为空)</div>
      </template>
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
        <input v-model="watchForm.filter_keyword" placeholder="过滤词(可空:只监控含该词的)" style="margin:0;flex:.7;min-width:110px" @keyup.enter="addWatch" />
        <select v-model="watchForm.date_window" style="width:auto" title="监控时段">
          <option v-for="op in windows" :key="op.v" :value="op.v">{{ op.label }}</option>
        </select>
        <button @click="addWatch">关注</button>
      </div>
      <div v-if="watches.length" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
        <div class="watch-card" v-for="w in watches" :key="w.keyword + w.list_type + (w.filter_keyword||'') + (w.date_window||'')">
          <div class="row" style="justify-content:space-between">
            <b>{{ w.keyword }} <span v-if="w.filter_keyword" style="opacity:.7;font-weight:400">只含「{{ w.filter_keyword }}」</span> <span v-if="w.burst" style="color:var(--down)">🔴重点</span></b>
            <span class="badge" :class="tclass(w.trend_label)">{{ (w.entries && w.entries.length) ? (w.trend_overview || w.trend_label) : w.trend_label }}</span>
            <select v-if="w.date_window" :value="w.date_window" @click.stop @change="changeWatchWindow(w, +$event.target.value)" style="width:auto;font-size:12px;padding:0 4px" title="观测时段">
              <option v-for="op in windows" :key="op.v" :value="op.v">{{ op.label }}</option>
            </select>
          </div>
          <div class="row" style="gap:14px;margin:8px 0">
            <div><div class="empty" style="padding:0">当前</div><b class="num">{{ fmt(w.last_score) }}</b></div>
            <div><div class="empty" style="padding:0">环比</div><b class="num" :class="tclass(w.trend_label)">{{ pct(w.growth) }}</b></div>
            <div><div class="empty" style="padding:0">预测</div><b class="num">{{ fmt(w.forecast_next) }}</b></div>
          </div>
          <!-- 榜单定向搜索类:逐条展示各相关主题趋势(可滚动看全部) -->
          <div v-if="w.entries && w.entries.length" style="margin:6px 0;font-size:12px;max-height:220px;overflow-y:auto">
            <div class="row" v-for="(e, ei) in w.entries" :key="e.title + ei" style="justify-content:space-between;gap:6px;padding:1px 0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">└ <span v-if="e.burst" style="color:var(--down)">🔴</span> {{ e.title.slice(0,20) }}<span v-if="e.points < 2" style="opacity:.5">(待积累)</span></span>
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
