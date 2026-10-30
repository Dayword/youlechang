// ============================================================
// 小程序登录体系 v2
// ------------------------------------------------------------
// ① 微信一键登录：wx.login code → /auth/wechat/login 换 JWT
// ② 手机号 + 密码登录：/auth/phone/pwd-login（未注册自动建号，密码 bcrypt 存 MySQL）
// ③ 历史账号免密登录：点一下头像/昵称即登录 → /auth/remember/login
//
// 关于「记住密码」：密码一律 bcrypt 不可逆哈希存 users.password_hash，绝不落明文。
// 本机只保存「记忆令牌」（随机串），库里存 sha256(令牌)，30 天有效、可吊销——
// 效果等同于记住密码，但不会造成明文泄露。
// ============================================================
import Taro from '@tarojs/taro'
import { API_BASE } from './config'

const TOKEN_KEY = 'mp_token'
const USER_KEY = 'mp_user'
const DEVICE_KEY = 'mp_device_id'
const REMEMBER_KEY = 'mp_remember_tokens'   // { [userId]: 记忆令牌 }
const LAST_ACCOUNT_KEY = 'mp_last_account'  // 上次登录账号快照（无网也能渲染卡片）
// 「用户主动退出过」标记：用户点了退出登录后，任何页面都不得再自动静默登录，
// 否则 AI 问答页/请求层的 401 兜底会把他重新登进去（表现就是"退出登录后没退回登录页"）。
// 一旦有任意一次成功登录（saveSession）就清除该标记。
const LOGGED_OUT_KEY = 'mp_logged_out'

export interface MpUser {
  id: number
  username: string
  nickname: string
  avatar?: string
  role: string
  phone?: string
  /** 1=已设置过登录密码（0/空 表示密码是系统占位串，首次输密码即设置） */
  pwd_set?: number
  is_new?: boolean
}

/** 本机历史登录账号（服务端 mp_login_accounts） */
export interface LoginAccount {
  id: number
  user_id: number
  phone: string
  username: string
  nickname: string
  avatar: string
  login_count: number
  last_login_at: string
}

/**
 * 账号合并明细（后端在"微信空壳账号并入手机号账号"时返回）。
 * 铁律：一个手机号 = 一个账号，手机号账号是主账号，历史数据都留在它上面。
 */
export interface MergeInfo {
  moved?: Record<string, number>   // 各业务表迁移行数
  openid_moved?: boolean           // 微信身份是否改绑到了主账号
  accounts_repointed?: number      // 本机历史账号卡片重指条数
}

/** 合并结果的提示文案（没有实际迁移记录时返回空串，避免"合并了 0 条"这种噪音） */
export function mergeTip(merged?: MergeInfo | null): string {
  if (!merged) return ''
  const moved = merged.moved || {}
  const total = Object.values(moved).reduce((sum, n) => sum + Number(n || 0), 0)
  return total > 0 ? `已合并你之前账号的 ${total} 条记录` : ''
}

/** 上次登录账号的本地快照（我的页未登录时展示"一键登录"卡片） */
export interface LastAccount {
  user_id: number
  phone: string
  nickname: string
  avatar: string
  username: string
}

// ============================================================
// 基础：令牌 / 用户 / 设备标识
// ============================================================
export function getToken(): string {
  return Taro.getStorageSync(TOKEN_KEY) || ''
}

export function getUser(): MpUser | null {
  try {
    return Taro.getStorageSync(USER_KEY) || null
  } catch {
    return null
  }
}

export function clearSession(): void {
  Taro.removeStorageSync(TOKEN_KEY)
  Taro.removeStorageSync(USER_KEY)
}

export function saveUser(user: MpUser): void {
  Taro.setStorageSync(USER_KEY, user)
}

function saveSession(token: string, user: MpUser): void {
  Taro.setStorageSync(TOKEN_KEY, token)
  Taro.setStorageSync(USER_KEY, user)
  // 有任意一次成功登录 → 解除"已退出"状态，重新允许静默登录兜底
  try {
    Taro.removeStorageSync(LOGGED_OUT_KEY)
  } catch { /* ignore */ }
}

