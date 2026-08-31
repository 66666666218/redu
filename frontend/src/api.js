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
  alertsList: () => req('GET', '/api/alerts/list')
}
