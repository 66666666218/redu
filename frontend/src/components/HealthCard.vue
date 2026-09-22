<!-- 采集健康度卡片(工作台/运维 tab 复用):各平台最近采集状态 + 数据写入 + 飞书推送 + Cookie -->
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { toastError } from '../toast'

const health = ref(null)
async function loadHealth() {
  try { health.value = await api.adminHealth() }
  catch (e) { health.value = null; toastError('健康度加载失败:' + e.message) }
}
function pname(k) {
  return { weibo: '微博', xianyu: '闲鱼', douhot: '抖音', baidu: '百度', xianyu_deep: '闲鱼深采',
    wechat: '公众号', wechat_listen: '公众号监听' }[k] || k
}
// Cookie 的平台名与板块名不同一套(goofish=闲鱼、weread=微信读书…),原样吐出运维看不懂
function cname(k) {
  return { weibo: '微博', baidu: '百度', goofish: '闲鱼', douyin: '抖音', weread: '微信读书',
    quark: '夸克网盘', baidupan: '百度网盘', dajiala: 'dajiala(付费接口)' }[k] || k
}
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso); const p = n => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
onMounted(loadHealth)
</script>

<template>
  <div class="card" style="margin-bottom:14px">
    <h3>🩺 采集健康度 <button class="ghost" @click="loadHealth">刷新</button></h3>
    <div v-if="!health" class="empty">加载中…</div>
    <template v-if="health">
      <table>
        <tr><th>平台</th><th>状态</th><th>最近运行</th><th>近24h 成功/失败</th><th>最近详情</th></tr>
        <tr v-for="(p, name) in health.platforms" :key="name">
          <td><b>{{ pname(name) }}</b></td>
          <td :class="{ up: p.last_status === 'success', error: p.last_status === 'failed' }">{{ p.last_status || '未运行' }}</td>
          <td class="empty">{{ p.last_run ? fmtTime(p.last_run) : '—' }}</td>
          <td class="num">{{ p.runs_24h }} / <span :class="{ error: p.failed_24h > 0 }">{{ p.failed_24h }}</span></td>
          <td class="empty">{{ (p.last_detail || '—').slice(0, 32) }}</td>
        </tr>
      </table>
      <p class="empty" v-if="health.wechat_monitor" style="margin:8px 0 0">
        公众号监听:在监对标号 <b>{{ health.wechat_monitor.benchmarks }}</b> 个
        ({{ health.wechat_monitor.users }} 个用户) · 近 24h 新收文章
        <b :class="{ error: !health.wechat_monitor.articles_24h }">{{ health.wechat_monitor.articles_24h }}</b> 篇
        · 定点 {{ health.wechat_monitor.fixed_hours }} · 下轮 {{ fmtTime(health.wechat_monitor.next_point) }}
      </p>
      <div class="grid" style="margin-top:10px">
        <div>
          <h4 style="color:var(--dim);margin:6px 0">最新数据写入</h4>
          <div v-for="(t, s) in health.data" :key="s" class="empty">{{ pname(s) }}: {{ t ? fmtTime(t) : '无' }}</div>
        </div>
        <div>
          <h4 style="color:var(--dim);margin:6px 0">飞书推送</h4>
          <div v-if="health.feishu.pushes_by_section.length">
            <div v-for="p in health.feishu.pushes_by_section" :key="p.section" class="empty">{{ pname(p.section) }}: {{ p.count }}</div>
          </div>
          <div v-else class="empty">暂无推送</div>
          <div class="empty" v-if="health.feishu.last_push">最近 {{ fmtTime(health.feishu.last_push) }}</div>
        </div>
        <div>
          <h4 style="color:var(--dim);margin:6px 0">已配 Cookie</h4>
          <div v-for="(n, pl) in health.cookies" :key="pl" class="empty">{{ cname(pl) }}: {{ n }}</div>
        </div>
      </div>
    </template>
  </div>
</template>
