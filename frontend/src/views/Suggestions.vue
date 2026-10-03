<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const recruits = ref([])
const msg = ref('')
const onlyActed = ref(false)
const loading = ref(false)
// 拉新周录表单
const week = ref(new Date().toISOString().slice(0, 10))
const recruitsNum = ref('')
const recruitNote = ref('')
// **分渠道**(2026-10-03 用户口径:"我只能给你我的",且要分渠道给)——
// 只有分开录,才能分别对账抖音/公众号两条链;两个都空时退回"只录总量"。
const dyNum = ref('')
const wxNum = ref('')
// 线索结算对账(系统侧线索量级 vs 人工周录真值)
const settlement = ref({ weeks: [], coefficients: {}, recorded_weeks: 0, note: '' })

const shown = computed(() => onlyActed.value ? items.value.filter(x => x.acted) : items.value)

// 文案生成(v2.5.0):按需生成,省 LLM 成本
const drafting = ref(0)
async function genDraft(s) {
  drafting.value = s.id
  try {
    await api.post(`/api/hotspot/suggestions/${s.id}/draft`)
    msg.value = '文案已生成'
    await load()
  } catch (e) {
    msg.value = '生成失败:' + (e?.response?.data?.detail || e.message || e)
  } finally {
    drafting.value = 0
  }
}

async function load() {
  loading.value = true
  msg.value = ''
  try {
    const [a, b, c] = await Promise.all([
      api.suggestions('limit=100'), api.recruits(), api.leadsSettlement(8)])
    items.value = a.list
    recruits.value = b.list
    settlement.value = c
  } catch (e) { msg.value = e.message }
  loading.value = false
}

async function toggleActed(row) {
  msg.value = ''
  try {
    await api.suggestionActed(row.id, !row.acted)
    row.acted = !row.acted
    row.acted_at = row.acted ? new Date().toISOString() : null
  } catch (e) { msg.value = e.message }
}

async function settle() {
  msg.value = ''
  try {
    const out = await api.suggestionSettle()
    msg.value = `结算完成:已发带链 ${out.acted_with_link} 条,盘链扩散写入 ${out.settled} 条,归因 ${out.attributed} 条`
    await load()
  } catch (e) { msg.value = e.message }
}

async function saveRecruit() {
  msg.value = ''
  // 分渠道优先:填了任一渠道就按渠道提交(后端以明细之和为准),否则退回总量。
  const channels = {}
  if (dyNum.value !== '' || wxNum.value !== '') {
    channels.douyin = parseInt(dyNum.value || '0', 10)
    channels.wechat = parseInt(wxNum.value || '0', 10)
  }
  const total = Object.keys(channels).length
    ? channels.douyin + channels.wechat
    : parseInt(recruitsNum.value || '0', 10)
  if (!Number.isFinite(total) || total < 0) { msg.value = '拉新数需为非负整数'; return }
  if (!Object.keys(channels).length && !/^\d+$/.test(recruitsNum.value)) {
    msg.value = '拉新数需为非负整数(或填分渠道)'; return
  }
  try {
    await api.recruitUpsert(week.value, total, recruitNote.value, channels)
    msg.value = Object.keys(channels).length
      ? `已记录 ${week.value} 起那周拉新:抖音 ${channels.douyin} / 公众号 ${channels.wechat}`
      : `已记录 ${week.value} 起那周拉新 ${total} 人`
    recruitsNum.value = ''
    dyNum.value = ''
    wxNum.value = ''
    recruitNote.value = ''
    await refreshRecruits()
  } catch (e) { msg.value = e.message }
}

async function refreshRecruits() {
  const [b, c] = await Promise.all([api.recruits(), api.leadsSettlement(8)])
  recruits.value = b.list
  settlement.value = c
}

function fmt(t) { return t ? t.slice(5, 16).replace('T', ' ') : '—' }
// 模板里**别直接用 Object.keys** —— 依赖全局白名单,且未声明变量会整页白屏(踩过)。
function hasAny(obj) { return !!obj && Object.keys(obj).length > 0 }
onMounted(load)
</script>

