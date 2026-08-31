<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

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

async function load() {
  msg.value = ''
  try {
    dash.value = await api.adminDashboard()
    users.value = await api.adminUsers()
    logins.value = await api.adminLogins()
    adminlogs.value = await api.adminLogs()
    config.value = await api.adminConfig()
    cats.value = await api.adminCategories()
  } catch (e) { msg.value = '加载失败:' + e.message }
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
      <button :class="tab==='users'?'':'ghost'" @click="tab='users'">用户管理</button>
      <button :class="tab==='data'?'':'ghost'" @click="tab='data';loadData()">数据</button>
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
            <div :title="`${t.date} 运行${t.runs}/告警${t.alerts}`" style="margin:0 auto;width:60%;background:var(--accent);opacity:.7" :style="{height:(t.runs/tmax()*70)+'px'}"></div>
            <div :title="`${t.date} 告警${t.alerts}`" style="margin:0 auto;width:60%;background:var(--up);opacity:.7" :style="{height:(t.alerts/tmax()*70)+'px'}"></div>
          </div>
        </div>
        <div class="empty">蓝=运行 绿=告警(近30天)</div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <h3>闲鱼类目分布(数量/想要)</h3>
        <div v-if="cats.length" class="row" style="flex-wrap:wrap;gap:10px">
          <div v-for="c in cats" :key="c.name" class="badge" style="font-size:13px">{{ c.name }} ×{{ c.count }} / 想要{{ c.want }}</div>
        </div>
        <div v-else class="empty">暂无类目数据(闲鱼深度采集后)</div>
      </div>
      <div class="card">
        <h3>近 7 天明细</h3>
        <table><tr><th>日期</th><th>运行</th><th>告警</th></tr>
          <tr v-for="t in dash.trend" :key="t.date"><td>{{ t.date }}</td><td>{{ t.runs }}</td><td>{{ t.alerts }}</td></tr>
        </table>
        <button class="ghost" @click="download('alerts')">导出告警CSV</button>
        <button class="ghost" @click="download('users')">导出用户CSV</button>
      </div>
    </template>

    <template v-if="tab==='users'">
      <div class="row" style="gap:8px;margin-bottom:8px">
        <input v-model="q" placeholder="搜索用户名/邮箱" style="margin:0" @keyup.enter="searchUsers" />
        <button @click="searchUsers">搜索</button>
      </div>
      <div class="card" style="margin-bottom:10px">
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
              <button class="ghost" @click="toggle(u)">{{ u.enabled ? '禁用' : '启用' }}</button>
              <button class="ghost" @click="del(u)">删除</button>
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
          <tr v-for="c in config" :key="c.key"><td>{{ c.key }}</td><td>{{ c.value }}</td><td><button class="ghost" @click="setCfg(c.key)">修改</button></td></tr>
        </table>
        <div v-if="!config.length" class="empty">暂无配置项</div>
      </div>
    </template>
  </div>
</template>
