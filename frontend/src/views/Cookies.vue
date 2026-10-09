<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const drafts = ref({})
const msg = ref('')
const smtp = ref({ host: '', port: 465, user: '', password: '', from_name: '' })
// 公众号后台(wemp)凭据:cookie + token 两个字段,单独一块(通用 Cookie 那套是单字段的)
const wemp = ref({ cookie: '', token: '' })
const wempState = ref({ configured: false, cookie_len: 0, token_len: 0 })
const wempMsg = ref('')
const wempOk = ref(false)

// ⚠️ **闲鱼的「扫码登录」按钮已删除**(2026-10-03)。
// 它原走纯协议二维码流程、把登录态写进 `cookie_store`;但 2026-10-02 起采集默认走
// **浏览器档案**(`xianyu_browser`),`tenant.run_xianyu` 在浏览器模式下**不读也不校验**那个
// cookie —— 于是那个按钮**扫了完全没效果,却会显示「✅ 登录成功」**(比报错更糟:用户以为修好了)。
// **现在闲鱼登录的正确做法**见本页闲鱼那行的说明。实测:摘除后采集照常(count=90)。

const labels = { weibo: '微博', baidu: '百度', douyin: '抖音(热点宝)', goofish: '闲鱼', baidupan: '百度网盘', quark: '夸克网盘', weread: '微信读书' }

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
async function loadWemp() {
  try { wempState.value = await api.wempCredGet() } catch (e) { console.debug('wemp 未配置', e) }
}
async function saveWemp() {
  wempMsg.value = ''; wempOk.value = false
  if (!wemp.value.cookie || !wemp.value.token) {
    wempMsg.value = 'cookie 与 token 都要填(缺任何一个后台凭据都用不了)'; return
  }
  try {
    const out = await api.wempCredPut(wemp.value.cookie, wemp.value.token)
    wempOk.value = true
    wempMsg.value = out.items >= 0
      ? `已保存并验活 ✓ 探针「${out.mp}」拿到 ${out.items} 篇`
      : `已保存(${out.note || '库里还没有可试的对标号,跳过探针'})`
    wemp.value = { cookie: '', token: '' }
    await loadWemp()
  } catch (e) { wempMsg.value = e.message }
}
async function delWemp() {
  wempMsg.value = ''
  try { await api.wempCredDel(); wempMsg.value = 'wemp 凭据已清除'; await loadWemp() }
  catch (e) { wempMsg.value = '清除失败:' + e.message }
}
async function saveSmtp() {
  msg.value = ''
  try { await api.userSmtpPut(smtp.value); msg.value = '告警邮箱已保存(预警发到该邮箱)' } catch (e) { msg.value = e.message }
}
onMounted(async () => { await load(); await loadWemp() })
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
    <!-- 公众号后台(wemp):**唯一需要两个字段的凭据** —— 它不走通用 Cookie 那套(见后端注释) -->
    <div class="card" style="margin-bottom:16px">
      <div class="row">
        <h3 style="margin:0">公众号后台凭据(列表源兜底 wemp)</h3>
        <span class="badge">{{ wempState.configured ? '已配置' : '未配置' }}</span>
      </div>
      <p class="empty">
        浏览器登录 <b>mp.weixin.qq.com</b> → F12 复制<b>整条 Cookie</b>,
        再把地址栏里 <code>?token=…</code> 那段粘到下面第二个框。保存时会**当场打一枪验活**。
      </p>
      <textarea v-model="wemp.cookie" :placeholder="wempState.configured
        ? `Cookie(已配置 ${wempState.cookie_len} 字符;留空则不修改)` 
        : '粘贴整条 Cookie'"></textarea>
      <input v-model="wemp.token" placeholder="地址栏里的 token" style="margin-top:6px" />
      <div class="row">
        <button @click="saveWemp">保存并验活</button>
        <button class="ghost" v-if="wempState.configured" @click="delWemp">清除</button>
      </div>
      <!-- ⚠️ 两种失败要说清分别怎么办:会话失效=重登;被限流=凭据是好的,等配额 -->
      <div :class="wempOk ? 'ok' : 'empty'" style="font-size:13px;margin-top:6px" v-if="wempMsg">
        {{ wempMsg }}
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
        <!-- ⚠️ 闲鱼这里原本有个「扫码登录」按钮,2026-10-03 已删 —— 它扫了不生效
             (采集走浏览器档案,不读这里存的 cookie),却显示"登录成功"。
             正确做法写在这行里,免得用户找不到入口。 -->
        <div class="empty" style="font-size:12px;margin-top:6px" v-if="c.platform === 'goofish'">
          闲鱼登录态在<b>浏览器档案</b>里,不在这里的 Cookie —— 需要重登时,在项目目录跑
          <code>python scripts/xianyu_login.py</code>(会打开浏览器,登完关窗口即可)。
        </div>
      </div>
    </div>
  </div>
</template>
