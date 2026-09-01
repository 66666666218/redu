<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { toastOk, toastError } from '../toast'

const items = ref([])
const choices = ref([])
const minInterval = ref(10)
const saving = ref('')

// 档位显示:超过 60 分钟按小时说更好读
function intervalLabel(m) {
  if (m < 60) return `${m} 分钟`
  if (m < 1440) return `${m / 60} 小时`
  return '每天 1 次'
}

async function load() {
  try {
    const d = await api.schedules()
    items.value = d.items
    choices.value = d.choices
    minInterval.value = d.min_interval
  } catch (e) { toastError(e.message) }
}

async function save(section, payload) {
  saving.value = section
  try {
    const updated = await api.setSchedule(section, payload)
    const i = items.value.findIndex(x => x.section === section)
    if (i > -1) items.value[i] = updated
    toastOk(`${updated.label}已更新:${updated.enabled ? intervalLabel(updated.interval_minutes) + '采集一次' : '已停用'}`)
  } catch (e) {
    toastError(e.message)
    await load()   // 后端拒绝(如低于下限)时回滚界面显示
  } finally { saving.value = '' }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="row" style="margin-bottom:16px">
      <h2 style="margin:0">采集频率</h2>
      <span class="empty">每个板块可单独设置多久采集一次,改完立即生效(下一分钟起按新频率)</span>
    </div>

    <div class="card" style="margin-bottom:16px">
      <p class="empty" style="margin:0">
        三个板块都需要你在「Cookie 管理」里配好对应平台的 Cookie 才会采集;
        未配置的板块会自动跳过,不会产生失败记录。
        由于都是登录态接口,采集过于频繁可能触发平台风控或导致 Cookie 失效,因此最小间隔为 {{ minInterval }} 分钟。
      </p>
    </div>

    <div class="grid">
      <div class="card" v-for="s in items" :key="s.section">
        <div class="row">
          <h3 style="margin:0">{{ s.label }}</h3>
          <span class="badge">{{ s.enabled ? '监控中' : '已停用' }}</span>
        </div>

        <p v-if="!s.cookie_ready" class="empty" style="color:#f0b429;margin:8px 0 0">
          ⚠ 未配置 Cookie,当前不会采集 —
          <router-link to="/cookies">去配置</router-link>
        </p>

        <label class="empty" style="display:block;margin:10px 0 4px">采集间隔</label>
        <select
          :value="s.interval_minutes"
          :disabled="!s.enabled || saving === s.section"
          @change="save(s.section, { interval_minutes: Number($event.target.value) })"
          style="width:100%"
        >
          <option v-for="c in choices" :key="c" :value="c">{{ intervalLabel(c) }}</option>
        </select>

        <p class="empty" style="margin:10px 0 0">
          上次采集:{{ s.last_run_at || '尚未采集' }}<br />
          下次预计:{{ s.enabled ? (s.next_run_at || '待定') : '已停用' }}
        </p>

        <div class="row" style="margin-top:10px">
          <button
            :class="s.enabled ? 'ghost' : ''"
            :disabled="saving === s.section"
            @click="save(s.section, { enabled: !s.enabled })"
          >{{ s.enabled ? '停用监控' : '启用监控' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
