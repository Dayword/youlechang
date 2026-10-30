// ============================================================
// 我的：微信昵称 / 余币钱包 + 每日签到 / 快捷提问 / 我的卡 /
//       联系服务（电话/导航）/ 会话信息 / 个人信息编辑弹窗
// ============================================================
import { useEffect, useRef, useState } from 'react'
import { View, Text, Button, Input, Image, ScrollView } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import UserAvatar from '@/components/UserAvatar'
import MarkdownView from '@/components/MarkdownView'
import {
  getUser, saveUser, bindPhone, setPassword, logout,
  rememberLogin, getLastAccount, hasRememberToken,
  isLoggedIn, requireLogin, sendPhoneCode, type MpUser, type LastAccount,
} from '@/utils/auth'
import { refreshUser, notifyUserChanged } from '@/utils/user'
import { onFavoritesChanged, notifyFavoritesChanged } from '@/utils/favorites'
import {
  getMyCards, getMyBalance, doCheckin, getCheckinStatus,
  updateProfile, uploadAvatar, useMyCard, refundMyCard,
  getMyConsumptions, getMyConsumptionStats, type ConsumptionStats,
  type CheckinStatus, type MyCardInfo, type MyConsumption,
  getFavorites, deleteFavorite, type FavoriteItem,
} from '@/utils/api'
import {
  PARK_ADDRESS, PARK_LATITUDE, PARK_LONGITUDE, PARK_NAME,
  PARK_PHONE, API_BASE, PENDING_QUESTION_KEY,
} from '@/utils/config'
import './index.scss'

// 项目游玩次数排行的周期切换（近一天在最左侧）
const RANK_PERIODS: { key: 'day' | 'week' | 'month' | 'year'; text: string }[] = [
  { key: 'day', text: '近一天' },
  { key: 'week', text: '近一周' },
  { key: 'month', text: '近一月' },
  { key: 'year', text: '近一年' },
]

