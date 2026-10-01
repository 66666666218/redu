<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const drafts = ref({})
const msg = ref('')
const smtp = ref({ host: '', port: 465, user: '', password: '', from_name: '' })

const qr = ref({ show: false, png: '', status: '', text: '' })
let qrTimer = null

function closeQr() {
  qr.value.show = false
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null }
}

async function startScan() {
  closeQr()
  try {
    const r = await api.post('/api/cookies/goofish/qr-start')
    qr.value = { show: true, png: r.data.qr_png, status: 'waiting', text: '等待扫码…' }
    qrTimer = setInterval(async () => {
      try {
        const s = await api.get('/api/cookies/goofish/qr-status', { params: { session_id: r.data.session_id } })
        const st = s.data.status
        qr.value.status = st
        qr.value.text = s.data.message || { waiting: '等待扫码…', scanned: '已扫码,请在手机上确认登录', confirmed: '确认中…', success: '✅ 登录成功,Cookie 已入库' }[st] || st
        if (st === 'success') {
          clearInterval(qrTimer); qrTimer = null
          await load()   // 刷新 Cookie 列表状态
        } else if (st === 'expired' || st === 'failed' || st === 'not_found') {
          clearInterval(qrTimer); qrTimer = null
        }
      } catch { /* 单次轮询失败忽略 */ }
    }, 2500)
  } catch (e) {
    msg.value = '二维码生成失败:' + (e?.response?.data?.detail || e.message || e)
  }
}

const labels = { weibo: '微博', baidu: '百度', douyin: '抖音(热点宝)', goofish: '闲鱼', baidupan: '百度网盘', quark: '夸克网盘', weread: '微信读书', dajiala: 'dajiala(付费接口)' }

async function load() {
  try {
    items.value = await api.cookies()
  } catch (e) { msg.value = 'Cookie 状态加载失败:' + e.message }
  try { smtp.value = await api.userSmtpGet() } catch (e) { console.debug('SMTP 未配置', e) }
}
function setDraft(p, v) { drafts.value[p] = v }
async function save(p) {
  msg.value = ''
  try {
    await api.setCookie(p, drafts.value[p] || '')
    msg.value = `${labels[p]} Cookie 已保存`
    await load()
  } catch (e) { msg.value = e.message }
}
async function remove(p) {
  msg.value = ''
  try {
    await api.delCookie(p); drafts.value[p] = ''; msg.value = `${labels[p]} Cookie 已删除`; await load()
  } catch (e) { msg.value = '删除失败:' + e.message }
}
async function saveSmtp() {
  msg.value = ''
  try { await api.userSmtpPut(smtp.value); msg.value = '告警邮箱已保存(预警发到该邮箱)' } catch (e) { msg.value = e.message }
}
onMounted(async () => { await load() })
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:16px">
      <h2 style="margin:0">各平台 Cookie</h2>
      <span class="empty">每个用户配置自己去平台获取的 Cookie,仅本人采集用</span>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h3>告警邮箱(SMTP,可选)</h3>
      <p class="empty">填了就用你邮箱发预警;不填则用系统全局 SMTP(收件人 NOTIFY_TO)</p>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <input v-model="smtp.host" placeholder="SMTP主机" style="flex:1;margin:0" />
        <input v-model.number="smtp.port" placeholder="端口" style="width:70px;margin:0" />
        <input v-model="smtp.user" placeholder="发件账号" style="flex:1;margin:0" />
        <input v-model="smtp.password" type="password" placeholder="授权码" style="flex:1;margin:0" />
        <input v-model="smtp.from_name" placeholder="发件人显示名" style="width:120px;margin:0" />
        <button @click="saveSmtp">保存</button>
      </div>
    </div>
    <span v-if="msg" class="ok">{{ msg }}</span>

    <div class="grid">
      <div class="card" v-for="c in items" :key="c.platform">
        <div class="row">
          <h3 style="margin:0">{{ labels[c.platform] || c.platform }}</h3>
          <span class="badge">{{ c.configured ? '已配置' : '未配置' }}</span>
        </div>
        <textarea
          :placeholder="'粘贴 ' + labels[c.platform] + ' 的 Cookie'"
          :value="drafts[c.platform]"
          @input="setDraft(c.platform, $event.target.value)"
        ></textarea>
        <div class="row">
          <button @click="save(c.platform)">保存</button>
          <button class="ghost" v-if="c.configured" @click="remove(c.platform)">删除</button>
          <button v-if="c.platform === 'goofish'" @click="startScan">📱 扫码登录</button>
        </div>
      </div>
    </div>

    <!-- 闲鱼扫码登录弹窗(v2.12.0 可视化一键扫码) -->
    <div v-if="qr.show" style="position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:99" @click.self="closeQr">
      <div class="card" style="max-width:360px;text-align:center">
        <h3 style="margin-top:0">闲鱼扫码登录</h3>
        <img v-if="qr.png" :src="'data:image/png;base64,' + qr.png" style="width:260px;height:260px" alt="二维码" />
        <p :style="{ color: qr.status === 'success' ? '#080' : qr.status === 'failed' || qr.status === 'expired' ? '#c00' : '' }">
          {{ qr.text }}
        </p>
        <p class="empty" style="font-size:12px">手机闲鱼 App → 我的 → 右上角扫一扫</p>
        <div class="row" style="justify-content:center">
          <button v-if="qr.status === 'waiting' || qr.status === 'scanned'" class="ghost" @click="closeQr">取消</button>
          <button v-else @click="startScan">重新生成二维码</button>
          <button v-if="qr.status === 'success'" @click="closeQr">完成</button>
        </div>
      </div>
    </div>
  </div>
</template>
