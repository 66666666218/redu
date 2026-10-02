<template>
  <div>
    <h2>迅雷群组 · 资源采集</h2>
    <p class="muted">
      群主在群里发的分享卡<b>自带迅雷分享链</b>，所以「口令 → 分享 id」这一步由群组替我们完成。
      采集只登记（秒级、可高频），<b>转存限量</b>（每条要真的存进你的盘 + 生成我方分享链，慢且占空间）。
    </p>
    <p v-if="msg" :style="{ color: msg.includes('失败') ? '#c00' : '#080' }">{{ msg }}</p>

    <div class="card" style="margin-bottom:14px">
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <span class="muted">群 {{ groups.length }} 个 · 分享 {{ items.length }} 条（待转存 {{ pendingCount }}）</span>
        <button :disabled="busy" @click="doSync">{{ busy === 'sync' ? '采集中…' : '采集一轮' }}</button>
        <button :disabled="busy" @click="doTransfer">
          {{ busy === 'transfer' ? '转存中（每条最慢约 1 分钟）…' : '转存 3 条' }}
        </button>
        <button class="ghost" @click="load">刷新</button>
        <button class="ghost" :disabled="busy" @click="doMint">
          {{ busy === 'mint' ? '补铸中（约 30 秒）…' : '补铸 captcha' }}
        </button>
      </div>
      <div class="muted" style="font-size:12px;margin-top:8px">
        已加入：{{ groups.map(g => g.name || g.group_id).join('、') || '未取到' }}
      </div>
      <div class="muted" style="font-size:12px">
        {{ captchaText }}
      </div>
      <div :style="{ fontSize: '12px', color: quota.full ? '#c00' : '#888' }">
        {{ quotaText }}
      </div>
    </div>

    <div class="card">
      <table v-if="items.length">
        <tr><th>群</th><th>资源</th><th>状态</th><th>群主原链</th><th>我方分享链</th><th>消息时间</th></tr>
        <tr v-for="it in items" :key="it.group_id + it.origin_url">
          <td style="font-size:12px">{{ it.group_name || it.group_id }}</td>
          <td>{{ it.title || '—' }}</td>
          <td>
            <span v-if="it.status === 'ok'" style="color:#080">已转存</span>
            <span v-else-if="it.status === 'skipped'" style="color:#b60" :title="it.message">已跳过</span>
            <span v-else-if="it.status === 'failed'" style="color:#c00" :title="it.message">失败</span>
            <span v-else class="muted">待转存</span>
          </td>
          <td style="font-size:12px">
            <a v-if="it.origin_url" :href="it.origin_url" target="_blank" rel="noopener">原链</a>
          </td>
          <td style="font-size:12px">
            <a v-if="it.our_url" :href="it.our_url" target="_blank" rel="noopener">
              我方链<span v-if="it.pass_code">（码 {{ it.pass_code }}）</span>
            </a>
            <span v-else class="muted">—</span>
          </td>
          <td class="muted" style="font-size:12px">{{ it.msg_time || '—' }}</td>
        </tr>
      </table>
      <p v-else class="empty">
        还没有采到——点「采集一轮」，或等定时任务（默认每 20 分钟一轮）
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const items = ref([])
const groups = ref([])
const captcha = ref({})
const quota = ref({})
const msg = ref('')
const busy = ref('')

const pendingCount = computed(() => items.value.filter(i => i.status === 'pending').length)
// 盘空间:转存闸门按它判 —— 到 90% 就整批不搬(2026-10-02 被大合集顶爆过一次)
const quotaText = computed(() => {
  if (!quota.value.ok) return `盘空间:${quota.value.message || '取不到'}`
  const pct = (Number(quota.value.ratio || 0) * 100).toFixed(1)
  return `盘空间:已用 ${quota.value.usage_text} / ${quota.value.limit_text}(${pct}%)`
    + (quota.value.full ? ' —— 已达闸门,转存已自动暂停,请先清理' : '')
})
// captcha 寿命只有十几分钟,转存时会自动补铸;这里只是把状态摆出来给人看
const captchaText = computed(() => {
  const t = captcha.value.last_minted_at
  if (!t) return 'captcha:还没铸过(转存时会自动铸)'
  const at = new Date(Number(t) * 1000).toLocaleTimeString()
  return `captcha:${at} 铸${captcha.value.last_error ? `(最近一次失败:${captcha.value.last_error})` : ''}`
})

async function load() {
  try {
    const [r, g, c, q] = await Promise.all([
      api.get('/api/xunlei/shares'),
      api.get('/api/xunlei/groups'),
      api.get('/api/xunlei/captcha'),
      api.get('/api/xunlei/quota'),
    ])
    items.value = r.data.items
    groups.value = g.data.items
    captcha.value = c.data || {}
    quota.value = q.data || {}
    msg.value = ''
  } catch (e) {
    msg.value = '加载失败:' + (e?.response?.data?.detail || e.message || e)
  }
}

async function doMint() {
  busy.value = 'mint'
  msg.value = '补铸中…（会开一次无头浏览器，约 30 秒）'
  try {
    const d = (await api.post('/api/xunlei/captcha/refresh')).data || {}
    msg.value = d.ok ? '已补铸一枚新 captcha' : '补铸失败 —— 可能登录态过期了，需要重新扫码'
    await load()
  } catch (e) {
    msg.value = '补铸失败：' + (e?.response?.data?.detail || e.message || e)
  } finally {
    busy.value = ''
  }
}

async function doSync() {
  busy.value = 'sync'
  msg.value = '采集中…'
  try {
    const d = (await api.post('/api/xunlei/sync')).data || {}
    msg.value = `采集完成：扫了 ${d.groups ?? 0} 个群，新增 ${d.new ?? 0} 条`
      + (d.status && d.status !== 'ok' ? `（状态：${d.status}）` : '')
    await load()
  } catch (e) {
    msg.value = '采集失败：' + (e?.response?.data?.detail || e.message || e)
  } finally {
    busy.value = ''
  }
}

async function doTransfer() {
  busy.value = 'transfer'
  msg.value = '转存中…（会真实写入你的迅雷盘，请勿关闭页面）'
  try {
    const d = (await api.post('/api/xunlei/transfer?limit=3')).data || {}
    msg.value = `转存完成：挑出 ${d.picked ?? 0} 条，成功 ${d.ok ?? 0}、失败 ${d.failed ?? 0}`
    await load()
  } catch (e) {
    msg.value = '转存失败：' + (e?.response?.data?.detail || e.message || e)
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>
