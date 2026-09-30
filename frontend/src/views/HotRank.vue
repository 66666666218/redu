<template>
  <div>
    <h2>多平台热榜 · 15 源雷达</h2>
    <p class="muted">
      B站/豆瓣为自研直连(零第三方),其余经自部署 newsnow;每小时 05 分自动刷新。
      条目出「网盘拉新适配度」筛选后进入 Agent 选题,本页是原料池总览。
    </p>
    <p v-if="msg" class="error">{{ msg }}</p>

    <div v-for="p in platforms" :key="p.source" class="card" style="margin-bottom:12px">
      <h3>{{ p.label }}
        <span class="empty" style="font-weight:normal;font-size:12px">
          {{ p.captured_at.slice(5, 16) }} · {{ p.items.length }} 条</span>
      </h3>
      <table>
        <tr v-for="it in p.items" :key="p.source + it.rank">
          <td style="width:34px">{{ it.rank }}</td>
          <td>
            <a v-if="it.url" :href="it.url" target="_blank">{{ it.title }}</a>
            <span v-else>{{ it.title }}</span>
          </td>
          <td class="muted" style="font-size:12px;max-width:220px">{{ it.extra }}</td>
        </tr>
      </table>
    </div>
    <p v-if="!platforms.length && !msg" class="empty">暂无数据——热榜每小时采集一轮,稍后刷新</p>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const platforms = ref([])
const msg = ref('')

onMounted(async () => {
  try {
    const r = await api.get('/api/hotspot/hot-rank', { params: { per: 10 } })
    platforms.value = r.data.platforms
  } catch (e) {
    msg.value = '加载失败:' + (e?.response?.data?.detail || e.message || e)
  }
})
</script>
