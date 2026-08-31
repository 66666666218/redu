<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const tab = ref('dashboard')
const dash = ref({ counts: {}, today_runs: 0, trend: [] })
const users = ref([])
const logins = ref([])
const adminlogs = ref([])
const config = ref([])
const msg = ref('')
const q = ref('')

async function load() {
  msg.value = ''
  try {
    dash.value = await api.adminDashboard()
    users.value = await api.adminUsers()
    logins.value = await api.adminLogins()
    adminlogs.value = await api.adminLogs()
    config.value = await api.adminConfig()
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
async function download(kind) {
  const r = await (kind === 'users' ? api.adminExportUsers() : api.adminExportAlerts())
  const blob = await r.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = kind + '.csv'; a.click()
}
const c = (k) => dash.value.counts[k] || 0
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
      <button :class="tab==='logs'?'':'ghost'" @click="tab='logs'">日志审计</button>
      <button :class="tab==='config'?'':'ghost'" @click="tab='config'">系统设置</button>
    </div>

    <template v-if="tab==='dashboard'">
      <div class="grid" style="margin-bottom:14px">
        <div class="card"><h3>用户</h3><div class="price" style="font-size:26px">{{ c('users') }}</div><span class="empty">启用 {{ c('enabled_users') }} · 管理员 {{ c('admins') }}</span></div>
        <div class="card"><h3>今日运行</h3><div class="up" style="font-size:26px">{{ dash.today_runs }}</div><span class="empty">累计 {{ c('runs') }}</span></div>
        <div class="card"><h3>告警</h3><div class="price" style="font-size:26px">{{ c('alerts') }}</div><span class="empty">微博{{ c('weibo_items') }}/闲鱼{{ c('xianyu_items') }}/抖音{{ c('douhot_words') }}</span></div>
      </div>
      <div class="card">
        <h3>近 7 天趋势</h3>
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
      <div class="card">
        <table><tr><th>ID</th><th>用户名</th><th>邮箱</th><th>角色</th><th>SMTP</th><th>状态</th><th></th></tr>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.id }}</td><td>{{ u.username }}</td><td>{{ u.email }}</td><td>{{ u.role }}</td>
            <td>{{ u.smtp ? '✓' : '—' }}</td>
            <td :class="{ up: u.enabled }">{{ u.enabled ? '启用' : '禁用' }}</td>
            <td>
              <button class="ghost" @click="toggle(u)">{{ u.enabled ? '禁用' : '启用' }}</button>
              <button class="ghost" @click="del(u)">删除</button>
            </td>
          </tr>
        </table>
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
