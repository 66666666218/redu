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
  const contentType = resp.headers.get('content-type') || ''
  const raw = await resp.text()
  let data = null
  if (raw) {
    if (contentType.includes('application/json')) {
      try { data = JSON.parse(raw) } catch { data = null }
    } else {
      // 非 JSON(网关 502/504 HTML、CDN 纯文本、反代错误页)不要塞进 data,
      // 否则下面 `typeof data === 'string'` 分支会把整段 HTML 当错误文案 toast 出来。
      data = null
    }
  }
  if (!resp.ok) {
    // 401 = 令牌过期/无效:清除本地残留并回登录页,避免每个请求都报"登录已过期"却停在原地
    if (resp.status === 401 && !path.startsWith('/api/auth/')) {
      clearToken()
      if (location.pathname !== '/login') location.href = '/login'
    }
    throw new Error(errMessage(data, raw, resp.status))
  }
  return data
}

// FastAPI 的错误体有两种形态:HTTPException 是 {detail:"文字"},
// 而 422 参数校验是 {detail:[{loc,msg,...}]}——后者直接当字符串用会显示成 [object Object]。
// 5xx 一律走固定文案:上游 body 可能带堆栈/SQL/文件路径,直接展示等于泄露内部拓扑。
function errMessage(data, raw, status) {
  if (status >= 500) return '服务器开小差了,请稍后重试'
  if (data && typeof data === 'object') {
    const detail = data.detail ?? data.message
    if (typeof detail === 'string' && detail.trim()) return detail.slice(0, 200)
    if (Array.isArray(detail) && detail.length) {
      return detail
        .map(d => {
          const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : ''
          const msg = typeof d?.msg === 'string' ? d.msg : ''
          return field ? `${field}: ${msg}` : msg
        })
        .filter(Boolean)
        .join('; ')
        .slice(0, 200)
    }
  }
  if (typeof data === 'string' && data.trim()) return data.slice(0, 200)
  // 只有 JSON 解析失败/非 JSON 时才落到这里;raw 已在上面被判过,不再重复塞进 UI
  if (status === 401) return '登录已过期,请重新登录'
  if (status === 403) return '没有权限执行该操作'
  if (status === 404) return '请求的资源不存在'
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
  platformAgent: () => req('GET', '/api/platform-agent'),
  platformView: (platform) => req('GET', '/api/platform/' + platform),
  crossRising: () => req('GET', '/api/cross/rising'),
  douhotList: (listType, keyword, filterKeyword, dateWindow) => {
    let q = ''
    if (keyword) q += (q ? '&' : '?') + 'keyword=' + encodeURIComponent(keyword)
    if (filterKeyword) q += (q ? '&' : '?') + 'filter_keyword=' + encodeURIComponent(filterKeyword)
    if (dateWindow) q += (q ? '&' : '?') + 'date_window=' + dateWindow
    return req('GET', '/api/douhot/list/' + listType + q)
  },
  watchAdd: (section, keyword, listType, filterKeyword, dateWindow) => req('POST', '/api/watch/' + section, { keyword, list_type: listType || 'word', filter_keyword: filterKeyword || '', date_window: dateWindow || null }),
  watchDel: (section, listType, keyword, filterKeyword) => req('DELETE', '/api/watch/' + section, { list_type: listType, keyword, filter_keyword: filterKeyword || '' }),
  watchUpdate: (section, listType, keyword, filterKeyword, dateWindow) => req('PATCH', '/api/watch/' + section, { list_type: listType, keyword, filter_keyword: filterKeyword || '', date_window: dateWindow }),
  watchList: (section) => req('GET', '/api/watch/' + section),
  watchAnalytics: (section) => req('GET', '/api/watch/' + section + '/analytics'),
  watchDigest: (section) => req('POST', '/api/watch/' + section + '/digest'),
  xianyuDaily: () => req('GET', '/api/xianyu/daily'),
  xianyuCollectDeep: () => req('POST', '/api/xianyu/collect-deep'),
  xianyuAnalytics: () => req('GET', '/api/xianyu/analytics'),
  douhotWatchAdd: (listType, keyword, filterKeyword, dateWindow) => req('POST', '/api/douhot/watch', { list_type: listType, keyword, filter_keyword: filterKeyword || '', date_window: dateWindow || null }),
  douhotWatchList: () => req('GET', '/api/douhot/watch'),
  douhotWatchAnalytics: () => req('GET', '/api/douhot/watch-analytics'),
  douhotWatchWindows: () => req('GET', '/api/douhot/watch-windows'),
  douhotWatchWindowsRefresh: () => req('POST', '/api/douhot/watch-windows/refresh'),
  douhotWindowsQuery: (listType, keyword) => req('POST', '/api/douhot/windows/query', { list_type: listType, keyword }),
  alertRules: () => req('GET', '/api/alerts/rules'),
  alertRuleAdd: (rule) => req('POST', '/api/alerts/rules', rule),
  alertRuleDel: (id) => req('DELETE', `/api/alerts/rules/${id}`),
  alertsList: () => req('GET', '/api/alerts/list'),
  userSmtpGet: () => req('GET', '/api/user/smtp'),
  userSmtpPut: (o) => req('PUT', '/api/user/smtp', o),
  adminMe: () => req('GET', '/api/admin/me'),
  adminDashboard: () => req('GET', '/api/admin/dashboard'),
  adminInsights: () => req('GET', '/api/admin/insights'),
  adminHealth: () => req('GET', '/api/admin/health'),
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
  adminExportAlerts: () => fetch('/api/admin/export/alerts', { headers: { Authorization: 'Bearer ' + getToken() } }),
  // 公众号监听(对标号 / 同步 / 阅读量)
  wechatBenchmarks: () => req('GET', '/api/wechat/benchmarks'),
  wechatStatus: () => req('GET', '/api/wechat/status'),
  wechatBenchmarkAdd: (url, nickname = '', note = '') => req('POST', '/api/wechat/benchmarks', { url, nickname, note }),
  wechatBenchmarkPatch: (id, o) => req('PATCH', `/api/wechat/benchmarks/${id}`, o),
  wechatBenchmarkDel: (id) => req('DELETE', `/api/wechat/benchmarks/${id}`),
  wechatBenchmarkSync: (id, maxPages) => req('POST', `/api/wechat/benchmarks/${id}/sync` + (maxPages ? `?max_pages=${maxPages}` : '')),
  wechatListen: () => req('POST', '/api/wechat/listen'),
  wechatShelf: () => req('GET', '/api/wechat/weread/shelf'),
  wechatWereadRefresh: () => req('POST', '/api/wechat/weread/refresh'),
  wechatCandidates: () => req('GET', '/api/wechat/candidates'),
  wechatCandidateDiscover: () => req('POST', '/api/wechat/candidates/discover'),
  wechatCandidatePatch: (id, o) => req('PATCH', `/api/wechat/candidates/${id}`, o),
  wechatImportShelf: () => req('POST', '/api/wechat/benchmarks/import_shelf'),
  wechatArticles: (q = '') => req('GET', '/api/wechat/articles' + (q ? '?' + q : '')),
  wechatTrafficRefresh: (o = {}) => req('POST', '/api/wechat/traffic/refresh', o),
  wechatArticleTraffic: (id) => req('GET', `/api/wechat/articles/${id}/traffic`),
  wechatArticleRewrite: (id) => req('POST', `/api/wechat/articles/${id}/rewrite`),
  wechatArticleRewrites: (id) => req('GET', `/api/wechat/articles/${id}/rewrites`)
  ,
  members: () => req('GET', '/api/members'),
  memberAdd: (o) => req('POST', '/api/members', o),
  memberRenew: (id) => req('POST', `/api/members/${id}/renew`),
  memberStatus: (id, status) => req('POST', `/api/members/${id}/status`, { status }),
  memberDel: (id) => req('DELETE', `/api/members/${id}`),
  events: (q = '') => req('GET', '/api/events' + (q ? '?' + q : '')),
  eventsAssign: () => req('POST', '/api/events/assign'),
  sourceHealth: () => req('GET', '/api/source-health'),
  trending: () => req('GET', '/api/trending')
}
