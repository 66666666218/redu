<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const dash = ref({ weibo_trends: [], xianyu_hot: [], douhot_words: [] })
const loading = ref(false)
const msg = ref('')
const busy = ref('')

const platforms = [
  { key: 'weibo', label: '微博' },
  { key: 'xianyu', label: '闲鱼' },
  { key: 'douhot', label: '抖音' }
]

async function load() {
  try { dash.value = await api.dashboard() } catch (e) { msg.value = e.message }
}
async function collect(key, label) {
  busy.value = key
  msg.value = ''
  try {
    const r = await api.collect(key)
    msg.value = `${label} 采集完成:${r.count} 条${r.rising ? `,判涨 ${r.rising.length}` : ''}`
    await load()
  } catch (e) { msg.value = `${label} 采集失败:${e.message}` }
  finally { busy.value = '' }
}
onMounted(load)
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:16px">
      <button v-for="p in platforms" :key="p.key" :disabled="busy === p.key" @click="collect(p.key, p.label)">
        {{ busy === p.key ? '采集中…' : '采集' + p.label }}
      </button>
      <button class="ghost" @click="load">刷新</button>
      <span class="empty">{{ msg }}</span>
    </div>

    <div class="grid">
      <div class="card">
        <h3>微博 · 上涨趋势</h3>
        <table v-if="dash.weibo_trends.length">
          <tr><th>关键词</th><th>增长率</th></tr>
          <tr v-for="t in dash.weibo_trends" :key="t.keyword">
            <td>{{ t.keyword }}</td><td class="up">{{ (t.growth * 100).toFixed(1) }}%</td>
          </tr>
        </table>
        <div v-else class="empty">暂无上涨趋势,先"采集微博"</div>
      </div>

      <div class="card">
        <h3>闲鱼 · 虚拟商品热榜</h3>
        <table v-if="dash.xianyu_hot.length">
          <tr><th>标题</th><th>价格</th><th>场次</th></tr>
          <tr v-for="(it, i) in dash.xianyu_hot.slice(0, 20)" :key="it.item_id">
            <td>{{ i + 1 }}. {{ it.title.slice(0, 32) }}</td><td class="price">{{ it.price }}</td><td>{{ it.hit_keywords }}</td>
          </tr>
        </table>
        <div v-else class="empty">暂无热榜,先"采集闲鱼"</div>
      </div>

      <div class="card">
        <h3>抖音 · 内容词趋势</h3>
        <table v-if="dash.douhot_words.length">
          <tr><th>内容词</th><th>飙升指数</th><th>趋势</th></tr>
          <tr v-for="it in dash.douhot_words.slice(0, 20)" :key="it.title">
            <td>{{ it.title }}</td><td class="price">{{ (it.score / 1e4).toFixed(0) }}万</td>
            <td :class="{ up: it.trend_delta > 0 }">{{ it.trend_delta > 0 ? '↑' : (it.trend_delta < 0 ? '↓' : '—') }}</td>
          </tr>
        </table>
        <div v-else class="empty">暂无内容词,先"采集抖音"</div>
      </div>
    </div>
  </div>
</template>
