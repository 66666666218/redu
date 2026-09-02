<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { toastError, toastOk } from '../toast'

const tab = ref('dashboard')
const dash = ref({ counts: {}, today_runs: 0, trend: [], trend30: [], pending_users: 0, failed_runs: 0 })
const users = ref([])
const logins = ref([])
const adminlogs = ref([])
const config = ref([])
const msg = ref('')
const q = ref('')
const detail = ref(null)
const importText = ref('')
const dataRef = ref([])
const dataSection = ref('weibo')
const cats = ref([])
const perms = ref([])
const failed = ref([])
const atrend = ref([])
const pie = ref({ alerts_section: [], watch_types: [] })
const insights = ref({ stats: {}, burst: [], rising: [], hot_words: [] })
const can = (p) => perms.value.includes(p)

async function loadInsights() {
  try { insights.value = await api.adminInsights() }
  catch (e) { toastError('洞察加载失败:' + e.message) }
}

async function load() {
  msg.value = ''
  try {
    const me = await api.adminMe(); perms.value = me.perms || []
    dash.value = await api.adminDashboard()
    users.value = await api.adminUsers()
    logins.value = await api.adminLogins()
    adminlogs.value = await api.adminLogs()
    config.value = await api.adminConfig()
    cats.value = await api.adminCategories()
    failed.value = await api.adminFailedRuns()
    atrend.value = await api.adminAlertTrend(30)
    pie.value = await api.adminCategoryPie()
  } catch (e) { toastError('加载失败:' + e.message) }
}
// 图表分类色板:已通过 dataviz 的 validate_palette.js 六项检查
// (暗色亮度带 0.48~0.67、彩度 >=0.1、色盲相邻对可分辨、对比度 >=3:1)。
// 固定顺序使用,**不循环**——超出色板的分类合并为"其他",否则第 7 类会与第 1 类撞色,图例就骗人了。
const PIE_COLORS = ['#12a7a7', '#c90084', '#0bb032', '#8625fe', '#da720d', '#026fd7']
const OTHER_COLOR = '#5b6b85'

// 把原始分类整理成绘图切片:降序、超出部分归并"其他",并附上颜色与占比
function slices(items) {
  const total = items.reduce((s, x) => s + x.value, 0)
  if (!total) return []
  const sorted = [...items].sort((a, b) => b.value - a.value)
  const out = sorted.slice(0, PIE_COLORS.length).map((x, i) => ({ ...x, color: PIE_COLORS[i] }))
  const rest = sorted.slice(PIE_COLORS.length)
  if (rest.length) out.push({ name: '其他', value: rest.reduce((s, x) => s + x.value, 0), color: OTHER_COLOR })
  return out.map(x => ({ ...x, pct: Math.round((x.value / total) * 100) }))
}

