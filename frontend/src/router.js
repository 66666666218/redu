import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from './api'
import Login from './views/Login.vue'
import Register from './views/Register.vue'
import Forgot from './views/Forgot.vue'
import Reset from './views/Reset.vue'
import Dashboard from './views/Dashboard.vue'
import Cookies from './views/Cookies.vue'
import Alerts from './views/Alerts.vue'

const routes = [
  { path: '/login', component: Login },
  { path: '/register', component: Register },
  { path: '/forgot', component: Forgot },
  { path: '/reset', component: Reset },
  { path: '/', component: Dashboard, meta: { auth: true } },
  { path: '/cookies', component: Cookies, meta: { auth: true } },
  { path: '/alerts', component: Alerts, meta: { auth: true } }
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.meta.auth && !getToken()) return '/login'
  if ((to.path === '/login' || to.path === '/register' || to.path === '/forgot') && getToken()) return '/'
})

export default router
