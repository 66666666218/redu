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
  let resp
  try {
    resp = await fetch(BASE + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined
    })
  } catch {
    // 断网/服务未启动时 fetch 直接抛 TypeError('Failed to fetch'),转成中文提示
    throw new Error('无法连接服务器,请检查网络或稍后重试')
  }
  const text = await resp.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!resp.ok) {
    throw new Error(errMessage(data, resp.status))
  }
  return data
}

// FastAPI 的错误体有两种形态:HTTPException 是 {detail:"文字"},
// 而 422 参数校验是 {detail:[{loc,msg,...}]}——后者直接当字符串用会显示成 [object Object]。
function errMessage(data, status) {
  if (typeof data === 'string' && data.trim()) return data
  const detail = data && (data.detail ?? data.message)
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map(d => {
        const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : ''
        return field ? `${field}: ${d.msg}` : d.msg
      })
      .filter(Boolean)
      .join('; ')
  }
  if (status === 401) return '登录已过期,请重新登录'
  if (status === 403) return '没有权限执行该操作'
  if (status >= 500) return '服务器开小差了,请稍后重试'
  return `请求失败(${status})`
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
  schedules: () => req('GET', '/api/schedules'),
  setSchedule: (section, payload) => req('PUT', `/api/schedules/${section}`, payload),
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
  adminInsights: () => req('GET', '/api/admin/insights'),
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
  adminAlertTrend: (days) => req('GET', '/api/admin/alert-trend' + (days ? `?days=${days}` : '')),
  adminCategoryPie: () => req('GET', '/api/admin/category-pie'),
  adminFailedRuns: () => req('GET', '/api/admin/runs/failed'),
  adminRunRetry: (runId) => req('POST', `/api/admin/runs/${runId}/retry`),
  adminExportUsers: () => fetch('/api/admin/export/users', { headers: { Authorization: 'Bearer ' + getToken() } }),
  adminExportAlerts: () => fetch('/api/admin/export/alerts', { headers: { Authorization: 'Bearer ' + getToken() } })
}
