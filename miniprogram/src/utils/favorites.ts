// ============================================================
// 收藏状态跨页同步：AI 问答页 / 我的页（我的收藏弹窗）共用一份"收藏已变更"通知
// ------------------------------------------------------------
// 背景：小程序各页面是独立实例，不共享 React 状态。
//   - 在「我的-我的收藏」取消收藏后，AI 问答页 state 里的 msg.favorited 不会变，
//     若不通知，用户切回问答页仍看到「⭐ 已收藏」，且再点会被短路导致无法重新收藏。
//   - 反向同理：在问答页取消/收藏，收藏列表也应跟着刷新。
// 方案：变更方调用 notifyFavoritesChanged(change)，已挂载页面订阅后就地更新；
//   同时各页 useDidShow 仍保留「拉服务器列表对齐」作为兜底（跨设备/数据修复）。
// ============================================================

/** 一次收藏变更的语义：取消 / 新增（含后端返回的收藏 id） */
export interface FavoriteChange {
  action: 'remove' | 'add'
  /** 收藏记录 id（remove 时用于精确匹配；add 时后端返回） */
  favoriteId?: number
  /** 该条收藏的「问 + 答」，用于按内容匹配到对应消息（remove/add 都可能只有它） */
  question?: string
  answer?: string
}

type Listener = (change: FavoriteChange) => void

const listeners = new Set<Listener>()

/** 订阅收藏变更（返回取消订阅函数） */
export function onFavoritesChanged(cb: Listener): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** 通知所有订阅者：收藏已发生变更 */
export function notifyFavoritesChanged(change: FavoriteChange): void {
  listeners.forEach((cb) => {
    try {
      cb(change)
    } catch {
      /* 单个订阅者异常不影响其他页面 */
    }
  })
}
