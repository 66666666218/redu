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
  register: (username, password) => req('POST', '/api/auth/register', { username, password }),
  login: (username, password) => req('POST', '/api/auth/login', { username, password }),
  me: () => req('GET', '/api/auth/me'),
  cookies: () => req('GET', '/api/cookies'),
  setCookie: (platform, cookie) => req('PUT', `/api/cookies/${platform}`, { cookie }),
  delCookie: (platform) => req('DELETE', `/api/cookies/${platform}`),
  collect: (platform) => req('POST', `/api/collect/${platform}`),
  dashboard: () => req('GET', '/api/dashboard'),
  xianyuDaily: () => req('GET', '/api/xianyu/daily')
}
