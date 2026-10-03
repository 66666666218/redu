<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { toastOk, toastError as toastErr } from '../toast'

const chr10 = () => String.fromCharCode(10)
const benches = ref([])
const articles = ref([])
const candidates = ref([])
const status = ref({})
const onlyPan = ref(false)
const sortBy = ref('time')
const searchKw = ref('')
let searchTimer = null
let articleReq = 0
function searchDebounce() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => loadArticles(), 300)
}
onUnmounted(() => clearTimeout(searchTimer))
const link = ref('')
const note = ref('')
const busy = ref('')
const msg = ref('')

const panCount = computed(() => articles.value.filter(a => a.pan_types).length)
const totalRead = computed(() => articles.value.reduce((s, a) => s + (a.read_num || 0), 0))

async function loadBenches() {
  try { benches.value = (await api.wechatBenchmarks()).items } catch (e) { msg.value = e.message }
}
async function loadArticles(append = false) {
  const seq = ++articleReq
  try {
    const q = new URLSearchParams()
    if (onlyPan.value) q.set('has_pan', '1')
    if (sortBy.value) q.set('sort', sortBy.value)
    if (searchKw.value.trim()) q.set('keyword', searchKw.value.trim())
    q.set('limit', '100')
    if (append) q.set('offset', String(articles.value.length))
    const items = (await api.wechatArticles(q.toString())).items
    // 换排序/改关键词会连发多请求,慢的那个后到不能覆盖新结果(搜索防抖挡不住上游耗时)
    if (!append && seq !== articleReq) return
    articles.value = append ? articles.value.concat(items) : items
  } catch (e) { msg.value = e.message }
}
async function load() {
  await Promise.all([loadBenches(), loadArticles(), loadCandidates(), loadStatus(), loadImportable()])
}

async function loadCandidates() {
  try { candidates.value = (await api.wechatCandidates()).items } catch (e) { msg.value = e.message }
}
async function loadStatus() {
  try { status.value = await api.wechatStatus() } catch (e) { msg.value = e.message }
}