function donut(items) {
  const parts = slices(items)
  if (!parts.length) return ''
  const total = parts.reduce((s, x) => s + x.value, 0)
  let acc = 0
  const stops = parts.map((x) => {
    const from = (acc / total) * 360; acc += x.value
    return `${x.color} ${from}deg ${(acc / total) * 360 || 360}deg`
  })
  return `conic-gradient(${stops.join(',')})`
}
async function toggle(u) {
  await api.adminUserToggle(u.id); await load()
}
async function del(u) {
  if (confirm('确认删除用户 ' + u.username + '?')) { await api.adminUserDel(u.id); await load() }
}
async function setCfg(k) {
  const v = prompt('设置 ' + k, config.value.find(c => c.key === k)?.value ?? '')
  if (v !== null) { await api.adminConfigSet(k, v); await load() }
}
async function searchUsers() { users.value = await api.adminUsers(q.value) }
async function loadData() { dataRef.value = await api.adminData(dataSection.value) }
async function retryRun(runId) {
  const r = await api.adminRunRetry(runId)
  if (r.ok) toastOk('重试成功'); else toastError('重试失败:' + (r.msg || ''))
  await load()
}
async function openDetail(u) { detail.value = await api.adminUserDetail(u.id) }
async function doImport() {
  if (!importText.value.trim()) return
  const r = await api.adminImportUsers(importText.value)
  msg.value = `导入完成:新增 ${r.created},跳过 ${r.skipped}`
  importText.value = ''
  await load()
}
async function download(kind) {
  const r = await (kind === 'users' ? api.adminExportUsers() : api.adminExportAlerts())
  const blob = await r.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = kind + '.csv'; a.click()
}
const c = (k) => dash.value.counts[k] || 0
const tmax = () => Math.max(1, ...dash.value.trend30.map(t => Math.max(t.runs, t.alerts)))
onMounted(load)
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:12px;gap:8px">
      <h2 style="margin:0">管理后台</h2>
      <span v-if="msg" class="error">{{ msg }}</span>
    </div>
    <div class="row" style="gap:8px;margin-bottom:14px">
      <button :class="tab==='dashboard'?'':'ghost'" @click="tab='dashboard'">工作台</button>
      <button :class="tab==='insights'?'':'ghost'" @click="tab='insights';loadInsights()">智能体洞察</button>
      <button :class="tab==='users'?'':'ghost'" @click="tab='users'">用户管理</button>
      <button :class="tab==='data'?'':'ghost'" @click="tab='data';loadData()">数据</button>
      <button :class="tab==='ops'?'':'ghost'" @click="tab='ops'">运维</button>
      <button :class="tab==='logs'?'':'ghost'" @click="tab='logs'">日志审计</button>
      <button :class="tab==='config'?'':'ghost'" @click="tab='config'">系统设置</button>
    </div>

    <template v-if="tab==='dashboard'">
      <div class="grid" style="margin-bottom:14px">
        <div class="card"><h3>用户</h3><div class="price" style="font-size:26px">{{ c('users') }}</div><span class="empty">启用 {{ c('enabled_users') }} · 管理员 {{ c('admins') }}</span></div>
        <div class="card"><h3>今日运行</h3><div class="up" style="font-size:26px">{{ dash.today_runs }}</div><span class="empty">累计 {{ c('runs') }} · 近2天失败 {{ dash.failed_runs }}</span></div>
        <div class="card"><h3>告警</h3><div class="price" style="font-size:26px">{{ c('alerts') }}</div><span class="empty">微博{{ c('weibo_items') }}/闲鱼{{ c('xianyu_items') }}/抖音{{ c('douhot_words') }}</span></div>
        <div class="card"><h3>待办</h3><div class="price" style="font-size:26px">{{ dash.pending_users }}</div><span class="empty">未配闲鱼Cookie用户 · 需引导</span></div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <h3>近 30 天运行/告警(柱状·相对)</h3>
        <div class="row" style="align-items:flex-end;gap:2px;height:90px">
          <div v-for="t in dash.trend30" :key="t.date" style="flex:1;text-align:center">
            <div :title="`${t.date} 运行${t.runs}/告警${t.alerts}`" class="bar-v" style="background:var(--c1)" :style="{height:(t.runs/tmax()*70)+'px'}"></div>
            <div :title="`${t.date} 告警${t.alerts}`" class="bar-v" style="background:var(--c5)" :style="{height:(t.alerts/tmax()*70)+'px'}"></div>
          </div>
        </div>
        <div class="legend" style="flex-direction:row;gap:14px">
          <span><i style="background:var(--c1)"></i>运行</span>
          <span><i style="background:var(--c5)"></i>告警</span>
          <span style="color:var(--dim)">近 30 天</span>
        </div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <h3>闲鱼类目分布(数量/想要)</h3>
        <div v-if="cats.length" class="row" style="flex-wrap:wrap;gap:10px">
          <div v-for="c in cats" :key="c.name" class="badge" style="font-size:13px">{{ c.name }} ×{{ c.count }} / 想要{{ c.want }}</div>
        </div>
        <div v-else class="empty">暂无类目数据(闲鱼深度采集后)</div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <div class="row" style="gap:24px;flex-wrap:wrap">
          <div style="flex:1;min-width:220px">
            <h3>各板块运行</h3>
            <div v-for="b in dash.breakdown?.runs_by_kind || []" :key="b.kind" class="empty">
              <div class="row" style="gap:6px"><span style="width:48px">{{ b.kind }}</span>
                <div class="bar-track"><div :style="{width:(b.count/(dash.breakdown?.runs_by_kind?.[0]?.count||1)*100)+'%',background:'var(--c1)',height:'12px'}"></div></div>
                <span>{{ b.count }}</span>
              </div>
            </div>
          </div>
          <div style="flex:1;min-width:220px">
            <h3>各板块告警</h3>
            <div v-for="b in dash.breakdown?.alerts_by_section || []" :key="b.section" class="empty">
              <div class="row" style="gap:6px"><span style="width:48px">{{ b.section }}</span>
                <div class="bar-track"><div :style="{width:(b.count/(dash.breakdown?.alerts_by_section?.[0]?.count||1)*100)+'%',background:'var(--c5)',height:'12px'}"></div></div>
                <span>{{ b.count }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <div class="row" style="gap:24px;flex-wrap:wrap">
          <div style="flex:2;min-width:260px">
            <h3>告警趋势(近30天)</h3>
            <div class="row" style="align-items:flex-end;gap:2px;height:90px">
              <div v-for="t in atrend" :key="t.date" style="flex:1;text-align:center" :title="`${t.date} 告警${t.total}`">
                <div class="bar-v" style="background:var(--c5)" :style="{height:(Math.min(t.total,10)/10*70)+'px'}"></div>
              </div>
            </div>
            <div class="empty">每日告警总数(近30天)</div>
          </div>
          <div style="flex:1;min-width:220px">
            <h3>分类饼图</h3>
            <div class="row" style="gap:22px;flex-wrap:wrap;align-items:flex-start">
              <div v-if="pie.alerts_section.length">
                <div class="donut" :style="{background:donut(pie.alerts_section)}"></div>
                <div class="legend">
                  <span v-for="s in slices(pie.alerts_section)" :key="s.name">
                    <i :style="{background:s.color}"></i>{{ s.name }} <b class="num">{{ s.value }}</b>({{ s.pct }}%)
                  </span>
                </div>
              </div>
              <div v-if="pie.watch_types.length">
                <div class="donut" :style="{background:donut(pie.watch_types)}"></div>
                <div class="legend">
                  <span v-for="s in slices(pie.watch_types)" :key="s.name">
                    <i :style="{background:s.color}"></i>{{ s.name }} <b class="num">{{ s.value }}</b>({{ s.pct }}%)
                  </span>
                </div>
              </div>
            </div>
            <div class="empty">左:告警来源 · 右:抖音监测类型</div>
          </div>
        </div>
      </div>
      <div class="card">
        <h3>近 7 天明细</h3>
        <table><tr><th>日期</th><th>运行</th><th>告警</th></tr>
          <tr v-for="t in dash.trend" :key="t.date"><td>{{ t.date }}</td><td>{{ t.runs }}</td><td>{{ t.alerts }}</td></tr>
        </table>
        <button v-if="can('data.export')" class="ghost" @click="download('alerts')">导出告警CSV</button>
        <button v-if="can('data.export')" class="ghost" @click="download('users')">导出用户CSV</button>
      </div>
    </template>

    <template v-if="tab==='insights'">
      <div class="grid" style="margin-bottom:16px">
        <div class="card"><h3>用户数</h3><div class="price" style="font-size:26px">{{ insights.stats.users || 0 }}</div></div>
        <div class="card"><h3>关注词</h3><div class="price" style="font-size:26px">{{ insights.stats.watch_keywords || 0 }}</div></div>
        <div class="card"><h3>🔥 爆发预警</h3><div class="price" style="font-size:26px;color:var(--down)">{{ insights.stats.burst || 0 }}</div></div>
        <div class="card"><h3>上升期</h3><div class="price" style="font-size:26px;color:var(--up)">{{ insights.stats.rising || 0 }}</div></div>
        <div class="card"><h3>今日告警</h3><div class="price" style="font-size:26px">{{ insights.stats.today_alerts || 0 }}</div></div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <h3>🔥 预测爆发关键词(跨用户)</h3>
        <table v-if="insights.burst.length"><tr><th>关键词</th><th>趋势</th><th>环比</th><th>预测</th><th>首次上涨</th><th>持续</th><th>峰值</th><th>置信</th></tr>
          <tr v-for="b in insights.burst" :key="b.keyword+'-'+b.user_id">
            <td>{{ b.keyword }}</td>
            <td :class="b.trend_label==='上升期'?'up':''">{{ b.trend_label }}</td>
            <td class="num" :class="{up:(b.growth||0)>0}">{{ b.growth!=null?(b.growth*100).toFixed(0)+'%':'—' }}</td>
            <td class="num">{{ b.forecast_next!=null?Math.round(b.forecast_next):'—' }}</td>
            <td class="num">{{ b.first_rise ? b.first_rise.slice(5,16) : '—' }}</td>
            <td class="num">{{ b.duration_hours!=null ? b.duration_hours+'h' : '—' }}</td>
            <td class="num">{{ b.peak_value!=null?Math.round(b.peak_value):'—' }}</td>
            <td>{{ b.confidence }}</td>
          </tr>
        </table>
        <div v-else class="empty">暂无爆发关键词(需先有关注词并积累多轮采集)</div>
      </div>

      <div class="grid">
        <div class="card">
          <h3>📈 上升期关键词</h3>
          <table v-if="insights.rising.length"><tr><th>关键词</th><th>环比</th><th>预测</th></tr>
            <tr v-for="r in insights.rising" :key="r.keyword+'-'+r.user_id"><td>{{ r.keyword }}</td><td class="num up">{{ r.growth!=null?(r.growth*100).toFixed(0)+'%':'—' }}</td><td class="num">{{ r.forecast_next!=null?Math.round(r.forecast_next):'—' }}</td></tr>
          </table>
          <div v-else class="empty">暂无</div>
        </div>
        <div class="card">
          <h3>🔥 抖音内容词 Top</h3>
          <table v-if="insights.hot_words.length"><tr><th>词</th><th>飙升</th><th>趋势</th></tr>
            <tr v-for="h in insights.hot_words" :key="h.title"><td>{{ h.title }}</td><td class="price num">{{ (h.score/1e4).toFixed(0) }}万</td><td :class="{up:h.trend_delta>0}">{{ h.trend_delta>0?'↑':(h.trend_delta<0?'↓':'—') }}</td></tr>
          </table>
          <div v-else class="empty">暂无</div>
        </div>
      </div>

      <div class="grid" style="margin-top:16px">
        <div class="card">
          <h3>📈 微博热点预测</h3>
          <table v-if="insights.weibo && insights.weibo.length"><tr><th>词</th><th>趋势</th><th>环比</th><th>预测</th></tr>
            <tr v-for="w in insights.weibo" :key="w.title"><td>{{ w.title.slice(0,16) }}</td><td :class="{up:w.trend_label==='上升期'}">{{ w.trend_label }}</td><td class="num up">{{ w.growth!=null?(w.growth*100).toFixed(0)+'%':'—' }}</td><td class="num">{{ Math.round(w.forecast_next||0) }}</td></tr>
          </table>
          <div v-else class="empty">暂无(需采集微博积累多轮)</div>
        </div>
        <div class="card">
          <h3>📈 闲鱼商品预测</h3>
          <table v-if="insights.xianyu && insights.xianyu.length"><tr><th>商品</th><th>趋势</th><th>环比</th><th>预测</th></tr>
            <tr v-for="x in insights.xianyu" :key="x.title"><td>{{ x.title.slice(0,16) }}</td><td :class="{up:x.trend_label==='上升期'}">{{ x.trend_label }}</td><td class="num up">{{ x.growth!=null?(x.growth*100).toFixed(0)+'%':'—' }}</td><td class="num">{{ Math.round(x.forecast_next||0) }}</td></tr>
          </table>
          <div v-else class="empty">暂无(需闲鱼深度采集积累每日想要数)</div>
        </div>
      </div>
    </template>

    <template v-if="tab==='users'">
      <div class="row" style="gap:8px;margin-bottom:8px">
        <input v-model="q" placeholder="搜索用户名/邮箱" style="margin:0" @keyup.enter="searchUsers" />
        <button @click="searchUsers">搜索</button>
      </div>
      <div class="card" style="margin-bottom:10px" v-if="can('users.import')">
        <h3>批量导入用户(每行:email,password[,role])</h3>
        <textarea v-model="importText" placeholder="a@x.com,123456,admin&#10;b@y.com,123456"></textarea>
        <button @click="doImport">导入</button>
      </div>
      <div class="card" v-if="detail">
        <h3>用户详情 #{{ detail.user.id }} {{ detail.user.username }}
          <button class="ghost" @click="detail=null">关闭</button></h3>
        <p class="empty">邮箱 {{ detail.user.email }} · 角色 {{ detail.user.role }} · {{ detail.user.enabled?'启用':'禁用' }} · SMTP {{ detail.user.smtp?'√':'—' }} · 创建 {{ detail.user.created }}</p>
        <div class="grid" style="margin-bottom:8px">
          <div><h4 style="color:var(--dim);margin:6px 0">Cookie</h4>
            <div v-for="c in detail.cookies" :key="c.platform" class="empty">{{ c.platform }}: {{ c.configured ? c.preview : '未配置' }}</div>
          </div>
          <div><h4 style="color:var(--dim);margin:6px 0">预警规则</h4>
            <div v-for="r in detail.rules" :key="r.id" class="empty">{{ r.section }}/{{ r.rule_type }} {{ r.metric||'' }}>{{ r.threshold??'' }} {{ r.keyword?'@'+r.keyword:'' }} {{ r.enabled?'':'[停用]' }}</div>
            <div v-if="!detail.rules.length" class="empty">无规则</div>
          </div>
        </div>
        <h4 style="color:var(--dim);margin:6px 0">最近告警</h4>
        <table><tr><th>项</th><th>说明</th><th>时间</th></tr>
          <tr v-for="(a,i) in detail.alerts.slice(0,10)" :key="i"><td>{{ a.keyword }}</td><td class="empty">{{ a.reason }}</td><td class="empty">{{ a.time }}</td></tr>
        </table>
        <div v-if="!detail.alerts.length" class="empty">无告警</div>
      </div>
      <div class="card">
        <table><tr><th>ID</th><th>用户名</th><th>邮箱</th><th>角色</th><th>SMTP</th><th>状态</th><th></th></tr>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.id }}</td><td>{{ u.username }}</td><td>{{ u.email }}</td><td>{{ u.role }}</td>
            <td>{{ u.smtp ? '✓' : '—' }}</td>
            <td :class="{ up: u.enabled }">{{ u.enabled ? '启用' : '禁用' }}</td>
            <td>
              <button class="ghost" @click="openDetail(u)">详情</button>
              <button v-if="can('users.toggle')" class="ghost" @click="toggle(u)">{{ u.enabled ? '禁用' : '启用' }}</button>
              <button v-if="can('users.delete')" class="ghost" @click="del(u)">删除</button>
            </td>
          </tr>
        </table>
      </div>
    </template>

    <template v-if="tab==='data'">
      <div class="row" style="gap:8px;margin-bottom:8px">
        <button v-for="s in [['weibo','微博'],['xianyu','闲鱼'],['douhot','抖音']]" :key="s[0]"
          :class="dataSection===s[0]?'':'ghost'" @click="dataSection=s[0];loadData()">{{ s[1] }}</button>
      </div>
      <div class="card">
        <table><tr><th>用户</th><th>内容</th><th>指标</th><th>时间</th></tr>
          <tr v-for="(d,i) in dataRef.slice(0,60)" :key="i">
            <td>{{ d.user_id }}</td>
            <td>{{ (d.title || d.keyword || '').slice(0,32) }}</td>
            <td class="price">{{ d.heat ?? d.score ?? d.hit_keywords ?? '' }}</td>
            <td class="empty">{{ d.captured_at || d.created_at || '' }}</td>
          </tr>
        </table>
        <div v-if="!dataRef.length" class="empty">暂无数据</div>
      </div>
    </template>

    <template v-if="tab==='ops'">
      <div class="card">
        <h3>采集失败运行(近24h 自动重试≤3次,可手动)</h3>
        <table><tr><th>运行</th><th>板块</th><th>用户</th><th>错误</th><th>重试</th><th></th></tr>
          <tr v-for="r in failed" :key="r.run_id">
            <td>{{ r.run_id }}</td><td>{{ r.kind }}</td><td>#{{ r.user_id }}</td>
            <td class="empty">{{ r.detail.slice(0,40) }}</td><td>{{ r.retry }}</td>
            <td><button class="ghost" v-if="can('users.toggle')" @click="retryRun(r.run_id)">重试</button></td>
          </tr>
        </table>
        <div v-if="!failed.length" class="empty">暂无失败运行</div>
        <p class="empty">系统每 30 分钟自动重试近24h失败且重试<3次的采集</p>
      </div>
    </template>

    <template v-if="tab==='logs'">
      <div class="card" style="margin-bottom:14px"><h3>登录日志</h3>
        <table><tr><th>账号</th><th>IP</th><th>UA</th><th>结果</th><th>时间</th></tr>
          <tr v-for="(l,i) in logins.slice(0,30)" :key="i"><td>{{ l.username }}</td><td>{{ l.ip }}</td><td class="empty">{{ l.ua }}</td><td :class="l.ok?'up':'error'">{{ l.ok?'成功':'失败' }}</td><td class="empty">{{ l.time }}</td></tr>
        </table>
      </div>
      <div class="card"><h3>操作日志</h3>
        <table><tr><th>管理员</th><th>动作</th><th>对象</th><th>时间</th></tr>
          <tr v-for="(l,i) in adminlogs.slice(0,30)" :key="i"><td>{{ l.admin }}</td><td>{{ l.action }}</td><td>{{ l.target }}</td><td class="empty">{{ l.time }}</td></tr>
        </table>
      </div>
    </template>

    <template v-if="tab==='config'">
      <div class="card"><h3>系统设置</h3>
        <table><tr><th>键</th><th>值</th><th></th></tr>
          <tr v-for="c in config" :key="c.key"><td>{{ c.key }}</td><td>{{ c.value }}</td><td><button v-if="can('config.set')" class="ghost" @click="setCfg(c.key)">修改</button></td></tr>
        </table>
        <div v-if="!config.length" class="empty">暂无配置项</div>
      </div>
    </template>
  </div>
</template>