/** 用户是否处于"主动退出登录"状态（此时禁止任何自动静默登录） */
export function isLoggedOut(): boolean {
  try {
    return !!Taro.getStorageSync(LOGGED_OUT_KEY)
  } catch {
    return false
  }
}

/** 本机设备标识：历史账号与免密令牌按设备维度存储（首次调用时生成并持久化） */
export function mpDeviceId(): string {
  try {
    let id = Taro.getStorageSync(DEVICE_KEY)
    if (!id) {
      id = `mp_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
      Taro.setStorageSync(DEVICE_KEY, id)
    }
    return id
  } catch {
    return 'mp_unknown'
  }
}

// ============================================================
// 记忆令牌（本机按账号保存，用于"下次点一下头像就登录"）
// ============================================================
function readRememberMap(): Record<string, string> {
  try {
    return Taro.getStorageSync(REMEMBER_KEY) || {}
  } catch {
    return {}
  }
}

export function saveRememberToken(userId: number, token: string): void {
  if (!userId || !token) return
  try {
    const map = readRememberMap()
    map[String(userId)] = token
    Taro.setStorageSync(REMEMBER_KEY, map)
  } catch { /* ignore */ }
}

export function getRememberToken(userId: number): string {
  return readRememberMap()[String(userId)] || ''
}

export function removeRememberToken(userId: number): void {
  try {
    const map = readRememberMap()
    delete map[String(userId)]
    Taro.setStorageSync(REMEMBER_KEY, map)
  } catch { /* ignore */ }
}

/** 缓存上次登录账号快照（供我的页/登录页离线渲染 + 一键登录入口） */
function cacheLastAccount(user: MpUser | null): void {
  if (!user?.id) return
  try {
    const snap: LastAccount = {
      user_id: user.id,
      phone: user.phone || '',
      nickname: user.nickname || '',
      avatar: user.avatar || '',
      username: user.username || '',
    }
    Taro.setStorageSync(LAST_ACCOUNT_KEY, snap)
  } catch { /* ignore */ }
}

export function getLastAccount(): LastAccount | null {
  try {
    return Taro.getStorageSync(LAST_ACCOUNT_KEY) || null
  } catch {
    return null
  }
}

/** 清掉本地历史账号入口（仅清本地展示信息，不动服务端历史） */
export function clearLastAccount(): void {
  Taro.removeStorageSync(LAST_ACCOUNT_KEY)
}

// ============================================================
// 请求封装（登录相关走原生 Taro.request，避免 utils/api ↔ utils/auth 循环依赖）
// ============================================================
/** 统一解析后端错误：detail 可能是字符串，也可能是 {code,msg,need} 结构 */
function throwApiError(status: number, json: any): never {
  const detail = json?.detail
  const msg =
    (typeof detail === 'object' ? detail?.msg : detail) || json?.msg || `请求失败(${status})`
  const err: any = new Error(msg)
  err.code = (typeof detail === 'object' ? detail?.code : undefined) ?? json?.code
  err.need = typeof detail === 'object' ? detail?.need : undefined
  throw err
}

async function post(path: string, data: any, useAuth = false): Promise<any> {
  const res = await Taro.request({
    url: `${API_BASE}${path}`,
    method: 'POST',
    data,
    header: {
      'Content-Type': 'application/json',
      ...(useAuth && getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    timeout: 10000,
  })
  const json = res.data as any
  if (res.statusCode !== 200 || json?.code !== 200) throwApiError(res.statusCode, json)
  return json
}

async function put(path: string, data: any): Promise<any> {
  const res = await Taro.request({
    url: `${API_BASE}${path}`,
    method: 'PUT',
    data,
    header: {
      'Content-Type': 'application/json',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    timeout: 10000,
  })
  const json = res.data as any
  if (res.statusCode !== 200 || json?.code !== 200) throwApiError(res.statusCode, json)
  return json
}

// ============================================================
// ① 微信一键登录（静默拿 code 换 JWT；用户无感知）
// ============================================================
export async function login(): Promise<MpUser> {
  const { code } = await Taro.login()
  const res = await Taro.request({
    url: `${API_BASE}/auth/wechat/login`,
    method: 'POST',
    data: { code, device_id: mpDeviceId() },
    header: { 'Content-Type': 'application/json' },
    timeout: 10000,
  })
  const json = res.data as any
  if (res.statusCode !== 200 || json?.code !== 200) throwApiError(res.statusCode, json)
  const { token, user } = json.data
  saveSession(token, user)
  cacheLastAccount(user)
  return user
}

let loginPromise: Promise<MpUser> | null = null

/**
 * 确保已登录（并发调用共享同一次登录）。
 * 请求层 401 时调用；没有 token 时不会被触发（登录门页负责首次登录）。
 *
 * ⚠️ 两个硬约束：
 *  1. 已有 token 时直接返回，绝不重新登录（重新登录会换账号！）；
 *  2. 用户主动退出过（isLoggedOut）时直接拒绝 —— 否则退出登录后被静默重新登入，
 *     表现就是"点了退出没退回登录页"。
 */
export function ensureLogin(force = false): Promise<MpUser> {
  if (!force && getToken()) return Promise.resolve(getUser()!)
  if (isLoggedOut()) return Promise.reject(new Error('已退出登录'))
  if (!loginPromise) {
    loginPromise = login().finally(() => {
      loginPromise = null
    })
  }
  return loginPromise
}

// ============================================================
// ② 手机号 + 密码登录（未注册自动建号）
// ============================================================
/**
 * 手机号密码登录。
 * - 手机号未注册 → 后端自动创建账号，本次密码即该账号登录密码
 * - 账号存在但从未设过密码 → 本次密码视为「设置密码」
 * - 已设过密码 → 校验 bcrypt，错误直接抛「手机号或密码错误」
 * 返回 { user, merged }：merged 非空表示后端把此微信号之前留下的空壳账号
 * 并入了这个手机号账号（数据都留在手机号账号上），调用方据此提示用户。
 */
export async function passwordLogin(
  phone: string,
  password: string,
  remember = true,
): Promise<{ user: MpUser; merged?: MergeInfo }> {
  let wxCode = ''
  try {
    const lr = await Taro.login()
    wxCode = lr.code || ''
  } catch { /* 拿不到 code 不阻塞登录，仅影响办卡所需的 openid */ }

  const json = await post('/auth/phone/pwd-login', {
    phone,
    password,
    remember,
    device_id: mpDeviceId(),
    wx_code: wxCode,
  })
  const user: MpUser = json.data.user
  saveSession(json.data.token, user)
  if (remember && json.data.remember_token) saveRememberToken(user.id, json.data.remember_token)
  cacheLastAccount(user)
  return { user, merged: json.data.merged || undefined }
}

// ============================================================
// ③ 历史账号免密登录（点一下头像/昵称直接进入）
// ============================================================
export function hasRememberToken(userId: number): boolean {
  return !!getRememberToken(userId)
}

/**
 * 凭记忆令牌免密登录。
 * 令牌失效/过期时抛错并清掉本地该账号的令牌（UI 引导改用密码登录）。
 */
export async function rememberLogin(userId: number): Promise<MpUser> {
  const token = getRememberToken(userId)
  if (!token) throw new Error('该账号需要输入密码登录')
  try {
    const json = await post('/auth/remember/login', { token, device_id: mpDeviceId() })
    const user: MpUser = json.data.user
    saveSession(json.data.token, user)
    if (json.data.remember_token) saveRememberToken(userId, json.data.remember_token)
    cacheLastAccount(user)
    return user
  } catch (e) {
    removeRememberToken(userId)
    throw e
  }
}

// ============================================================
// 历史账号列表 / 移除 / 退出登录
// ============================================================
/** 服务端本机历史账号（失败返回空数组，不抛错） */
export async function listLoginAccounts(): Promise<LoginAccount[]> {
  try {
    const res = await Taro.request({
      url: `${API_BASE}/auth/accounts?device_id=${encodeURIComponent(mpDeviceId())}`,
      method: 'GET',
      timeout: 8000,
    })
    const json = res.data as any
    if (res.statusCode === 200 && json?.code === 200) return (json.data || []) as LoginAccount[]
  } catch { /* 静默 */ }
  return []
}

/** 移除本机某条历史账号（同时吊销该账号在本设备的免密令牌） */
export async function removeLoginAccount(account: LoginAccount): Promise<void> {
  const res = await Taro.request({
    url: `${API_BASE}/auth/accounts/${account.id}?device_id=${encodeURIComponent(mpDeviceId())}`,
    method: 'DELETE',
    timeout: 8000,
  })
  const json = res.data as any
  if (res.statusCode !== 200 || json?.code !== 200) {
    throw new Error(json?.detail || '移除失败')
  }
  removeRememberToken(account.user_id)
  const last = getLastAccount()
  if (last && last.user_id === account.user_id) clearLastAccount()
}

/** 退出登录：吊销本机免密令牌（历史账号仍保留，下次需输入密码） */
export async function logout(): Promise<void> {
  const u = getUser()
  const rt = u?.id ? getRememberToken(u.id) : ''
  try {
    await post('/auth/logout', { remember_token: rt, device_id: mpDeviceId() })
  } catch { /* 网络异常也要清本地 */ }
  if (u?.id) removeRememberToken(u.id)
  clearSession()
  // 打上"已退出"标记：此后任何页面的静默登录兜底都不再自动把用户登回来
  try {
    Taro.setStorageSync(LOGGED_OUT_KEY, 1)
  } catch { /* ignore */ }
}

// ============================================================
// 绑定 / 换绑手机号（首次绑定设置密码；换绑需当前密码；手机号属于旧账号时验证后合并）
// ============================================================
/**
 * 绑定 / 换绑手机号。
 * 当手机号已绑在**另一个账号**上时，后端返回 409 且 err.code=40901、err.need='verify_code'，
 * 此时用该手机号收到的验证码（verifyCode）再提交一次即可完成「验证并合并」——
 * 这是"旧账号登不进去、手机号被占"死锁的解决通道。
 * 返回 { user, msg, merged }：msg 是后端给出的准确结果说明（含是否切换/迁移了多少条），
 * 直接拿去 toast 即可；user 可能已经不是原来的账号（空壳并入手机号账号时会切换）。
 */
export async function bindPhone(
  phone: string,
  password: string,
  currentPassword = '',
  verifyCode = '',
): Promise<{ user: MpUser; msg: string; merged?: MergeInfo }> {
  const json = await put('/auth/phone/bind', {
    phone,
    password,
    current_password: currentPassword,
    verify_code: verifyCode,
  })
  const user: MpUser = json.data.user
  saveSession(json.data.token, user)
  cacheLastAccount(user)
  return { user, msg: json.msg || '', merged: json.data.merged || undefined }
}

/** 获取手机号验证码（演示环境后端直接回显验证码）
 *  仅用于「忘记密码」与「合并账号」这类找回/接管兜底，不作为登录方式 */
export async function sendPhoneCode(phone: string): Promise<string> {
  const json = await post('/auth/sms-code', { phone })
  return json?.data?.demo_code || ''
}

/** 忘记密码：验证码 + 新密码 → 重置登录密码并直接登录 */
export async function resetPassword(
  phone: string,
  code: string,
  newPassword: string,
): Promise<MpUser> {
  const json = await post('/auth/phone/reset-pwd', {
    phone,
    code,
    new_password: newPassword,
  })
  const user: MpUser = json.data.user
  saveSession(json.data.token, user)
  cacheLastAccount(user)
  return user
}

/** 设置 / 重设登录密码（已设过密码时必须提供原密码） */
export async function setPassword(newPassword: string, oldPassword = ''): Promise<void> {
  await put('/auth/password', { new_password: newPassword, old_password: oldPassword })
  const u = getUser()
  if (u) saveUser({ ...u, pwd_set: 1 })
}

// ============================================================
// 登录态守卫：未登录时引导用户到「我的」页完成登录
// ============================================================
export function isLoggedIn(): boolean {
  return !!getToken()
}

/**
 * 后端功能统一登录守卫。
 * 未登录：弹窗说明 → 确认后跳「我的」页登录入口，返回 false；
 * 已登录：返回 true。
 */
export function requireLogin(action: string): boolean {
  if (isLoggedIn()) return true
  Taro.showModal({
    title: '需要登录',
    content: `登录后才能${action}，是否前往？`,
    confirmText: '去登录',
    cancelText: '暂不',
    success: (res) => {
      if (res.confirm) Taro.switchTab({ url: '/pages/mine/index' })
    },
  })
  return false
}