async function discoverCandidates() {
  busy.value = 'discover'
  try {
    const r = await api.wechatCandidateDiscover()
    const note = r.blocked ? `(搜狗验证码拦截 ${r.blocked} 词)` : ''
    if (r.new > 0) toastOk(`发现 ${r.new} 个同类候选号,已推飞书${note}`)
    else toastErr(`本轮无新候选${note};搜索词:${(r.terms || []).join('、')}`)
    await loadCandidates()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function dismissCandidate(c) {
  await api.wechatCandidatePatch(c.id, { status: 'dismissed' })
  await loadCandidates()
}

async function importCandidate(c) {
  busy.value = 'imp' + c.id
  try {
    const r = await api.wechatCandidateImport(c.id)
    if (r.listenable) toastOk(`已收录「${r.nickname}」,可直接监听 ✅`)
    else toastOk(`已收录「${r.nickname}」——${r.hint || '暂无可监听标识'}`)
    await load()
  } catch (e) {
    toastErr('收录失败:' + (e.message || e))
  } finally {
    busy.value = ''
  }
}

// ---- 候选批量收录(2026-10-01):自动发现→按标准挑号→补进 WeRSS 订阅池 ----
const selected = ref(new Set())
const importableIds = ref(new Set())
const onlyImportable = ref(false)

const visibleCandidates = computed(() => {
  const rows = candidates.value.filter(c => c.status === 'new')
  return onlyImportable.value ? rows.filter(c => importableIds.value.has(c.id)) : rows
})

async function loadImportable() {
  try {
    importableIds.value = new Set((await api.wechatCandidateImportable()).items.map(i => i.id))
  } catch (e) { msg.value = e.message }
}

function toggleSelect(id) {
  const s = new Set(selected.value)
  if (s.has(id)) s.delete(id); else s.add(id)
  selected.value = s
}
function selectAllVisible() {
  selected.value = new Set(visibleCandidates.value.filter(c => !c.imported).map(c => c.id))
}
function clearSelect() { selected.value = new Set() }

async function importBatch() {
  const ids = [...selected.value]
  if (!ids.length) { toastErr('请先勾选候选号'); return }
  busy.value = 'impbatch'
  try {
    const r = await api.wechatCandidateImportBatch(ids)
    const miss = r.items.filter(x => x.status === 'ok' && !x.listenable).length
    toastOk(`收录完成:${r.ok}/${r.count} 个建号,${r.listenable} 个可直接监听` +
            (miss ? `,${miss} 个待补标识(见提示)` : ''))
    clearSelect()
    await load()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function dismissBatch() {
  const ids = [...selected.value]
  if (!ids.length) { toastErr('请先勾选候选号'); return }
  busy.value = 'disbatch'
  try {
    const r = await api.wechatCandidateDismissBatch(ids)
    toastOk(`已忽略 ${r.dismissed} 个候选`)
    clearSelect()
    await load()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function autoImport() {
  busy.value = 'autoimp'
  try {
    const r = await api.wechatCandidateAutoImport(0)
    if (!r.picked) toastErr('没有符合标准的候选(需 LLM 判为资源号,或资源已被多号验证)')
    else toastOk(`自动收录:符合标准 ${r.picked} 个,本轮处理 ${r.imported} 个(可监听 ${r.listenable})`)
    await load()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

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

async function refreshWeread() {
  busy.value = 'wrrefresh'
  try {
    const r = await api.wechatWereadRefresh()
    if (r.status === 'skipped') {
      // 三种跳过原因各说各的话:把 renewal_cooldown 说成"无 wr_rt"会让人白去重贴 Cookie
      const why = { no_cookie: '未配置微信读书 Cookie(Cookie 管理 → weread)',
                   no_rt: 'Cookie 中无 wr_rt,无法自动续期,请重新复制完整 Cookie',
                   renewal_cooldown: `刚续期失败过,处于冷却期(${fmt(r.retry_after)} 前不再重试),请稍后再点` }
      toastErr(why[r.reason] || `续期未执行:${r.reason}`)
    }
    else toastOk(`微信读书 Cookie 已续期${r.verified ? ',书架验证通过 ✅' : '(书架验证未通过,可能被风控,稍后自动重试)'}`)
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function listenAll() {
  busy.value = 'listen'
  try {
    const r = await api.wechatListen()
    if (r.status === 'skipped') {
      const why = { no_benchmarks: '还没有对标号', no_source: '未配置微信读书/dajiala',
                    running: '上一轮还在跑(一轮要几分钟),这轮不再重复触发' }
      toastErr(`监听跳过:${why[r.reason] || r.reason}`)
    }
    else {
      const text = `监听完成:检查 ${r.accounts} 个号,新文 ${r.new} 篇`
        + (r.repushed ? ` · 已补推上轮欠推 ${r.repushed} 篇` : '')
        + (r.dajiala_skipped === 'low_balance'
          ? ` · dajiala 余额不足(¥${Number(r.balance ?? 0).toFixed(2)}),本轮仅免费源,请充值恢复付费监听` : '')
      r.dajiala_skipped === 'low_balance' ? toastErr(text) : toastOk(text)
    }
    await loadArticles()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function syncOne(b) {
  busy.value = 'sync' + b.id
  try {
    const r = await api.wechatBenchmarkSync(b.id)
    if (r.status === 'skipped') {
      toastErr('同步未执行:没有可用数据源(需 dajiala key,或微信读书 Cookie + 书架导入)')
    } else {
      const pushTxt = `已转存并推送 ${r.pushed ?? 0} 篇`
        + (r.deduped ? `(相同链接去重 ${r.deduped} 篇)` : '')
        + (r.truncated ? `(超出单轮上限,剩余 ${r.truncated} 篇留给监听补转存)` : '')
      // 余额中途见底时后端不再报"成功":这里必须红字提示,否则显示"翻 0 页,新增 0 篇"
      // 会被读成"这个号没有历史文章",而真相是我们的付费额度用完了
      if (r.no_balance) toastErr(`同步中断:dajiala 余额不足(已翻 ${r.pages ?? 0} 页、入库 ${r.new ?? 0} 篇),充值后请再点一次「同步文章」`)
      else if (String(r.reason || '').startsWith('weread_list_')) toastOk(`同步完成:微信读书近期列表不可用,本次只拿到最新一篇(同日其它篇需 wewe-rss 或 dajiala),${pushTxt}`)
      else toastOk(`同步完成:翻 ${r.pages} 页,新增 ${r.new} 篇,${pushTxt}`)
    }
    await loadArticles()
  } catch (e) { toastErr(e.message) } finally { busy.value = '' }
}

async function toggleBench(b) {
  try {
    await api.wechatBenchmarkPatch(b.id, { active: !b.active })
    await loadBenches()
  } catch (e) { toastErr(e.message) }
}
async function delBench(b) {
  if (!confirm(`删除对标号「${b.nickname}」?(已入库文章保留)`)) return
  try {
    await api.wechatBenchmarkDel(b.id)
    await loadBenches()
  } catch (e) { toastErr(e.message) }
}

// ⚠️ `refreshTraffic()`(调 `api.wechatTrafficRefresh` → POST /api/wechat/traffic/refresh)已删除(2026-10-03):
// 那条路由后端从来没有,dajiala 付费阅读采样 2026-09-29 已废弃,点下去只会 404。
// 阅读/点赞走微信读书站内数,随监听免费入库 —— 无需手动触发(守卫:scripts/check_frontend_routes.py)。

const fmt = (t) => t ? String(t).replace('T', ' ') : '—'
const rewriting = ref(0)
const rewriteText = ref('')
const rewriteTitle = ref('')
const rewriteArticleId = ref(0)
async function rewrite(a) {
  rewriting.value = a.id
  rewriteText.value = ''
  rewriteArticleId.value = a.id
  try {
    const r = await api.wechatArticleRewrite(a.id)
    rewriteTitle.value = r.title
    rewriteText.value = r.content
    toastOk('AI 改写完成,请在下方复制')
  } catch (e) { toastErr(e.message) } finally { rewriting.value = 0 }
}
async function copyRewrite() {
  const full = rewriteTitle.value + String.fromCharCode(10) + String.fromCharCode(10) + rewriteText.value
  try { await navigator.clipboard.writeText(full); toastOk('已复制到剪贴板') } catch { toastErr('复制失败,请手动选择复制') }
}
const firstMy = (s) => {
  const line = (s || '').split(chr10()).find(x => x.trim())
  if (!line) return ''
  return line.includes(' (') ? line.slice(0, line.indexOf(' (')) : line.trim()
}
onMounted(load)
</script>

<template>
  <div class="page">
    <h2 style="margin:0 0 12px">公众号监听(对标号 · 新发文 · 盘链识别)</h2>
    <div class="card" style="margin-bottom:16px;padding:10px 16px">
      <span style="margin-right:16px">🎧 在监 <b>{{ status.benchmarks || 0 }}</b> 号</span>
      <span style="margin-right:16px">🆕 近24h <b>{{ status.new_24h || 0 }}</b> 篇</span>
      <span style="margin-right:16px">🔴 盘链 <b>{{ status.pan_articles || 0 }}</b></span>
      <span style="margin-right:16px">🚀 爆点 <b>{{ status.burst || 0 }}</b></span>
      <span>🔍 候选 <b>{{ status.candidates || 0 }}</b> 个</span>
      <span style="margin-left:auto">排序:
        <select v-model="sortBy" style="margin:0 4px" @change="loadArticles()">
          <option value="time">发现时间</option>
          <option value="reads">阅读量</option>
        </select>
      </span>
    </div>
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
        <button class="ghost" :disabled="busy==='wrrefresh'" @click="refreshWeread">{{ busy==='wrrefresh' ? '续期中…' : '续期微信读书 Cookie' }}</button>
        <button class="ghost" :disabled="busy==='discover'" @click="discoverCandidates">{{ busy==='discover' ? '发现中…' : '发现同类号(免费)' }}</button>
        <span class="empty">加号免费;导入需先在微信读书 App 关注公众号;Cookie 过期会自动续期,无需手动更换</span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <button :disabled="busy==='listen'" @click="listenAll">{{ busy==='listen' ? '监听中…' : '立即监听一轮' }}</button>
        <!-- ⚠️ 这里原本有个「刷新阅读量(¥0.06/篇)」按钮,2026-10-03 已删:
             它调的 `POST /api/wechat/traffic/refresh` **后端根本没有这条路由**(必 404),
             写的是 dajiala 付费采样 —— 而 dajiala 2026-09-29 已废弃(scheduler 的 traffic_tick 停用)。
             阅读/点赞现在来自**微信读书站内数,随监听免费一并入库、零额外请求**,没有"手动刷新"这回事。 -->
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" v-model="onlyPan" @change="loadArticles()" />只看带网盘链接</label>
        <span class="empty">盘链文 {{ panCount }} 篇 · 阅读量合计 {{ totalRead }}</span>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>同类候选号({{ visibleCandidates.filter(c => !c.imported).length }})
        <span class="empty" v-if="importableIds.size">符合收录标准 {{ importableIds.size }} 个</span>
      </h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        <button :disabled="busy==='discover'" @click="discoverCandidates">{{ busy==='discover' ? '发现中…' : '发现同类号' }}</button>
        <button :disabled="busy==='autoimp'" @click="autoImport">
          {{ busy==='autoimp' ? '收录中…' : '一键自动收录(按标准)' }}</button>
        <label style="display:flex;align-items:center;gap:4px">
          <input type="checkbox" v-model="onlyImportable" @change="clearSelect()" />只看符合标准的</label>
        <span class="empty">已勾选 {{ selected.size }} 个</span>
        <button class="ghost" :disabled="busy==='impbatch'" @click="importBatch">
          {{ busy==='impbatch' ? '收录中…' : '收录选中' }}</button>
        <button class="ghost" :disabled="busy==='disbatch'" @click="dismissBatch">
          {{ busy==='disbatch' ? '处理中…' : '忽略选中' }}</button>
        <button class="ghost" @click="selectAllVisible">全选</button>
        <button class="ghost" @click="clearSelect">清空</button>
      </div>
      <table v-if="visibleCandidates.length">
        <tr><th style="width:32px"></th><th>公众号</th><th>代表文章</th><th>来源词</th><th>发现时间</th><th>操作</th></tr>
        <tr v-for="c in visibleCandidates" :key="c.id">
          <td><input type="checkbox" :checked="selected.has(c.id)" @change="toggleSelect(c.id)" /></td>
          <td>{{ c.name }}<span v-if="c.imported" class="empty"> (已收录)</span>
            <div class="empty" v-if="importableIds.has(c.id)">✅ 符合收录标准</div></td>
          <td class="empty">{{ (c.title || '').slice(0, 40) }}{{ c.title_ts ? ' (' + c.title_ts.slice(5, 10) + ')' : '' }}</td>
          <td class="empty">{{ c.term }}</td>
          <td class="empty">{{ fmt(c.discovered_at) }}</td>
          <td>
            <button v-if="!c.imported" :disabled="busy === 'imp' + c.id" @click="importCandidate(c)">
              {{ busy === 'imp' + c.id ? '收录中…' : '收录' }}</button>
            <button v-if="!c.imported" class="ghost" @click="dismissCandidate(c)" style="margin-left:6px">忽略</button>
          </td>
        </tr>
      </table>
      <div v-else class="empty">
        暂无候选:点「发现同类号」按标题画像词搜同类公众号(免费),也可每日 08:20 自动发现。
        收录即把该号补进 WeRSS 订阅池,下一轮监听自动接上,无需再去微信读书关注导入。
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
      <div v-else class="empty">
        还没有对标号。<b>三步开始:</b><br/>
        ① 「Cookie 管理」配置微信读书 Cookie(或 dajiala key)<br/>
        ② 手机微信读书 App 搜索并<b>关注</b>你的对标公众号<br/>
        ③ 点上方「从微信读书书架导入」→ 「立即监听一轮」<br/>
        也可以直接粘贴某篇公众号文章的链接快速添加。
      </div>
    </div>

    <div class="card">
      <h3>监听到的文章({{ articles.length }})</h3>
      <table v-if="articles.length">
        <tr>
          <th>发现时间</th><th>公众号</th><th>标题</th><th>网盘</th><th>我的链接</th>
          <th>操作</th><th>阅读</th><th>点赞</th>
        </tr>
        <tr v-for="a in articles" :key="a.id">
          <td class="empty">{{ fmt(a.created_at) }}</td>
          <td>{{ a.author }}</td>
          <td><a :href="a.url" target="_blank" rel="noopener">{{ a.title }}</a></td>
          <td>{{ a.pan_types ? '🔴 ' + a.pan_types : '—' }}<span v-if="a.trend_flag" :class="a.trend_flag==='回落' ? 'empty' : ''">{{ a.trend_flag==='爆点苗头' ? ' 🚀爆点苗头' : a.trend_flag==='回落' ? ' 📉回落' : '' }}</span></td>
          <td><a v-if="firstMy(a.my_pan_urls)" :href="firstMy(a.my_pan_urls)" target="_blank" rel="noopener">打开</a><span v-else class="empty">—</span></td>
          <td><button class="ghost" :disabled="rewriting===a.id" @click="rewrite(a)">{{ rewriting===a.id ? '…' : 'AI改写' }}</button></td>
          <!-- ⚠️ 这两格 2026-10-03 之前挂的是 `a.traffic_at ? ... : '—'`,而 `traffic_at` 是 dajiala
               采样时间戳 —— dajiala 2026-09-29 废弃后**全库无一行非空**(实测 703 篇全为空),
               于是**有真实阅读量的 168 篇也被显示成 '—'**。改挂真数据源本身。
               原「转发」「采样时间」两列已删:前者 `share_num` 全项目从不写入、
               后者 `traffic_at` 永不设置 —— 永远为空的列就是"拿空冒充数据"(本项目反复修的那一类)。 -->
          <td>{{ a.read_num || '—' }}</td>
          <td>{{ a.zan_num || '—' }}</td>
        </tr>
      </table>
      <div v-else class="empty">暂无文章:添加对标号后点「立即监听一轮」</div>
      <div class="row" style="margin-top:8px;justify-content:center" v-if="articles.length >= 100">
        <button class="ghost" @click="loadArticles(true)">加载更多</button>
      </div>
      <div class="empty" style="margin-top:8px">阅读/点赞取自<b>微信读书站内数</b>(免费,随监听一并入库,零额外请求);"—"=该篇微信读书未返回此数</div>
    <div v-if="rewriteText" class="card" style="margin-top:16px">
      <h3>AI 改写稿:{{ rewriteTitle }}</h3>
      <!-- ⚠️ 用 `:value` 而不是 `{{ }}`:Vue 官方明确说 **textarea 里不要用插值**
           (eslint-plugin-vue 的 vue/no-textarea-mustache 就是为这个设的) ——
           插值能不能渲染出来依编译器版本而异,渲染不出来时用户看到的是**一个空框**,
           而"AI 改写稿"正是靠这个框显示内容的。`:value` 在任何版本都成立(2026-10-03 修)。 -->
      <textarea readonly :value="rewriteText" style="width:100%;min-height:300px;font-size:13px"></textarea>
      <button style="margin-top:8px" @click="copyRewrite">复制全文</button>
    </div>
    </div>
  </div>
</template>
