import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAuth } from '@/hooks/useAuth'
import { User, LogOut } from 'lucide-react'
import './homepage.css'

// AI 回答 Markdown 渲染样式（段落/加粗/列表统一排版）
const mdComponents = {
  p: ({ children }: any) => <p className="mb-2 leading-relaxed last:mb-0">{children}</p>,
  strong: ({ children }: any) => <strong className="font-bold text-orange-600">{children}</strong>,
  em: ({ children }: any) => <em className="italic">{children}</em>,
  ol: ({ children }: any) => <ol className="my-1 ml-1 space-y-1.5 list-decimal list-outside pl-4">{children}</ol>,
  ul: ({ children }: any) => <ul className="my-1 ml-1 space-y-1.5 list-disc list-outside pl-4">{children}</ul>,
  li: ({ children }: any) => <li className="leading-relaxed pl-1">{children}</li>,
  h1: ({ children }: any) => <h1 className="mb-2 text-base font-bold text-gray-800">{children}</h1>,
  h2: ({ children }: any) => <h2 className="mb-2 text-base font-bold text-gray-800">{children}</h2>,
  h3: ({ children }: any) => <h3 className="mb-1.5 text-sm font-bold text-gray-800">{children}</h3>,
  blockquote: ({ children }: any) => <blockquote className="my-1 border-l-4 border-orange-300 bg-orange-50/60 py-1 pl-3 text-gray-600">{children}</blockquote>,
  code: ({ children }: any) => <code className="rounded bg-gray-100 px-1 py-0.5 text-xs text-pink-600">{children}</code>,
  a: ({ href, children }: any) => <a href={href} target="_blank" rel="noopener noreferrer" className="text-green-600 underline hover:text-green-700">{children}</a>,
  hr: () => <hr className="my-2 border-gray-200" />,
  table: ({ children }: any) => <table className="my-2 w-full border-collapse text-xs">{children}</table>,
  th: ({ children }: any) => <th className="border border-gray-200 bg-gray-50 px-2 py-1 text-left font-bold">{children}</th>,
  td: ({ children }: any) => <td className="border border-gray-200 px-2 py-1">{children}</td>,
}

// 游乐项目数据
const PROJECTS = [
  { icon: '🎠', name: '旋转木马', price: '30元 / 5分钟（仅限儿童）', bgClass: 'bg-orange-50', borderClass: 'border-orange-100', textClass: 'text-orange-700', question: '旋转木马' },
  { icon: '🎡', name: '荡秋千', price: '40元 / 4分钟', bgClass: 'bg-sky-50', borderClass: 'border-sky-100', textClass: 'text-sky-700', question: '荡秋千' },
  { icon: '🎢', name: '小型过山车', price: '50元 / 单次15圈', bgClass: 'bg-red-50', borderClass: 'border-red-100', textClass: 'text-red-700', question: '过山车' },
  { icon: '🦘', name: '蹦床', price: '40元 / 5分钟', bgClass: 'bg-green-50', borderClass: 'border-green-100', textClass: 'text-green-700', question: '蹦床' },
  { icon: '🚿', name: '浴室大作战', price: '40元 / 5分钟', bgClass: 'bg-purple-50', borderClass: 'border-purple-100', textClass: 'text-purple-700', question: '浴室大作战' },
  { icon: '💣', name: '大炮乐园', price: '30元 / 80发子弹', bgClass: 'bg-amber-50', borderClass: 'border-amber-100', textClass: 'text-amber-700', question: '大炮乐园' },
]

const QUICK_QUESTIONS = [
  { icon: '🎫', text: '门票价格', question: '门票多少钱' },
  { icon: '💳', text: '怎么办卡', question: '怎么办卡充值' },
  { icon: '🕐', text: '营业时间', question: '营业时间' },
  { icon: '💰', text: '退卡规则', question: '能退卡吗' },
  { icon: '👶', text: '儿童安全', question: '儿童游玩注意事项' },
  { icon: '🔧', text: '设备故障', question: '设备故障怎么办' },
]

interface Message {
  id: number
  type: 'user' | 'ai' | 'system'
  content: string
  // 思考过程时间线：status/debug 事件逐条追加，回答开始后折叠
  thinking?: string[]
  thinkingDone?: boolean
  references?: string[]
  // 高德地图资源（由后端生成，前端只消费不调用高德restapi）
  mapUrl?: string
  mapImage?: string
  extraInfo?: Record<string, any> | null
  isError?: boolean
}