export default function MinePage() {
  const [user, setUser] = useState<MpUser | null>(getUser())
  const [cards, setCards] = useState<MyCardInfo[]>([])
  const [balance, setBalance] = useState(0)
  const [checkin, setCheckin] = useState<CheckinStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [signing, setSigning] = useState(false)
  // 是否成功拉到过数据：没拉到之前不要把 0 当成"真实值"展示（否则页面看起来"没内容"）
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [loadError, setLoadError] = useState('')
  // 并发/重复触发保护：useDidShow 与首帧兜底会同时想拉数据，只允许一个在飞
  const inFlightRef = useRef(false)
  const retriedRef = useRef(false)
  const lastLoadAtRef = useRef(0)

  // 消费记录只用来给入口卡显示「还有几张待玩」；明细、筛选都在游园码页里看
  const [consList, setConsList] = useState<MyConsumption[]>([])
  const passLeft = consList.reduce((n, c) => n + (c.pass_unused || 0), 0)

  // 项目游玩次数排行（横向柱状图）
  const [rankStats, setRankStats] = useState<ConsumptionStats | null>(null)
  const [rankPeriod, setRankPeriod] = useState<'day' | 'week' | 'month' | 'year'>('month')
  const [rankLoading, setRankLoading] = useState(false)

  // 我的卡（默认折叠）
  const [showCards, setShowCards] = useState(false)
  // 我的卡抽屉分类：member=会员充值 / small=小额充值（均为充值记录）
  const [cardsTab, setCardsTab] = useState<'member' | 'small'>('member')

  // 个人信息弹窗（头像 / 昵称 / 手机号换绑 / 密码设置）
  const [showProfile, setShowProfile] = useState(false)
  const [profileView, setProfileView] = useState<'info' | 'rebind' | 'password'>('info')
  const [nicknameDraft, setNicknameDraft] = useState('')
  const [avatarPreview, setAvatarPreview] = useState('')
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [profileBusy, setProfileBusy] = useState(false)
  const [rebindPhone, setRebindPhone] = useState('')
  const [rebindPwd, setRebindPwd] = useState('')
  const [rebindCurrentPwd, setRebindCurrentPwd] = useState('')
  // 目标手机号已被另一个账号占用时：就地补验证码完成"验证并合并"，不再把用户赶去登录页
  const [rebindVerifyCode, setRebindVerifyCode] = useState('')
  const [rebindNeedVerify, setRebindNeedVerify] = useState(false)
  const [rebindCountdown, setRebindCountdown] = useState(0)
  const rebindTimer = useRef<any>(null)
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [newPwd2, setNewPwd2] = useState('')
  const [refundCard, setRefundCard] = useState<MyCardInfo | null>(null)
  const [refunding, setRefunding] = useState(false)

  // 上次登录账号（未登录时卡片直接给"一键登录"入口）
  const [lastAccount, setLastAccount] = useState<LastAccount | null>(getLastAccount())
  const [oneTapBusy, setOneTapBusy] = useState(false)

  // 我的收藏（AI 问答页回答操作条「收藏」写入）
  const [showFavorites, setShowFavorites] = useState(false)
  const [favorites, setFavorites] = useState<FavoriteItem[]>([])
  const [favLoading, setFavLoading] = useState(false)
  /** 正在看详情的那条收藏（null = 停在列表视图） */
  const [favDetailId, setFavDetailId] = useState<number | null>(null)
  /** 搜索关键字（前端过滤，不打接口） */
  const [favKeyword, setFavKeyword] = useState('')
  /** 正在滑出删除的那条（删除前先播个退场动画，别一按就没了） */
  const [favClosingId, setFavClosingId] = useState<number | null>(null)
  const [favClearing, setFavClearing] = useState(false)

  useDidShow(() => {
    refresh()
  })

  // 首帧兜底：Taro 的 useDidShow 是在 effect 里注册的，页面**第一次**显示时
  // onShow 有可能会早于注册完成（表现为首次进入页面数据一直空着，要重新编译/切走再回来才有）。
  // 这里在挂载时也拉一次，与 useDidShow 共享 inFlightRef 去重，不会重复请求。
  useEffect(() => {
    refresh()
    return () => {
      if (rebindTimer.current) clearInterval(rebindTimer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 收藏变更即时同步：在 AI 问答页取消收藏后，若「我的收藏」弹窗正开着，就地移除该条；
  // 新增收藏则拉一次列表补上（数量少，成本可忽略）。弹窗未打开时不做任何请求。
  useEffect(() => {
    const off = onFavoritesChanged((change) => {
      if (!showFavorites) return
      if (change.action === 'remove') {
        setFavorites((prev) => {
          const next = prev.filter((f) =>
            change.favoriteId ? f.id !== change.favoriteId : f.question !== change.question)
          return next.length === prev.length ? prev : next
        })
        setFavDetailId((cur) => (cur != null && cur === change.favoriteId ? null : cur))
      } else {
        getFavorites(50).then((r) => setFavorites(r.data || [])).catch(() => {})
      }
    })
    return () => { off() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showFavorites])

  const refresh = async (silent = false) => {
    if (!isLoggedIn()) {
      // 未登录：清空数据，页面显示登录引导态（有历史账号则显示一键登录卡片）
      setUser(getUser())
      setLastAccount(getLastAccount())
      setCards([])
      setBalance(0)
      setCheckin(null)
      setConsList([])
      setRankStats(null)
      setFavorites([])
      setLoadedOnce(false)
      setLoadError('')
      return
    }
    if (inFlightRef.current) return
    // 800ms 内重复触发（useDidShow + 首帧兜底）直接跳过，避免进页面就打两次全套接口
    if (Date.now() - lastLoadAtRef.current < 800) return
    lastLoadAtRef.current = Date.now()
    inFlightRef.current = true
    if (!silent) setLoading(true)
    setLoadError('')
    try {
      // 三项核心数据各自独立 catch：任何一个失败都不再拖垮整页渲染
      const [cardsRes, balRes, checkinRes] = await Promise.all([
        getMyCards().catch(() => null),
        getMyBalance().catch(() => null),
        getCheckinStatus().catch(() => null),
      ])
      if (!cardsRes && !balRes && !checkinRes) throw new Error('数据加载失败，请检查网络')
      if (cardsRes) setCards(cardsRes.data || [])
      if (balRes) setBalance(balRes.data.coins || 0)
      if (checkinRes) setCheckin(checkinRes.data)
      setUser(getUser())
      setLoadedOnce(true)
      // 部分失败：把成功的那部分先渲染出来，同时明确提示哪块没拉到（可点击重试）
      const failedParts = [
        !cardsRes && '我的卡',
        !balRes && '余币',
        !checkinRes && '签到',
      ].filter(Boolean) as string[]
      if (failedParts.length) setLoadError(`部分数据未加载（${failedParts.join('、')}）`)
      // 以下数据互不依赖，失败只影响自己那一块
      refreshUser().then((u) => setUser(u)).catch(() => {})
      getMyConsumptions(50).then((r) => setConsList(r.data || [])).catch(() => setConsList([]))
      getFavorites(50).then((r) => setFavorites(r.data || [])).catch(() => setFavorites([]))
      loadRank(rankPeriod)
    } catch (e: any) {
      const msg = e?.message || '加载失败'
      setLoadError(msg)
      // 冷启动首个请求偶发失败很常见（后端预热/网络抖动）→ 自动重试一次，
      // 免得用户对着空页面等，只能靠"重新编译"才能恢复。
      // 但"登录已过期"这类重试也没用，直接交给提示条引导重新登录。
      const authIssue = /登录|过期|认证|凭证/.test(msg)
      if (!retriedRef.current && !authIssue) {
        retriedRef.current = true
        setTimeout(() => {
          retriedRef.current = false
          refresh(true)
        }, 1500)
      }
    } finally {
      inFlightRef.current = false
      setLoading(false)
    }
  }

  // ---------- 我的收藏 ----------
  const openFavorites = async () => {
    if (!requireLogin('查看我的收藏')) return
    setShowFavorites(true)
    setFavDetailId(null)
    setFavLoading(true)
    try {
      const r = await getFavorites(50)
      setFavorites(r.data || [])
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '收藏加载失败', icon: 'none' })
    } finally {
      setFavLoading(false)
    }
  }

  /**
   * 关闭弹窗：顺带把详情视图复位，下次进来还是列表。
   * 显式收下事件并 stopPropagation —— 与列表「查看/删除」按钮保持同一写法。
   */
  const closeFavorites = (e?: { stopPropagation?: () => void }) => {
    e?.stopPropagation?.()
    setShowFavorites(false)
    setFavDetailId(null)
  }

  /** 从详情返回列表（弹窗不关）；写法同上，保证详情里每个可点元素都一致 */
  const backToList = (e?: { stopPropagation?: () => void }) => {
    e?.stopPropagation?.()
    setFavDetailId(null)
  }

  const copyFavorite = (fav: FavoriteItem) => {
    const text = `【问】${fav.question}\n${fav.answer}`
    Taro.setClipboardData({ data: text })
      .then(() => Taro.showToast({ title: '已复制', icon: 'none' }))
      .catch(() => Taro.showToast({ title: '复制失败', icon: 'none' }))
  }

  const removeFavorite = (fav: FavoriteItem) => {
    Taro.showModal({
      title: '取消收藏？',
      content: '将从我的收藏中移除这条问答',
      confirmText: '取消收藏',
      confirmColor: '#e4572e',
      success: async (r) => {
        if (!r.confirm) return
        try {
          await deleteFavorite(fav.id)
          // 通知 AI 问答页：该条回答回到「☆ 收藏」，可重新收藏（含 id 与问+答双通道匹配）
          notifyFavoritesChanged({
            action: 'remove',
            favoriteId: fav.id,
            question: fav.question,
            answer: fav.answer,
          })
          // 先左滑+收拢再移除：直接 filter 掉会"啪"地跳一下
          // 280ms 必须与 .fav-row 的 transition 时长一致，否则行还没收拢就被摘掉会跳
          setFavClosingId(fav.id)
          setTimeout(() => {
            setFavorites((prev) => prev.filter((f) => f.id !== fav.id))
            setFavClosingId(null)
          }, 280)
          Taro.showToast({ title: '已取消收藏', icon: 'none' })
        } catch (e: any) {
          Taro.showToast({ title: e?.message || '操作失败', icon: 'none' })
        }
      },
    })
  }

  /** 清空全部收藏（后端没有批量接口，逐条删；有失败就回读真实列表，别让界面和服务器对不上） */
  const clearAllFavorites = () => {
    const ids = favorites.map((f) => f.id)
    if (!ids.length) return
    Taro.showModal({
      title: '清空收藏？',
      content: `将删除全部 ${ids.length} 条收藏，不可恢复`,
      confirmText: '清空',
      confirmColor: '#e4572e',
      success: async (r) => {
        if (!r.confirm) return
        setFavClearing(true)
        try {
          const rs = await Promise.allSettled(ids.map((id) => deleteFavorite(id)))
          const ok = rs.filter((x) => x.status === 'fulfilled').length
          if (ok === ids.length) {
            setFavorites([])
            setFavDetailId(null)
            // 通知 AI 问答页：所有回答的收藏标记复位，均可重新收藏
            ids.forEach((id) => notifyFavoritesChanged({ action: 'remove', favoriteId: id }))
            Taro.showToast({ title: '已清空', icon: 'none' })
          } else {
            await openFavorites()
            Taro.showToast({ title: `${ids.length - ok} 条删除失败，已刷新`, icon: 'none' })
          }
        } finally {
          setFavClearing(false)
        }
      },
    })
  }

  /** 相对时间：收藏列表里"3 天前"比"09-18 14:32"好扫得多 */
  const favTimeAgo = (s: string) => {
    const raw = String(s || '')
    if (!raw) return ''
    const t = Date.parse(raw.includes('T') ? raw : raw.replace(/-/g, '/'))
    if (!t) return raw.slice(5, 16)
    const mins = Math.floor((Date.now() - t) / 60000)
    if (mins < 1) return '刚刚'
    if (mins < 60) return `${mins} 分钟前`
    if (mins < 1440) return `${Math.floor(mins / 60)} 小时前`
    if (mins < 43200) return `${Math.floor(mins / 1440)} 天前`
    return raw.slice(0, 10)
  }

  /**
   * 列表行左侧的「内容简写」：MarkdownView 只有 content 没有纯文本模式，
   * 这里把 AI 回答剥成一行纯文本。只处理 MarkdownView 实际支持的语法，
   * 不碰 `_斜体_`（英文/变量里下划线太常见，误伤比收益大）。
   */
  const favSummary = (answer: string, max = 60) => {
    let s = String(answer || '')
    s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')   // 图片：url 对摘要没意义，整段去掉
    s = s.replace(/```[\s\S]*?```/g, ' ')         // 代码块
    s = s.replace(/`([^`]*)`/g, '$1')             // 行内 code
    s = s.replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '') // 标题
    s = s.replace(/^[ \t]{0,3}[-*+][ \t]+/gm, '')  // 无序列表
    s = s.replace(/^[ \t]{0,3}\d+[.、][ \t]*/gm, '') // 有序列表
    s = s.replace(/\*\*([^*]+)\*\*/g, '$1')       // 加粗
    s = s.replace(/\s+/g, ' ').trim()             // 换行/多空白 → 单空格
    if (!s) return '（图片回答）'
    return s.length > max ? `${s.slice(0, max)}…` : s
  }

  /** 搜索过滤后的收藏（问题和答案都参与匹配，关键字为空就是全部） */
  const favKey = favKeyword.trim()
  const favShown = favKey
    ? favorites.filter(
        (f) => String(f.question || '').includes(favKey) || String(f.answer || '').includes(favKey),
      )
    : favorites

  /**
   * 详情视图当前这条。**必须查 favorites 全量**：
   * 搜索态下用 favShown 查，一旦关键字把当前条筛掉就会解析成 null，详情会凭空关掉。
   */
  const favDetail = favDetailId == null ? null : (favorites.find((f) => f.id === favDetailId) || null)

  /** 从收藏继续追问：写入待发问题后跳 AI 问答页自动发送 */
  const askFromFavorite = (fav: FavoriteItem) => {
    try {
      Taro.setStorageSync(PENDING_QUESTION_KEY, fav.question)
    } catch { /* ignore */ }
    closeFavorites()
    Taro.switchTab({ url: '/pages/chat/index' })
  }

  /**
   * 图片地址补全（收藏附图可能是 /static/... 相对路径）。
   * 先 String() 归一再用：images_json 是 TEXT 列，历史上可能混进非字符串元素，
   * 直接对非字符串调 .startsWith 会抛 TypeError —— 渲染期抛错会让整页按钮静默失效。
   */
  const resolveImageUrl = (url: unknown) => {
    const u = String(url ?? '').trim()
    if (!u) return ''
    return /^https?:\/\//i.test(u) ? u : API_BASE + (u.startsWith('/') ? u : `/${u}`)
  }

  /**
   * 收藏附图统一入口。后端 images_json 是 TEXT 列，新老数据都可能是 null / 非数组，
   * 直接 .map 会炸在渲染期 —— 小程序所有页面共用一个 JS 线程，一次渲染异常的表现
   * 就是「弹窗关不掉、整页按钮全点不动」，所以这里必须先归一化成数组再用。
   */
  const favImages = (fav: FavoriteItem): string[] =>
    (Array.isArray(fav.images) ? fav.images : []).map((u) => resolveImageUrl(u)).filter((u) => !!u)

  /** 详情视图当前这条的附图（提前算好，JSX 里不再重复 map） */
  const favDetailImgs = favDetail ? favImages(favDetail) : []

  // 项目游玩次数排行数据（横向柱状图数据源）
  const loadRank = async (p: 'day' | 'week' | 'month' | 'year') => {
    if (!isLoggedIn()) return
    setRankLoading(true)
    try {
      const r = await getMyConsumptionStats(p)
      setRankStats(r.data || {})
    } catch {
      setRankStats(null)
    } finally {
      setRankLoading(false)
    }
  }

  const switchRankPeriod = (p: 'day' | 'week' | 'month' | 'year') => {
    setRankPeriod(p)
    loadRank(p)
  }

  // 游园码入口：券、消费明细都在那一页（跳过去前先把张数刷成最新的）
  const goPasses = () => {
    if (!requireLogin('查看游园码')) return
    Taro.navigateTo({ url: '/pages/passes/index' })
  }

  // ---------- 个人信息弹窗 ----------
  const maskedPhone = (p?: string) =>
    p && p.length === 11 ? p.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2') : p || '未绑定'

  const openProfile = () => {
    if (!isLoggedIn()) {
      Taro.navigateTo({ url: '/pages/login/index' })
      return
    }
    const u = getUser()
    setNicknameDraft(u?.nickname || '')
    setAvatarPreview(u?.avatar ? API_BASE + u.avatar : '')
    setProfileView('info')
    setShowProfile(true)
  }

  const closeProfile = () => {
    setShowProfile(false)
    setProfileView('info')
    setRebindPhone('')
    setRebindPwd('')
    setRebindCurrentPwd('')
    setRebindVerifyCode('')
    setRebindNeedVerify(false)
    setOldPwd('')
    setNewPwd('')
    setNewPwd2('')
  }

  // 换绑时若目标号码已被别的账号占用：发验证码证明号码归属（演示环境后端直接回显）
  const sendRebindCode = async () => {
    if (rebindCountdown > 0) return
    if (!/^1[3-9]\d{9}$/.test(rebindPhone)) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    try {
      const demo = await sendPhoneCode(rebindPhone)
      if (demo) {
        setRebindVerifyCode(demo)
        Taro.showToast({ title: `演示环境验证码：${demo}`, icon: 'none', duration: 4000 })
      } else {
        Taro.showToast({ title: '验证码已发送', icon: 'none' })
      }
      setRebindCountdown(60)
      if (rebindTimer.current) clearInterval(rebindTimer.current)
      rebindTimer.current = setInterval(() => {
        setRebindCountdown((c) => {
          if (c <= 1) {
            if (rebindTimer.current) clearInterval(rebindTimer.current)
            rebindTimer.current = null
            return 0
          }
          return c - 1
        })
      }, 1000)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '验证码发送失败', icon: 'none' })
    }
  }

  // 头像：微信 chooseAvatar → 上传服务器存储（跨设备同步，商家端可见）
  const onChooseAvatar = async (e: any) => {
    const tmp = e?.detail?.avatarUrl
    if (!tmp || avatarBusy) return
    setAvatarBusy(true)
    setAvatarPreview(tmp)
    try {
      const res = await uploadAvatar(tmp)
      const u = getUser()
      const updated = { ...(u || ({} as MpUser)), avatar: res.data.avatar }
      saveUser(updated)
      setUser(updated)
      setAvatarPreview(API_BASE + res.data.avatar)
      // 通知 AI 问答页 / 充值页立即同步新头像
      notifyUserChanged()
      Taro.showToast({ title: '头像已更新', icon: 'success' })
    } catch (err: any) {
      setAvatarPreview('')
      Taro.showToast({ title: err?.message || '头像上传失败', icon: 'none' })
    } finally {
      setAvatarBusy(false)
    }
  }

  // 保存昵称（Input type=nickname 支持微信快捷填入）
  const saveNickname = async () => {
    const nick = nicknameDraft.trim()
    if (!nick) {
      Taro.showToast({ title: '昵称不能为空', icon: 'none' })
      return
    }
    if (profileBusy) return
    setProfileBusy(true)
    try {
      await updateProfile(nick)
      const u = getUser()
      const updated = { ...(u || ({} as MpUser)), nickname: nick }
      saveUser(updated)
      setUser(updated)
      Taro.showToast({ title: '昵称已保存', icon: 'success' })
      closeProfile()
    } catch (err: any) {
      Taro.showToast({ title: err?.message || '保存失败', icon: 'none' })
    } finally {
      setProfileBusy(false)
    }
  }

  // 使用某张卡：标记为生效中，充值页会员码按它出码
  const handleUseCard = async (c: MyCardInfo) => {
    if (c.in_use) return
    try {
      await useMyCard(c.id)
      Taro.showToast({ title: `已切换生效卡：${c.card_name}`, icon: 'none' })
      refresh()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '操作失败', icon: 'none' })
    }
  }

  // 退卡确认（剩余币不足面值时后端拒绝，前端直接展示不可退原因）
  const handleConfirmRefund = async () => {
    if (!refundCard || refunding) return
    setRefunding(true)
    try {
      const res = await refundMyCard(refundCard.id)
      Taro.showToast({ title: res.msg || '退卡成功', icon: 'none', duration: 2500 })
      setRefundCard(null)
      refresh()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '退卡失败', icon: 'none', duration: 3000 })
    } finally {
      setRefunding(false)
    }
  }

  // 确认绑定/换绑（首次绑定=设置密码；已设过密码=用当前密码确认身份；
  // 号码已被别的账号占用时=补验证码验证归属后就地合并，不再把用户赶去登录页）
  const submitRebind = async () => {
    if (profileBusy) return
    if (!/^1[3-9]\d{9}$/.test(rebindPhone)) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    const hasPwd = !!user?.pwd_set
    if (hasPwd && !rebindCurrentPwd) {
      Taro.showToast({ title: '请输入当前密码以确认身份', icon: 'none' })
      return
    }
    if (rebindNeedVerify && !rebindVerifyCode.trim()) {
      Taro.showToast({ title: '请输入该手机号收到的验证码', icon: 'none' })
      return
    }
    if (hasPwd) {
      // 已设过密码 → 换绑；新密码可留空（表示只换号不改密码）
      if (rebindPwd && rebindPwd.length < 6) {
        Taro.showToast({ title: '新密码至少 6 位', icon: 'none' })
        return
      }
    } else if (rebindPwd.length < 6) {
      Taro.showToast({ title: '请设置至少 6 位的登录密码', icon: 'none' })
      return
    }
    setProfileBusy(true)
    try {
      const res = await bindPhone(rebindPhone, rebindPwd, rebindCurrentPwd,
                                  rebindVerifyCode.trim())
      saveUser(res.user)
      setUser(res.user)
      setRebindPwd('')
      setRebindCurrentPwd('')
      setRebindVerifyCode('')
      setRebindNeedVerify(false)
      Taro.showToast({ title: res.msg || '手机号已绑定', icon: 'success', duration: 2500 })
      setProfileView('info')
      refresh(true)
    } catch (err: any) {
      // 40901：该手机号已绑在另一个账号上 → 就地展开验证码输入，验证后由后端完成合并
      if (err?.code === 40901) {
        setRebindNeedVerify(true)
        Taro.showToast({
          title: '该手机号已绑定其他账号，请获取并输入验证码以合并',
          icon: 'none',
          duration: 4000,
        })
        return
      }
      Taro.showToast({ title: err?.message || '操作失败，请稍后重试', icon: 'none' })
    } finally {
      setProfileBusy(false)
    }
  }

  // 设置 / 重设登录密码（忘记密码时：先微信一键登录进入，再在这里重设）
  const submitPassword = async () => {
    if (profileBusy) return
    if (newPwd.length < 6) {
      Taro.showToast({ title: '新密码至少 6 位', icon: 'none' })
      return
    }
    if (newPwd !== newPwd2) {
      Taro.showToast({ title: '两次输入的密码不一致', icon: 'none' })
      return
    }
    setProfileBusy(true)
    try {
      await setPassword(newPwd, oldPwd)
      const u = getUser()
      if (u) setUser({ ...u, pwd_set: 1 })
      setOldPwd('')
      setNewPwd('')
      setNewPwd2('')
      Taro.showToast({ title: '密码已更新', icon: 'success' })
      setProfileView('info')
    } catch (err: any) {
      Taro.showToast({ title: err?.message || '密码更新失败', icon: 'none' })
    } finally {
      setProfileBusy(false)
    }
  }

  // 未登录时：点历史账号卡片直接登录（免密令牌；无令牌则去登录页输密码）
  const handleOneTapLogin = async () => {
    if (oneTapBusy || !lastAccount) return
    if (!hasRememberToken(lastAccount.user_id)) {
      Taro.navigateTo({ url: '/pages/login/index' })
      return
    }
    setOneTapBusy(true)
    try {
      const u = await rememberLogin(lastAccount.user_id)
      saveUser(u)
      setUser(u)
      Taro.showToast({ title: '登录成功', icon: 'success' })
      refresh()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '免密登录失败，请重新登录', icon: 'none', duration: 3000 })
      Taro.navigateTo({ url: '/pages/login/index' })
    } finally {
      setOneTapBusy(false)
    }
  }

  // 退出登录：吊销本机免密令牌 → 清本地会话 → **退回登录页**（历史账号卡片保留）
  // 注意：退出后 auth 层会打上"已退出"标记，任何页面的静默登录兜底都不会再把人自动登回来。
  const handleLogout = () => {
    Taro.showModal({
      title: '退出登录',
      content: '退出后需重新输入密码登录（本机历史账号会保留）',
      confirmText: '退出',
      cancelText: '取消',
      success: async (res) => {
        if (!res.confirm) return
        await logout()
        // 先收干净页面上的登录态（弹窗也要关，否则会带着旧数据停在原地）
        setShowProfile(false)
        setProfileView('info')
        setUser(null)
        setLastAccount(getLastAccount())
        setCards([])
        setBalance(0)
        setCheckin(null)
        setConsList([])
        setFavorites([])
        setRankStats(null)
        setLoadedOnce(false)
        setLoadError('')
        Taro.showToast({ title: '已退出登录', icon: 'none' })
        // 退回登录页：reLaunch 会清空页面栈，避免"退出了却还停在『我的』页"
        setTimeout(() => {
          Taro.reLaunch({ url: '/pages/login/index' })
        }, 600)
      },
    })
  }

  // 每日签到（当日重复签后端幂等，不重复发币）
  const handleCheckin = async () => {
    if (!requireLogin('每日签到')) return
    if (signing) return
    if (checkin?.signed_today) {
      Taro.showToast({ title: '今天已经签过啦，明天再来～', icon: 'none' })
      return
    }
    setSigning(true)
    try {
      const res = await doCheckin()
      setCheckin(res.data)
      if (res.data.signed_now) {
        setBalance((prev) => prev + 1)
        Taro.showToast({ title: '签到成功，+1 币已到账 🪙', icon: 'none', duration: 2000 })
      } else {
        Taro.showToast({ title: '今天已经签过啦～', icon: 'none' })
      }
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '签到失败，请稍后重试', icon: 'none' })
    } finally {
      setSigning(false)
    }
  }

  const goRecharge = () => Taro.navigateTo({ url: '/pages/recharge/index' })

  /** 还没成功拉到过数据：数字位显示占位符，避免把"未加载"误读成"就是 0" */
  const pending = isLoggedIn() && !loadedOnce

  /** 加载失败是"登录态失效"而不是网络问题：重新登录而不是傻重试 */
  const needRelogin = /登录|过期|认证|凭证/.test(loadError)
  const handleLoadErrorTap = async () => {
    if (!needRelogin) {
      refresh()
      return
    }
    // 先清会话再回登录页：否则登录页会以为还登录着，直接把用户又弹回首页
    await logout()
    Taro.reLaunch({ url: '/pages/login/index' })
  }
  const callUs = () => Taro.makePhoneCall({ phoneNumber: PARK_PHONE })
  const openMap = () =>
    Taro.openLocation({
      latitude: PARK_LATITUDE,
      longitude: PARK_LONGITUDE,
      name: PARK_NAME,
      address: PARK_ADDRESS,
      scale: 16,
    })

  return (
    <View className='mine-page'>
      {/* 加载中 / 加载失败提示：让"没内容"有明确原因，失败可直接点着重试 */}
      {isLoggedIn() && pending && loading && (
        <View className='data-tip'>正在加载我的数据…</View>
      )}
      {isLoggedIn() && !!loadError && (
        <View className='data-tip data-tip-error' hoverClass='press' onClick={handleLoadErrorTap}>
          <Text>⚠ {loadError}，点击{needRelogin ? '重新登录' : '重试'}</Text>
        </View>
      )}
      {/* 用户卡（未登录=登录入口；已登录=个人信息弹窗） */}
      <View
        className='profile-card card'
        hoverClass='card-hover'
        hoverStayTime={80}
        onClick={() => {
          if (isLoggedIn()) return openProfile()
          // 未登录：有历史账号点一下直接免密登录，否则去登录页
          if (lastAccount) return handleOneTapLogin()
          Taro.navigateTo({ url: '/pages/login/index' })
        }}
      >
        <UserAvatar
          user={
            isLoggedIn()
              ? user
              : lastAccount
                ? ({ nickname: lastAccount.nickname, avatar: lastAccount.avatar } as MpUser)
                : null
          }
          className='profile-avatar'
          imgClassName='profile-avatar-img'
          textClassName='profile-avatar-emoji'
          fallback='🦆'
        />
        <View className='profile-info'>
          <Text className='profile-nickname'>
            {isLoggedIn()
              ? (user?.nickname || '游客')
              : (lastAccount?.nickname || '未登录')}
          </Text>
          <Text className='profile-sub'>
            {isLoggedIn()
              ? (user?.phone ? `已绑定 ${maskedPhone(user.phone)}` : '点击完善个人信息')
              : lastAccount
                ? (oneTapBusy ? '登录中...' : '点击一键登录 · 或切换其他账号')
                : '点击登录，解锁 AI 问答 · 签到 · 办卡'}
          </Text>
        </View>
        {isLoggedIn() ? (
          <View className='profile-right'>
            {/* 每日签到：缩小放入个人信息栏右侧空白区 */}
            <View
              className={`checkin-pill ${checkin?.signed_today ? 'checkin-pill-signed' : ''}`}
              hoverClass='press'
              hoverStayTime={60}
              onClick={(e) => {
                e.stopPropagation()
                handleCheckin()
              }}
            >
              <Text className='checkin-pill-text'>
                {checkin?.signed_today ? `✅ 已签 ${checkin.streak_days ?? 0}天` : '📅 签到'}
              </Text>
            </View>
            <Text className='profile-arrow'>›</Text>
          </View>
        ) : lastAccount ? (
          <View className='profile-right'>
            <View
              className='profile-login-btn'
              hoverClass='press'
              hoverStayTime={60}
              onClick={(e) => {
                e.stopPropagation()
                handleOneTapLogin()
              }}
            >
              <Text>{oneTapBusy ? '登录中' : '一键登录'}</Text>
            </View>
            <Text
              className='profile-switch'
              onClick={(e) => {
                e.stopPropagation()
                Taro.navigateTo({ url: '/pages/login/index' })
              }}
            >
              切换
            </Text>
          </View>
        ) : (
          <View className='profile-login-btn'>
            <Text>登 录</Text>
          </View>
        )}
      </View>

      {/* 钱包 + 游园码（两列：游园码入口放在原“今日签到”的位置） */}
      <View className='wallet-row'>
        <View className='wallet card' hoverClass='card-hover' hoverStayTime={80} onClick={goRecharge}>
          <View className='wallet-coin-wrap'>
            <Text className='wallet-emoji'>🪙</Text>
            <Text className='wallet-num'>{pending ? '—' : balance}</Text>
          </View>
          <Text className='wallet-label'>余币余额</Text>
          <Text className='wallet-action'>去充值 ›</Text>
        </View>
        <View
          className='wallet card consume-card'
          hoverClass='card-hover'
          hoverStayTime={80}
          onClick={goPasses}
        >
          <View className='wallet-coin-wrap'>
            <Text className='consume-emoji'>🎫</Text>
            <Text className='consume-count'>{pending ? '' : (passLeft || '')}</Text>
          </View>
          <Text className='wallet-label'>游园码</Text>
          <Text className='wallet-action'>
            {passLeft > 0 ? `还有 ${passLeft} 张待玩 ›` : '查看券与明细 ›'}
          </Text>
        </View>
      </View>

      {/* 我的卡 / 我的收藏（与上方「余币/消费记录」同尺寸大卡，点击打开弹窗） */}
      <View className='wallet-row'>
        <View
          className='wallet card entry-cards'
          hoverClass='card-hover'
          hoverStayTime={80}
          onClick={() => {
            if (!requireLogin('查看我的卡')) return
            setShowCards(true)
          }}
        >
          <View className='wallet-coin-wrap'>
            <Text className='wallet-emoji'>💳</Text>
            <Text className='wallet-num'>{pending ? '—' : cards.length}</Text>
          </View>
          <Text className='wallet-label'>我的卡</Text>
          <Text className='wallet-action'>
            {cards.length === 0
              ? '去办卡 ›'
              : `会员 ${cards.filter((c) => (c.kind || 'member') !== 'small').length} · 小额 ${cards.filter((c) => c.kind === 'small').length} ›`}
          </Text>
        </View>
        <View
          className='wallet card entry-fav'
          hoverClass='card-hover'
          hoverStayTime={80}
          onClick={openFavorites}
        >
          <View className='wallet-coin-wrap'>
            <Text className='wallet-emoji'>⭐</Text>
            <Text className='wallet-num'>{pending ? '—' : favorites.length}</Text>
          </View>
          <Text className='wallet-label'>我的收藏</Text>
          <Text className='wallet-action'>
            {favorites.length === 0 ? '去 AI 问答收藏 ›' : '点击查看全部 ›'}
          </Text>
        </View>
      </View>

      {/* 项目游玩次数排行（横向柱状图；替换原“快捷提问”） */}
      {isLoggedIn() && (
        <View className='section'>
          <View className='section-head'>
            <Text className='section-title'>🎢 项目游玩次数排行</Text>
            <View className='period-tabs'>
              {RANK_PERIODS.map((p) => (
                <View
                  key={p.key}
                  className={`period-tab ${rankPeriod === p.key ? 'period-tab-active' : ''}`}
                  hoverClass='press'
                  hoverStayTime={60}
                  onClick={() => switchRankPeriod(p.key)}
                >
                  <Text>{p.text}</Text>
                </View>
              ))}
            </View>
          </View>

          {rankLoading && !rankStats ? (
            <View className='rank-tip'>统计中...</View>
          ) : !rankStats?.project_rank?.length ? (
            <View className='empty-box card' onClick={goRecharge}>
              <Text className='empty-emoji'>📊</Text>
              <Text className='empty-text'>
                {isLoggedIn() ? '该周期还没有游玩记录，去首页「游乐消费」玩一次就有排行啦～' : '登录后查看我的游玩排行'}
              </Text>
            </View>
          ) : (
            <View className='rank-list card'>
              {rankStats.project_rank.slice(0, 6).map((r, i) => {
                const max = Math.max(...rankStats.project_rank!.slice(0, 6).map((x) => x.qty), 1)
                return (
                  <View key={r.name} className='rank-row'>
                    <Text className='rank-name'>
                      {i + 1}. {r.name}
                    </Text>
                    <View className='rank-track'>
                      <View
                        className='rank-fill'
                        style={{ width: `${Math.max(6, Math.round((r.qty / max) * 100))}%` }}
                      />
                    </View>
                    <Text className='rank-val'>{r.qty} 次 · {r.coins}币</Text>
                  </View>
                )
              })}
            </View>
          )}
        </View>
      )}

      {/* 我的卡弹窗（从「我的卡」大卡进入；原页面内嵌列表已改为弹窗） */}
      {showCards && (
        <View className='pm-mask' onClick={() => setShowCards(false)}>
          <View className='pm-card cons-modal' onClick={(e) => e.stopPropagation()}>
            <View className='pm-head'>
              <Text className='pm-title'>💳 我的卡（{cards.length}）</Text>
              <Text className='pm-close' onClick={() => setShowCards(false)}>✕</Text>
            </View>
            <ScrollView scrollY className='cons-modal-list'>
              <View className='cards-modal-bar'>
                <Text className='cards-modal-summary'>
                  会员 {cards.filter((c) => (c.kind || 'member') !== 'small').length} · 小额 {cards.filter((c) => c.kind === 'small').length}
                </Text>
                <Text
                  className='section-action'
                  onClick={(e) => {
                    e.stopPropagation()
                    setShowCards(false)
                    goRecharge()
                  }}
                >
                  ＋ 充值余币
                </Text>
              </View>
              {cards.length === 0 ? (
                <View className='empty-box card' onClick={goRecharge}>
                  <Text className='empty-emoji'>🎫</Text>
                  <Text className='empty-text'>还没有卡，点击「＋ 充值余币」办理，或问 AI「怎么办卡」～</Text>
                </View>
              ) : (
                <>
                  {/* 分类切换：会员充值 / 小额充值（都是充值记录；小额直接存入余币，不提供使用/退卡） */}
                  <View className='cards-tabs'>
                    {[
                      { key: 'member', text: '💳 会员充值', sub: '可设生效 · 可退卡' },
                      { key: 'small', text: '🪙 小额充值', sub: '直接存入余币' },
                    ].map((t) => (
                      <View
                        key={t.key}
                        className={`cards-tab ${cardsTab === t.key ? 'cards-tab-active' : ''}`}
                        hoverClass='press'
                        hoverStayTime={60}
                        onClick={() => setCardsTab(t.key as 'member' | 'small')}
                      >
                        <Text className='cards-tab-text'>{t.text}</Text>
                        <Text className='cards-tab-sub'>{t.sub}</Text>
                      </View>
                    ))}
                  </View>

                  {cards
                    .filter((c) =>
                      cardsTab === 'small' ? (c.kind || '') === 'small' : (c.kind || 'member') !== 'small')
                    .map((c) =>
                      cardsTab === 'small' ? (
                        /* 小额充值记录：仅展示余额/订单，无使用与退卡按钮 */
                        <View key={c.id} className='card-item card msg-enter'>
                          <View className='card-item-left'>
                            <Text className='card-item-name'>🪙 {c.card_name}</Text>
                            <Text className='card-item-no'>余 {c.coins} 币 · 订单号 {c.order_no}</Text>
                          </View>
                          <View className='card-item-actions'>
                            <Text className='small-flag'>已计入余币</Text>
                          </View>
                        </View>
                      ) : (
                        <View key={c.id} className='card-item card msg-enter'>
                          <View className='card-item-left'>
                            <Text className='card-item-name'>💳 {c.card_name}</Text>
                            <Text className='card-item-no'>余 {c.coins} 币 · 订单号 {c.order_no}</Text>
                          </View>
                          <View className='card-item-actions'>
                            {c.in_use ? (
                              <Text className='card-item-status'>生效中</Text>
                            ) : (
                              <View
                                className='card-op-btn card-op-use'
                                hoverClass='press'
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleUseCard(c)
                                }}
                              >
                                <Text>使用</Text>
                              </View>
                            )}
                            <View
                              className='card-op-btn card-op-refund'
                              hoverClass='press'
                              onClick={(e) => {
                                e.stopPropagation()
                                setRefundCard(c)
                              }}
                            >
                              <Text>退卡</Text>
                            </View>
                          </View>
                        </View>
                      ),
                    )}

                  {cards.filter((c) =>
                    cardsTab === 'small' ? (c.kind || '') === 'small' : (c.kind || 'member') !== 'small').length === 0 && (
                    <View className='empty-box card'>
                      <Text className='empty-text'>
                        {cardsTab === 'small'
                          ? '暂无小额充值记录，去充值页选「小额充值」试试～'
                          : '暂无会员卡，点击上方「＋ 充值余币」办理～'}
                      </Text>
                    </View>
                  )}
                </>
              )}
            </ScrollView>
          </View>
        </View>
      )}

      {/* 联系服务 */}
      <View className='section'>
        <Text className='section-title'>🤝 联系服务</Text>
        <View className='contact-box card'>
          <View className='contact-item' hoverClass='card-hover' hoverStayTime={80} onClick={callUs}>
            <Text className='contact-icon'>📞</Text>
            <View className='contact-info'>
              <Text className='contact-name'>电话咨询</Text>
              <Text className='contact-desc'>{PARK_PHONE}</Text>
            </View>
            <Text className='contact-arrow'>›</Text>
          </View>
          <View className='contact-item' hoverClass='card-hover' hoverStayTime={80} onClick={openMap}>
            <Text className='contact-icon'>📍</Text>
            <View className='contact-info'>
              <Text className='contact-name'>一键导航</Text>
              <Text className='contact-desc'>{PARK_ADDRESS}</Text>
            </View>
            <Text className='contact-arrow'>›</Text>
          </View>
        </View>
      </View>

      {/* 我的收藏弹窗：列表（一行一条）/ 详情，两个视图在**同一张卡片内**切换 */}
      {showFavorites && (
        <View className='pm-mask' onClick={closeFavorites}>
          <View className='pm-card fav-modal' onClick={(e) => e.stopPropagation()}>
            {favDetail ? (
              /* ===== 详情视图：完整问题 + 附图 + 完整回答；复制/追问收在这里 ===== */
              <View className='fav-detail'>
                {/* 命中区一律用 <View> 承载 onClick：小程序里 <text> 的可点区域只有文字本身，
                    点偏一点事件就落到父节点上，表现就是"按钮点不动" */}
                <View className='fav-detail-head'>
                  <View className='fav-back' onClick={backToList}>
                    <Text>‹ 返回</Text>
                  </View>
                  <View className='fav-detail-tags'>
                    <Text className='fav-badge'>问答</Text>
                    <Text className='fav-time'>{favTimeAgo(favDetail.created_at)}</Text>
                  </View>
                  <View className='pm-close' onClick={closeFavorites}>
                    <Text>✕</Text>
                  </View>
                </View>

                {/* 入场动效挂在 .fav-detail-body 上（不挂在 .fav-detail）：动效要靠 transform，
                    而带 transform 的祖先会给整棵子树建立层叠上下文 —— 把 5 个可点元素留在
                    有 transform 的容器之外，最稳。 */}
                <ScrollView scrollY className='fav-detail-scroll'>
                  <View className='fav-detail-body'>
                    <Text className='fav-detail-q'>{favDetail.question || '（图片提问）'}</Text>

                    {/* 附图：点击放大，可左右滑动浏览本条的图（stopPropagation 防止顺带触发卡片点击） */}
                    {favDetailImgs.length > 0 && (
                      <View className='fav-thumbs'>
                        {favDetailImgs.map((src, i) => (
                          <Image
                            key={`${favDetail.id}-${i}`}
                            className='fav-thumb'
                            src={src}
                            mode='aspectFill'
                            onClick={(e) => {
                              e.stopPropagation()
                              Taro.previewImage({ urls: favDetailImgs, current: src })
                            }}
                          />
                        ))}
                      </View>
                    )}

                    <View className='fav-detail-answer'>
                      <MarkdownView content={favDetail.answer} />
                    </View>
                  </View>
                </ScrollView>

                <View className='fav-detail-bar'>
                  <View
                    className='fav-btn'
                    hoverClass='press'
                    hoverStayTime={60}
                    onClick={(e) => {
                      e.stopPropagation()
                      copyFavorite(favDetail)
                    }}
                  >
                    <Text>复制</Text>
                  </View>
                  <View
                    className='fav-btn'
                    hoverClass='press'
                    hoverStayTime={60}
                    onClick={(e) => {
                      e.stopPropagation()
                      askFromFavorite(favDetail)
                    }}
                  >
                    <Text>追问</Text>
                  </View>
                  <View className='fav-actions-spacer' />
                  <View
                    className='fav-btn fav-btn-back'
                    hoverClass='press'
                    hoverStayTime={60}
                    onClick={backToList}
                  >
                    <Text>返回列表</Text>
                  </View>
                </View>
              </View>
            ) : (
              /* ===== 列表视图 ===== */
              <View className='fav-list-view'>
                <View className='pm-head'>
                  <Text className='pm-title'>我的收藏（{favorites.length}）</Text>
                  <View className='fav-head-right'>
                    {favorites.length > 0 && (
                      <View className='fav-clear' onClick={clearAllFavorites}>
                        <Text>{favClearing ? '清空中…' : '清空'}</Text>
                      </View>
                    )}
                    <View className='pm-close' onClick={closeFavorites}>
                      <Text>✕</Text>
                    </View>
                  </View>
                </View>

                {/* 搜索：只就地问答里找，前端过滤，不打接口 */}
                {favorites.length > 0 && (
                  <View className='fav-search'>
                    <Input
                      className='fav-search-input'
                      value={favKeyword}
                      placeholder='搜索收藏的问题'
                      confirmType='search'
                      onInput={(e) => setFavKeyword(e.detail.value)}
                    />
                    {!!favKeyword && (
                      <View className='fav-search-clear' onClick={() => setFavKeyword('')}>
                        <Text>✕</Text>
                      </View>
                    )}
                  </View>
                )}

                <ScrollView scrollY className='fav-modal-list'>
                  {favLoading ? (
                    <View className='empty-box card'>
                      <Text className='empty-text'>加载中...</Text>
                    </View>
                  ) : favorites.length === 0 ? (
                    <View className='empty-box card'>
                      <Text className='empty-text'>还没有收藏。去 AI 问答页点回答右下角的「收藏」吧～</Text>
                    </View>
                  ) : favShown.length === 0 ? (
                    <View className='empty-box card'>
                      <Text className='empty-text'>没有匹配「{favKeyword}」的收藏</Text>
                    </View>
                  ) : (
                    favShown.map((f, i) => {
                      const imgs = favImages(f)
                      return (
                        /* 外层 .fav-row 只负责删除时的「左滑 + 高度收拢」过渡；
                           内层 .fav-row-in 负责入场动画。动画与过渡必须落在不同元素上：
                           同元素既挂 animation(fill:both) 又挂 transition 同属性时，
                           微信 WebView 里 animation 终值会压住 transition，表现为「删除不动」。 */
                        <View
                          key={f.id}
                          className={`fav-row${favClosingId === f.id ? ' fav-row-out' : ''}`}
                        >
                          {/* 入场错峰靠 .fav-dN 类名（不用内联 style：小程序端 style 对象的
                              序列化路径不保证，类名最稳），最多错 8 档、每档 40ms */}
                          <View className={`fav-row-in fav-d${Math.min(i, 8)}`}>
                            {/* 左：内容简写（问句 + 摘要 + 时间 + 附图缩略），点一下同样进详情 */}
                            <View
                              className='fav-row-main'
                              hoverClass='card-hover'
                              hoverStayTime={80}
                              onClick={() => setFavDetailId(f.id)}
                            >
                              {imgs.length > 0 && (
                                <View className='fav-row-thumb-wrap'>
                                  <Image
                                    className='fav-row-thumb'
                                    src={imgs[0]}
                                    mode='aspectFill'
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      Taro.previewImage({ urls: imgs, current: imgs[0] })
                                    }}
                                  />
                                  {imgs.length > 1 && (
                                    <Text className='fav-row-more'>{`+${imgs.length - 1}`}</Text>
                                  )}
                                </View>
                              )}
                              <View className='fav-row-text'>
                                <Text className='fav-row-q'>{f.question || '（图片提问）'}</Text>
                                <Text className='fav-row-a'>{favSummary(f.answer)}</Text>
                                {/* 徽标 + 时间放同一行：比挤在问句左边省一整列宽度 */}
                                <View className='fav-row-meta'>
                                  <Text className='fav-badge'>问答</Text>
                                  <Text className='fav-row-meta-txt'>
                                    {favTimeAgo(f.created_at)}
                                    {imgs.length > 0 ? ` · ${imgs.length} 张图` : ''}
                                  </Text>
                                </View>
                              </View>
                            </View>

                            {/* 右：查看 / 删除（与左侧是兄弟节点，不会误触进详情；stopPropagation 兜底） */}
                            <View className='fav-row-btns'>
                              <View
                                className='fav-row-btn'
                                hoverClass='press'
                                hoverStayTime={60}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  setFavDetailId(f.id)
                                }}
                              >
                                <Text>查看</Text>
                              </View>
                              <View
                                className='fav-row-btn fav-row-btn-del'
                                hoverClass='press'
                                hoverStayTime={60}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  removeFavorite(f)
                                }}
                              >
                                <Text>取消</Text>
                              </View>
                            </View>
                          </View>
                        </View>
                      )
                    })
                  )}
                </ScrollView>

                {favorites.length > 0 && (
                  <Text className='cons-modal-tip'>收藏只存在你的账号里，取消后可在问答页重新收藏</Text>
                )}
              </View>
            )}
          </View>
        </View>
      )}

      {/* 消费记录明细已并入「游园码」页（/pages/passes/index），这里不再有弹窗 */}

      {/* 退卡确认弹窗 */}
      {refundCard && (
        <View className='pm-mask' onClick={() => setRefundCard(null)}>
          <View className='pm-card rf-card' onClick={(e) => e.stopPropagation()}>
            <View className='pm-head'>
              <Text className='pm-title'>退卡</Text>
              <Text className='pm-close' onClick={() => setRefundCard(null)}>✕</Text>
            </View>
            <View className='pm-body'>
              <Text className='rf-card-name'>💳 {refundCard.card_name}</Text>
              <View className='rf-row'>
                <Text className='rf-label'>剩余币数</Text>
                <Text className='rf-value'>🪙 {refundCard.coins} 币</Text>
              </View>
              <View className='rf-row'>
                <Text className='rf-label'>支付金额</Text>
                <Text className='rf-value'>¥{refundCard.price_yuan}</Text>
              </View>
              {refundCard.refundable ? (
                <View className='rf-refund-sec'>
                  <View className='rf-row'>
                    <Text className='rf-label'>可退款金额</Text>
                    <Text className='rf-value rf-money'>¥{refundCard.price_yuan}</Text>
                  </View>
                  <Text className='rf-tip'>退款将原路退回，退卡后该卡剩余币不可再使用</Text>
                  <Button className='pm-save rf-confirm' hoverClass='press' onClick={handleConfirmRefund} disabled={refunding}>
                    {refunding ? '退款中...' : `确认退款 ¥${refundCard.price_yuan}`}
                  </Button>
                </View>
              ) : (
                <View className='rf-block'>
                  <Text className='rf-block-text'>该卡剩余 {refundCard.coins} 币，不足面值 {refundCard.face_coins} 币，无法退款</Text>
                  <Text className='rf-block-text'>请到游乐场进行游玩，用完剩余币～</Text>
                </View>
              )}
              <Text className='pm-alt' onClick={() => setRefundCard(null)}>暂不退卡</Text>
            </View>
          </View>
        </View>
      )}

      {/* 个人信息弹窗（头像/昵称/手机号换绑） */}
      {showProfile && (
        <View className='pm-mask' onClick={closeProfile}>
          <View className='pm-card' onClick={(e) => e.stopPropagation()}>
            <View className='pm-head'>
              <Text className='pm-title'>
                {profileView === 'info' ? '个人信息' : profileView === 'rebind' ? '换绑手机号' : '登录密码'}
              </Text>
              <Text className='pm-close' onClick={closeProfile}>✕</Text>
            </View>

            {profileView === 'info' ? (
              <View className='pm-body'>
                <View className='pm-avatar-wrap'>
                  <Button
                    className='pm-avatar-btn'
                    openType='chooseAvatar'
                    onChooseAvatar={onChooseAvatar}
                    disabled={avatarBusy}
                  >
                    {avatarPreview ? (
                      <Image className='pm-avatar-img' src={avatarPreview} mode='aspectFill' />
                    ) : (
                      <Text className='pm-avatar-emoji'>🦆</Text>
                    )}
                  </Button>
                  <Text className='pm-avatar-tip'>{avatarBusy ? '上传中...' : '点击头像更换'}</Text>
                </View>

                <Text className='pm-label'>昵称</Text>
                <Input
                  className='pm-input'
                  type='nickname'
                  maxlength={20}
                  value={nicknameDraft}
                  onInput={(e) => setNicknameDraft(e.detail.value)}
                  placeholder='输入昵称'
                />

                <Text className='pm-label'>手机号</Text>
                <View className='pm-phone-row'>
                  <Text className='pm-phone'>{maskedPhone(user?.phone)}</Text>
                  <View className='pm-rebind-btn' hoverClass='press' onClick={() => setProfileView('rebind')}>
                    <Text>换绑</Text>
                  </View>
                </View>

                <Text className='pm-label'>登录密码</Text>
                <View className='pm-phone-row'>
                  <Text className='pm-phone'>{user?.pwd_set ? '已设置' : '未设置'}</Text>
                  <View className='pm-rebind-btn' hoverClass='press' onClick={() => setProfileView('password')}>
                    <Text>{user?.pwd_set ? '修改' : '设置'}</Text>
                  </View>
                </View>

                <Button className='pm-save' hoverClass='press' onClick={saveNickname} disabled={profileBusy}>
                  {profileBusy ? '保存中...' : '保存昵称'}
                </Button>

                <Button className='pm-logout' hoverClass='press' onClick={handleLogout}>
                  ⏻ 退出登录
                </Button>
              </View>
            ) : profileView === 'rebind' ? (
              <View className='pm-body'>
                <Text className='pm-card-sub'>当前手机号：{maskedPhone(user?.phone)}</Text>

                <Text className='pm-label'>新手机号</Text>
                <Input
                  className='pm-input'
                  type='number'
                  maxlength={11}
                  value={rebindPhone}
                  onInput={(e) => setRebindPhone(e.detail.value)}
                  placeholder='请输入新的 11 位手机号'
                />

                {user?.pwd_set ? (
                  <>
                    <Text className='pm-label'>当前密码（确认身份）</Text>
                    <Input
                      className='pm-input'
                      password
                      maxlength={32}
                      value={rebindCurrentPwd}
                      onInput={(e) => setRebindCurrentPwd(e.detail.value)}
                      placeholder='请输入当前登录密码'
                    />
                  </>
                ) : null}

                <Text className='pm-label'>{user?.pwd_set ? '新密码（留空则不改）' : '设置登录密码'}</Text>
                <Input
                  className='pm-input'
                  password
                  maxlength={32}
                  value={rebindPwd}
                  onInput={(e) => setRebindPwd(e.detail.value)}
                  placeholder='至少 6 位'
                />

                {rebindNeedVerify && (
                  <>
                    <View className='pm-conflict-tip'>
                      <Text className='pm-conflict-text'>
                        该手机号已绑定其他账号。验证该手机号后，你的微信会关联到该手机号的原账号，
                        持卡、余币、消费记录都留在原账号上。
                      </Text>
                    </View>
                    <Text className='pm-label'>短信验证码</Text>
                    <View className='pm-code-row'>
                      <Input
                        className='pm-input pm-code-input'
                        type='number'
                        maxlength={6}
                        value={rebindVerifyCode}
                        onInput={(e) => setRebindVerifyCode(e.detail.value)}
                        placeholder='6 位验证码'
                      />
                      <View
                        className={`pm-code-btn ${rebindCountdown > 0 ? 'pm-code-btn-off' : ''}`}
                        hoverClass='press'
                        onClick={sendRebindCode}
                      >
                        <Text>{rebindCountdown > 0 ? `${rebindCountdown}s 后重发` : '获取验证码'}</Text>
                      </View>
                    </View>
                  </>
                )}

                <Text className='pm-tip-note'>绑定后可用「手机号 + 密码」直接登录，无需验证码</Text>

                <Button className='pm-save' hoverClass='press' onClick={submitRebind} disabled={profileBusy}>
                  {profileBusy
                    ? '提交中...'
                    : rebindNeedVerify
                      ? '验证并合并'
                      : user?.pwd_set ? '确认换绑' : '绑定手机号'}
                </Button>
                <Text className='pm-alt' onClick={() => setProfileView('info')}>‹ 返回个人信息</Text>
              </View>
            ) : (
              <View className='pm-body'>
                <Text className='pm-card-sub'>
                  {user?.pwd_set
                    ? '修改密码后，其他设备需重新登录'
                    : '设置登录密码后，即可改用手机号 + 密码登录'}
                </Text>

                {user?.pwd_set ? (
                  <>
                    <Text className='pm-label'>原密码</Text>
                    <Input
                      className='pm-input'
                      password
                      maxlength={32}
                      value={oldPwd}
                      onInput={(e) => setOldPwd(e.detail.value)}
                      placeholder='请输入原密码'
                    />
                  </>
                ) : null}

                <Text className='pm-label'>新密码</Text>
                <Input
                  className='pm-input'
                  password
                  maxlength={32}
                  value={newPwd}
                  onInput={(e) => setNewPwd(e.detail.value)}
                  placeholder='至少 6 位'
                />

                <Text className='pm-label'>确认新密码</Text>
                <Input
                  className='pm-input'
                  password
                  maxlength={32}
                  value={newPwd2}
                  onInput={(e) => setNewPwd2(e.detail.value)}
                  placeholder='再次输入新密码'
                />

                <Button className='pm-save' hoverClass='press' onClick={submitPassword} disabled={profileBusy}>
                  {profileBusy ? '提交中...' : '保存密码'}
                </Button>
                <Text className='pm-alt' onClick={() => setProfileView('info')}>‹ 返回个人信息</Text>
              </View>
            )}
          </View>
        </View>
      )}

      {/* 服务信息 */}
      <View className='section'>
        <Text className='section-title'>ℹ️ 关于服务</Text>
        <View className='about-box card'>
          <Text className='about-line'>🤖 AI 问答由知识库驱动，回答均标注来源</Text>
          <Text className='about-line'>🗺️ 位置/路线/天气由高德实时数据支持</Text>
          <Text className='about-line'>🔒 登录采用微信授权，无需注册密码</Text>
        </View>
      </View>
    </View>
  )
}
