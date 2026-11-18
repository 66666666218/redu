<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { toastOk, toastError } from '../toast'

const items = ref([])
const msg = ref('')
const busy = ref('')
const status = ref('active')
const platformLabel = { weibo: '微博', baidu: '百度', douhot: '抖音', xianyu: '闲鱼' }

async function load() {
  msg.value = ''
  try { items.value = await api.events(`status=${status.value}`) } catch (e) { msg.value = e.message }
}
async function assign() {
  busy.value = 'assign'
  try {
    const r = await api.eventsAssign()
    //: ⚠️ 本机(wechat 侧)**不跑**事件归并 —— 事件是远程归并后拉取来的镜像,
    //: 生命周期归远程管。这里必须**说清没跑**,不能弹一句绿色的「新建 0 / 归并 0 / 终结 0」
    //: 让人以为跑过了(假成功,本仓反复栽过)。
    if (r.skipped) toastOk(r.note || '本机不跑事件归并')
    else toastOk(`归属完成:新建 ${r.created} / 归并 ${r.merged} / 终结 ${r.ended}`)
    await load()
  } catch (e) { toastError(e.message) } finally { busy.value = '' }
}

onMounted(load)
</script>

<template>
  <div>
    <h2>热点事件(Hotspot → Event)</h2>
    <p class="muted">多平台同主题标题自动归并为同一事件(规范化+相似度聚类,每 15 分钟归属一轮)。
      平台数≥2 = 跨平台共振;峰值/持续时间 = 生命周期基础事实。</p>

    <div style="display:flex;gap:8px;margin:12px 0;align-items:center">
      <select v-model="status" @change="load">
        <option value="active">进行中</option>
        <option value="ended">已终结</option>
      </select>
      <button @click="assign" :disabled="busy==='assign'">{{ busy==='assign' ? '归属中…' : '立即归属一轮' }}</button>
      <span v-if="msg" style="color:#c00">{{ msg }}</span>
    </div>

    <table>
      <thead><tr><th>事件</th><th>平台</th><th>峰值热度</th><th>峰值时刻</th><th>首见</th><th>持续</th><th>快照数</th><th>阶段</th></tr></thead>
      <tbody>
        <tr v-for="e in items" :key="e.id">
          <td style="max-width:340px">{{ e.title }}</td>
          <td>{{ e.platforms.map(p => platformLabel[p] || p).join(' + ') }}
              <b v-if="e.platform_count >= 2" style="color:#c00">({{ e.platform_count }}平台)</b></td>
          <td>{{ e.peak_value ? e.peak_value.toLocaleString() : '—' }}</td>
          <td>{{ (e.peak_at || '').slice(5, 16) }}</td>
          <td>{{ e.first_seen.slice(5, 16) }}</td>
          <td>{{ e.duration_hours }}h</td>
          <td>{{ e.sample_count }}</td>
          <td>{{ e.stage }}</td>
        </tr>
        <tr v-if="!items.length"><td colspan="8" class="muted">暂无事件(等下轮归属或点"立即归属")</td></tr>
      </tbody>
    </table>
  </div>
</template>