// 思考过程时间线：思考中逐条展开实时滚动，回答开始后折叠为可展开标签
function ThinkingBox({ steps, done }: { steps: string[]; done: boolean }) {
  const [open, setOpen] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  // 思考中新条目自动滚动到底部
  useEffect(() => {
    if (!done && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [steps.length, done])
  const showList = done ? open : true
  return (
    <div className="mb-2 rounded-xl bg-sky-50/80 border border-sky-100 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold text-sky-600 hover:bg-sky-100/60 transition text-left"
      >
        {!done && <span className="ferris-loader">🎡</span>}
        <span>{done ? `🧠 已深度思考（${steps.length} 步）` : '🧠 鸭鸭思考中...'}</span>
        {done && <span className="ml-auto text-gray-400 font-normal">{open ? '▴ 收起' : '▾ 展开'}</span>}
      </button>
      {showList && (
        <div ref={listRef} className="px-3 pb-2 max-h-40 overflow-y-auto">
          {steps.map((s, i) => (
            <div key={i} className="flex gap-1.5 text-xs text-gray-500 py-0.5 leading-relaxed msg-enter">
              <span className="text-sky-400 shrink-0">•</span>
              <span>{s}</span>
            </div>
          ))}
          {!done && <div className="text-xs text-sky-400 animate-pulse py-0.5">💭 思考中...</div>}
        </div>
      )}
    </div>
  )
}

export default function HomePage() {
  const navigate = useNavigate()
  const { currentUser, logout } = useAuth()
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 1,
      type: 'ai',
      content: '嗨～欢迎来到大黄鸭游乐园！🎉\n我是你的专属小助手 鸭鸭，关于票价、设施、游玩攻略都可以问我哦～',
    },
  ])
  const [inputValue, setInputValue] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [showProjectList, setShowProjectList] = useState(false)
  const [showQR, setShowQR] = useState(false)
  const [showMap, setShowMap] = useState(false)
  // 最近一次 AI 回答携带的地图资源（地图按钮弹窗优先展示）
  const [lastMapInfo, setLastMapInfo] = useState<{ mapUrl: string; mapImage: string; extraInfo: any } | null>(null)
  const [showRating, setShowRating] = useState(false)
  const [ratingShown, setRatingShown] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const chatBoxRef = useRef<HTMLDivElement>(null)
  const mapContainerRef = useRef<HTMLDivElement>(null)
  const mapInstanceRef = useRef<any>(null)
  const recognitionRef = useRef<any>(null)
  const chatHistoryRef = useRef('')
  const messageIdRef = useRef(2)

  const API_BASE = `http://${window.location.hostname}:8000`
  const API_URL = API_BASE + '/chat/stream'

  // 地图资源相对路径（/amap/staticmap 等后端代理地址）补全为完整地址
  const resolveMapUrl = (url: string) => (url && url.startsWith('http') ? url : API_BASE + url)

  // 自动滚动到底部
  useEffect(() => {
    if (chatBoxRef.current) {
      chatBoxRef.current.scrollTop = chatBoxRef.current.scrollHeight
    }
  }, [messages])

  // 初始化地图（仅当无实时地图资源时加载 Leaflet 园区地图）
  useEffect(() => {
    if (showMap && !lastMapInfo?.mapImage && mapContainerRef.current && !mapInstanceRef.current) {
      // 动态加载 leaflet
      const loadLeaflet = () => {
        if ((window as any).L) {
          initMap()
          return
        }
        const link = document.createElement('link')
        link.rel = 'stylesheet'
        link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'
        document.head.appendChild(link)

        const script = document.createElement('script')
        script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
        script.onload = initMap
        document.head.appendChild(script)
      }
      loadLeaflet()
    } else if (showMap && mapInstanceRef.current) {
      setTimeout(() => mapInstanceRef.current?.invalidateSize(), 200)
    }
  }, [showMap, lastMapInfo])

  const initMap = () => {
    const L = (window as any).L
    if (!L || !mapContainerRef.current) return
    // 大黄鸭室外游乐园 - 北京市怀柔区万达广场2号门（高德地理编码门址坐标，GCJ-02）
    // 配高德瓦片（同为 GCJ-02 坐标系，定位精准无偏移）：无需用户提问，点开即展示园区真实地理位置
    const PARK_LAT = 40.317475, PARK_LNG = 116.630026
    mapInstanceRef.current = L.map(mapContainerRef.current).setView([PARK_LAT, PARK_LNG], 16)
    L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
      attribution: '&copy; 高德地图',
      maxZoom: 18,
      subdomains: ['1', '2', '3', '4'],
    }).addTo(mapInstanceRef.current)
    L.marker([PARK_LAT, PARK_LNG])
      .addTo(mapInstanceRef.current)
      .bindPopup('<b>🎠 大黄鸭室外游乐园</b><br>北京市怀柔区万达广场2号门')
      .openPopup()
  }

  const handleLogout = () => {
    logout()
    toast.success('已退出登录，下次见～👋')
    window.location.href = '/login'
  }

  const addMessage = (msg: Omit<Message, 'id'>) => {
    const id = messageIdRef.current++
    setMessages(prev => [...prev, { ...msg, id }])
    return id
  }

  const updateMessage = (id: number, updates: Partial<Message>) => {
    setMessages(prev => prev.map(m => m.id === id ? { ...m, ...updates } : m))
  }

  // 追加一条思考过程（status/debug 事件）
  const appendThinking = (id: number, text: string) => {
    setMessages(prev => prev.map(m => m.id === id
      ? { ...m, thinking: [...(m.thinking || []), text] }
      : m))
  }

  const quickAsk = (q: string) => {
    setInputValue(q)
    setShowProjectList(false)
    setTimeout(() => sendQuestion(q), 100)
  }

  const sendQuestion = async (question?: string) => {
    const q = (question || inputValue).trim()
    if (!q || isSending) return

    setIsSending(true)
    setInputValue('')

    // 添加用户消息
    addMessage({ type: 'user', content: q })

    // 添加 AI 消息（思考中：思考时间线逐条展示）
    const aiMsgId = addMessage({
      type: 'ai',
      content: '',
      thinking: ['🚀 已收到问题，开始思考...'],
      thinkingDone: false,
    })

    let fullAnswer = ''

    try {
      const res = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, history: chatHistoryRef.current })
      })

      if (!res.ok) throw new Error('请求失败')

      const reader = res.body?.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            try {
              const event = JSON.parse(line.slice(6))
              if (event.type === 'chunk') {
                fullAnswer += event.content
                // 生成开始即思考结束：思考时间线折叠
                updateMessage(aiMsgId, { content: fullAnswer, thinkingDone: true })
              } else if (event.type === 'status' || event.type === 'debug') {
                // 思考过程逐条追加展示
                appendThinking(aiMsgId, event.content)
              } else if (event.type === 'meta') {
                // 解析地图资源（map_url/map_image/extra_info 由后端 Agent 工具生成）
                const mapInfo = {
                  mapUrl: event.map_url || '',
                  mapImage: event.map_image || '',
                  extraInfo: event.extra_info || null,
                }
                updateMessage(aiMsgId, { references: event.reference_doc, ...mapInfo })
                if (mapInfo.mapUrl || mapInfo.mapImage) {
                  setLastMapInfo(mapInfo)
                }
              } else if (event.type === 'error') {
                updateMessage(aiMsgId, { content: event.content, isError: true, thinkingDone: true })
              }
            } catch (e) { /* ignore */ }
          }
        }
      }

      chatHistoryRef.current += `用户：${q}\nAI：${fullAnswer}\n`

      // 随机弹出评价
      if (fullAnswer.length > 20 && !ratingShown && Math.random() < 0.3) {
        setRatingShown(true)
        setTimeout(() => setShowRating(true), 1500)
      }
    } catch (err) {
      updateMessage(aiMsgId, {
        content: '网络请求失败，请确认后端服务已启动！',
        isError: true,
        thinkingDone: true,
      })
    } finally {
      // 兕底：无论成功失败，思考时间线都结束
      updateMessage(aiMsgId, { thinkingDone: true })
      setIsSending(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendQuestion()
    }
  }

  // 语音识别
  const startVoice = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognition) {
      toast.error('您的浏览器不支持语音输入，请使用 Chrome 或 Edge 浏览器')
      return
    }

    if (!recognitionRef.current) {
      recognitionRef.current = new SpeechRecognition()
      recognitionRef.current.lang = 'zh-CN'
      recognitionRef.current.interimResults = false
      recognitionRef.current.maxAlternatives = 3

      recognitionRef.current.onresult = (event: any) => {
        const transcript = event.results[0][0].transcript.trim()
        setIsListening(false)
        setInputValue(transcript)
        setTimeout(() => sendQuestion(transcript), 300)
      }

      recognitionRef.current.onerror = () => {
        setIsListening(false)
        toast.error('语音识别失败，请手动输入')
      }

      recognitionRef.current.onend = () => {
        setIsListening(false)
      }
    }

    setIsListening(true)
    recognitionRef.current.start()
  }

  const rateStar = (n: number) => {
    toast.success(`感谢您的 ${n} 星评价！🎉`)
    setShowRating(false)
  }

  return (
    <div className="homepage-container bunting-bg">
      {/* 右上角：我的 + 退出登录 */}
      <div className="fixed top-3 right-3 z-50 flex gap-2">
        <button
          onClick={() => navigate('/profile')}
          className="bg-white rounded-full px-4 py-2 shadow-lg border-2 border-pink-300 text-sm font-bold text-pink-600
                     hover:bg-pink-50 transition flex items-center gap-2 min-h-[48px]"
          title="个人中心"
        >
          <User className="h-4 w-4" /> 我的
        </button>
        <button
          onClick={handleLogout}
          className="bg-white rounded-full px-4 py-2 shadow-lg border-2 border-gray-300 text-sm font-bold text-gray-600
                     hover:bg-gray-50 transition flex items-center gap-2 min-h-[48px]"
          title="退出登录"
        >
          <LogOut className="h-4 w-4" /> 退出
        </button>
      </div>

      <div className="max-w-2xl mx-auto px-3 py-3" style={{ maxWidth: '720px' }}>
        {/* 顶部标题栏 */}
        <div className="bg-white/85 backdrop-blur rounded-2xl shadow-md p-4 mb-3 flex items-center gap-3 border-2 border-orange-200">
          <div className="relative">
            <div className="w-14 h-14 rounded-full bg-gradient-to-br from-yellow-300 to-orange-400 flex items-center justify-center text-2xl shadow-lg">
              🦆
            </div>
            <span className="absolute -top-1 -right-1 text-sm">🎈</span>
          </div>
          <div className="flex-1">
            <h1 className="text-xl font-bold" style={{ color: 'var(--orange)' }}>鸭先知</h1>
            <p className="text-xs text-gray-500">🎢 票务 · 设施 · 游玩攻略 · 随时问我～</p>
          </div>
          <div className="flex gap-1 text-sm">
            <span>🎪</span><span>🎠</span><span>🎡</span>
          </div>
        </div>

        {/* 对话区 */}
        <div
          ref={chatBoxRef}
          className="bg-white/70 backdrop-blur rounded-2xl shadow-md p-4 mb-3 overflow-y-auto chat-scroll"
          style={{ height: 'calc(100vh - 340px)', minHeight: '300px' }}
        >
          {messages.map(msg => (
            <div key={msg.id}>
              {msg.type === 'system' ? (
                <div className="text-center text-xs text-gray-400 my-2 msg-enter">{msg.content}</div>
              ) : msg.type === 'user' ? (
                <div className="flex gap-3 mb-4 justify-end msg-enter">
                  <div className="ticket-bubble p-3 max-w-[80%]">
                    <p className="text-sm text-white whitespace-pre-wrap">{msg.content}</p>
                  </div>
                  <div className="w-9 h-9 rounded-full bg-gradient-to-br from-gray-400 to-gray-600 flex items-center justify-center shrink-0 text-sm text-white shadow">
                    👤
                  </div>
                </div>
              ) : (
                <div className="flex gap-3 mb-4 msg-enter">
                  <div className="w-10 h-10 rounded-full bg-gradient-to-br from-yellow-300 to-orange-400 flex items-center justify-center shrink-0 text-xl shadow">
                    🦆
                  </div>
                  <div className="cloud-bubble p-3 max-w-[80%] flex-1">
                    {/* 思考过程时间线：思考中展开，回答开始后折叠 */}
                    {msg.thinking && msg.thinking.length > 0 && (
                      <ThinkingBox steps={msg.thinking} done={!!msg.thinkingDone} />
                    )}
                    {msg.content && (
                      msg.isError ? (
                        <p className="text-sm whitespace-pre-wrap text-red-500">{msg.content}</p>
                      ) : (
                        // AI 回答用 Markdown 渲染：支持加粗/列表/标题等排版
                        <div className="ai-markdown text-sm text-gray-700">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                            {msg.content}
                          </ReactMarkdown>
                        </div>
                      )
                    )}
                    {msg.references && msg.references.length > 0 && (
                      <div className="mt-2 text-xs text-gray-400 border-t pt-2">
                        📄 参考：{msg.references.join('、')}
                      </div>
                    )}
                    {/* 高德地图资源：静态图 / 跳转链接 / POI / 路线 / 天气（后端生成） */}
                    {msg.mapImage && (
                      <div className="mt-2">
                        <img
                          src={resolveMapUrl(msg.mapImage)}
                          alt="地图"
                          className="w-full max-w-[300px] rounded-lg border border-green-200 shadow-sm"
                        />
                      </div>
                    )}
                    {msg.mapUrl && (
                      <div className="mt-2">
                        <a
                          href={msg.mapUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-block bg-green-600 hover:bg-green-700 text-white text-xs font-bold px-3 py-1.5 rounded-lg"
                        >📍 打开地图 / 导航</a>
                      </div>
                    )}
                    {msg.extraInfo?.pois?.length > 0 && (
                      <div className="mt-2 bg-sky-50 rounded-lg p-2 text-xs text-gray-600">
                        <p className="font-bold mb-1">🏷️ 地点信息：</p>
                        {msg.extraInfo.pois.slice(0, 4).map((p: any, idx: number) => (
                          <p key={idx} className="py-0.5">• {p.name}{p.address ? `（${p.address}）` : ''}</p>
                        ))}
                      </div>
                    )}
                    {msg.extraInfo?.route && (
                      <div className="mt-2 bg-sky-50 rounded-lg p-2 text-xs text-gray-600">
                        🗺️ {msg.extraInfo.route.mode}路线：约 {msg.extraInfo.route.distance_km} 公里，预计 {msg.extraInfo.route.duration_min} 分钟
                      </div>
                    )}
                    {msg.extraInfo?.weather && (
                      <div className="mt-2 bg-sky-50 rounded-lg p-2 text-xs text-gray-600">
                        🌤️ {msg.extraInfo.weather.city}：{msg.extraInfo.weather.weather}，{msg.extraInfo.weather.temperature}，{msg.extraInfo.weather.wind}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        {/* 快捷问题卡片 */}
        <div className="mb-3">
          <div className="flex gap-2 mb-2 text-xs font-bold items-center">
            <button
              onClick={() => setShowQR(true)}
              className="bg-pink-100 text-pink-600 px-3 py-1 rounded-full cursor-pointer hover:bg-pink-200 transition min-h-[32px] flex items-center gap-1 border-0"
              title="点击获取购票二维码"
            >🎫 购票入园</button>
            <button
              onClick={() => setShowProjectList(!showProjectList)}
              className={`px-3 py-1 rounded-full cursor-pointer transition min-h-[32px] flex items-center gap-1 border-0 ${
                showProjectList ? 'bg-sky-200 text-sky-700' : 'bg-sky-100 text-sky-600 hover:bg-sky-200'
              }`}
              title="点击查看所有游乐项目"
            >🎢 项目游玩 {showProjectList ? '▴' : '▾'}</button>
            <span className="flex-1"></span>
            <button
              onClick={startVoice}
              className={`flex flex-col items-center text-xs px-1.5 py-1 rounded-lg transition min-w-[40px] ${
                isListening ? 'text-red-500 bg-red-50' : 'text-gray-500 hover:text-red-500 hover:bg-red-50'
              }`}
              title="语音输入"
            >
              <span className="text-base">🎤</span>语音
            </button>
            <button
              onClick={() => setShowMap(true)}
              className="flex flex-col items-center text-xs text-gray-500 hover:text-green-600 px-1.5 py-1 rounded-lg hover:bg-green-50 transition min-w-[40px]"
              title="园区地图"
            ><span className="text-base">📍</span>地图</button>
            <button
              onClick={() => toast.info('文件上传功能开发中～')}
              className="flex flex-col items-center text-xs text-gray-500 hover:text-blue-600 px-1.5 py-1 rounded-lg hover:bg-blue-50 transition min-w-[40px]"
              title="发图片/文件"
            ><span className="text-base">📎</span>传文件</button>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {QUICK_QUESTIONS.map((q, i) => (
              <button
                key={i}
                className="quick-card"
                onClick={() => quickAsk(q.question)}
              >{q.icon} {q.text}</button>
            ))}
          </div>
        </div>

        {/* 游乐项目列表面板 */}
        {showProjectList && (
          <div className="bg-white/95 backdrop-blur rounded-2xl shadow-lg p-4 mb-3 border-2 border-sky-300 msg-enter">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-sm font-bold text-sky-600">🎢 大黄鸭全部游乐项目（共6项）</h3>
              <button
                onClick={() => setShowProjectList(false)}
                className="text-gray-400 hover:text-gray-600 text-lg leading-none min-h-[32px] min-w-[32px]"
              >×</button>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {PROJECTS.map((p, i) => (
                <div
                  key={i}
                  className={`${p.bgClass} rounded-xl p-3 border ${p.borderClass} flex items-center gap-3 hover:shadow-md transition cursor-pointer`}
                  onClick={() => quickAsk(p.question)}
                >
                  <span className="text-2xl">{p.icon}</span>
                  <div>
                    <p className={`text-sm font-bold ${p.textClass}`}>{p.name}</p>
                    <p className="text-xs text-gray-500">{p.price}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 购票二维码弹窗 */}
        {showQR && (
          <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 msg-enter"
            onClick={() => setShowQR(false)}
          >
            <div
              className="bg-white rounded-2xl p-6 shadow-2xl max-w-sm w-full mx-4 text-center"
              onClick={e => e.stopPropagation()}
            >
              <div className="flex justify-between items-center mb-3">
                <h3 className="text-lg font-bold text-pink-600">🎫 扫码购票入园</h3>
                <button
                  onClick={() => setShowQR(false)}
                  className="text-gray-400 hover:text-gray-600 text-xl leading-none min-h-[32px] min-w-[32px]"
                >×</button>
              </div>
              <p className="text-xs text-gray-500 mb-3">请使用微信/支付宝扫描下方二维码</p>
              <div className="bg-gray-100 rounded-xl p-4 inline-block">
                <img
                  src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=https://example.com/amusement-park-ticket&bgcolor=ffffff&color=FF6B35"
                  alt="购票二维码"
                  className="w-48 h-48 mx-auto"
                />
              </div>
              <p className="text-xs text-gray-400 mt-3">📱 到店出示二维码即可快速入园</p>
              <button
                className="mt-3 bg-pink-500 text-white px-6 py-2 rounded-full text-sm font-bold hover:bg-pink-600 transition"
                onClick={() => setShowQR(false)}
              >知道啦 👍</button>
            </div>
          </div>
        )}

        {/* 园区地图弹窗：优先展示 AI 回答携带的实时地图资源，无资源时回退 Leaflet 园区地图 */}
        {showMap && (
          <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 msg-enter"
            onClick={() => setShowMap(false)}
          >
            <div
              className="bg-white rounded-2xl p-4 shadow-2xl max-w-lg w-full mx-4"
              onClick={e => e.stopPropagation()}
            >
              <div className="flex justify-between items-center mb-3">
                <h3 className="text-base font-bold text-green-600">
                  {lastMapInfo?.mapImage ? '📍 实时地图信息' : '📍 大黄鸭室外游乐园 - 北京市怀柔区万达广场2号门'}
                </h3>
                <button
                  onClick={() => setShowMap(false)}
                  className="text-gray-400 hover:text-gray-600 text-xl leading-none min-h-[32px] min-w-[32px]"
                >×</button>
              </div>

              {lastMapInfo?.mapImage ? (
                <>
                  <div className="rounded-xl overflow-hidden border-2 border-green-200">
                    <img
                      src={resolveMapUrl(lastMapInfo.mapImage)}
                      alt="地图"
                      className="w-full"
                      style={{ maxHeight: '350px', objectFit: 'cover' }}
                    />
                  </div>
                  {lastMapInfo.mapUrl && (
                    <div className="mt-3 text-center">
                      <a
                        href={lastMapInfo.mapUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-block bg-green-600 hover:bg-green-700 text-white text-sm font-bold px-4 py-2 rounded-lg"
                      >📍 打开地图 / 开始导航</a>
                    </div>
                  )}
                  {lastMapInfo.extraInfo?.pois?.length > 0 && (
                    <div className="mt-3 bg-sky-50 rounded-lg p-3 text-xs text-gray-600">
                      <p className="font-bold mb-1">🏷️ 地点信息：</p>
                      {lastMapInfo.extraInfo.pois.slice(0, 4).map((p: any, idx: number) => (
                        <p key={idx} className="py-0.5">• {p.name}{p.address ? `（${p.address}）` : ''}</p>
                      ))}
                    </div>
                  )}
                  {lastMapInfo.extraInfo?.route && (
                    <div className="mt-2 bg-sky-50 rounded-lg p-2 text-xs text-gray-600">
                      🗺️ {lastMapInfo.extraInfo.route.mode}路线：约 {lastMapInfo.extraInfo.route.distance_km} 公里，预计 {lastMapInfo.extraInfo.route.duration_min} 分钟
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div
                    ref={mapContainerRef}
                    className="rounded-xl overflow-hidden border-2 border-green-200"
                    style={{ height: '350px' }}
                  ></div>
                  <p className="text-xs text-gray-400 mt-2 text-center">🏠 北京市怀柔区万达广场2号门</p>
                </>
              )}

              <p className="text-xs text-gray-400 mt-2 text-center">
                💡 询问"在哪里 / 怎么去 / 附近有什么"等，可查看实时地图与导航
              </p>
            </div>
          </div>
        )}

        {/* 小火车输入区域 */}
        <div className="train-input flex items-end gap-2">
          <textarea
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            className="flex-1 resize-none bg-transparent outline-none text-sm text-gray-700 placeholder-gray-400 py-1"
            placeholder="在这里输入你想问的..."
            rows={2}
            style={{ minHeight: '44px' }}
          />
          <button
            className="coaster-btn flex items-center gap-1 shrink-0"
            onClick={() => sendQuestion()}
            disabled={isSending}
          >
            {isSending ? '🎡 等待...' : '🚀 出发'}
          </button>
        </div>

        {/* 语音识别状态条 */}
        {isListening && (
          <div className="text-center text-xs mt-1">
            <span className="inline-flex items-center gap-1 text-red-500">
              <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-pulse"></span>
              🎤 正在聆听...
            </span>
          </div>
        )}

        <div className="text-center text-xs text-gray-400 mt-1">
          <span className="animate-pulse">🎠</span>  · 小助手飞速赶来中～
        </div>

        {/* 星级评价弹窗 */}
        {showRating && (
          <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
            <div className="bg-white rounded-2xl p-6 shadow-2xl max-w-sm w-full mx-4 msg-enter">
              <p className="text-center text-lg font-bold mb-1">🎉 本次服务怎么样？</p>
              <p className="text-center text-xs text-gray-500 mb-4">鸭鸭会继续努力的～</p>
              <div className="flex justify-center gap-2 mb-4">
                {[1, 2, 3, 4, 5].map(n => (
                  <span
                    key={n}
                    className="text-3xl cursor-pointer star-pop"
                    style={{ animationDelay: `${(n - 1) * 0.08}s` }}
                    onClick={() => rateStar(n)}
                  >⭐</span>
                ))}
              </div>
              <div className="flex justify-center gap-2 flex-wrap mb-3">
                <button className="text-xs bg-green-100 text-green-700 px-3 py-1 rounded-full" onClick={() => rateStar(5)}>✅ 解答准确</button>
                <button className="text-xs bg-blue-100 text-blue-700 px-3 py-1 rounded-full" onClick={() => rateStar(5)}>😊 态度友好</button>
                <button className="text-xs bg-orange-100 text-orange-700 px-3 py-1 rounded-full" onClick={() => rateStar(5)}>⚡ 响应速度</button>
              </div>
              <button
                className="w-full bg-gray-200 text-gray-600 py-2 rounded-lg text-sm"
                onClick={() => setShowRating(false)}
              >关闭</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
