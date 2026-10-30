// ============================================================
// 首页：欢迎插画 + 双主卡 + 8 宫格独立功能弹窗（不走 AI 模型）
//       + 园区公告 / 今日热点（商家 PC 端发布，此处只读展示）
// ============================================================
import { useState } from 'react'
import { View, Text, Image, Input, Textarea } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import {
  PARK_NAME, PARK_ADDRESS, PARK_LATITUDE, PARK_LONGITUDE, PARK_PHONE, API_BASE,
} from '@/utils/config'
import {
  getNotices, getLostItems, reportLostItem, submitFeedback,
  getProjects, consumeItems, getMyBalance, getMyConsumptions,
  type NoticeInfo, type LostItem, type ParkProject, type MyConsumption, type ConsumeResult,
} from '@/utils/api'
import { requireLogin } from '@/utils/auth'
import './index.scss'

// 8 宫格：每个按钮打开各自的居中弹窗，均为独立功能，不经 AI 模型
type PopupType = 'nav' | 'guide' | 'hours' | 'report' | 'call' | 'lost' | 'consume' | 'feedback'

// 两张大按钮（主卡 3/4 大小）：游乐消费 + 失物招领（位置与大小对调）
const BIG_TILES: Array<{ icon: string; name: string; desc: string; type: PopupType; needLogin?: boolean }> = [
  { icon: '🎠', name: '游乐消费', desc: '多选项目扣费', type: 'consume', needLogin: true },
  { icon: '🔍', name: '失物招领', desc: '丢失物品认领', type: 'lost', needLogin: true },
]

// 六个功能按钮（3 列 × 2 行）
const SMALL_ROWS: Array<Array<{ icon: string; name: string; desc: string; type: PopupType; needLogin?: boolean }>> = [
  [
    { icon: '🧭', name: '一键导航', desc: '找路线更轻松', type: 'nav' },
    { icon: '🕐', name: '营业时间', desc: '今日开闭园', type: 'hours' },
    { icon: '🏰', name: '园区介绍', desc: '六大招牌项目', type: 'guide' },
  ],
  [
    { icon: '🧸', name: '我要报失', desc: '一键登记', type: 'report', needLogin: true },
    { icon: '📞', name: '电话咨询', desc: '快速联络人工', type: 'call' },
    { icon: '💬', name: '意见反馈', desc: '倾听您的建议', type: 'feedback', needLogin: true },
  ],
]

const POPUP_TITLES: Record<string, string> = {
  nav: '📍 一键导航',
  guide: '🏰 园区介绍',
  hours: '🕐 营业时间',
  report: '🧸 我要报失',
  call: '📞 电话咨询',
  lost: '🔍 失物招领',
  consume: '🎠 游乐消费',
  feedback: '💬 意见反馈',
}

// 以下静态内容与园区知识库一致（票价/时段），弹窗内直接展示，不经 AI
const PARK_HOURS = [
  { name: '周一至周五', value: '9:00 – 21:00' },
  { name: '周末及节假日', value: '9:00 – 21:30' },
]

// 园区介绍：6 个招牌项目（项目名 / 币值 / 时长与后端 park_projects 种子数据一致）
// 实景照放在后端 static/park/，前端用 API_BASE 拼绝对地址（与失物招领照片同一套 /static 路由）
type ParkSpot = {
  key: string
  name: string
  coins: number
  unit: string
  photo: string
  brief: string
  detail: string
}

