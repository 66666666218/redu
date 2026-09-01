// API 封装:带 JWT 的 fetch
const BASE = '';

export function getToken() {
  return localStorage.getItem('token') || ''
}
export function setToken(t) {
  localStorage.setItem('token', t)
}
export function clearToken() {
  localStorage.removeItem('token')
}

async function req(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = 'Bearer ' + token
  const resp = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined
  })
  const text = await resp.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!resp.ok) {
    const msg = data && (data.detail || data.message) ? (data.detail || data.message) : '请求失败'
    throw new Error(msg)
  }
  return data
}

export const api = {
  register: (email, password, username) => req('POST', '/api/auth/register', { email, password, username }),
  login: (login, password) => req('POST', '/api/auth/login', { login, password }),
  forgot: (email) => req('POST', '/api/auth/forgot', { email }),
  reset: (token, new_password) => req('POST', '/api/auth/reset', { token, new_password }),
  me: () => req('GET', '/api/auth/me'),
  cookies: () => req('GET', '/api/cookies'),
  setCookie: (platform, cookie) => req('PUT', `/api/cookies/${platform}`, { cookie }),
  delCookie: (platform) => req('DELETE', `/api/cookies/${platform}`),
  collect: (platform) => req('POST', `/api/collect/${platform}`),
  dashboard: () => req('GET', '/api/dashboard'),
  xianyuDaily: () => req('GET', '/api/xianyu/daily'),
  xianyuCollectDeep: () => req('POST', '/api/xianyu/collect-deep'),
  xianyuAnalytics: () => req('GET', '/api/xianyu/analytics'),
  douhotWatchAdd: (listType, keyword) => req('POST', '/api/douhot/watch', { list_type: listType, keyword }),
  douhotWatchList: () => req('GET', '/api/douhot/watch'),
  douhotWatchAnalytics: () => req('GET', '/api/douhot/watch-analytics'),
  alertRules: () => req('GET', '/api/alerts/rules'),
  alertRuleAdd: (rule) => req('POST', '/api/alerts/rules', rule),
  alertRuleDel: (id) => req('DELETE', `/api/alerts/rules/${id}`),
  alertsList: () => req('GET', '/api/alerts/list'),
  userSmtpGet: () => req('GET', '/api/user/smtp'),
  userSmtpPut: (o) => req('PUT', '/api/user/smtp', o),
  adminMe: () => req('GET', '/api/admin/me'),
  adminDashboard: () => req('GET', '/api/admin/dashboard'),
  adminUsers: (q) => req('GET', '/api/admin/users' + (q ? '?q=' + encodeURIComponent(q) : '')),
  adminUserToggle: (id) => req('POST', `/api/admin/users/${id}/toggle`),
  adminUserDel: (id) => req('DELETE', `/api/admin/users/${id}`),
  adminUserDetail: (id) => req('GET', `/api/admin/users/${id}`),
  adminImportUsers: (text) => req('POST', '/api/admin/import/users', { text }),
  adminLogins: () => req('GET', '/api/admin/logins'),
  adminLogs: () => req('GET', '/api/admin/logs'),
  adminConfig: () => req('GET', '/api/admin/config'),
  adminConfigSet: (key, value) => req('PUT', `/api/admin/config/${key}`, { value }),
  adminData: (section, userId) => req('GET', `/api/admin/data/${section}` + (userId ? `?user_id=${userId}` : '')),
  adminCategories: () => req('GET', '/api/admin/categories'),
  adminFailedRuns: () => req('GET', '/api/admin/runs/failed'),
  adminRunRetry: (runId) => req('POST', `/api/admin/runs/${runId}/retry`),
  adminExportUsers: () => fetch('/api/admin/export/users', { headers: { Authorization: 'Bearer ' + getToken() } }),
  adminExportAlerts: () => fetch('/api/admin/export/alerts', { headers: { Authorization: 'Bearer ' + getToken() } })
}