<template>
  <div>
    <h2>热点建议 · 预测→下注→结算</h2>
    <p class="muted">
      推送里的每条建议在此回看;系统会**自动从发文数据识别**建议是否被执行(发文必有盘链/标题命中),无需手动标记。【已发】按钮仅作手动纠正。
      结算 = 按盘链归因发文 → 盘链全网扩散增量(免费信号);拉新总账每周从夸克官方后台抄一次。
    </p>
    <details class="muted" style="margin:8px 0;font-size:13px">
      <summary>📖 如何利用一条热点建议(五步法)</summary>
      <ol style="line-height:1.9">
        <li><b>蹭热度做资料</b>:按建议里的「资源」栏,把热点配套资料整理成文件(课件/真题/模板/壁纸/清单),热点发酵期 24~48h 内动手。</li>
        <li><b>标题带热词</b>:标题里放建议的关键词(用户搜的就是它),再叠"免费领/打包/持续更新"钩子。</li>
        <li><b>发对渠道</b>:公众号文末挂夸克转存链;同热点多号同发能触发「资源共振」加权。</li>
        <li><b>等系统归因</b>:发出去之后什么都不用做——监听会自动发现你的发文并结算;【已发】按钮只在系统没认出来时手动补标。</li>
        <li><b>看结算复盘</b>:结算列显示该文盘链被全网转存的扩散增量;连续为 0 的热点形态下次少跟,高倍数的加码。</li>
      </ol>
    </details>
    <p v-if="msg" :style="{ color: msg.includes('完成') || msg.includes('已记录') ? '#080' : '#c00' }">{{ msg }}</p>

    <div style="margin:10px 0">
      <button @click="onlyActed = !onlyActed">{{ onlyActed ? '显示全部' : '只看已发' }}</button>
      <button @click="settle" style="margin-left:8px">手动结算一次</button>
      <button @click="load" style="margin-left:8px">刷新</button>
    </div>

    <table style="margin-top:8px">
      <thead><tr>
        <th>#</th><th>热点</th><th>类型</th><th>涨幅</th><th>平台</th><th>机会分</th>
        <th>资源/方案</th><th>文案</th><th>已发</th><th>盘链扩散</th><th>建议时间</th>
      </tr></thead>
      <tbody>
        <tr v-for="s in shown" :key="s.id">
          <td>{{ s.id }}</td>
          <td style="max-width:140px">{{ s.keyword }}</td>
          <td>{{ s.kind === 'match' ? '跟发现成' : '拉新选题' }}</td>
          <td>{{ s.growth ? '+' + s.growth.toFixed(0) + '%' : '—' }}</td>
          <td style="font-size:12px">{{ s.platforms || '—' }}</td>
          <td>{{ s.opportunity ? s.opportunity.toFixed(0) : '—' }}</td>
          <td style="max-width:300px;font-size:12px">
            <div v-if="s.resource_title">{{ s.resource_title }}</div>
            <a v-if="s.link" :href="s.link" target="_blank" class="muted">{{ s.link.slice(0, 32) }}…</a>
            <div class="muted" v-if="s.plan" style="white-space:pre-wrap">{{ s.plan }}</div>
          </td>
          <td style="font-size:12px;max-width:220px">
            <button v-if="!s.draft" @click="genDraft(s)" :disabled="drafting === s.id">
              {{ drafting === s.id ? '生成中…' : '生成文案' }}
            </button>
            <div v-else style="white-space:pre-wrap;max-height:160px;overflow:auto">{{ s.draft }}</div>
          </td>
          <td>
            <button :style="{ background: s.acted ? '#0a0' : '' }" @click="toggleActed(s)">
              {{ s.acted ? '✓已发' : '标记已发' }}
            </button>
          </td>
          <td>{{ s.acted ? '+' + s.repost_gain : '—' }}</td>
          <td class="muted" style="font-size:12px">{{ fmt(s.created_at) }}</td>
        </tr>
        <tr v-if="!shown.length && !loading"><td colspan="10" class="muted">暂无建议(Agent 每日 9:10/15:10/21:10 产出)</td></tr>
      </tbody>
    </table>

    <h2 style="margin-top:28px">拉新周录(总账)</h2>
    <p class="muted">夸克官方不提供链接级转存统计(2026-09-29 确认)——每周从官方后台抄数录入。<b>填分渠道最好</b>:只有分开录,才能分别对账抖音/公众号两条链(两个都留空则按总量录)。</p>
    <div style="margin:10px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <label>周起始 <input v-model="week" type="date"></label>
      <label>抖音 <input v-model="dyNum" type="number" min="0" style="width:70px" placeholder="42"></label>
      <label>公众号 <input v-model="wxNum" type="number" min="0" style="width:70px" placeholder="18"></label>
      <label>或总量 <input v-model="recruitsNum" type="number" min="0" style="width:80px" placeholder="仅总量"></label>
      <label>备注 <input v-model="recruitNote" style="width:140px"></label>
      <button @click="saveRecruit">录入</button>
    </div>
    <table style="margin-top:8px;max-width:640px">
      <thead><tr><th>周起始</th><th>拉新数</th><th>分渠道</th><th>备注</th><th>录入时间</th></tr></thead>
      <tbody>
        <tr v-for="r in recruits" :key="r.week_start">
          <td>{{ r.week_start }}</td><td>{{ r.recruits }}</td>
          <td class="muted" style="font-size:12px">
            <span v-if="r.channels && r.channels.douyin">抖音 {{ r.channels.douyin }}</span>
            <span v-if="r.channels && r.channels.wechat"> · 公众号 {{ r.channels.wechat }}</span>
            <span v-if="!hasAny(r.channels)">未分渠道</span>
          </td>
          <td class="muted">{{ r.note || '—' }}</td><td class="muted" style="font-size:12px">{{ fmt(r.created_at) }}</td>
        </tr>
        <tr v-if="!recruits.length"><td colspan="5" class="muted">暂无录入</td></tr>
      </tbody>
    </table>

    <h2 style="margin-top:28px">线索结算对账</h2>
    <p class="muted">
      系统侧 = <b>本周发现的线索数 + 转发量之和</b>;真值 = 你录的周拉新。
      <b>两边口径不同源</b>:转发量是<b>别人视频</b>的(衡量这个资源在抖音有多热),
      不是你自己的转化 —— <b>只看趋势是否同步,别拿它们相除当转化率</b>。
      <span v-if="settlement.note"> {{ settlement.note }}</span>
    </p>
    <table style="margin-top:8px;max-width:760px">
      <thead><tr>
        <th>周起始</th><th>线索数</th><th>转发量合计</th>
        <th>粗估(×{{ (settlement.coefficients || {}).douyin || 0.7 }})</th>
        <th>你录的真值</th><th>分渠道</th>
      </tr></thead>
      <tbody>
        <tr v-for="w in settlement.weeks" :key="w.week_start">
          <td>{{ w.week_start }}</td>
          <td>{{ w.leads }}</td>
          <td>{{ w.share_total }}</td>
          <td class="muted">≈{{ w.estimated }}</td>
          <td>
            <span v-if="w.has_record">{{ w.recorded_total }}</span>
            <span v-else class="muted" style="font-size:12px">未录</span>
          </td>
          <td class="muted" style="font-size:12px">
            <span v-if="w.recorded_channels && w.recorded_channels.douyin">抖音 {{ w.recorded_channels.douyin }}</span>
            <span v-if="w.recorded_channels && w.recorded_channels.wechat"> · 公众号 {{ w.recorded_channels.wechat }}</span>
            <span v-if="!hasAny(w.recorded_channels)">—</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
