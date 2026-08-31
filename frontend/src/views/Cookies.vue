<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const drafts = ref({})
const msg = ref('')
const smtp = ref({ host: '', port: 465, user: '', password: '', from_name: '' })

const labels = { weibo: '微博', baidu: '百度', douyin: '抖音(热点宝)', goofish: '闲鱼' }

async function load() {
  items.value = await api.cookies()
  try { smtp.value = await api.userSmtpGet() } catch {}
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
async function remove(p) { await api.delCookie(p); drafts.value[p] = ''; msg.value = `${labels[p]} Cookie 已删除`; await load() }
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
        </div>
      </div>
    </div>
  </div>
</template>
