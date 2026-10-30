// ============================================================
// AI 问答页（移植自 PC 端 HomePage，交互效果全保留）：
// 流式输出 / 思考时间线 / Markdown / 地图POI路线天气 / 快捷提问 /
// 项目勾选下单（一单一券包）/ 游园码入口 / 星级评价 / 语音输入 / 办卡卡片
// SSE: Taro.request enableChunked（utils/sse.ts），异常自动降级 POST /chat
// ============================================================
import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { View, Text, ScrollView, Image, Textarea, Map, Button, Input, Canvas, Picker, Switch } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import ThinkingBox from '@/components/ThinkingBox'
import type { ThinkingStep } from '@/components/ThinkingBox'
import MarkdownView from '@/components/MarkdownView'
import CardAction from '@/components/CardAction'
import UserAvatar from '@/components/UserAvatar'
import type { CardInfo, LostItem, LostPhotoInsight, HotQuestion } from '@/utils/api'
import type { CardItem } from '@/components/CardAction'
import { API_BASE, PARK_LATITUDE, PARK_LONGITUDE, PENDING_QUESTION_KEY } from '@/utils/config'
import { ensureLogin, getToken, isLoggedIn, requireLogin, getUser, type MpUser } from '@/utils/auth'
import { onUserChanged, refreshUser } from '@/utils/user'
import { onFavoritesChanged, notifyFavoritesChanged } from '@/utils/favorites'
import { sseRequest } from '@/utils/sse'
import { speakText } from '@/utils/tts'
import {
  request, getLostItems, reportLostItem, askVision, analyzeLostPhoto,
  getHotQuestions, addFavorite, uploadFavoriteImage, getFavorites, deleteFavorite,
  getProjects, consumeItems, getMyBalance,
  getMyProfile, updateMyProfile, clearMyProfile, setPersonalization, getProfileOptions,
  type ParkProject, type ConsumeResult,
  type MyProfile, type ProfileKid, type ProfileOptions,
} from '@/utils/api'
import './index.scss'

// 游乐项目面板过去是这里硬编码的一份 6 项清单（单位写「元」），点一下把项目名当问题
// 丢给模型 —— 价格与实际扣币口径会分叉（真实价格只认 park_projects 表），
// 2026-09-20 改成读 /pay/projects + 就地勾选下单，硬编码常量已删除。

// 快捷提问兜底（后端 /chat/hot-questions 拉取失败或未登录时使用）
// 2026-09-21：这 6 条已**写死**在后端（并落 Redis），不再是"近 7 天日志自动统计"——
//   自动统计会把按钮变成日志回放（同一主题重复占位、冒出极低频问法），
//   反而盖住游客真正常用的入口。这里那份兜底必须与后端 PINNED_HOT_QUESTIONS 逐条一致，
//   否则断网时用户在首页看到的六个词和平时不一样。
const DEFAULT_QUICK_QUESTIONS: HotQuestion[] = [
  { icon: '🧸', label: '失物招领', question: '东西丢了怎么找回', hit_count: 0 },
  { icon: '💳', label: '办卡充值', question: '怎么办卡充值', hit_count: 0 },
  { icon: '🕐', label: '营业时间', question: '营业时间', hit_count: 0 },
  { icon: '💰', label: '退卡规则', question: '能退卡吗', hit_count: 0 },
  { icon: '👶', label: '儿童安全', question: '儿童游玩注意事项', hit_count: 0 },
  { icon: '🔧', label: '设备故障', question: '设备故障怎么办', hit_count: 0 },
]

/** 可点击的服务出口（由业务 Agent 通过 action 事件下发） */
export interface ServiceItem {
  id: string      // recharge / lost_menu / report_lost / lost_claim
  title: string
}

interface Message {
  id: number
  type: 'user' | 'ai' | 'system'
  content: string
  /** 用户消息附带的图片（本地临时路径，上传提问前展示在气泡内） */
  image?: string
  thinking?: ThinkingStep[]
  thinkingDone?: boolean
  references?: string[]
  cards?: CardItem[]
  services?: ServiceItem[]
  /** 依据上下文的主动追问（杠杆3） */
  suggestions?: string[]
  /** 消费统计等可视化图表 [{chart_id,title,url}] */
  charts?: any[]
  /** 回答耗时/缓存命中（杠杆4） */
  elapsedSec?: number
  cached?: boolean
  /** 该条回答是否已收藏（操作条「收藏」按钮状态） */
  favorited?: boolean
  /** 该条回答对应的收藏记录 id（取消收藏 / 与收藏列表对齐时使用） */
  favoriteId?: number
  mapUrl?: string
  mapImage?: string
  extraInfo?: Record<string, any> | null
  isError?: boolean
  /** 用户提问（AI 消息记录所属问题，收藏/转发时带上） */
  question?: string
  /** 图片提问时用户上传的那张图（本地临时路径；收藏时会上传到服务器长期保存） */
  sourceImage?: string
}

// ============================================================
// 对话区（React.memo 隔离）：只有 messages/锚点变化才重渲染。
// 弹窗开关、工具栏、输入框等任意 setState 不再触碰 scroll-view，
// 避免小程序基础库重新应用 scroll-into-view 定位导致聊天位置跳动
// ============================================================
interface ChatMessagesProps {
  messages: Message[]
  scrollAnchor: string
  /** 当前登录用户（头像与「我的」页同源同步） */
  user: MpUser | null
  onCardPaid: (card: CardInfo) => void
  onShowMap: () => void
  onNavigate: () => void
  onService: (svc: ServiceItem) => void
  onSuggest: (question: string) => void
  onSpeak: (text: string) => void
  /** AI 回答右下角操作条：复制 / 收藏 / 转发 / 删除 */
  onCopy: (msg: Message) => void
  onFavorite: (msg: Message) => void
  onForward: (msg: Message) => void
  onDelete: (msg: Message) => void
  resolveMapUrl: (url: string) => string
}

