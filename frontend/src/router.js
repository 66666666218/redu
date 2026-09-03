import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api'
import Login from './views/Login.vue'
import Register from './views/Register.vue'
import Forgot from './views/Forgot.vue'
import Reset from './views/Reset.vue'
import Dashboard from './views/Dashboard.vue'
import Platform from './views/Platform.vue'
import Douhot from './views/Douhot.vue'
import Cookies from './views/Cookies.vue'
import Schedule from './views/Schedule.vue'
import Alerts from './views/Alerts.vue'
import Admin from './views/Admin.vue'
import Screen from './views/Screen.vue'

const routes = [
  { path: '/login', component: Login },
  { path: '/register', component: Register },
  { path: '/forgot', component: Forgot },
  { path: '/reset', component: Reset },
  { path: '/', component: Dashboard, meta: { auth: true } },
  { path: '/weibo', component: Platform, props: { platform: 'weibo' }, meta: { auth: true } },
  { path: '/xianyu', component: Platform, props: { platform: 'xianyu' }, meta: { auth: true } },
  { path: '/baidu', component: Platform, props: { platform: 'baidu' }, meta: { auth: true } },
  { path: '/douhot', component: Douhot, meta: { auth: true } },
  { path: '/cookies', component: Cookies, meta: { auth: true } },
  { path: '/schedule', component: Schedule, meta: { auth: true } },
  { path: '/alerts', component: Alerts, meta: { auth: true } },
  { path: '/admin', component: Admin, meta: { auth: true, admin: true } },
  { path: '/screen', component: Screen, meta: { auth: true, screen: true } }
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  if (!getToken()) {
    return to.meta.auth ? '/login' : true
  }
  if (to.path === '/login' || to.path === '/register' || to.path === '/forgot') return '/'
  if (to.meta.admin) {
    try {
      const me = await fetch('/api/auth/me', { headers: { Authorization: 'Bearer ' + getToken() } }).then(r => r.json())
      if (me.role !== 'admin' && me.role !== 'operator') return '/'
    } catch { return '/login' }
  }
  return true
})

export default router
