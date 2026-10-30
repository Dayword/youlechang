// ============================================================
// 用户资料跨页同步（头像/昵称）：AI 问答页 / 办卡充值页 / 我的页 共用同一份数据
// ------------------------------------------------------------
// 背景：小程序各页面是独立实例，不会共享 React 状态。原先「我的」页用服务器头像，
// 而问答页、充值页头像写死成 👤，改了头像别处不同步。
// 方案：
//   1. 本地缓存（Taro storage）为唯一读取源，`getAvatarUrl()` 统一出口；
//   2. 各页 useDidShow 时 `refreshUser()` 拉一次 /auth/me（网络失败不影响展示）；
//   3. 头像/昵称变更后 `notifyUserChanged()`，已挂载页面收到通知立即刷新。
// ============================================================
import Taro from '@tarojs/taro'
import { API_BASE } from './config'
import { getToken, getUser, saveUser, type MpUser } from './auth'

type Listener = (u: MpUser | null) => void

const listeners = new Set<Listener>()

/** 订阅用户资料变更（返回取消订阅函数） */
export function onUserChanged(cb: Listener): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** 通知所有订阅者：用户资料已变化（头像/昵称/手机号） */
export function notifyUserChanged(): void {
  const u = getUser()
  listeners.forEach((cb) => {
    try {
      cb(u)
    } catch {
      /* 单个订阅者异常不影响其他页面 */
    }
  })
}

/**
 * 统一头像地址：有服务器头像则返回完整 URL，否则返回空串（调用方渲染兜底 emoji）。
 * 兼容后端返回的 "/static/avatars/x.png" 与已是绝对地址两种情况。
 */
export function getAvatarUrl(user?: MpUser | null): string {
  const u = user === undefined ? getUser() : user
  const avatar = (u?.avatar || '').trim()
  if (!avatar) return ''
  if (/^https?:\/\//i.test(avatar)) return avatar
  return API_BASE + (avatar.startsWith('/') ? avatar : `/${avatar}`)
}

/** 昵称兜底显示 */
export function getDisplayName(user?: MpUser | null): string {
  const u = user === undefined ? getUser() : user
  return (u?.nickname || '').trim() || '游客'
}

/**
 * 拉取服务端最新用户资料并写回本地缓存（各页 useDidShow 调用）。
 * 失败时静默返回本地缓存，不阻塞页面渲染。
 */
export async function refreshUser(): Promise<MpUser | null> {
  const token = getToken()
  if (!token) return getUser()
  try {
    const res = await Taro.request({
      url: `${API_BASE}/auth/me`,
      method: 'GET',
      header: { Authorization: `Bearer ${token}` },
      timeout: 8000,
    })
    const json = res.data as any
    if (res.statusCode === 200 && json?.code === 200 && json.data) {
      const local = getUser()
      const merged: MpUser = { ...(local || ({} as MpUser)), ...json.data } as MpUser
      const changed =
        !local || local.avatar !== merged.avatar || local.nickname !== merged.nickname
      saveUser(merged)
      if (changed) notifyUserChanged()
      return merged
    }
  } catch {
    /* 网络异常：沿用本地缓存 */
  }
  return getUser()
}