const ChatMessages = memo(function ChatMessages({
  messages, scrollAnchor, user, onCardPaid, onShowMap, onNavigate, onService,
  onSuggest, onSpeak, onCopy, onFavorite, onForward, onDelete, resolveMapUrl,
}: ChatMessagesProps) {
  const copyRef = (name: string) => {
    Taro.setClipboardData({ data: name }).then(() => Taro.showToast({ title: '来源已复制', icon: 'none' }))
  }
  return (
    <ScrollView
      scrollY
      className='chat-box'
      scrollIntoView={scrollAnchor || undefined}
      scrollWithAnimation
      enhanced
      showScrollbar={false}
    >
      {messages.map((msg) => (
        <View key={msg.id} id={`msg-anchor-${msg.id}`}>
          {msg.type === 'user' ? (
            <View className='row-user msg-enter'>
              <View className='bubble-user'>
                {!!msg.image && (
                  <Image
                    className='bubble-user-img'
                    src={msg.image}
                    mode='widthFix'
                    onClick={() => Taro.previewImage({ urls: [msg.image!], current: msg.image! })}
                  />
                )}
                {!!msg.content && <Text className='bubble-user-text'>{msg.content}</Text>}
              </View>
              <UserAvatar
                user={user}
                className='avatar-user'
                imgClassName='avatar-user-img'
                textClassName='avatar-user-emoji'
              />
            </View>
          ) : (
            <View className='row-ai msg-enter'>
              <View className='avatar-ai'>
                <Text>🦆</Text>
              </View>
              <View className='bubble-ai'>
                {msg.thinking && msg.thinking.length > 0 && (
                  <ThinkingBox steps={msg.thinking} done={!!msg.thinkingDone} />
                )}

                {!!msg.content && (
                  msg.isError ? (
                    <Text className='error-text'>{msg.content}</Text>
                  ) : (
                    <MarkdownView content={msg.content} />
                  )
                )}

                {/* 办卡推荐卡片 */}
                {msg.cards?.map((c) => (
                  <CardAction key={c.card_id} card={c} onSuccess={onCardPaid} />
                ))}

                {/* 可执行服务按钮（业务 Agent：去办卡 / 失物认领 / 报失登记） */}
                {!!msg.services?.length && (
                  <View className='service-row'>
                    {msg.services.map((s) => (
                      <View
                        key={s.id}
                        className='service-chip'
                        hoverClass='service-chip-hover'
                        onClick={() => onService(s)}
                      >
                        <Text className='service-chip-text'>{s.title}</Text>
                      </View>
                    ))}
                  </View>
                )}
                {/* 可点击来源锚点（点击复制文档名） */}
                {!!msg.references?.length && (
                  <View className='references'>
                    <Text className='references-text'>📄 来源：</Text>
                    {msg.references.map((r, i) => (
                      <Text
                        key={`${r}-${i}`}
                        className='reference-chip'
                        hoverClass='reference-chip-hover'
                        onClick={() => copyRef(r)}
                      >
                        {r}
                      </Text>
                    ))}
                  </View>
                )}

                {/* 可视化图表（消费统计工具生成，点击看大图） */}
                {!!msg.charts?.length && (
                  <View className='charts-row'>
                    {msg.charts.map((ch) => {
                      const full = ch.url && (ch.url.startsWith('http') ? ch.url : API_BASE + ch.url)
                      return (
                        <View key={ch.chart_id || ch.url} className='chart-card'>
                          <Text className='chart-title'>{ch.title || '图表'}</Text>
                          {full ? (
                            <Image
                              className='chart-img'
                              src={full}
                              mode='widthFix'
                              onClick={() =>
                                Taro.previewImage({ urls: [full], current: full })
                              }
                            />
                          ) : null}
                        </View>
                      )
                    })}
                  </View>
                )}

                {/* 依据上下文的主动追问（杠杆3） */}
                {!!msg.suggestions?.length && (
                  <View className='suggest-row'>
                    <Text className='suggest-label'>💡 可以接着问：</Text>
                    {msg.suggestions.map((q, i) => (
                      <View
                        key={i}
                        className='suggest-chip'
                        hoverClass='suggest-chip-hover'
                        onClick={() => onSuggest(q)}
                      >
                        <Text className='suggest-chip-text'>{q}</Text>
                      </View>
                    ))}
                  </View>
                )}

                {/* 回答耗时 / 缓存命中（杠杆4） */}
                {(msg.cached || msg.elapsedSec != null) && (
                  <View className='answer-meta'>
                    {!!msg.cached && <Text className='answer-meta-tag'>⚡ 缓存秒回</Text>}
                    {msg.elapsedSec != null && <Text className='answer-meta-tag'>⏱ {msg.elapsedSec}s</Text>}
                  </View>
                )}

                {/* 回答操作条：复制 / 收藏 / 转发 / 删除（生成完成后出现） */}
                {!!msg.content && !msg.isError && msg.thinkingDone !== false && (
                  <View className='msg-actions'>
                    <View
                      className='msg-action'
                      hoverClass='msg-action-hover'
                      hoverStayTime={60}
                      onClick={() => onCopy(msg)}
                    >
                      <Text className='msg-action-icon'>📋</Text>
                      <Text className='msg-action-text'>复制</Text>
                    </View>
                    <View
                      className={`msg-action ${msg.favorited ? 'msg-action-done' : ''}`}
                      hoverClass='msg-action-hover'
                      hoverStayTime={60}
                      onClick={() => onFavorite(msg)}
                    >
                      <Text className='msg-action-icon'>{msg.favorited ? '⭐' : '☆'}</Text>
                      <Text className='msg-action-text'>{msg.favorited ? '已收藏' : '收藏'}</Text>
                    </View>
                    <View
                      className='msg-action'
                      hoverClass='msg-action-hover'
                      hoverStayTime={60}
                      onClick={() => onForward(msg)}
                    >
                      <Text className='msg-action-icon'>↗️</Text>
                      <Text className='msg-action-text'>转发</Text>
                    </View>
                    <View
                      className='msg-action msg-action-del'
                      hoverClass='msg-action-hover'
                      hoverStayTime={60}
                      onClick={() => onDelete(msg)}
                    >
                      <Text className='msg-action-icon'>🗑️</Text>
                      <Text className='msg-action-text'>删除</Text>
                    </View>
                  </View>
                )}

                {/* 语音朗读按钮暂时隐藏（依赖微信「同声传译」插件，插件开通后取消注释恢复）
                {!!msg.content && !msg.isError && (
                  <View className='tts-btn' onClick={() => onSpeak(msg.content)}>
                    <Text className='tts-btn-icon'>🔊</Text>
                  </View>
                )} */}

                {/* 地图静态图（点击放大查看，长按/下方按钮进地图弹窗） */}
                {!!msg.mapImage && (
                  <Image
                    className='map-image'
                    src={resolveMapUrl(msg.mapImage!)}
                    mode='aspectFill'
                    onClick={() => Taro.previewImage({
                      urls: [resolveMapUrl(msg.mapImage!)],
                      current: resolveMapUrl(msg.mapImage!),
                    })}
                  />
                )}

                {/* 地图跳转（后端 Agent 生成的链接） */}
                {!!msg.mapUrl && (
                  <View className='map-link-btn' onClick={onNavigate}>
                    <Text className='map-link-text'>📍 打开地图 / 导航</Text>
                  </View>
                )}

                {/* POI / 路线 / 天气信息卡（后端高德 Agent 返回） */}
                {!!msg.extraInfo?.pois?.length && (
                  <View className='extra-box'>
                    <Text className='extra-title'>🏷️ 地点信息：</Text>
                    {msg.extraInfo.pois.slice(0, 4).map((p: any, i: number) => (
                      <Text key={i} className='extra-line'>
                        • {p.name}{p.address ? `（${p.address}）` : ''}
                      </Text>
                    ))}
                  </View>
                )}
                {!!msg.extraInfo?.route && (
                  <View className='extra-box'>
                    <Text className='extra-line'>
                      🗺️ {msg.extraInfo.route.mode}路线：约 {msg.extraInfo.route.distance_km} 公里，预计 {msg.extraInfo.route.duration_min} 分钟
                    </Text>
                  </View>
                )}
                {!!msg.extraInfo?.weather && (
                  <View className='extra-box'>
                    <Text className='extra-line'>
                      🌤️ {msg.extraInfo.weather.city}：{msg.extraInfo.weather.weather}，{msg.extraInfo.weather.temperature}，{msg.extraInfo.weather.wind}
                    </Text>
                  </View>
                )}
              </View>
            </View>
          )}
        </View>
      ))}
      <View className='chat-bottom-pad' />
    </ScrollView>
  )
})

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 1,
      type: 'ai',
      content: '嗨～欢迎来到大黄鸭游乐园！🎉\n我是你的专属小助手 鸭鸭，关于票价、设施、游玩攻略都可以问我哦～',
    },
  ])
  const [inputValue, setInputValue] = useState('')
  // 待发送图片（拍照/相册选择后，可再输入文字一起发送；纯图片也可提问）
  const [attachImg, setAttachImg] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [showProjectList, setShowProjectList] = useState(false)
  // 项目游玩面板：读后端价目表 + 就地勾选下单（一单一券包，下单后出 N 张游园券）
  const [projects, setProjects] = useState<ParkProject[]>([])
  const [projLoading, setProjLoading] = useState(false)
  const [projSel, setProjSel] = useState<Record<number, number>>({})
  const [projAvail, setProjAvail] = useState<number | null>(null)
  const [projConsuming, setProjConsuming] = useState(false)
  const [projResult, setProjResult] = useState<ConsumeResult | null>(null)
  const [showMap, setShowMap] = useState(false)
  const [showRating, setShowRating] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const [selectedStars, setSelectedStars] = useState(0)
  const [ratingSubmitting, setRatingSubmitting] = useState(false)
  const [lastMapInfo, setLastMapInfo] = useState<{ mapUrl: string; mapImage: string; extraInfo: any } | null>(null)
  // 失物招领：下拉菜单 / 报失表单 / 认领列表
  const [showLostMenu, setShowLostMenu] = useState(false)
  const [showLostReport, setShowLostReport] = useState(false)
  const [showLostClaim, setShowLostClaim] = useState(false)
  const [lostName, setLostName] = useState('')
  const [lostReporter, setLostReporter] = useState('')
  const [lostPhotoPath, setLostPhotoPath] = useState('')
  // 照片预识别：选完图立刻让多模态模型看图，自动把「失物名称」填好
  const [lostAnalyzing, setLostAnalyzing] = useState(false)
  const [lostAi, setLostAi] = useState<LostPhotoInsight | null>(null)
  const [lostItems, setLostItems] = useState<LostItem[]>([])
  const [lostLoading, setLostLoading] = useState(false)
  const [lostSubmitting, setLostSubmitting] = useState(false)

  // ---------- AI 画像（工具条「画像」小图标 → 底部抽屉填写） ----------
  // 数据源只有 /profile/me 一处；选项词表由 /profile/options 下发（项目名不在前端硬编码）。
  // 只在打开抽屉时请求，问答链路一行都不碰 —— 模型响应耗时不受影响。
  const [showProfile, setShowProfile] = useState(false)
  const [profileData, setProfileData] = useState<MyProfile | null>(null)
  const [profileOpts, setProfileOpts] = useState<ProfileOptions | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileSaving, setProfileSaving] = useState(false)
  const [draftKids, setDraftKids] = useState<ProfileKid[]>([])
  const [draftProjects, setDraftProjects] = useState<string[]>([])
  const [draftTopics, setDraftTopics] = useState<string[]>([])
  // 打开时的原值快照：保存时只提交**真正改过**的字段，
  // 避免「点一下保存就把 AI 自动识别的东西全冻成手改值」。
  const profileOrigRef = useRef('')

  const ageRange = useMemo(() => {
    const a = profileOpts?.age || { min: 1, max: 18, step: 1 }
    const out = ['未填']
    for (let v = a.min; v <= a.max + 1e-9; v += a.step) out.push(String(v))
    return out
  }, [profileOpts])

  const heightRange = useMemo(() => {
    const h = profileOpts?.height || { min: 0.9, max: 1.6, step: 0.05 }
    const out = ['未填']
    for (let v = h.min; v <= h.max + 1e-9; v += h.step) out.push(v.toFixed(2))
    return out
  }, [profileOpts])

  const rangeIndex = (range: string[], v: number | null | undefined, fixed = false) => {
    if (v === null || v === undefined) return 0
    const i = range.indexOf(fixed ? Number(v).toFixed(2) : String(v))
    return i < 0 ? 0 : i
  }

  const applyProfile = useCallback((p: MyProfile, o?: ProfileOptions | null) => {
    setProfileData(p)
    if (o) setProfileOpts(o)
    const view = p.view || {}
    const kids: ProfileKid[] = (view.family?.kids || []).map((k) => ({ ...k }))
    // 只知道人数、没有明细时按人数补空行，让「几个孩子」与下面的卡片对得上
    const want = kids.length ? kids.length : Math.min(view.family?.kids_count || 0, 6)
    while (kids.length < want) kids.push({ age: null, height: null, gender: null })
    const projects = [...(view.interest?.projects || [])]
    const topics = [...(view.interest?.topics || [])]
    setDraftKids(kids)
    setDraftProjects(projects)
    setDraftTopics(topics)
    profileOrigRef.current = JSON.stringify({ kids, projects, topics })
  }, [])

  const openProfile = async () => {
    if (!requireLogin('查看 AI 画像')) return
    if (showProfile) return
    setShowProfile(true)
    setProfileLoading(true)
    try {
      const [p, o] = await Promise.all([getMyProfile(), getProfileOptions()])
      applyProfile(p.data, o.data)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '加载失败', icon: 'none' })
    } finally {
      setProfileLoading(false)
    }
  }

  const setKidField = (i: number, field: 'age' | 'height' | 'gender', v: number | string | null) =>
    setDraftKids((prev) => prev.map((k, idx) => (idx === i ? { ...k, [field]: v } : k)))

  /** v5：勾选某个孩子身上的一个标签（喜欢 / 不喜欢 / 症状） */
  const toggleKidChip = (i: number, field: 'likes' | 'dislikes' | 'symptoms', v: string) =>
    setDraftKids((prev) => prev.map((k, idx) => {
      if (idx !== i) return k
      const cur = (k[field] || []) as string[]
      const next = cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]
      return { ...k, [field]: next }
    }))

  const addKid = () => {
    const max = profileOpts?.kids_max || 6
    setDraftKids((prev) => (prev.length >= max ? prev : [...prev, { age: null, height: null, gender: null }]))
  }

  const removeKid = () => setDraftKids((prev) => prev.slice(0, Math.max(0, prev.length - 1)))

  const toggleChip = (which: 'projects' | 'topics', v: string) => {
    const setter = which === 'projects' ? setDraftProjects : setDraftTopics
    setter((prev) => (prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v]))
  }

  const saveProfile = async () => {
    if (profileSaving) return
    if (JSON.stringify({ kids: draftKids, projects: draftProjects, topics: draftTopics })
        === profileOrigRef.current) {
      Taro.showToast({ title: '没有改动', icon: 'none' })
      return
    }
    const orig = JSON.parse(profileOrigRef.current || '{}') as Record<string, unknown>
    const patch: {
      kids?: ProfileKid[] | null
      kids_count?: number | null
      projects?: string[] | null
      topics?: string[] | null
    } = {}
    if (JSON.stringify(draftKids) !== JSON.stringify(orig.kids)) {
      // v5：只填了「喜欢/不喜欢/症状」、没填年龄身高的孩子也要保留 ——
      //     否则用户给孩子勾了项目标签，一保存就整条被过滤掉（信息静默丢失）
      const clean = draftKids
        .map((k) => {
          const out: ProfileKid = { age: k.age ?? null, height: k.height ?? null, gender: k.gender ?? null }
          const likes = (k.likes || []).filter(Boolean)
          const dislikes = (k.dislikes || []).filter(Boolean)
          const symptoms = (k.symptoms || []).filter(Boolean)
          if (likes.length) out.likes = likes
          if (dislikes.length) out.dislikes = dislikes
          if (symptoms.length) out.symptoms = symptoms
          return out
        })
        .filter((k) => k.age != null || k.height != null || k.gender
          || (k.likes && k.likes.length) || (k.dislikes && k.dislikes.length)
          || (k.symptoms && k.symptoms.length))
      patch.kids = clean.length ? clean : null
      // 有「只报了人数、没填明细」的空行时，把人数据另存下来（不然人数会丢）
      patch.kids_count = clean.length < draftKids.length ? (draftKids.length || null) : null
    }
    if (JSON.stringify(draftProjects) !== JSON.stringify(orig.projects)) patch.projects = draftProjects
    if (JSON.stringify(draftTopics) !== JSON.stringify(orig.topics)) patch.topics = draftTopics

    setProfileSaving(true)
    try {
      const r = await updateMyProfile(patch)
      applyProfile(r.data)
      Taro.showToast({ title: '已保存', icon: 'success' })
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '保存失败', icon: 'none' })
    } finally {
      setProfileSaving(false)
    }
  }

  const clearProfile = async () => {
    const res = await Taro.showModal({
      title: '清空 AI 记忆',
      content: '将删除 AI 记住的孩子年龄、身高、性别、关注点等信息。清除后需要重新对话才能再次积累。',
      confirmText: '确认清空',
      cancelText: '取消',
    })
    if (!res.confirm) return
    try {
      await clearMyProfile(false)
      applyProfile((await getMyProfile()).data)
      Taro.showToast({ title: '已清空', icon: 'none' })
    } catch {
      Taro.showToast({ title: '操作失败', icon: 'none' })
    }
  }

  const togglePersonalization = async () => {
    const next = !(profileData?.personalization_on ?? true)
    try {
      const r = await setPersonalization(next)
      setProfileData((d) => (d ? { ...d, personalization_on: r.data.personalization_on } : d))
      Taro.showToast({ title: next ? '已开启' : '已关闭', icon: 'none' })
    } catch {
      Taro.showToast({ title: '操作失败', icon: 'none' })
    }
  }

  const ratingShownRef = useRef(false)
  const askCountRef = useRef(0)
  const chatHistoryRef = useRef('')
  const sessionIdRef = useRef('')
  // 当前登录用户（头像与「我的」页同源：本页显示头像 / 提问者头像都用它）
  const [user, setUser] = useState<MpUser | null>(getUser())
  // 快捷提问 6 按钮：后端近 7 天热点问题（失败回退默认 6 条）
  const [quickQuestions, setQuickQuestions] = useState<HotQuestion[]>(DEFAULT_QUICK_QUESTIONS)
  // 服务端会话记忆（杠杆3）：同一会话持续累积结构化多轮消息与用户画像
  const getSessionId = () => {
    if (!sessionIdRef.current) {
      try { sessionIdRef.current = (Taro.getStorageSync('chat_session_id') as string) || '' } catch { /* ignore */ }
      if (!sessionIdRef.current) {
        sessionIdRef.current = `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
        try { Taro.setStorageSync('chat_session_id', sessionIdRef.current) } catch { /* ignore */ }
      }
    }
    return sessionIdRef.current
  }
  const messageIdRef = useRef(2)
  const sseTaskRef = useRef<{ abort: () => void } | null>(null)
  const voiceManagerRef = useRef<any>(null)
  // 滚动锚点用 state（非 ref）：仅在收到新消息时更新，其余 setState 不影响 scroll-view 定位
  const [scrollAnchor, setScrollAnchor] = useState('')

  useDidShow(() => {
    // 首次进入确保登录态（App onLaunch 已静默登录，此处兜底）
    ensureLogin().catch(() => {})
    // 头像/昵称跨页同步：拉取服务器最新资料（改了头像后本页立即跟着变）
    refreshUser().then((u) => setUser(u)).catch(() => {})
    loadHotQuestions()
    // 收藏状态与服务器对齐（在「我的-我的收藏」删掉后返回本页，标记能及时复位，允许重新收藏）
    syncFavoriteFlags()
    // 跨页自动提问：其他页面（我的-快捷提问）写入待发问题，此处读取后自动发送
    try {
      const pending = Taro.getStorageSync(PENDING_QUESTION_KEY) as string
      if (pending) {
        Taro.removeStorageSync(PENDING_QUESTION_KEY)
        // 延迟到页面渲染完成后再发送，避免首帧状态未就绪
        setTimeout(() => sendQuestion(pending), 300)
      }
    } catch { /* storage 异常忽略 */ }
  })

  // 我的页改头像后，本页订阅通知立即刷新（无需重新进入页面）
  useEffect(() => {
    const off = onUserChanged((u) => setUser(u))
    return () => { off() }
  }, [])

  // 收藏变更即时同步：在「我的-我的收藏」取消收藏后，本页该条回答立刻回到「☆ 收藏」，
  // 不必等切 Tab 回读（回读仍在 useDidShow 里兜底，覆盖跨设备等场景）。
  useEffect(() => {
    const off = onFavoritesChanged((change) => {
      const key = favKey(change.question, change.answer)
      setMessages((prev) => {
        let changed = false
        const next = prev.map((m) => {
          if (m.type !== 'ai') return m
          // 优先按收藏 id 精确匹配；无 id 时退回「问+答」内容匹配
          const hit = change.favoriteId
            ? m.favoriteId === change.favoriteId
            : favKey(m.question, m.content) === key
          if (!hit) return m
          const favorited = change.action === 'add'
          if (!!m.favorited === favorited) return m
          changed = true
          return { ...m, favorited, favoriteId: favorited ? change.favoriteId : undefined }
        })
        return changed ? next : prev
      })
    })
    return () => { off() }
  }, [])

  /**
   * 用服务器收藏列表「双向对齐」每条 AI 消息的 favorited 标记。
   * 必要性：用户可能在「我的-我的收藏」页删掉某条收藏，聊天页 state 收不到通知，
   * 若不清标记，再点「收藏」会被 msg.favorited 短路，导致"删掉后无法再次收藏"。
   * 仅当标记真的变化时才改动该条消息（保持其余项引用不变，避免整列表重渲染动到滚动）。
   */
  const syncFavoriteFlags = async () => {
    if (!isLoggedIn()) return
    try {
      const r = await getFavorites(100)
      const rows = r?.data || []
      const byKey = new Map<string, number>()
      rows.forEach((f) => byKey.set(favKey(f.question, f.answer), Number(f.id)))
      setMessages((prev) => {
        let changed = false
        const next = prev.map((m) => {
          if (m.type !== 'ai') return m
          const id = byKey.get(favKey(m.question, m.content))
          const shouldFav = id !== undefined
          if (shouldFav === !!m.favorited && m.favoriteId === id) return m
          changed = true
          return { ...m, favorited: shouldFav, favoriteId: shouldFav ? id : undefined }
        })
        return changed ? next : prev
      })
    } catch {
      /* 收藏列表拉取失败：保持本地标记，不影响正常使用 */
    }
  }

  // ---------- 快捷提问：拉取后端"近 7 天热点问题 Top6"（每 7 天自动刷新） ----------
  const loadHotQuestions = async () => {
    try {
      const res = await getHotQuestions(6)
      const list = (res.data || []).filter((q) => q?.question)
      if (list.length) {
        setQuickQuestions(list.map((q) => ({
          question: q.question,
          label: (q.label || q.question).slice(0, 4),
          icon: q.icon || '💬',
          hit_count: q.hit_count || 0,
        })))
      }
    } catch {
      /* 后端不可用时保留默认 6 条静态快捷问题 */
    }
  }

  // 滚动锚点：受控的 scrollIntoView。只允许"发新消息 / 流式回答跟随"改写它，
  // 且滚动到位后立刻复位为空，避免旧锚点被后续重渲染反复重放（表现为"点按钮跳位置"）。
  const scrollAnchorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scrollToBottom = useCallback((id: number) => {
    setScrollAnchor(`msg-anchor-${id}`)
    if (scrollAnchorTimerRef.current) clearTimeout(scrollAnchorTimerRef.current)
    // 350ms 覆盖 scrollWithAnimation 的滚动时长；复位为空 -> 后续 setState 不再触发定位
    scrollAnchorTimerRef.current = setTimeout(() => {
      scrollAnchorTimerRef.current = null
      setScrollAnchor('')
    }, 350)
  }, [])

  const addMessage = useCallback((msg: Omit<Message, 'id'>): number => {
    const id = messageIdRef.current++
    setMessages((prev) => [...prev, { ...msg, id }])
    scrollToBottom(id)
    return id
  }, [scrollToBottom])

  /**
   * 更新消息。默认**不滚动**（收藏/删除/状态变更不应移动用户视口）；
   * 仅流式回答跟随需要时显式传 { scroll: true }。
   */
  const updateMessage = useCallback((
    id: number,
    updates: Partial<Message>,
    opts?: { scroll?: boolean },
  ) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...updates } : m)))
    if (opts?.scroll) scrollToBottom(id)
  }, [scrollToBottom])

  const appendThinking = useCallback((id: number, step: ThinkingStep) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, thinking: [...(m.thinking || []), step] } : m)),
    )
  }, [])

  const resolveMapUrl = useCallback((url: string) =>
    url && url.startsWith('http') ? url : API_BASE + url, [])

  // ---------- 办卡成功：模型回复办理成功 → 2 秒后跳转充值页 ----------
  const handleCardPaid = useCallback((card: CardInfo) => {
    const coinPart = card.coins ? `，${card.coins} 币已到账` : ''
    addMessage({
      type: 'ai',
      content: `🎉 办理成功！已为您开通「${card.name}」${coinPart}。\n正在带您前往办卡充值页查看～`,
      thinkingDone: true,
    })
    setTimeout(() => {
      Taro.navigateTo({ url: '/pages/recharge/index' })
    }, 2000)
  }, [addMessage])

  // ---------- 发送（网络预检 → SSE 流式 → 降级；带图走视觉模型同步问答） ----------
  const sendQuestion = async (question?: string) => {
    if (!requireLogin('与鸭鸭 AI 对话')) return
    const q = (question || inputValue).trim()
    const img = attachImg || ''
    if ((!q && !img) || isSending) return

    // 网络预检：断网直接提示，不再白等请求超时
    try {
      const net = await Taro.getNetworkType()
      if (net.networkType === 'none') {
        Taro.showToast({ title: '当前无网络连接，请检查网络后重试', icon: 'none', duration: 2500 })
        addMessage({
          type: 'ai',
          content: '📡 当前无网络连接，请检查 Wi-Fi 或移动数据后重试～',
          isError: true,
          thinkingDone: true,
        })
        return
      }
    } catch { /* 检查失败不拦截，交给请求超时兑底 */ }

    askCountRef.current += 1  // 累计提问计数（满意度调查触发条件：≥3 问）
    setIsSending(true)
    setInputValue('')
    if (img) setAttachImg('')  // 已取用后清空待发图片
    addMessage({ type: 'user', content: q, image: img || undefined })

    const aiMsgId = addMessage({
      type: 'ai',
      content: '',
      question: q,
      sourceImage: img || undefined,
      thinking: [{ icon: '🚀', text: '已收到问题，开始思考...' }],
      thinkingDone: false,
    })

    let fullAnswer = ''
    let chartsAcc: any[] = []
    let gotAnyChunk = false

    // —— 图片（可含文字）提问：上传图片 → 后端走视觉模型（deepseek-v4-flash-vision-exp），
    //    并结合园区知识库与模型推理；图片问答为同步返回，不走 SSE ——
    if (img) {
      updateMessage(aiMsgId, {
        thinking: [{ icon: '📷', text: q ? '正在看图片并理解你的问题...' : '正在识别图片内容...' }],
      }, { scroll: true })
      try {
        const res = await askVision(img, q, getSessionId())
        fullAnswer = res?.data?.answer || ''
        updateMessage(aiMsgId, {
          content: fullAnswer,
          references: res.data?.references || [],
          thinkingDone: true,
        }, { scroll: true })
        chatHistoryRef.current += `用户：${q || '[图片]'}\nAI：${fullAnswer}\n`
      } catch (e: any) {
        updateMessage(aiMsgId, {
          content: e?.message || '图片提问失败，请稍后重试',
          isError: true,
          thinkingDone: true,
        }, { scroll: true })
      } finally {
        setIsSending(false)
        sseTaskRef.current = null
      }
      return
    }

    // 降级：流式失败 → 一次性接口（降级前再查一次网络，断网不再白等第二次超时）
    const fallbackToSync = async () => {
      try {
        const net = await Taro.getNetworkType()
        if (net.networkType === 'none') {
          throw new Error('网络连接已断开，请检查网络后重试')
        }
        const res = await request<{
          data: { answer: string; references: string[]; cards?: CardItem[] }
        }>('/chat', {
          method: 'POST',
          data: { question: q, history: '', session_id: getSessionId(), source: 'miniprogram' },
          // 非流式回答可能需等待检索+重排+LLM（数十秒），放宽超时避免误报“无法连接后端服务”
          timeout: 120000,
        })
        fullAnswer = res.data.answer || ''
        updateMessage(aiMsgId, {
          content: fullAnswer,
          references: res.data.references,
          cards: res.data.cards || [],
          thinkingDone: true,
        }, { scroll: true })
        chatHistoryRef.current += `用户：${q}\nAI：${fullAnswer}\n`
      } catch (e: any) {
        updateMessage(aiMsgId, {
          content: e?.message || '网络请求失败，请稍后重试！',
          isError: true,
          thinkingDone: true,
        }, { scroll: true })
      }
    }

    const doFallback = () => {
      if (gotAnyChunk) {
        // 已有部分内容：保持已收到的文本，仅结束
        updateMessage(aiMsgId, { thinkingDone: true })
        chatHistoryRef.current += `用户：${q}\nAI：${fullAnswer}\n`
      } else {
        fallbackToSync()
      }
    }

    sseTaskRef.current = sseRequest({
      url: '/chat/stream',
      data: { question: q, history: '', session_id: getSessionId(), source: 'miniprogram' },
      onEvent: (ev) => {
        switch (ev.type) {
          case 'chunk':
            gotAnyChunk = true
            fullAnswer += ev.content || ''
            updateMessage(aiMsgId, { content: fullAnswer, thinkingDone: true }, { scroll: true })
            break
          case 'status':
          case 'debug':
            // 兼容旧事件：纯文本思考提示
            appendThinking(aiMsgId, { icon: '💭', text: ev.content || '' })
            break
          case 'step': {
            // 真实思考步骤：改写结果 / 各路召回数 / 精排保留数 / 校验通过数 / 耗时
            const st = ev.step || {}
            appendThinking(aiMsgId, {
              key: st.id,
              phase: st.phase,
              title: st.title,
              text: st.detail || st.title || '',
              elapsed: st.elapsed,
              meta: st.meta,
            })
            break
          }
          case 'meta': {
            const mapInfo = {
              mapUrl: ev.map_url || '',
              mapImage: ev.map_image || '',
              extraInfo: ev.extra_info || null,
            }
            updateMessage(aiMsgId, { references: ev.reference_doc, cached: !!ev.cached, ...mapInfo })
            if (mapInfo.mapUrl || mapInfo.mapImage) setLastMapInfo(mapInfo)
            break
          }
          case 'suggestions':
            // 依据上下文的主动追问（杠杆3）
            if (ev.items?.length) updateMessage(aiMsgId, { suggestions: ev.items }, { scroll: true })
            break
          case 'charts':
            // 消费统计可视化图表（杠杆：图表工具调用）
            if (ev.charts?.length) {
              chartsAcc = chartsAcc.concat(ev.charts)
              updateMessage(aiMsgId, { charts: [...chartsAcc] }, { scroll: true })
            }
            break
          case 'action': {
            // 办卡推荐卡片 / 可执行服务按钮（回答完整后推送）
            const upd: Partial<Message> = {}
            if (ev.cards?.length) upd.cards = ev.cards
            if (ev.services?.length) upd.services = ev.services as ServiceItem[]
            if (Object.keys(upd).length) updateMessage(aiMsgId, upd, { scroll: true })
            break
          }
          case 'error':
            updateMessage(aiMsgId, { content: ev.content, isError: true, thinkingDone: true }, { scroll: true })
            break
          case 'done': {
            chatHistoryRef.current += `用户：${q}\nAI：${fullAnswer}\n`
            // 回答耗时展示（杠杆4）
            if (typeof ev.elapsed === 'number') {
              updateMessage(aiMsgId, { elapsedSec: Number(ev.elapsed.toFixed(1)) })
            }
            // 收尾事件已到：立即解除"发送中"，用户可继续提问
            // （追问建议/日志落库在 done 之后推送，不再让用户干等）
            setIsSending(false)
            // 满意度调查：累计提问 ≥ 3 问后，首次回答完成时弹出（单次）
            if (askCountRef.current >= 3 && !ratingShownRef.current) {
              ratingShownRef.current = true
              setSelectedStars(0)
              setTimeout(() => setShowRating(true), 1200)
            }
            break
          }
        }
      },
      onError: () => doFallback(),
      onComplete: () => {
        updateMessage(aiMsgId, { thinkingDone: true }, { scroll: true })
        setIsSending(false)
        sseTaskRef.current = null
      },
    })
  }

  const quickAsk = (q: string) => {
    setShowProjectList(false)
    setTimeout(() => sendQuestion(q), 100)
  }

  const handleSend = () => sendQuestion()

  // ---------- 项目游玩面板：勾选 → 数量 → 合计 → 确认扣费 ----------
  // 与首页「游乐消费」是同一套交互、同一个 /pay/consume，
  // 扣费核心只有 services/purchase.py 一份，这里不另写扣费逻辑。
  const totalProjCoins = Object.entries(projSel).reduce((sum, [pid, qty]) => {
    const p = projects.find((x) => x.id === Number(pid))
    return sum + (p ? p.price_coins * qty : 0)
  }, 0)

  const openProjectPanel = async () => {
    if (showProjectList) {
      setShowProjectList(false)
      return
    }
    setShowProjectList(true)
    setProjResult(null)
    if (projects.length) return
    setProjLoading(true)
    try {
      const [pr, bal] = await Promise.all([
        getProjects(),
        isLoggedIn() ? getMyBalance().catch(() => null) : Promise.resolve(null),
      ])
      setProjects(pr.data || [])
      setProjAvail(bal ? bal.data.coins : null)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '价目表加载失败', icon: 'none' })
    } finally {
      setProjLoading(false)
    }
  }

  const toggleProj = (id: number) => {
    setProjSel((prev) => {
      const next = { ...prev }
      if (next[id]) delete next[id]
      else next[id] = 1
      return next
    })
  }

  const changeProjQty = (id: number, delta: number) => {
    setProjSel((prev) => {
      const q = (prev[id] || 0) + delta
      if (q < 1) return prev
      return { ...prev, [id]: Math.min(q, 99) }
    })
  }

  const confirmProjConsume = async () => {
    if (projConsuming) return
    if (!isLoggedIn()) {
      requireLogin('购买游园券')
      return
    }
    const items = Object.entries(projSel).map(([pid, qty]) => ({ project_id: Number(pid), qty }))
    if (!items.length) {
      Taro.showToast({ title: '请先勾选要玩的项目', icon: 'none' })
      return
    }
    if (projAvail !== null && totalProjCoins > projAvail) {
      Taro.showToast({ title: `余币不足，还差 ${totalProjCoins - projAvail} 币`, icon: 'none' })
      return
    }
    setProjConsuming(true)
    try {
      const res = await consumeItems(items)
      setProjResult(res.data)
      setProjAvail(res.data.remaining_coins)
      setProjSel({})
      Taro.showToast({ title: '扣费成功', icon: 'success' })
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '扣费失败，请稍后重试', icon: 'none', duration: 3000 })
    } finally {
      setProjConsuming(false)
    }
  }

  const goMyPasses = () => {
    setShowProjectList(false)
    Taro.navigateTo({ url: '/pages/passes/index' })
  }

  // ---------- 待发图片：拍照 / 相册 选择与移除（输入栏左侧 + 按钮） ----------
  const chooseAttachImg = () => {
    if (isSending) return
    Taro.showActionSheet({
      itemList: ['📷 拍照', '🖼 从相册选择'],
      success: (res: any) => {
        const sourceType = res.tapIndex === 0 ? ['camera'] : ['album']
        Taro.chooseImage({
          count: 1,
          sizeType: ['compressed'],
          sourceType: sourceType as any,
          success: (r: any) => {
            const path = r.tempFilePaths && r.tempFilePaths[0]
            if (path) {
              setAttachImg(path)
              Taro.showToast({ title: '图片已选，可输入问题一起发送', icon: 'none' })
            }
          },
        })
      },
    })
  }

  const removeAttachImg = () => setAttachImg('')

  // ---------- 停止生成（杠杆4）：中断当前 SSE 流 ----------
  const stopGenerating = () => {
    try {
      sseTaskRef.current?.abort()
    } catch { /* ignore */ }
    setIsSending(false)
  }

  // ---------- 主动追问点击（杠杆3） ----------
  const handleSuggest = useCallback((q2: string) => {
    if (!isSending) sendQuestion(q2)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSending])

  // ---------- 语音朗读（杠杆4，TTS） ----------
  const handleSpeak = useCallback((text: string) => {
    speakText(text)
  }, [])

  // ---------- 图片提问（杠杆4）：选图 → 上传 /chat/vision → 展示回答 ----------
  const askWithImage = () => {
    if (isSending) return
    Taro.chooseImage({
      count: 1,
      sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
      success: async (res) => {
        const fp = res.tempFilePaths && res.tempFilePaths[0]
        if (!fp) return
        Taro.showLoading({ title: '识别图片中...' })
        try {
          const up = await Taro.uploadFile({
            url: `${API_BASE}/chat/vision`,
            filePath: fp,
            name: 'file',
            formData: { session_id: getSessionId(), source: 'miniprogram' },
            header: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
            timeout: 120000,
          })
          const json = JSON.parse(up.data || '{}')
          const data = json?.data || {}
          Taro.hideLoading()
          addMessage({ type: 'user', content: '📷 [发来一张图片提问]' })
          const aiId = addMessage({
            type: 'ai', content: '', thinkingDone: true,
            thinking: [{ icon: '🖼️', text: 'AI 正在直接看图并回答...' }],
          })
          const answer = data.answer || '抱歉，这张图我没看明白，请换一张清晰的图试试。'
          updateMessage(aiId, {
            content: answer,
            references: data.references || [],
            thinking: [{ icon: '🖼️', text: '已看图回答' }],
            thinkingDone: true,
          }, { scroll: true })
          chatHistoryRef.current += '用户：[图片提问]\nAI：' + answer + '\n'
        } catch (e: any) {
          Taro.hideLoading()
          Taro.showToast({ title: e?.message || '图片提问失败，请稍后重试', icon: 'none' })
        }
      },
    })
  }

  // ---------- 语音输入（微信同声传译插件，未添加插件时友好提示） ----------
  const startVoice = () => {
    if (isListening) {
      voiceManagerRef.current?.stop?.()
      setIsListening(false)
      return
    }
    try {
      if (!voiceManagerRef.current) {
        const plugin = (Taro as any).requirePlugin?.('WechatSI')
        if (!plugin) throw new Error('plugin unavailable')
        const manager = plugin.getRecordRecognitionManager()
        manager.onRecognize((res: any) => {
          if (res.result) setInputValue(res.result)
        })
        manager.onStop((res: any) => {
          setIsListening(false)
          const text = (res.result || '').trim()
          if (text) {
            setInputValue(text)
            setTimeout(() => sendQuestion(text), 300)
          }
        })
        manager.onError(() => {
          setIsListening(false)
          Taro.showToast({ title: '语音识别失败，请手动输入', icon: 'none' })
        })
        voiceManagerRef.current = manager
      }
      setIsListening(true)
      voiceManagerRef.current.start({ duration: 30000, lang: 'zh_CN' })
    } catch {
      setIsListening(false)
      Taro.showToast({
        title: '语音插件不可用：请在小程序后台添加"同声传译"插件',
        icon: 'none',
        duration: 3000,
      })
    }
  }

  // ---------- 地图导航 ----------
  const openNavigation = useCallback(() => {
    Taro.openLocation({
      latitude: PARK_LATITUDE,
      longitude: PARK_LONGITUDE,
      name: '大黄鸭室外游乐园',
      address: '北京市怀柔区万达广场2号门',
      scale: 16,
    })
  }, [])

  // 打开地图弹窗（稳定引用，供 memo 对话区使用）
  const openMapModal = useCallback(() => setShowMap(true), [])

  // ---------- 满意度评价：1-5 星可选，提交落库（POST /feedback） ----------
  const submitRating = async () => {
    if (!selectedStars || ratingSubmitting) return
    setRatingSubmitting(true)
    try {
      await request('/feedback', {
        method: 'POST',
        data: { rating: selectedStars, comment: '' },
      })
      Taro.showToast({ title: `感谢您的 ${selectedStars} 星评价！🎉`, icon: 'none', duration: 2000 })
      setShowRating(false)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '提交失败，请稍后重试', icon: 'none' })
    } finally {
      setRatingSubmitting(false)
    }
  }

  // ============================================================
  // 失物招领：下拉菜单（我要报失 / 失物认领）
  // 报失 -> POST /lost-found/items（名称+照片，时间后端自动生成，统一入库商家端）
  // 认领 -> GET /lost-found/items（展示全部失物的名称/时间/图片）
  // ============================================================
  // 照片/静态资源相对路径补全为完整地址（/static/xxx -> http://host:8000/static/xxx）
  const resolveImageUrl = (url: string) => (url && url.startsWith('http') ? url : API_BASE + url)

  // 切换下拉菜单时收起项目列表，避免两个面板叠加
  const toggleLostMenu = () => {
    setShowProjectList(false)
    setShowLostMenu((v) => !v)
  }

  // 打开报失弹窗（清空上次表单）
  const openLostReport = () => {
    if (!requireLogin('登记失物')) return
    setShowLostMenu(false)
    setLostName('')
    setLostReporter('')
    setLostPhotoPath('')
    setLostAi(null)
    setLostAnalyzing(false)
    setShowLostReport(true)
  }

  // 选完照片立刻交给多模态模型识别 → 自动填好「失物名称」（用户仍可改）
  const recognizeLostPhoto = async (path: string) => {
    setLostAnalyzing(true)
    try {
      const res = await analyzeLostPhoto(path, lostName.trim())
      const insight: LostPhotoInsight | undefined = res?.data
      if (insight?.ok) {
        setLostAi(insight)
        // 只在用户自己还没填名字时才自动补，绝不覆盖用户输入
        setLostName((prev) => prev.trim() || insight.suggested_name || '')
      } else {
        setLostAi(null)
      }
    } catch (e: any) {
      // 识别是增强项：失败就退回手动填写，不阻断报失
      setLostAi(null)
      console.log('[失物识别] 失败:', e?.message)
    } finally {
      setLostAnalyzing(false)
    }
  }

  // 选择照片（相册/拍照）
  const chooseLostPhoto = () => {
    Taro.chooseImage({
      count: 1,
      sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        const path = res.tempFilePaths && res.tempFilePaths[0]
        if (!path) return
        setLostPhotoPath(path)
        setLostAi(null)
        void recognizeLostPhoto(path)
      },
    })
  }

  // 提交报失（名称 + 照片 multipart 上传）
  const submitLostReport = async () => {
    if (!lostPhotoPath) {
      Taro.showToast({ title: '请上传失物照片', icon: 'none' })
      return
    }
    // 名称优先取用户填的；用户没填时用 AI 建议名（服务端还会再兜一次）
    const name = lostName.trim() || (lostAi?.ok ? lostAi.suggested_name.trim() : '')
    if (!name) {
      Taro.showToast({ title: '请填写失物名称（或换张更清晰的照片）', icon: 'none' })
      return
    }
    if (lostSubmitting) return
    setLostSubmitting(true)
    try {
      const res = await reportLostItem(name, lostPhotoPath, lostReporter.trim())
      Taro.showToast({ title: res.msg || '报失成功', icon: 'success' })
      setShowLostReport(false)
      setLostAi(null)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '报失失败，请稍后重试', icon: 'none' })
    } finally {
      setLostSubmitting(false)
    }
  }

  // 打开失物认领弹窗（拉取全部待认领失物）
  const openLostClaim = async () => {
    if (!requireLogin('查看失物认领')) return
    setShowLostMenu(false)
    setShowLostClaim(true)
    setLostLoading(true)
    try {
      const res = await getLostItems()
      setLostItems(res.data || [])
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '加载失物失败，请稍后重试', icon: 'none' })
      setLostItems([])
    } finally {
      setLostLoading(false)
    }
  }

  // 业务 Agent 下发的可执行服务按钮：跳充值页 / 打开失物菜单 / 报失登记 / 失物认领
  const handleService = useCallback((svc: ServiceItem) => {
    if (svc.id === 'recharge') {
      Taro.navigateTo({ url: '/pages/recharge/index' })
    } else if (svc.id === 'lost_menu') {
      toggleLostMenu()
    } else if (svc.id === 'report_lost') {
      openLostReport()
    } else if (svc.id === 'lost_claim') {
      openLostClaim()
    } else {
      Taro.showToast({ title: svc.title || '该功能即将上线', icon: 'none' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ============================================================
  // 回答右下角操作条：复制 / 收藏 / 转发 / 删除
  // ============================================================
  /** 组装复制/转发文本：问 + 答 + 来源 */
  const buildShareText = (msg: Message) => {
    const parts: string[] = []
    if (msg.question) parts.push(`【问】${msg.question}`)
    parts.push(msg.content)
    if (msg.references?.length) parts.push(`【来源】${msg.references.join('、')}`)
    return parts.join('\n')
  }

  /** 复制到剪贴板 */
  const handleCopyMsg = useCallback((msg: Message) => {
    Taro.setClipboardData({ data: buildShareText(msg) })
      .then(() => Taro.showToast({ title: '已复制到剪贴板', icon: 'none' }))
      .catch(() => Taro.showToast({ title: '复制失败，请长按选择复制', icon: 'none' }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /**
   * 收藏附图收集：
   * - localPaths：用户上传的原图（本地临时路径，收藏前需先上传到服务器，否则重启后失效）
   * - serverUrls：地图静态图 / 统计图表 / 回答正文里的 markdown 图片（已是服务器或 http 地址）
   */
  const collectFavoriteImages = (msg: Message) => {
    const localPaths: string[] = []
    const serverUrls: string[] = []
    if (msg.sourceImage) localPaths.push(msg.sourceImage)
    if (msg.mapImage) serverUrls.push(resolveMapUrl(msg.mapImage))
    ;(msg.charts || []).forEach((c: any) => {
      if (c?.url) serverUrls.push(c.url.startsWith('http') ? c.url : API_BASE + c.url)
    })
    const re = /!\[[^\]]*\]\(([^)\s]+)\)/g
    let m: RegExpExecArray | null
    while ((m = re.exec(msg.content || '')) !== null) {
      const u = m[1]
      if (u) serverUrls.push(/^https?:\/\//i.test(u) ? u : API_BASE + (u.startsWith('/') ? u : `/${u}`))
    }
    const uniq = (arr: string[]) => Array.from(new Set(arr.filter(Boolean)))
    return { localPaths: uniq(localPaths), serverUrls: uniq(serverUrls).slice(0, 6) }
  }

  /**
   * 收藏状态比对键：同一条 AI 回答由「问 + 答」唯一标识，
   * 后端 add_favorite 也是按 (user, question) upsert，故用其判断某条消息是否在收藏列表里。
   */
  const favKey = (q?: string, a?: string) => `${(q || '').trim()}\u0000${(a || '').trim()}`

  /** 收藏到「我的-我的收藏」（含附图：本地图先上传落盘）；已收藏则再次点击 = 取消收藏 */
  const handleFavoriteMsg = useCallback(async (msg: Message) => {
    if (!requireLogin('收藏回答')) return
    // —— 已收藏：再点一次 = 取消收藏（与「我的-我的收藏」页删除口径一致）——
    if (msg.favorited) {
      Taro.showModal({
        title: '取消收藏？',
        content: '将从「我的-我的收藏」中移除这条问答',
        confirmText: '取消收藏',
        confirmColor: '#e4572e',
        success: async (r) => {
          if (!r.confirm) return
          try {
            if (msg.favoriteId) await deleteFavorite(msg.favoriteId)
          } catch (e: any) {
            Taro.showToast({ title: e?.message || '取消收藏失败', icon: 'none' })
            return
          }
          updateMessage(msg.id, { favorited: false, favoriteId: undefined })
          // 通知「我的收藏」列表同步移除（含无 favoriteId 的兜底：按问+答匹配）
          notifyFavoritesChanged({
            action: 'remove',
            favoriteId: msg.favoriteId,
            question: msg.question,
            answer: msg.content,
          })
          Taro.showToast({ title: '已取消收藏', icon: 'none' })
        },
      })
      return
    }
    const { localPaths, serverUrls } = collectFavoriteImages(msg)
    let images = [...serverUrls]
    if (localPaths.length) {
      Taro.showLoading({ title: '保存图片...' })
      for (const p of localPaths) {
        try {
          const up = await uploadFavoriteImage(p)
          if (up?.data?.url) images.unshift(API_BASE + up.data.url)
        } catch {
          /* 单张上传失败不阻塞收藏（至少保留文字与其它图） */
        }
      }
      Taro.hideLoading()
    }
    try {
      const res = await addFavorite(msg.question || '', msg.content, getSessionId(),
                                    Array.from(new Set(images)).slice(0, 6))
      updateMessage(msg.id, { favorited: true, favoriteId: res?.data?.favorite_id })
      // 通知「我的收藏」列表：新增一条（该页若已打开会立即补上）
      notifyFavoritesChanged({
        action: 'add',
        favoriteId: res?.data?.favorite_id,
        question: msg.question,
        answer: msg.content,
      })
      Taro.showToast({ title: res?.msg || '已收藏', icon: 'none', duration: 2000 })
    } catch (e: any) {
      Taro.hideLoading()
      Taro.showToast({ title: e?.message || '收藏失败，请稍后重试', icon: 'none' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requireLogin])

  /** 删除该条回答（仅界面移除，二次确认） */
  const handleDeleteMsg = useCallback((msg: Message) => {
    Taro.showModal({
      title: '删除这条回答？',
      content: '仅从当前对话界面移除，已收藏的内容不受影响',
      confirmText: '删除',
      confirmColor: '#e4572e',
      success: (r) => {
        if (!r.confirm) return
        setMessages((prev) => prev.filter((m) => m.id !== msg.id))
        // 同步维护本地多轮历史，避免后续追问引用已删除内容
        if (msg.question && msg.content) {
          chatHistoryRef.current = chatHistoryRef.current.replace(
            `用户：${msg.question}\nAI：${msg.content}\n`, '')
        }
        Taro.showToast({ title: '已删除', icon: 'none' })
      },
    })
  }, [])

  /** 把文本按画布宽度折行（中文按字宽计算） */
  const wrapText = (ctx: any, text: string, maxWidth: number): string[] => {
    const lines: string[] = []
    text.split('\n').forEach((para) => {
      let line = ''
      for (const ch of para) {
        const test = line + ch
        if (ctx.measureText(test).width > maxWidth && line) {
          lines.push(line)
          line = ch
        } else {
          line = test
        }
      }
      lines.push(line)
    })
    return lines
  }

  /** 生成"回答分享长图"（本地临时文件路径） */
  const buildShareImage = async (msg: Message): Promise<string> => {
    const W = 750
    const PAD = 40
    const dpr = 2
    const node: any = await new Promise((resolve) => {
      Taro.createSelectorQuery()
        .select('#share-canvas')
        .fields({ node: true, size: true }, (res) => resolve(res))
        .exec()
    })
    const canvas = node?.node
    if (!canvas) throw new Error('分享画布不可用')
    const ctx = canvas.getContext('2d')
    ctx.font = '28px sans-serif'

    const qLines = msg.question ? wrapText(ctx, `问：${msg.question}`, W - PAD * 2) : []
    const aLines = wrapText(ctx, msg.content, W - PAD * 2)
    const refLine = msg.references?.length
      ? wrapText(ctx, `来源：${msg.references.join('、')}`, W - PAD * 2)
      : []
    const H = 190 + (qLines.length + aLines.length + refLine.length) * 46 + 90

    canvas.width = W * dpr
    canvas.height = H * dpr
    ctx.scale(dpr, dpr)
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, W, H)
    // 顶部品牌条
    const grad = ctx.createLinearGradient(0, 0, W, 110)
    grad.addColorStop(0, '#ffd166')
    grad.addColorStop(1, '#ff9a3d')
    ctx.fillStyle = grad
    ctx.fillRect(0, 0, W, 110)
    ctx.fillStyle = '#7a4a00'
    ctx.font = 'bold 34px sans-serif'
    ctx.fillText('🦆 鸭先知', PAD, 70)

    let y = 175
    if (qLines.length) {
      ctx.fillStyle = '#2b8fe3'
      ctx.font = 'bold 28px sans-serif'
      qLines.forEach((l) => { ctx.fillText(l, PAD, y); y += 46 })
      y += 14
    }
    ctx.fillStyle = '#333333'
    ctx.font = '28px sans-serif'
    aLines.forEach((l) => { ctx.fillText(l, PAD, y); y += 46 })
    if (refLine.length) {
      y += 14
      ctx.fillStyle = '#9aa7b8'
      ctx.font = '24px sans-serif'
      refLine.forEach((l) => { ctx.fillText(l, PAD, y); y += 40 })
    }
    ctx.fillStyle = '#c8b79a'
    ctx.font = '22px sans-serif'
    ctx.fillText('由 AI 生成 · 详情请咨询园区工作人员', PAD, H - 40)

    const tmp: any = await Taro.canvasToTempFilePath({ canvas, fileType: 'png' } as any)
    const path = tmp?.tempFilePath || ''
    if (!path) throw new Error('分享图生成失败')
    return path
  }

  /** 转发给微信好友：优先生成长图走微信分享面板（可发好友/收藏），失败降级为复制 */
  const handleForwardMsg = useCallback(async (msg: Message) => {
    Taro.showLoading({ title: '生成分享图...' })
    try {
      const path = await buildShareImage(msg)
      Taro.hideLoading()
      const anyTaro = Taro as any
      if (typeof anyTaro.showShareImageMenu === 'function') {
        await new Promise<void>((resolve, reject) => {
          anyTaro.showShareImageMenu({
            path,
            success: () => resolve(),
            fail: (err: any) => reject(new Error(err?.errMsg || 'share failed')),
          })
        })
        return
      }
      // 低版本基础库不支持分享图：降级为复制文案
      await Taro.setClipboardData({ data: buildShareText(msg) })
      Taro.showToast({ title: '已复制文案，去微信粘贴给好友即可', icon: 'none', duration: 2500 })
    } catch (e: any) {
      Taro.hideLoading()
      try {
        await Taro.setClipboardData({ data: buildShareText(msg) })
        Taro.showToast({ title: '已复制文案，去微信粘贴给好友即可', icon: 'none', duration: 2500 })
      } catch {
        Taro.showToast({ title: e?.message || '转发失败，请稍后重试', icon: 'none' })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <View className='chat-page'>
      {/* 顶部标题栏 */}
      <View className='header card'>
        <View className='header-avatar'>
          <Text className='header-emoji'>🦆</Text>
          <Text className='header-balloon'>🎈</Text>
        </View>
        <View className='header-info'>
          <Text className='header-title'>鸭先知</Text>
          <Text className='header-sub'>🎢 票务 · 设施 · 游玩攻略 · 随时问我～</Text>
        </View>
        <Text className='header-deco'>🎪🎠🎡</Text>
      </View>

      {/* 对话区（memo 隔离：仅 messages/锚点变化时重渲染，防弹窗/工具栏 setState 导致滚动位置跳动） */}
      <ChatMessages
        messages={messages}
        scrollAnchor={scrollAnchor}
        user={user}
        onCardPaid={handleCardPaid}
        onShowMap={openMapModal}
        onNavigate={openNavigation}
        onService={handleService}
        onSuggest={handleSuggest}
        onSpeak={handleSpeak}
        onCopy={handleCopyMsg}
        onFavorite={handleFavoriteMsg}
        onForward={handleForwardMsg}
        onDelete={handleDeleteMsg}
        resolveMapUrl={resolveMapUrl}
      />

      {/* 转发长图用的离屏画布（不可见，仅用于绘制后导出图片） */}
      <Canvas type='2d' id='share-canvas' className='share-canvas' />

      {/* 快捷按钮 + 快捷问题 */}
      <View className='quick-area'>
        {/* 快捷工具栏：左滑三个 pill（购票/项目/失物），右侧语音/地图固定 */}
        <View className='quick-toolbar-wrap'>
          <ScrollView scrollX className='quick-toolbar-scroll' showScrollbar={false}>
            <View className='quick-toolbar'>
              <View
                className='pill pill-pink'
                onClick={() => {
                  if (!requireLogin('查看游园码')) return
                  Taro.navigateTo({ url: '/pages/passes/index' })
                }}
              >
                <Text>🎫 游园码</Text>
              </View>
              <View
                className={`pill ${showProjectList ? 'pill-sky-active' : 'pill-sky'}`}
                onClick={openProjectPanel}
              >
                <Text>🎢 项目游玩 {showProjectList ? '▴' : '▾'}</Text>
              </View>
              <View
                className={`pill ${showLostMenu ? 'pill-lost-active' : 'pill-lost'}`}
                onClick={toggleLostMenu}
              >
                <Text>🧸 失物招领 {showLostMenu ? '▴' : '▾'}</Text>
              </View>
            </View>
          </ScrollView>
          <View className='quick-toolbar-fixed'>
            {/* 图片/语音按钮暂时隐藏（2026-09-05）：
                - 图片提问：后端 paddleocr 在当前环境导入失败（distutils 断言错误，OCR 始终降级为"服务不可用"），
                  修复 paddle 环境后取消注释即可恢复
                - 语音输入：依赖微信「同声传译」插件，需在 mp.weixin.qq.com 后台添加插件
                  并在 app.config.ts 取消 plugins 注释后，取消注释即可恢复
            <View className='tool-btn' onClick={askWithImage}>
              <Text className='tool-icon'>📷</Text>
              <Text className='tool-label'>图片</Text>
            </View>
            <View className={`tool-btn ${isListening ? 'tool-listening' : ''}`} onClick={startVoice}>
              <Text className='tool-icon'>🎤</Text>
              <Text className='tool-label'>语音</Text>
            </View>
            */}
            {/* AI 画像：靠账号走的长期记忆，点开可看可改可清 */}
            <View className='tool-btn' onClick={openProfile}>
              <Text className='tool-icon'>🧠</Text>
              <Text className='tool-label'>画像</Text>
            </View>
            <View className='tool-btn' onClick={() => setShowMap(true)}>
              <Text className='tool-icon'>📍</Text>
              <Text className='tool-label'>地图</Text>
            </View>
          </View>
        </View>

        <ScrollView scrollX className='quick-scroll' showScrollbar={false}>
          <View className='quick-row'>
            {quickQuestions.map((q, i) => (
              <View key={`${q.question}-${i}`} className='quick-card' onClick={() => quickAsk(q.question)}>
                <Text>{q.icon} {q.label}</Text>
              </View>
            ))}
          </View>
        </ScrollView>
      </View>

      {/* 失物招领下拉菜单（我要报失 / 失物认领） */}
      {showLostMenu && (
        <View className='lost-menu card msg-enter'>
          <View className='lost-menu-item' onClick={openLostReport}>
            <Text className='lost-menu-icon'>📝</Text>
            <View className='lost-menu-info'>
              <Text className='lost-menu-title'>我要报失</Text>
              <Text className='lost-menu-desc'>填写名称并上传照片，登记丢失物品</Text>
            </View>
          </View>
          <View className='lost-menu-item' onClick={openLostClaim}>
            <Text className='lost-menu-icon'>🔍</Text>
            <View className='lost-menu-info'>
              <Text className='lost-menu-title'>失物认领</Text>
              <Text className='lost-menu-desc'>查看园区登记的全部失物信息</Text>
            </View>
          </View>
        </View>
      )}

      {/* 游乐项目面板：读后端价目表，就地勾选下单，下单后直接出券 */}
      {showProjectList && (
        <View className='projects-panel card msg-enter'>
          <View className='projects-header'>
            <Text className='projects-title'>🎢 选要玩的项目</Text>
            <Text className='projects-close' onClick={() => setShowProjectList(false)}>×</Text>
          </View>

          {projResult ? (
            <View className='proj-done'>
              <Text className='proj-done-title'>已生成 {projResult.pass_count} 张游园券</Text>
              <Text className='proj-done-sub'>
                共扣 {projResult.total_coins} 币 · 余币 {projResult.remaining_coins} 币
              </Text>
              <View className='proj-done-list'>
                {(projResult.passes || []).map((p, i) => (
                  <View key={i} className='proj-done-row'>
                    <Text className='proj-done-name'>{p.project_name} × 1</Text>
                    <Text className='proj-done-code'>{p.item_code}</Text>
                  </View>
                ))}
              </View>
              <View className='proj-submit' onClick={goMyPasses}>
                <Text className='proj-submit-text'>去「游园码」出示</Text>
              </View>
              <Text className='proj-done-tip'>一券一项一次，工作人员扫一张核销一张</Text>
            </View>
          ) : projLoading ? (
            <Text className='proj-hint'>价目表加载中…</Text>
          ) : !projects.length ? (
            <Text className='proj-hint'>暂时没有在售项目</Text>
          ) : (
            <>
              {/* 两列网格：名称左上 / 勾选框右上 / 价格 / 数量。
                  未选中时数量行置灰且不拦截点击，点它等于选中这张卡 */}
              <View className='proj-grid'>
                {projects.map((p) => {
                  const qty = projSel[p.id] || 0
                  return (
                    <View
                      key={p.id}
                      className={`proj-card${qty ? ' proj-card-on' : ''}`}
                      onClick={() => toggleProj(p.id)}
                    >
                      <View className='proj-card-top'>
                        <Text className='proj-name'>{p.name}</Text>
                        <View className={`proj-check${qty ? ' proj-check-on' : ''}`}>
                          {qty ? <Text className='proj-check-tick'>✓</Text> : null}
                        </View>
                      </View>
                      <Text className='proj-price'>
                        {p.price_coins} 币{p.unit_desc ? ` · ${p.unit_desc}` : ''}
                      </Text>
                      {/* stopPropagation 必须一直在：否则未选中时点「＋」，
                          子事件会冒泡到卡片把刚选中的又取消掉 */}
                      <View
                        className={`proj-qty${qty ? '' : ' proj-qty-off'}`}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <View className='proj-qty-btn' onClick={() => changeProjQty(p.id, -1)}>
                          <Text>−</Text>
                        </View>
                        <Text className='proj-qty-num'>{qty}</Text>
                        <View className='proj-qty-btn' onClick={() => changeProjQty(p.id, 1)}>
                          <Text>＋</Text>
                        </View>
                      </View>
                    </View>
                  )
                })}
              </View>
              <View className='proj-total'>
                <Text className='proj-total-label'>
                  合计 <Text className='proj-total-num'>{totalProjCoins}</Text> 币
                </Text>
                <Text className='proj-total-avail'>
                  {projAvail === null ? '登录后查看余币' : `可用余币 ${projAvail} 币`}
                </Text>
              </View>
              <View
                className={`proj-submit${totalProjCoins ? '' : ' proj-submit-off'}`}
                onClick={confirmProjConsume}
              >
                <Text className='proj-submit-text'>
                  {projConsuming ? '处理中…' : `确认扣费 ${totalProjCoins} 币`}
                </Text>
              </View>
            </>
          )}
        </View>
      )}

      {/* 输入区 */}
      <View className='input-area'>
        {/* 已选图片：可先输入文字再一起发送（拍照/相册由左侧 + 唤起） */}
        {attachImg && (
          <View className='attach-row'>
            <Image
              className='attach-img'
              src={attachImg}
              mode='aspectFill'
              onClick={() => Taro.previewImage({ urls: [attachImg], current: attachImg })}
            />
            <View className='attach-meta'>
              <Text className='attach-tip'>图片已就绪，可输入想补充的问题一起发送～</Text>
              <Text className='attach-del' onClick={removeAttachImg}>移除图片 ✕</Text>
            </View>
          </View>
        )}
        <View className='input-box'>
          <View className='attach-btn' hoverClass='press' hoverStayTime={60} onClick={chooseAttachImg}>
            <Text className='attach-btn-plus'>+</Text>
          </View>
          <Textarea
            className='input-textarea'
            value={inputValue}
            onInput={(e: any) => setInputValue(e.detail.value)}
            placeholder={attachImg ? '选图成功！输入问题一起发送，或直接点出发' : '在这里输入你想问的...'}
            maxlength={500}
            autoHeight
            confirmType='send'
            onConfirm={handleSend as unknown as (e: KeyboardEvent) => void}
          />
          <View
            className={`send-btn ${isSending ? 'send-stop' : ''}`}
            onClick={isSending ? stopGenerating : handleSend}
          >
            <Text>{isSending ? '■ 停止' : attachImg ? '📷 发送' : '🚀 出发'}</Text>
          </View>
        </View>
        {isListening && (
          <View className='listening-bar'>
            <Text className='breathe'>🎤 正在聆听...</Text>
          </View>
        )}
      </View>

      {/* 我要报失弹窗：名称 + 照片 + 联系方式，提交后统一入库商家端失物招领模块 */}
      {showLostReport && (
        <View className='modal-mask' onClick={() => setShowLostReport(false)}>
          <View className='modal-body card' onClick={(e) => e.stopPropagation()}>
            <View className='modal-header'>
              <Text className='modal-title-pink'>📝 我要报失</Text>
              <Text className='modal-close' onClick={() => setShowLostReport(false)}>×</Text>
            </View>
            <Text className='modal-tip'>先传一张照片，AI 会自动帮你填好失物名称（也可以自己填）</Text>

            <View className='lost-form'>
              <View className='lost-field'>
                <Text className='lost-label'>失物名称</Text>
                <Input
                  className='lost-input'
                  value={lostName}
                  onInput={(e: any) => setLostName(e.detail.value)}
                  placeholder='如：粉色水杯、黑色钱包'
                  maxlength={100}
                />
              </View>
              <View className='lost-field'>
                <Text className='lost-label'>失物照片</Text>
                <View className='lost-photo-row'>
                  {lostPhotoPath ? (
                    <View className='lost-photo-preview'>
                      <Image
                        className='lost-photo-img'
                        src={lostPhotoPath}
                        mode='aspectFill'
                        onClick={() => Taro.previewImage({ urls: [lostPhotoPath], current: lostPhotoPath })}
                      />
                      <Text
                        className='lost-photo-del'
                        onClick={() => { setLostPhotoPath(''); setLostAi(null) }}
                      >×</Text>
                    </View>
                  ) : (
                    <View className='lost-photo-add' onClick={chooseLostPhoto}>
                      <Text className='lost-photo-icon'>📷</Text>
                      <Text className='lost-photo-hint'>点击拍照 / 相册上传</Text>
                    </View>
                  )}
                </View>
                {lostAnalyzing && (
                  <View className='lost-ai-tip'>
                    <Text className='lost-ai-tip-text'>🔍 正在识别照片，稍后自动填好名称…</Text>
                  </View>
                )}
                {!lostAnalyzing && lostAi?.ok && (
                  <View className='lost-ai-tags'>
                    <Text className='lost-ai-title'>✨ 已识别</Text>
                    {!!lostAi.category && <Text className='lost-ai-tag'>{lostAi.category}</Text>}
                    {!!lostAi.color && <Text className='lost-ai-tag'>{lostAi.color}</Text>}
                    {!!lostAi.brand && <Text className='lost-ai-tag'>{lostAi.brand}</Text>}
                    {lostAi.features.slice(0, 3).map((f) => (
                      <Text key={f} className='lost-ai-tag'>{f}</Text>
                    ))}
                  </View>
                )}
              </View>
              <View className='lost-field'>
                <Text className='lost-label'>联系方式（选填）</Text>
                <Input
                  className='lost-input'
                  value={lostReporter}
                  onInput={(e: any) => setLostReporter(e.detail.value)}
                  placeholder='便于失物找到后联系你'
                  maxlength={50}
                />
              </View>
            </View>

            <View
              className={`rating-submit ${lostPhotoPath && (lostName.trim() || lostAi?.ok) && !lostAnalyzing && !lostSubmitting ? '' : 'rating-submit-disabled'}`}
              hoverClass='rating-submit-hover'
              hoverStayTime={80}
              onClick={submitLostReport}
            >
              <Text className='rating-submit-text'>
                {lostAnalyzing ? '识别中...' : lostSubmitting ? '提交中...' : '提交报失'}
              </Text>
            </View>
            <View className='rating-close' onClick={() => setShowLostReport(false)}>
              <Text>取消</Text>
            </View>
          </View>
        </View>
      )}

      {/* 失物认领弹窗：展示全部失物的名称/时间/图片（商家端统一登记的数据源） */}
      {showLostClaim && (
        <View className='modal-mask' onClick={() => setShowLostClaim(false)}>
          <View className='modal-body card' onClick={(e) => e.stopPropagation()}>
            <View className='modal-header'>
              <Text className='modal-title-green'>🔍 失物认领</Text>
              <Text className='modal-close' onClick={() => setShowLostClaim(false)}>×</Text>
            </View>
            <Text className='modal-tip'>园区登记的失物信息，如看到您的物品请联系工作人员认领</Text>

            {lostLoading ? (
              <View className='lost-empty'>加载中...</View>
            ) : lostItems.length === 0 ? (
              <View className='lost-empty'>🎉 暂无待认领的失物</View>
            ) : (
              <ScrollView scrollY className='lost-list'>
                {lostItems.map((item) => (
                  <View key={item.id} className='lost-item card'>
                    {item.photo_url ? (
                      <Image
                        className='lost-item-img'
                        src={resolveImageUrl(item.photo_url)}
                        mode='aspectFill'
                        onClick={() => Taro.previewImage({
                          urls: lostItems.filter((x) => x.photo_url).map((x) => resolveImageUrl(x.photo_url)),
                          current: resolveImageUrl(item.photo_url),
                        })}
                      />
                    ) : (
                      <View className='lost-item-img lost-item-img-empty'>🧸</View>
                    )}
                    <View className='lost-item-info'>
                      <Text className='lost-item-name'>{item.name}</Text>
                      <Text className='lost-item-time'>🕐 {item.created_at}</Text>
                      {!!item.reporter && <Text className='lost-item-reporter'>📞 {item.reporter}</Text>}
                    </View>
                  </View>
                ))}
              </ScrollView>
            )}

            <View className='modal-ok-btn' onClick={() => setShowLostClaim(false)}>
              <Text className='modal-ok-text'>知道啦 👍</Text>
            </View>
          </View>
        </View>
      )}

      {/* AI 画像底部抽屉：填写区全部点选（步进器 / 滚轮 / 标签），
          每项都显示"当前值"，拨回「未填」即恢复 AI 自动识别 */}
      {showProfile && (
        <View className='modal-mask profile-mask' onClick={() => setShowProfile(false)}>
          <View className='profile-sheet' onClick={(e) => e.stopPropagation()}>
            <View className='profile-head'>
              <View className='profile-head-main'>
                <Text className='profile-title'>AI 记得我</Text>
                <Text className='profile-sub'>
                  {profileData?.has_data
                    ? `已基于 ${profileData.msg_count} 轮对话 · 跟着账号走，换设备不丢`
                    : '填一次，之后问什么都能对上'}
                </Text>
              </View>
              {!!profileData?.has_data && (
                <Text className='profile-clear' onClick={clearProfile}>清空记忆</Text>
              )}
            </View>

            <ScrollView scrollY className='profile-body' showScrollbar={false}>
              {profileLoading ? (
                <Text className='profile-hint'>正在读取…</Text>
              ) : (
                <>
                  <View className='profile-line'>
                    <Text className='profile-label'>家里几个孩子</Text>
                    <View className='stepper'>
                      <View className='step-btn' onClick={removeKid}><Text>−</Text></View>
                      <Text className='step-num'>{draftKids.length}</Text>
                      <View className='step-btn' onClick={addKid}><Text>＋</Text></View>
                    </View>
                  </View>

                  {draftKids.length === 0 && (
                    <Text className='profile-hint'>
                      点右上「＋」添加孩子，AI 就能按年龄身高推荐合适的项目
                    </Text>
                  )}

                  {draftKids.map((k, i) => (
                    <View className='kid-card' key={`kid-${i}`}>
                      <Text className='kid-title'>孩子 {i + 1}</Text>
                      <View className='kid-line'>
                        <Picker
                          mode='selector'
                          range={ageRange}
                          value={rangeIndex(ageRange, k.age)}
                          onChange={(e) => {
                            const s = ageRange[Number(e.detail.value)]
                            setKidField(i, 'age', s === '未填' ? null : Number(s))
                          }}
                        >
                          <View className='picker-box'>
                            <Text className={k.age != null ? 'picker-val' : 'picker-val picker-empty'}>
                              {k.age != null ? `${k.age} 岁` : '未填'}
                            </Text>
                            <Text className='picker-arrow'>▾</Text>
                          </View>
                        </Picker>
                        <Picker
                          mode='selector'
                          range={heightRange}
                          value={rangeIndex(heightRange, k.height, true)}
                          onChange={(e) => {
                            const s = heightRange[Number(e.detail.value)]
                            setKidField(i, 'height', s === '未填' ? null : Number(s))
                          }}
                        >
                          <View className='picker-box'>
                            <Text className={k.height != null ? 'picker-val' : 'picker-val picker-empty'}>
                              {k.height != null ? `${k.height} 米` : '未填'}
                            </Text>
                            <Text className='picker-arrow'>▾</Text>
                          </View>
                        </Picker>
                      </View>
                      <View className='kid-line'>
                        {['男', '女'].map((g) => (
                          <View
                            key={g}
                            className={`chip${k.gender === g ? ' chip-on' : ''}`}
                            onClick={() => setKidField(i, 'gender', k.gender === g ? null : g)}
                          >
                            <Text>{g}孩</Text>
                          </View>
                        ))}
                      </View>

                      {/* v5：这个孩子喜欢玩什么 —— 每个孩子**各挂一排**项目，
                          点一下=他喜欢（再点一下取消）；长按=标"不喜欢" */}
                      <Text className='kid-sub'>
                        喜欢玩什么项目
                        <Text className='kid-sub-tip'>（点一下选中；长按标为不喜欢）</Text>
                      </Text>
                      <View className='chip-wrap'>
                        {(profileOpts?.projects || []).map((p) => {
                          const liked = (k.likes || []).includes(p)
                          const disliked = (k.dislikes || []).includes(p)
                          const cls = liked ? ' chip-on'
                            : (disliked ? ' chip-warn chip-on' : '')
                          return (
                            <View
                              key={`pref-${p}`}
                              className={`chip chip-sm${cls}`}
                              onClick={() => toggleKidChip(i, 'likes', p)}
                              onLongPress={() => toggleKidChip(i, 'dislikes', p)}
                            >
                              <Text>{disliked ? `${p} ✕` : p}</Text>
                            </View>
                          )
                        })}
                      </View>

                      {/* v5：选填 —— 有没有什么症状 / 注意事项 */}
                      {(profileOpts?.symptoms || []).length > 0 && (
                        <>
                          <Text className='kid-sub'>有没有什么症状（选填）</Text>
                          <View className='chip-wrap'>
                            {(profileOpts?.symptoms || []).map((s) => (
                              <View
                                key={`sym-${s}`}
                                className={`chip chip-sm chip-ghost${(k.symptoms || []).includes(s) ? ' chip-on' : ''}`}
                                onClick={() => toggleKidChip(i, 'symptoms', s)}
                              >
                                <Text>{s}</Text>
                              </View>
                            ))}
                          </View>
                        </>
                      )}
                    </View>
                  ))}

                  {/* 账号级偏好：说不清是哪个孩子时落这里；孩子专属的填在上面每张卡片里 */}
                  <Text className='profile-label'>
                    {draftKids.length > 0 ? '全家都喜欢玩的项目' : '喜欢玩什么项目'}
                  </Text>
                  <View className='chip-wrap'>
                    {(profileOpts?.projects || []).map((p) => (
                      <View
                        key={p}
                        className={`chip${draftProjects.includes(p) ? ' chip-on' : ''}`}
                        onClick={() => toggleChip('projects', p)}
                      >
                        <Text>{p}</Text>
                      </View>
                    ))}
                  </View>
                  {draftKids.length > 0 && (
                    <Text className='profile-hint'>
                      某个孩子专属的喜好，填在上面「孩子 N」那张卡片里
                    </Text>
                  )}

                  <Text className='profile-label'>常关注</Text>
                  <View className='chip-wrap'>
                    {(profileOpts?.topics || []).map((t) => (
                      <View
                        key={t}
                        className={`chip${draftTopics.includes(t) ? ' chip-on' : ''}`}
                        onClick={() => toggleChip('topics', t)}
                      >
                        <Text>{t}</Text>
                      </View>
                    ))}
                  </View>

                  <Text className='profile-label'>AI 从对话里记住的</Text>
                  {profileData?.tags?.length ? (
                    <View className='chip-wrap'>
                      {profileData.tags.map((t) => (
                        <View key={t} className='chip chip-ro'><Text>{t}</Text></View>
                      ))}
                    </View>
                  ) : (
                    <Text className='profile-hint'>
                      还没有积累。聊聊票价、项目或孩子的年龄身高，AI 会自动记住
                    </Text>
                  )}

                  <View className='profile-line profile-switch'>
                    <Text className='profile-label'>以后按我的情况来回答</Text>
                    <Switch
                      checked={profileData?.personalization_on ?? true}
                      color='#e8963a'
                      onChange={togglePersonalization}
                    />
                  </View>
                </>
              )}
            </ScrollView>

            <View
              className={`profile-save${profileSaving ? ' profile-save-off' : ''}`}
              onClick={saveProfile}
            >
              <Text>{profileSaving ? '保存中…' : '保存'}</Text>
            </View>
          </View>
        </View>
      )}

      {/* 园区地图弹窗（原生 Map 组件，GCJ-02 无偏移） */}
      {showMap && (
        <View className='modal-mask' onClick={() => setShowMap(false)}>
          <View className='modal-body card' onClick={(e) => e.stopPropagation()}>
            <View className='modal-header'>
              <Text className='modal-title-green'>
                {lastMapInfo?.mapImage ? '📍 实时地图信息' : '📍 大黄鸭室外游乐园'}
              </Text>
              <Text className='modal-close' onClick={() => setShowMap(false)}>×</Text>
            </View>

            {lastMapInfo?.mapImage ? (
              <>
                <Image
                  className='map-image-lg'
                  src={resolveMapUrl(lastMapInfo.mapImage)}
                  mode='aspectFill'
                  onClick={() => Taro.previewImage({
                    urls: [resolveMapUrl(lastMapInfo.mapImage)],
                    current: resolveMapUrl(lastMapInfo.mapImage),
                  })}
                />
                <View className='map-link-btn center' onClick={openNavigation}>
                  <Text className='map-link-text'>📍 打开地图 / 开始导航</Text>
                </View>
              </>
            ) : (
              <>
                <Map
                  className='mini-map'
                  latitude={PARK_LATITUDE}
                  longitude={PARK_LONGITUDE}
                  scale={16}
                  markers={[{
                    id: 1,
                    latitude: PARK_LATITUDE,
                    longitude: PARK_LONGITUDE,
                    title: '大黄鸭室外游乐园',
                    width: 32,
                    height: 32,
                  }]}
                />
                <Text className='map-address'>🏠 北京市怀柔区万达广场2号门</Text>
              </>
            )}

            <Text className='map-hint'>💡 询问"在哪里 / 怎么去 / 附近有什么"，可查看实时地图与导航</Text>
          </View>
        </View>
      )}

      {/* 满意度评价弹窗：累计 3 问后触发，1-5 星可选后提交 */}
      {showRating && (
        <View className='modal-mask light' onClick={() => setShowRating(false)}>
          <View className='modal-body card' onClick={(e) => e.stopPropagation()}>
            <Text className='rating-title'>🎉 本次服务怎么样？</Text>
            <Text className='rating-sub'>点击星星打分，鸭鸭会继续努力的～</Text>
            <View className='rating-stars'>
              {[1, 2, 3, 4, 5].map((n) => (
                <Text
                  key={n}
                  className={`star ${n <= selectedStars ? 'star-active' : ''}`}
                  onClick={() => setSelectedStars(n)}
                >
                  {n <= selectedStars ? '★' : '☆'}
                </Text>
              ))}
            </View>
            <View className='rating-tags'>
              <Text className='rating-tag green' onClick={() => setSelectedStars(5)}>✅ 解答准确</Text>
              <Text className='rating-tag blue' onClick={() => setSelectedStars(5)}>😊 态度友好</Text>
              <Text className='rating-tag orange' onClick={() => setSelectedStars(5)}>⚡ 响应速度</Text>
            </View>
            <View
              className={`rating-submit ${selectedStars && !ratingSubmitting ? '' : 'rating-submit-disabled'}`}
              hoverClass='rating-submit-hover'
              hoverStayTime={80}
              onClick={submitRating}
            >
              <Text className='rating-submit-text'>
                {ratingSubmitting ? '提交中...' : selectedStars ? `提交 ${selectedStars} 星评价` : '请先选择星级'}
              </Text>
            </View>
            <View className='rating-close' onClick={() => setShowRating(false)}>
              <Text>暂不评价</Text>
            </View>
          </View>
        </View>
      )}
    </View>
  )
}
