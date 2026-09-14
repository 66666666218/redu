<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const msg = ref('')
const healthLabel = { HEALTHY: '🟢 健康', DEGRADED: '🟡 降级', CIRCUIT_OPEN: '🔴 熔断' }

async function load() {
  msg.value = ''
  try { items.value = await api.sourceHealth() } catch (e) { msg.value = e.message }
}
onMounted(load)
</script>

<template>
  <div>
    <h2>数据源健康</h2>
    <p class="muted">每采集源三态:🟢 HEALTHY / 🟡 DEGRADED(新鲜度超标、24h 失败≥3)/ 🔴 CIRCUIT_OPEN(熔断/Cookie 失效)。判定信号:最近成功、24h 失败数、滑块/WAF 冷却、数据写入新鲜度。</p>
    <p v-if="msg" style="color:#c00">{{ msg }}</p>
    <table style="margin-top:12px">
      <thead><tr><th>数据源</th><th>健康</th><th>问题</th><th>最近成功</th><th>24h失败</th><th>数据新鲜度</th><th>采集间隔</th></tr></thead>
      <tbody>
        <tr v-for="s in items" :key="s.section">
          <td>{{ s.label }}</td>
          <td>{{ healthLabel[s.health] || s.health }}</td>
          <td style="max-width:380px">
            <span v-if="!s.problems.length" class="muted">—</span>
            <span v-for="p in s.problems" :key="p" style="display:block;color:#c00">· {{ p }}</span>
            <span v-if="s.last_fail_detail" class="muted" style="display:block;font-size:12px">最近失败: {{ s.last_fail_detail }}</span>
          </td>
          <td>{{ s.last_success_at ? s.last_success_at.slice(5, 16) : '从未' }}<span class="muted" v-if="s.last_success_age_h!==null"> ({{ s.last_success_age_h }}h前)</span></td>
          <td>{{ s.fails_24h }}</td>
          <td>{{ s.data_age_h === null ? '从未' : s.data_age_h + 'h 前' }}</td>
          <td>{{ s.interval_h }}h</td>
        </tr>
        <tr v-if="!items.length"><td colspan="7" class="muted">加载中…</td></tr>
      </tbody>
    </table>
  </div>
</template>
