<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const drafts = ref({})
const msg = ref('')

const labels = { weibo: '微博', baidu: '百度', douyin: '抖音(热点宝)', goofish: '闲鱼' }

async function load() {
  items.value = await api.cookies()
}
function setDraft(p, v) {
  drafts.value[p] = v
}
async function save(p) {
  msg.value = ''
  try {
    await api.setCookie(p, drafts.value[p] || '')
    msg.value = `${labels[p]} Cookie 已保存`
    await load()
  } catch (e) { msg.value = e.message }
}
async function remove(p) {
  await api.delCookie(p)
  drafts.value[p] = ''
  msg.value = `${labels[p]} Cookie 已删除`
  await load()
}
onMounted(async () => { await load() })
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:16px">
      <h2 style="margin:0">各平台 Cookie</h2>
      <span class="empty">每个用户配置自己去平台获取的 Cookie,仅本人采集用</span>
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