const PARK_SPOTS: ParkSpot[] = [
  {
    key: 'carousel',
    name: '旋转木马',
    coins: 30,
    unit: '5 分钟',
    photo: '/static/park/carousel.jpg',
    brief: '橙黄顶棚 · 白马与独角兽',
    detail: '园区正门的经典旋转木马，橙黄色顶棚下是白马、独角兽和彩绘小马车，一开转灯光就跟着音乐一起亮。单次约 5 分钟、30 币。傍晚灯带全亮的时候最出片，建议安排在最后一场。',
  },
  {
    key: 'trampoline',
    name: '蹦床',
    coins: 40,
    unit: '5 分钟',
    photo: '/static/park/trampoline.jpg',
    brief: '弹力带辅助 · 越弹越高',
    detail: '带安全背带的蹦极蹦床，弹力带会帮着一起往上提，越弹越高，工作人员全程在跳台边看着。单次 5 分钟、40 币。建议穿运动鞋来玩，家长可以在围栏外拍照。',
  },
  {
    key: 'bath',
    name: '浴室大作战',
    coins: 40,
    unit: '5 分钟',
    photo: '/static/park/bath.jpg',
    brief: '玻璃泡泡舱 · 浴缸小车',
    detail: '坐在浴缸造型的小车里，在透明玻璃舱中旋转起伏，舱内是暖色调的浴室场景，外面看进去像一整颗会发光的泡泡。单次 5 分钟、40 币。舱体比较封闭，容易晕的小朋友可以先在舱外看一轮再决定。',
  },
  {
    key: 'swing',
    name: '荡秋千',
    coins: 40,
    unit: '4 分钟',
    photo: '/static/park/swing.jpg',
    brief: '超大座椅 · 大幅摆荡',
    detail: '黄色超大座椅整体吊起，带着人前后大幅摆荡，是园区里失重感最强的一台。单次 4 分钟、40 币。落座后工作人员会帮忙把安全带系好，家长站在围栏外就能看到全程。',
  },
  {
    key: 'coaster',
    name: '小型过山车',
    coins: 50,
    unit: '15 圈',
    photo: '/static/park/coaster.jpg',
    brief: '木屋主题 · 连转 15 圈',
    detail: '木屋主题的亲子小过山车，小汽车沿木栅栏绕行 15 圈，沿途会经过蘑菇、风车和彩虹拱门。单次 15 圈、50 币，是园区单价最高的项目，适合家长陪同小朋友一起坐。',
  },
  {
    key: 'cannon',
    name: '大炮乐园',
    coins: 30,
    unit: '80 发子弹',
    photo: '/static/park/cannon.jpg',
    brief: '一排鸭鸭炮台 · 80 发子弹',
    detail: '一排鸭鸭造型的射击机位并排摆开，一局 80 发子弹，打完看命中分数。单次 30 币。机位前配了小黄椅，家长可以坐下来陪玩。',
  },
]

