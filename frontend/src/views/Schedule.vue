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

// ---- 推送时段(2026-10-01):取代写死在 settings 里的 7 条推送 cron ----
// 后端每分钟判定一次"当前时刻是否命中某类推送"(见 app/services/push_timeline.py),
// 所以这里改完**立即生效**,不用重启服务。
const pushes = ref([])
const savingPush = ref('')
const DAY_LABELS = ['日', '一', '二', '三', '四', '五', '六']   // 索引即 POSIX 星期:0=周日

async function loadPush() {
  try { pushes.value = (await api.pushTimeline()).kinds } catch (e) { toastError(e.message) }
}

function dayText(k) {
  return k.days.length === 7 ? '每天' : k.days.map(d => '周' + DAY_LABELS[d]).join('、')
}

async function savePush(k) {
  savingPush.value = k.key
  try {
    const r = await api.setPushTimeline(pushes.value)
    pushes.value = r.kinds          // 后端会丢弃非法时刻/星期,以它的回包为准
    toastOk(`${k.label}已更新:${k.enabled ? dayText(k) + ' ' + k.times.join(' / ') : '已停用'}`)
  } catch (e) {
    toastError(e.message)
    await loadPush()                 // 被拒时回滚界面,别让用户以为改成了
  } finally { savingPush.value = '' }
}

function togglePush(k) { k.enabled = !k.enabled; savePush(k) }

function toggleDay(k, d) {
  const i = k.days.indexOf(d)
  if (i > -1) k.days.splice(i, 1); else k.days.push(d)
  k.days.sort((a, b) => a - b)
  savePush(k)
}

function addTime(k) {
  // 挑一个没被占用的默认值:时刻是按分钟精确匹配的,塞个重复的等于加了个哑条目
  const used = new Set(k.times)
  const t = ['12:00', '18:00', '07:00', '20:00', '22:00', '09:00'].find(x => !used.has(x)) || '23:00'
  k.times.push(t)
  k.times.sort()
  savePush(k)
}

function setTime(k, i, v) { k.times[i] = v; k.times.sort(); savePush(k) }
function delTime(k, i) { k.times.splice(i, 1); savePush(k) }

onMounted(() => { load(); loadPush() })
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

        <p v-if="s.fixed_hours" class="empty" style="margin:8px 0 0;color:#8a94a6">
          按每天 4 个定点运行:{{ s.fixed_hours }},下面的间隔不影响它的触发时刻
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

    <div class="row" style="margin:28px 0 16px">
      <h2 style="margin:0">推送时段</h2>
      <span class="empty">决定各类推送几点发、星期几发;调度器每分钟比对一次,改完立即生效</span>
    </div>

    <div class="card" style="margin-bottom:16px">
      <p class="empty" style="margin:0">
        这些是发给你的飞书/邮件内容。时刻按分钟精确匹配;
        <b>清空某类的全部时刻等于关掉它</b>。周末不想被工作日报打扰,把该类的星期里去掉「六」「日」即可。
      </p>
    </div>

    <div class="grid">
      <div class="card" v-for="k in pushes" :key="k.key">
        <div class="row">
          <h3 style="margin:0">{{ k.label }}</h3>
          <span class="badge">{{ k.enabled && k.times.length ? '推送中' : '已停用' }}</span>
        </div>

        <label class="empty" style="display:block;margin:10px 0 4px">发送时刻</label>
        <div class="row" style="flex-wrap:wrap;gap:6px" v-if="k.times.length">
          <span v-for="(t, i) in k.times" :key="i" style="display:flex;align-items:center;gap:4px">
            <input type="time" :value="t" :disabled="savingPush === k.key"
                   @change="setTime(k, i, $event.target.value)" />
            <button class="ghost" :disabled="savingPush === k.key" @click="delTime(k, i)">×</button>
          </span>
        </div>
        <p v-else class="empty" style="margin:6px 0 0;color:#f0b429">
          没有时刻 —— 这类推送已停发
        </p>
        <button class="ghost" style="margin-top:8px" :disabled="savingPush === k.key" @click="addTime(k)">
          + 加一个时刻
        </button>

        <label class="empty" style="display:block;margin:12px 0 4px">生效星期</label>
        <div class="row" style="flex-wrap:wrap;gap:4px">
          <button v-for="(l, d) in DAY_LABELS" :key="d"
                  :class="k.days.includes(d) ? '' : 'ghost'"
                  :disabled="savingPush === k.key"
                  @click="toggleDay(k, d)">{{ l }}</button>
        </div>
        <p class="empty" style="margin:8px 0 0">
          {{ k.enabled && k.times.length ? dayText(k) + ' 共 ' + k.times.length + ' 次' : '当前停发' }}
        </p>

        <div class="row" style="margin-top:10px">
          <button :class="k.enabled ? 'ghost' : ''" :disabled="savingPush === k.key"
                  @click="togglePush(k)">{{ k.enabled ? '停用推送' : '启用推送' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
