<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { toastOk, toastError as toastErr } from '../toast'

const benches = ref([])
const articles = ref([])
const onlyPan = ref(false)
const link = ref('')
const note = ref('')
const busy = ref('')
const msg = ref('')

const panCount = computed(() => articles.value.filter(a => a.pan_types).length)
const totalRead = computed(() => articles.value.reduce((s, a) => s + (a.read_num || 0), 0))

async function loadBenches() {
  try { benches.value = (await api.wechatBenchmarks()).items } catch (e) { msg.value = e.message }
}
async function loadArticles() {
  try {
    const q = new URLSearchParams()
    if (onlyPan.value) q.set('has_pan', '1')
    q.set('limit', '100')
    articles.value = (await api.wechatArticles(q.toString())).items
  } catch (e) { msg.value = e.message }
}
async function load() { await Promise.all([loadBenches(), loadArticles()]) }

async function addBench() {
  msg.value = ''
  if (!link.value.trim()) { toastErr('请粘贴公众号文章链接'); return }
  busy.value = 'add'
  try {
    const r = await api.wechatBenchmarkAdd(link.value.trim(), '', note.value.trim())
    toastOk(`已添加对标号:${r.nickname}`)
    link.value = ''; note.value = ''
    await loadBenches()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function importShelf() {
  busy.value = 'shelf'
  try {
    const r = await api.wechatImportShelf()
    if (r.status === 'skipped') toastErr('未配置微信读书 Cookie(Cookie 管理 → weread)')
    else toastOk(`书架导入完成:新增 ${r.created},回填 ${r.updated}`)
    await loadBenches()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function listenAll() {
  busy.value = 'listen'
  try {
    const r = await api.wechatListen()
    if (r.status === 'skipped') toastErr(`监听跳过:${r.reason === 'no_benchmarks' ? '还没有对标号' : r.reason === 'no_source' ? '未配置微信读书/dajiala' : r.reason}`)
    else toastOk(`监听完成:检查 ${r.accounts} 个号,新文 ${r.new} 篇`)
    await loadArticles()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function syncOne(b) {
  busy.value = 'sync' + b.id
  try {
    const r = await api.wechatBenchmarkSync(b.id)
    if (r.status === 'partial' && r.reason === 'weread_latest_only') toastOk('同步完成:微信读书源仅能拿最新一篇(历史需 dajiala)')
    else toastOk(`同步完成:翻 ${r.pages} 页,新增 ${r.new} 篇`)
    await loadArticles()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function toggleBench(b) {
  await api.wechatBenchmarkPatch(b.id, { active: !b.active })
  await loadBenches()
}
async function delBench(b) {
  if (!confirm(`删除对标号「${b.nickname}」?(已入库文章保留)`)) return
  await api.wechatBenchmarkDel(b.id)
  await loadBenches()
}

async function refreshTraffic() {
  busy.value = 'traffic'
  try {
    const r = await api.wechatTrafficRefresh({ limit: 30 })
    if (r.status === 'skipped') toastErr(`采样跳过:${r.reason === 'no_targets' ? '没有待采样的文章' : r.reason === 'no_key' ? '未配置 DAJIALA_KEY' : '余额不足(¥' + (r.balance ?? 0).toFixed(2) + ')'}`)
    else toastOk(`阅读量采样完成:更新 ${r.sampled} 篇`)
    await loadArticles()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

const fmt = (t) => t ? String(t).replace('T', ' ') : '—'
onMounted(load)
</script>

<template>
  <div class="page">
    <h2 style="margin:0 0 12px">公众号监听(对标号 · 新发文 · 盘链识别)</h2>
    <span v-if="msg" class="error">{{ msg }}</span>

    <div class="card" style="margin-bottom:16px">
      <h3>添加对标号</h3>
      <div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:6px">
        <input v-model="link" placeholder="粘贴该公众号任意一篇文章链接(mp.weixin.qq.com/s/…)" style="flex:1;margin:0" @keyup.enter="addBench" />
        <input v-model="note" placeholder="备注(可空)" style="width:160px;margin:0" />
        <button :disabled="busy==='add'" @click="addBench">{{ busy==='add' ? '添加中…' : '添加对标号' }}</button>
      </div>
      <div class="row" style="gap:10px;flex-wrap:wrap">
        <button class="ghost" :disabled="busy==='shelf'" @click="importShelf">{{ busy==='shelf' ? '导入中…' : '从微信读书书架导入' }}</button>
        <span class="empty">加号免费;导入需先在微信读书 App 关注公众号,并在「Cookie 管理」配置 weread</span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <button :disabled="busy==='listen'" @click="listenAll">{{ busy==='listen' ? '监听中…' : '立即监听一轮' }}</button>
        <button class="ghost" :disabled="busy==='traffic'" @click="refreshTraffic">{{ busy==='traffic' ? '采样中…' : '刷新阅读量(¥0.06/篇)' }}</button>
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" v-model="onlyPan" @change="loadArticles" />只看带网盘链接</label>
        <span class="empty">盘链文 {{ panCount }} 篇 · 阅读量合计 {{ totalRead }}</span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>对标号({{ benches.length }})</h3>
      <table v-if="benches.length">
        <tr><th>公众号</th><th>标识</th><th>状态</th><th>连续空轮</th><th>最近新文</th><th>操作</th></tr>
        <tr v-for="b in benches" :key="b.id">
          <td>{{ b.nickname }}<div class="empty" v-if="b.note">{{ b.note }}</div></td>
          <td class="empty">{{ b.biz || b.weread_book_id || b.ghid || '—' }}</td>
          <td>{{ b.active ? '✅ 监听中' : '⏸ 已停用' }}<div class="empty" v-if="b.miss_count">连续 {{ b.miss_count }} 轮未发文</div></td>
          <td class="empty">{{ b.miss_count }}</td>
          <td class="empty">{{ fmt(b.last_item_at) }}</td>
          <td>
            <button class="ghost" :disabled="busy==='sync'+b.id" @click="syncOne(b)">{{ busy==='sync'+b.id ? '同步中…' : '同步文章' }}</button>
            <button class="ghost" @click="toggleBench(b)">{{ b.active ? '停用' : '启用' }}</button>
            <button class="ghost" @click="delBench(b)">删除</button>
          </td>
        </tr>
      </table>
      <div v-else class="empty">还没有对标号:粘贴文章链接添加,或从微信读书书架导入</div>
    </div>

    <div class="card">
      <h3>监听到的文章({{ articles.length }})</h3>
      <table v-if="articles.length">
        <tr><th>发现时间</th><th>公众号</th><th>标题</th><th>网盘</th><th>阅读</th><th>点赞</th><th>转发</th><th>采样</th></tr>
        <tr v-for="a in articles" :key="a.id">
          <td class="empty">{{ fmt(a.created_at) }}</td>
          <td>{{ a.author }}</td>
          <td><a :href="a.url" target="_blank" rel="noopener">{{ a.title }}</a></td>
          <td>{{ a.pan_types ? '🔴 ' + a.pan_types : '—' }}</td>
          <td>{{ a.traffic_at ? a.read_num : '—' }}</td>
          <td>{{ a.traffic_at ? a.zan_num : '—' }}</td>
          <td>{{ a.traffic_at ? a.share_num : '—' }}</td>
          <td class="empty">{{ fmt(a.traffic_at) }}</td>
        </tr>
      </table>
      <div v-else class="empty">暂无文章:添加对标号后点「立即监听一轮」</div>
      <div class="empty" style="margin-top:8px">阅读列"—"=尚未采样;「刷新阅读量」按 ¥0.06/篇 调用 dajiala,每轮最多 30 篇(可在 .env 调整)</div>
    </div>
  </div>
</template>
