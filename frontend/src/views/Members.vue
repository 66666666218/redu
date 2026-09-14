<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import { toastOk, toastError } from '../toast'

const items = ref([])
const msg = ref('')
const form = ref({ nickname: '', wechat_id: '', group_name: '', joined_at: new Date().toISOString().slice(0, 10), cycle_days: 30, note: '' })

const stateLabel = { ok: '✅ 有效期内', due: '📞 该收续费', overdue: '❌ 该踢出', kicked: '已踢出', exempt: '已豁免' }
const sorted = computed(() => {
  // 该收/该踢排前,其余按剩余天数升序
  const w = { overdue: 0, due: 1, ok: 2, exempt: 3, kicked: 4 }
  return [...items.value].sort((a, b) => (w[a.state] - w[b.state]) || (a.remaining_days - b.remaining_days))
})

async function load() {
  msg.value = ''
  try { items.value = await api.members() } catch (e) { msg.value = '加载失败:' + e.message }
}
async function add() {
  msg.value = ''
  if (!form.value.nickname.trim()) { msg.value = '昵称不能为空'; return }
  try {
    await api.memberAdd(form.value)
    toastOk('已添加')
    form.value.nickname = ''; form.value.wechat_id = ''; form.value.note = ''
    await load()
  } catch (e) { toastError(e.message) }
}
async function renew(m) {
  try { await api.memberRenew(m.id); toastOk(`${m.nickname} 已续费,周期刷新`); await load() }
  catch (e) { toastError(e.message) }
}
async function mark(m, status) {
  try { await api.memberStatus(m.id, status); await load() } catch (e) { toastError(e.message) }
}
async function del(m) {
  if (!confirm(`删除成员「${m.nickname}」?`)) return
  try { await api.memberDel(m.id); await load() } catch (e) { toastError(e.message) }
}

onMounted(load)
</script>

<template>
  <div>
    <h2>群会员续费管理</h2>
    <p class="muted">按入群时间+周期自动算到期;每日 10:05 飞书推送"该收续费/该踢"名单。私信与踢群手动处理(微信无合规自动踢人 API,防封号)。</p>

    <div class="card" style="margin:12px 0">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
        <label>昵称* <input v-model="form.nickname" placeholder="群昵称" style="width:120px"/></label>
        <label>微信号 <input v-model="form.wechat_id" placeholder="私信用,可空" style="width:110px"/></label>
        <label>群 <input v-model="form.group_name" placeholder="如:资源1群" style="width:90px"/></label>
        <label>入群日期 <input v-model="form.joined_at" type="date" style="width:140px"/></label>
        <label>周期(天) <input v-model.number="form.cycle_days" type="number" min="1" style="width:70px"/></label>
        <label>备注 <input v-model="form.note" style="width:140px"/></label>
        <button @click="add">添加成员</button>
      </div>
      <p v-if="msg" style="color:#c00">{{ msg }}</p>
    </div>

    <table>
      <thead><tr><th>昵称</th><th>微信号</th><th>群</th><th>入群</th><th>到期日</th><th>状态</th><th>备注</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="m in sorted" :key="m.id">
          <td>{{ m.nickname }}</td>
          <td>{{ m.wechat_id || '—' }}</td>
          <td>{{ m.group_name || '—' }}</td>
          <td>{{ m.joined_at.slice(0, 10) }}</td>
          <td>{{ m.due_date.slice(0, 10) }}<span class="muted"> ({{ m.remaining_days > 0 ? '剩' + Math.ceil(m.remaining_days) + '天' : '超' + Math.abs(Math.floor(m.remaining_days)) + '天' }})</span></td>
          <td>{{ stateLabel[m.state] || m.state }}</td>
          <td>{{ m.note || '—' }}</td>
          <td style="white-space:nowrap">
            <button class="ghost" @click="renew(m)">续费</button>
            <button class="ghost" v-if="m.state !== 'exempt'" @click="mark(m, 'exempt')">豁免</button>
            <button class="ghost" v-if="m.state === 'exempt'" @click="mark(m, 'active')">取消豁免</button>
            <button class="ghost" v-if="m.state !== 'kicked'" @click="mark(m, 'kicked')">已踢出</button>
            <button class="ghost" @click="del(m)">删除</button>
          </td>
        </tr>
        <tr v-if="!sorted.length"><td colspan="8" class="muted">还没有成员,先在上方添加</td></tr>
      </tbody>
    </table>
  </div>
</template>
