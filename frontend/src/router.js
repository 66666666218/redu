import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api'
import Login from './views/Login.vue'
import Register from './views/Register.vue'
import Forgot from './views/Forgot.vue'
import Reset from './views/Reset.vue'
import Dashboard from './views/Dashboard.vue'
import Cookies from './views/Cookies.vue'
import Alerts from './views/Alerts.vue'
import Admin from './views/Admin.vue'

const routes = [
  { path: '/login', component: Login },
  { path: '/register', component: Register },
  { path: '/forgot', component: Forgot },
  { path: '/reset', component: Reset },
  { path: '/', component: Dashboard, meta: { auth: true } },
  { path: '/cookies', component: Cookies, meta: { auth: true } },
  { path: '/alerts', component: Alerts, meta: { auth: true } },
  { path: '/admin', component: Admin, meta: { auth: true, admin: true } }
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