export default function IndexPage() {
  const [popup, setPopup] = useState<null | {
    type: string
    kind?: 'announcement' | 'hot'
    item?: NoticeInfo
  }>(null)
  // 园区介绍：当前查看的招牌项目（null = 停留在 6 图列表）
  const [guideSpot, setGuideSpot] = useState<ParkSpot | null>(null)
  // 游乐消费（多选项目合并扣币）
  const [projects, setProjects] = useState<ParkProject[]>([])
  const [selected, setSelected] = useState<Record<number, number>>({})
  const [availCoins, setAvailCoins] = useState(0)
  const [consuming, setConsuming] = useState(false)
  const [consumeResult, setConsumeResult] = useState<ConsumeResult | null>(null)
  const [recentCons, setRecentCons] = useState<MyConsumption[]>([])
  const [notices, setNotices] = useState<{ announcement: NoticeInfo[]; hot: NoticeInfo[] }>({
    announcement: [],
    hot: [],
  })

  // 报失表单
  const [lostName, setLostName] = useState('')
  const [lostReporter, setLostReporter] = useState('')
  const [lostPhotoPath, setLostPhotoPath] = useState('')
  const [lostSubmitting, setLostSubmitting] = useState(false)

  // 失物认领列表
  const [lostItems, setLostItems] = useState<LostItem[]>([])
  const [lostLoading, setLostLoading] = useState(false)

  // 意见反馈表单
  const [fbContent, setFbContent] = useState('')
  const [fbNickname, setFbNickname] = useState('')
  const [fbSubmitting, setFbSubmitting] = useState(false)

  // 每次进入首页刷新公告/热点（商家 PC 端发布后这里实时展示）
  useDidShow(() => {
    getNotices()
      .then((res) => {
        const list = res.data || []
        setNotices({
          announcement: list.filter((n) => n.kind === 'announcement'),
          hot: list.filter((n) => n.kind === 'hot'),
        })
      })
      .catch(() => {
        /* 静默失败：保留兜底文案 */
      })
  })

  const goChat = () => Taro.switchTab({ url: '/pages/chat/index' })
  const goRecharge = () => Taro.navigateTo({ url: '/pages/recharge/index' })
  const openMap = () =>
    Taro.openLocation({
      latitude: PARK_LATITUDE,
      longitude: PARK_LONGITUDE,
      name: PARK_NAME,
      address: PARK_ADDRESS,
      scale: 16,
    })
  const callUs = () => Taro.makePhoneCall({ phoneNumber: PARK_PHONE })

  const closePopup = () => {
    setPopup(null)
    setGuideSpot(null)
    setLostName('')
    setLostReporter('')
    setLostPhotoPath('')
    setFbContent('')
    setFbNickname('')
    setSelected({})
    setConsumeResult(null)
  }

  // 打开弹窗：失物认领弹窗拉取待认领列表
  const openPopup = async (type: PopupType, needLogin?: boolean) => {
    if (needLogin && !requireLogin('使用该功能')) return
    setPopup({ type })
    if (type === 'consume') {
      getProjects().then((r) => setProjects(r.data || [])).catch(() => setProjects([]))
      // 可用余币 = 会员卡 + 小额充值 + 签到所得（有多少就能扣多少；生效卡只决定优先扣哪张）
      getMyBalance()
        .then((r) => setAvailCoins((r.data && r.data.coins) || 0))
        .catch(() => setAvailCoins(0))
      getMyConsumptions(5, true).then((r) => setRecentCons(r.data || [])).catch(() => setRecentCons([]))
    }
    if (type === 'lost') {
      setLostLoading(true)
      try {
        const res = await getLostItems()
        setLostItems(res.data || [])
      } catch {
        setLostItems([])
      } finally {
        setLostLoading(false)
      }
    }
  }

  // 点击公告/热点标题 → 弹出内容详情卡片
  const openNoticeDetail = (kind: 'announcement' | 'hot', item: NoticeInfo) => {
    setPopup({ type: 'notice-detail', kind, item })
  }

  // ---------- 游乐消费 ----------
  const toggleProject = (id: number) => {
    setSelected((prev) => {
      const next = { ...prev }
      if (next[id]) delete next[id]
      else next[id] = 1
      return next
    })
  }

  const changeQty = (id: number, delta: number) => {
    setSelected((prev) => {
      const q = (prev[id] || 0) + delta
      if (q < 1) return prev
      return { ...prev, [id]: Math.min(q, 99) }
    })
  }

  const totalCoins = Object.entries(selected).reduce((sum, [pid, qty]) => {
    const p = projects.find((x) => x.id === Number(pid))
    return sum + (p ? p.price_coins * qty : 0)
  }, 0)

  const confirmConsume = async () => {
    if (consuming) return
    const items = Object.entries(selected).map(([pid, qty]) => ({ project_id: Number(pid), qty }))
    if (items.length === 0) {
      Taro.showToast({ title: '请先勾选要玩的项目', icon: 'none' })
      return
    }
    if (totalCoins > availCoins) {
      Taro.showToast({ title: `余币不足，还差 ${totalCoins - availCoins} 币`, icon: 'none' })
      return
    }
    setConsuming(true)
    try {
      const res = await consumeItems(items)
      setConsumeResult(res.data)
      setAvailCoins(res.data.remaining_coins)
      Taro.showToast({ title: res.msg || '扣费成功', icon: 'none' })
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '扣费失败，请稍后重试', icon: 'none', duration: 3000 })
    } finally {
      setConsuming(false)
    }
  }

  // 选择报失照片（相册/拍照）
  const chooseLostPhoto = () => {
    Taro.chooseImage({
      count: 1,
      sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        if (res.tempFilePaths && res.tempFilePaths.length > 0) {
          setLostPhotoPath(res.tempFilePaths[0])
        }
      },
    })
  }

  // 提交报失（名称 + 照片 multipart 上传，与 AI 问答页报失同一接口）
  const submitLostReport = async () => {
    const name = lostName.trim()
    if (!name) {
      Taro.showToast({ title: '请填写失物名称', icon: 'none' })
      return
    }
    if (!lostPhotoPath) {
      Taro.showToast({ title: '请上传失物照片', icon: 'none' })
      return
    }
    if (lostSubmitting) return
    setLostSubmitting(true)
    try {
      const res = await reportLostItem(name, lostPhotoPath, lostReporter.trim())
      Taro.showToast({ title: res.msg || '报失成功', icon: 'success' })
      closePopup()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '报失失败，请稍后重试', icon: 'none' })
    } finally {
      setLostSubmitting(false)
    }
  }

  // 提交意见反馈
  const submitFb = async () => {
    const content = fbContent.trim()
    if (!content) {
      Taro.showToast({ title: '请填写反馈内容', icon: 'none' })
      return
    }
    if (fbSubmitting) return
    setFbSubmitting(true)
    try {
      const res = await submitFeedback(content, fbNickname.trim())
      Taro.showToast({ title: res.msg || '反馈已提交', icon: 'success' })
      closePopup()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '提交失败，请稍后重试', icon: 'none' })
    } finally {
      setFbSubmitting(false)
    }
  }

  return (
    <View className='home-page'>
      {/* —— 装饰层 —— */}
      <View className='bunting'>
        {Array.from({ length: 10 }).map((_, i) => (
          <Text key={i} className={`flag f${i % 4}`} />
        ))}
      </View>
      <Text className='deco cloud c1'>☁️</Text>
      <Text className='deco cloud c2'>☁️</Text>
      <Text className='deco balloon b1'>🎈</Text>
      <Text className='deco balloon b2'>🎈</Text>
      <Text className='deco wheel'>🎡</Text>

      {/* —— 欢迎区 —— */}
      <View className='hero'>
        <View className='mascot-row'>
          <Text className='mascot m-horse'>🎠</Text>
          <Text className='mascot m-duck breathe'>🦆</Text>
          <Text className='mascot m-wheel'>🎡</Text>
        </View>
        <Text className='hero-title'>欢迎来到{PARK_NAME}</Text>
        <View className='hero-sub'>
          <Text className='hero-check'>✅</Text>
          <Text>AI问答 · 办卡充值 · 游乐服务一站式</Text>
        </View>
      </View>

      {/* —— 双主卡 —— */}
      <View className='hero-row'>
        <View className='hero-card hc-ask' hoverClass='press' hoverStayTime={80} onClick={goChat}>
          <Text className='hc-icon'>💬</Text>
          <Text className='hc-star'>⭐</Text>
          <Text className='hc-name'>AI问答</Text>
          <Text className='hc-desc'>智能客服随时解答</Text>
          <View className='hc-btn'>
            <Text>开始咨询</Text>
          </View>
        </View>
        <View className='hero-card hc-pay' hoverClass='press' hoverStayTime={80} onClick={goRecharge}>
          <Text className='hc-icon'>💳</Text>
          <Text className='hc-coins'>🪙</Text>
          <Text className='hc-name'>办卡充值</Text>
          <Text className='hc-desc'>充值办卡 · 优惠畅玩</Text>
          <View className='hc-btn'>
            <Text>立即办理</Text>
          </View>
        </View>
      </View>

      {/* —— 两张大按钮（主卡 3/4）：失物招领 / 项目游玩 —— */}
      <View className='big-row'>
        {BIG_TILES.map((t) => (
          <View key={t.type} className='big-tile' hoverClass='press' hoverStayTime={80} onClick={() => openPopup(t.type, t.needLogin)}>
            <Text className='big-icon'>{t.icon}</Text>
            <Text className='big-name'>{t.name}</Text>
            <Text className='big-desc'>{t.desc}</Text>
            <Text className='big-arrow'>›</Text>
          </View>
        ))}
      </View>

      {/* —— 六个功能按钮（3 列 × 2 行）—— */}
      {SMALL_ROWS.map((row, ri) => (
        <View className='tile-row' key={ri}>
          {row.map((t) => (
            <View key={t.type} className='tile' hoverClass='press' hoverStayTime={80} onClick={() => openPopup(t.type, t.needLogin)}>
              <Text className='tile-icon'>{t.icon}</Text>
              <Text className='tile-name'>{t.name}</Text>
              <Text className='tile-desc'>{t.desc}</Text>
            </View>
          ))}
        </View>
      ))}

      {/* —— 园区公告 / 今日热点：仅展示全部标题（按商家设置的序号小号在上），点标题看详情 —— */}
      <View className='notice-row'>
        <View className='notice-card'>
          <View className='notice-head'>
            <Text className='notice-dot' />
            <Text className='notice-title'>园区公告</Text>
          </View>
          <View className='notice-list'>
            {notices.announcement.length === 0 ? (
              <Text className='notice-line'>暂无公告，敬请关注</Text>
            ) : (
              notices.announcement.map((n) => (
                <View
                  key={n.id}
                  className='notice-tile'
                  hoverClass='press'
                  hoverStayTime={80}
                  onClick={() => openNoticeDetail('announcement', n)}
                >
                  <Text className='notice-tile-dot' />
                  <Text className='notice-tile-text'>{n.title}</Text>
                  <Text className='notice-tile-arrow'>›</Text>
                </View>
              ))
            )}
          </View>
        </View>
        <View className='notice-card'>
          <View className='notice-head'>
            <Text className='notice-dot' />
            <Text className='notice-title'>今日热点</Text>
          </View>
          <View className='notice-list'>
            {notices.hot.length === 0 ? (
              <Text className='notice-line'>暂无热点，敬请关注</Text>
            ) : (
              notices.hot.map((n) => (
                <View
                  key={n.id}
                  className='notice-tile'
                  hoverClass='press'
                  hoverStayTime={80}
                  onClick={() => openNoticeDetail('hot', n)}
                >
                  <Text className='notice-tile-dot' />
                  <Text className='notice-tile-text'>{n.title}</Text>
                  <Text className='notice-tile-arrow'>›</Text>
                </View>
              ))
            )}
          </View>
        </View>
      </View>

      <View className='home-footer'>
        <Text>🎡 鸭先知 · 大黄鸭游乐园智能游客服务</Text>
      </View>

      {/* ================= 居中功能弹窗 ================= */}
      {popup && (
        <View className='popup-mask' onClick={closePopup}>
          <View className='popup-card' onClick={(e) => e.stopPropagation()}>
            <View className='popup-head'>
              <Text className='popup-title'>
                {popup.type === 'notice-detail'
                  ? (popup.kind === 'announcement' ? '📢 园区公告' : '🔥 今日热点')
                  : popup.type === 'guide' && guideSpot
                    ? `🏰 ${guideSpot.name}`
                    : POPUP_TITLES[popup.type] || ''}
              </Text>
              <Text className='popup-close' onClick={closePopup}>✕</Text>
            </View>

            <View className='popup-body'>
              {/* 一键导航 */}
              {popup.type === 'nav' && (
                <View className='popup-sec'>
                  <View className='info-row'>
                    <Text className='info-name'>园区地址</Text>
                    <Text className='info-value addr'>{PARK_ADDRESS}</Text>
                  </View>
                  <View className='info-row'>
                    <Text className='info-name'>服务电话</Text>
                    <Text className='info-value'>{PARK_PHONE}</Text>
                  </View>
                  <Text className='popup-tip'>点击下方按钮直接打开地图导航到园区</Text>
                  <View className='popup-btn' hoverClass='press' onClick={openMap}>
                    <Text>🗺️ 打开地图导航</Text>
                  </View>
                </View>
              )}

              {/* 营业时间 */}
              {popup.type === 'hours' && (
                <View className='popup-sec'>
                  {PARK_HOURS.map((h) => (
                    <View className='info-row' key={h.name}>
                      <Text className='info-name'>{h.name}</Text>
                      <Text className='info-value'>{h.value}</Text>
                    </View>
                  ))}
                  <Text className='popup-tip'>🌧️ 恶劣暴雨大风天气会临时闭园，以园区公告为准</Text>
                </View>
              )}

              {/* 游乐消费（多选项目合并扣币） */}
              {popup.type === 'consume' && (consumeResult ? (
                <View className='popup-sec'>
                  <View className='cs-success'>
                    <Text className='cs-success-icon'>✅</Text>
                    <Text className='cs-success-title'>扣费成功</Text>
                    <Text className='cs-success-sub'>向项目工作人员出示核销码</Text>
                    <View className='cs-code-box'>
                      <Text className='cs-code'>{consumeResult.verify_code}</Text>
                    </View>
                  </View>
                  <View className='rf-row'>
                    <Text className='rf-label'>消费明细</Text>
                    <Text className='rf-value'>
                      {consumeResult.items.map((it) => `${it.name}×${it.qty}`).join('、')}
                    </Text>
                  </View>
                  <View className='rf-row'>
                    <Text className='rf-label'>合计扣除</Text>
                    <Text className='rf-value rf-money'>🪙 {consumeResult.total_coins} 币</Text>
                  </View>
                  <View className='rf-row'>
                    <Text className='rf-label'>剩余余额</Text>
                    <Text className='rf-value'>🪙 {consumeResult.remaining_coins} 币</Text>
                  </View>
                  <View className='popup-btn' hoverClass='press' onClick={closePopup}>
                    <Text>完成</Text>
                  </View>
                </View>
              ) : (
                <View className='popup-sec'>
                  <View className='cs-balance-bar'>
                    <Text className='cs-balance-label'>可用余币</Text>
                    <Text className='cs-balance-num'>🪙 {availCoins} 币</Text>
                  </View>
                  <Text className='cs-balance-note'>含会员卡 · 小额充值 · 签到所得，均可抵扣</Text>

                  {projects.length === 0 ? (
                    <Text className='popup-empty'>项目价目加载中...</Text>
                  ) : (
                    projects.map((p) => {
                      const qty = selected[p.id] || 0
                      return (
                        <View
                          key={p.id}
                          className={`cs-row ${qty > 0 ? 'cs-row-on' : ''}`}
                          hoverClass='press'
                          onClick={() => toggleProject(p.id)}
                        >
                          <View className={`cs-check ${qty > 0 ? 'cs-check-on' : ''}`}>
                            {qty > 0 ? <Text>✓</Text> : <Text />}
                          </View>
                          <View className='cs-info'>
                            <Text className='cs-name'>{p.name}</Text>
                            {!!p.unit_desc && <Text className='cs-unit'>{p.unit_desc}</Text>}
                          </View>
                          <Text className='cs-price'>{p.price_coins} 币</Text>
                          {qty > 0 && (
                            <View className='cs-stepper' onClick={(e) => e.stopPropagation()}>
                              <View
                                className='cs-step-btn'
                                onClick={(e) => { e.stopPropagation(); changeQty(p.id, -1) }}
                              >
                                <Text>−</Text>
                              </View>
                              <Text className='cs-qty-num'>{qty}</Text>
                              <View
                                className='cs-step-btn'
                                onClick={(e) => { e.stopPropagation(); changeQty(p.id, 1) }}
                              >
                                <Text>＋</Text>
                              </View>
                            </View>
                          )}
                        </View>
                      )
                    })
                  )}

                  <View className='cs-total-bar'>
                    <Text className='cs-total-label'>合计</Text>
                    <Text className={`cs-total-num ${totalCoins > availCoins ? 'cs-total-bad' : ''}`}>
                      🪙 {totalCoins} 币
                    </Text>
                  </View>

                  {totalCoins > availCoins && (
                    <Text className='popup-tip'>余币不足，还差 {totalCoins - availCoins} 币，请先充值</Text>
                  )}

                  <View
                    className={`popup-btn ${consuming || totalCoins === 0 || totalCoins > availCoins ? 'popup-btn-disabled' : ''}`}
                    hoverClass='press'
                    onClick={confirmConsume}
                  >
                    <Text>{consuming ? '扣费中...' : totalCoins > 0 ? `确认扣费 ${totalCoins} 币` : '请先勾选项目'}</Text>
                  </View>

                  <Text className='popup-tip'>💡 一名儿童可由一名大人免费陪同入场；1 元 = 1 币</Text>

                  {recentCons.length === 0 && (
                    <Text className='cs-recent-empty'>今日暂无消费</Text>
                  )}
                  {recentCons.length > 0 && (
                    <View className='cs-recent'>
                      <Text className='cs-recent-title'>今日消费</Text>
                      {recentCons.map((c) => (
                        <View className='cs-recent-row' key={c.order_no}>
                          <Text className='cs-recent-code'>{c.verify_code}</Text>
                          <Text className='cs-recent-coins'>🪙 {c.total_coins}</Text>
                          <Text className={`cs-recent-status ${c.status === 'used' ? 'used' : c.status === 'refunded' ? 'refunded' : ''}`}>
                            {c.status === 'used' ? '已核销' : c.status === 'refunded' ? '已退款' : '未核销'}
                          </Text>
                        </View>
                      ))}
                    </View>
                  )}
                </View>
              ))}

              {/* 园区介绍：6 个招牌项目（点卡片看详细介绍） */}
              {popup.type === 'guide' && (guideSpot ? (
                <View className='popup-sec'>
                  <Image
                    className='pg-hero'
                    src={API_BASE + guideSpot.photo}
                    mode='aspectFill'
                    onClick={() => Taro.previewImage({
                      urls: PARK_SPOTS.map((s) => API_BASE + s.photo),
                      current: API_BASE + guideSpot.photo,
                    })}
                  />
                  <View className='pg-row'>
                    <Text className='pg-dname'>{guideSpot.name}</Text>
                    <Text className='pg-meta'>🪙 {guideSpot.coins} 币 · {guideSpot.unit}</Text>
                  </View>
                  <Text className='pg-dtext'>{guideSpot.detail}</Text>
                  <Text className='popup-tip'>💡 点大图可全屏查看；价格与时长以园区现场公示为准</Text>
                  <View className='popup-btn' hoverClass='press' onClick={() => setGuideSpot(null)}>
                    <Text>‹ 返回园区介绍</Text>
                  </View>
                </View>
              ) : (
                <View className='popup-sec'>
                  {PARK_SPOTS.map((s) => (
                    <View
                      className='pg-item'
                      key={s.key}
                      hoverClass='press'
                      hoverStayTime={80}
                      onClick={() => setGuideSpot(s)}
                    >
                      <Image className='pg-img' src={API_BASE + s.photo} mode='aspectFill' />
                      <View className='pg-info'>
                        <Text className='pg-name'>{s.name}</Text>
                        <Text className='pg-brief'>{s.brief}</Text>
                        <Text className='pg-meta'>🪙 {s.coins} 币 · {s.unit}</Text>
                      </View>
                      <Text className='pg-arrow'>›</Text>
                    </View>
                  ))}
                  <Text className='popup-tip'>💡 一名儿童可由一名大人免费陪同入场；点任意项目可看详细介绍</Text>
                </View>
              ))}

              {/* 电话咨询 */}
              {popup.type === 'call' && (
                <View className='popup-sec'>
                  <View className='call-box'>
                    <Text className='call-icon'>📞</Text>
                    <Text className='call-num'>{PARK_PHONE}</Text>
                    <Text className='call-tip'>服务时间与园区营业时间一致</Text>
                  </View>
                  <View className='popup-btn' hoverClass='press' onClick={callUs}>
                    <Text>立即拨打</Text>
                  </View>
                </View>
              )}

              {/* 我要报失 */}
              {popup.type === 'report' && (
                <View className='popup-sec'>
                  <View className='form-label'>失物名称</View>
                  <Input
                    className='form-input'
                    value={lostName}
                    onInput={(e) => setLostName(e.detail.value)}
                    placeholder='如：粉色水壶'
                    maxlength={50}
                  />
                  <View className='form-label'>失物照片</View>
                  {lostPhotoPath ? (
                    <View className='photo-preview' onClick={chooseLostPhoto}>
                      <Image
                        className='photo-img'
                        src={lostPhotoPath}
                        mode='aspectFill'
                        onClick={(e) => {
                          e.stopPropagation()
                          Taro.previewImage({ urls: [lostPhotoPath], current: lostPhotoPath })
                        }}
                      />
                    </View>
                  ) : (
                    <View className='photo-box' hoverClass='press' onClick={chooseLostPhoto}>
                      <Text className='photo-icon'>📷</Text>
                      <Text className='photo-text'>点击上传照片</Text>
                    </View>
                  )}
                  <View className='form-label'>联系方式（选填）</View>
                  <Input
                    className='form-input'
                    value={lostReporter}
                    onInput={(e) => setLostReporter(e.detail.value)}
                    placeholder='手机号 / 称呼，方便工作人员联系您'
                    maxlength={30}
                  />
                  <View
                    className={`popup-btn ${lostSubmitting ? 'popup-btn-disabled' : ''}`}
                    hoverClass='press'
                    onClick={submitLostReport}
                  >
                    <Text>{lostSubmitting ? '提交中...' : '提交报失'}</Text>
                  </View>
                </View>
              )}

              {/* 失物招领 */}
              {popup.type === 'lost' && (
                <View className='popup-sec'>
                  {lostLoading ? (
                    <Text className='popup-empty'>加载中...</Text>
                  ) : lostItems.length === 0 ? (
                    <Text className='popup-empty'>🎉 暂无待认领的失物</Text>
                  ) : (
                    lostItems.map((item) => (
                      <View className='lost-item' key={item.id}>
                        {item.photo_url ? (
                          <Image
                            className='lost-img'
                            src={API_BASE + item.photo_url}
                            mode='aspectFill'
                            onClick={() => Taro.previewImage({
                              urls: lostItems.filter((x) => x.photo_url).map((x) => API_BASE + x.photo_url),
                              current: API_BASE + item.photo_url,
                            })}
                          />
                        ) : (
                          <View className='lost-img lost-img-empty'><Text>🧸</Text></View>
                        )}
                        <View className='lost-info'>
                          <Text className='lost-name'>{item.name}</Text>
                          <Text className='lost-time'>{item.created_at}</Text>
                        </View>
                        <Text className='lost-badge'>待认领</Text>
                      </View>
                    ))
                  )}
                  <Text className='popup-tip'>认领请到园区服务台核对信息；丢了东西？点「我要报失」登记</Text>
                </View>
              )}

              {/* 意见反馈 */}
              {popup.type === 'feedback' && (
                <View className='popup-sec'>
                  <View className='form-label'>您的建议（必填）</View>
                  <Textarea
                    className='form-textarea'
                    value={fbContent}
                    onInput={(e) => setFbContent(e.detail.value)}
                    placeholder='欢迎告诉我们您的游玩建议、意见或发现的问题～'
                    maxlength={300}
                  />
                  <View className='form-label'>称呼 / 联系方式（选填）</View>
                  <Input
                    className='form-input'
                    value={fbNickname}
                    onInput={(e) => setFbNickname(e.detail.value)}
                    placeholder='方便我们回访感谢您'
                    maxlength={30}
                  />
                  <View
                    className={`popup-btn ${fbSubmitting ? 'popup-btn-disabled' : ''}`}
                    hoverClass='press'
                    onClick={submitFb}
                  >
                    <Text>{fbSubmitting ? '提交中...' : '提交反馈'}</Text>
                  </View>
                </View>
              )}

              {/* 公告 / 热点详情卡片（首页点标题进入） */}
              {popup.type === 'notice-detail' && popup.item && (
                <View className='popup-sec'>
                  <Text className='n-detail-title'>{popup.item.title}</Text>
                  <Text className='n-detail-content'>
                    {popup.item.content || '暂无详细内容'}
                  </Text>
                  <Text className='n-detail-time'>发布时间：{popup.item.created_at}</Text>
                </View>
              )}
            </View>
          </View>
        </View>
      )}
    </View>
  )
}
