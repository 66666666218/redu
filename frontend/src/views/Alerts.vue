<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const sections = [ { key: 'weibo', label: '微博' }, { key: 'xianyu', label: '闲鱼' }, { key: 'douhot', label: '抖音热点' } ]
const ruleTypes = [ { key: 'threshold', label: '阈值触发(涨到一定程度)' }, { key: 'new', label: '新增提醒(出现新词/商品)' }, { key: 'fixed_time', label: '定时总结' } ]
const metrics = [ { key: 'growth', label: '增长率' }, { key: 'pct', label: '涨跌%' }, { key: 'delta', label: '增量' }, { key: 'score', label: '飙升/热度分' }, { key: 'heat', label: '热度(微博)' }, { key: 'hit_keywords', label: '命中词数(闲鱼)' }, { key: 'want_count', label: '想要数(闲鱼)' } ]

const form = ref({ section: 'weibo', rule_type: 'threshold', metric: 'growth', threshold: 0.3, keyword: '', alert_time: '20:00' })
const rules = ref([])
const items = ref([])
const msg = ref('')

async function load() {
  try { rules.value = await api.alertRules() } catch {}
  try { items.value = await api.alertsList() } catch {}
}
async function add() {
  msg.value = ''
  const body = { section: form.value.section, rule_type: form.value.rule_type }
  if (form.value.rule_type === 'threshold') { body.metric = form.value.metric; body.threshold = Number(form.value.threshold) }
  if (form.value.keyword) body.keyword = form.value.keyword.trim()
  if (form.value.rule_type === 'fixed_time') body.alert_time = form.value.alert_time
  try { await api.alertRuleAdd(body); await load(); form.value.keyword = '' } catch (e) { msg.value = e.message }
}
async function del(id) { await api.alertRuleDel(id); await load() }
const sl = (k) => (sections.find(x => x.key === k)?.label || k)
const tl = (k) => (ruleTypes.find(x => x.key === k)?.label || k)

onMounted(load)
</script>

<template>
  <div class="page">
    <h2 style="margin:0 0 12px">预警规则(每个板块可配)</h2>
    <span v-if="msg" class="error">{{ msg }}</span>

    <div class="card" style="margin-bottom:16px">
      <h3>新增规则</h3>
      <div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:8px">
        <select v-model="form.section"><option v-for="s in sections" :key="s.key" :value="s.key">{{ s.label }}</option></select>
        <select v-model="form.rule_type"><option v-for="r in ruleTypes" :key="r.key" :value="r.key">{{ r.label }}</option></select>
        <select v-if="form.rule_type==='threshold'" v-model="form.metric"><option v-for="m in metrics" :key="m.key" :value="m.key">{{ m.label }}</option></select>
        <input v-if="form.rule_type==='threshold'" v-model="form.threshold" type="number" step="0.01" placeholder="阈值" style="width:90px;margin:0" />
        <input v-model="form.keyword" placeholder="关键词(可空=全部)" style="flex:1;margin:0" />
        <input v-if="form.rule_type==='fixed_time'" v-model="form.alert_time" placeholder="20:00" style="width:90px;margin:0" />
        <button @click="add">添加</button>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>我的规则</h3>
      <table v-if="rules.length">
        <tr><th>板块</th><th>类型</th><th>指标</th><th>阈值</th><th>关键词</th><th>时间</th><th></th></tr>
        <tr v-for="r in rules" :key="r.id">
          <td>{{ sl(r.section) }}</td><td>{{ tl(r.rule_type) }}</td>
          <td>{{ r.metric || '—' }}</td><td>{{ r.threshold ?? '—' }}</td>
          <td>{{ r.keyword || '全部' }}</td><td>{{ r.alert_time || '—' }}</td>
          <td><button class="ghost" @click="del(r.id)">删除</button></td>
        </tr>
      </table>
      <div v-else class="empty">暂无规则,先添加(阈值/新增/定时)</div>
    </div>

    <div class="card">
      <h3>最近预警</h3>
      <table v-if="items.length"><tr><th>触发项</th><th>说明</th><th>时间</th></tr>
        <tr v-for="(it,i) in items.slice(0,30)" :key="i"><td>{{ it.keyword }}</td><td>{{ it.reason }}</td><td class="empty">{{ it.time }}</td></tr>
      </table>
      <div v-else class="empty">暂无预警记录</div>
    </div>
  </div>
</template>
