// ============================================================
// 小程序请求封装：自动带 JWT；401 自动重登一次；业务码 4001（支付未开通）透传
// ============================================================
import Taro from '@tarojs/taro'
import { API_BASE } from './config'
import { ensureLogin, getToken } from './auth'

export class ApiError extends Error {
  status: number
  code?: number

  constructor(message: string, status: number, code?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  data?: any
  auth?: boolean
  /** 是否在 401 时自动重登一次再重试（默认 true） */
  retryOn401?: boolean
  /** 请求超时（毫秒）。默认 30000；慢接口（如非流式聊天降级）需传更长，避免误报无法连接 */
  timeout?: number
}

export async function request<T = any>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', data, auth = true, retryOn401 = true, timeout = 30000 } = options

  const doRequest = () =>
    Taro.request({
      url: API_BASE + path,
      method,
      data,
      header: {
        'Content-Type': 'application/json',
        ...(auth && getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      timeout,
    })

  let res: Taro.request.SuccessCallbackResult<any>
  try {
    res = await doRequest()
  } catch {
    throw new ApiError(`无法连接后端服务（${API_BASE}）`, 0)
  }

  // 401：只处理"本来就没登录"的情况 —— 用静默微信登录补一个 token 给公开页面用。
  // ⚠️ 绝不能对"已有 token 但过期/失效"的请求做强制静默登录：
  //    那会用微信身份换一个**另一个账号**的 token 覆盖当前会话，
  //    用户会突然变成"游客"、我的页数据全空（拿的是另一个账号的数据），且不会自愈。
  //    正确做法是把"登录已过期"如实抛给调用方，由页面引导重新登录。
  if (res.statusCode === 401 && auth && retryOn401) {
    if (getToken()) {
      throw new ApiError('登录已过期，请重新登录', 401)
    }
    try {
      await ensureLogin(true)
      return request<T>(path, { ...options, retryOn401: false })
    } catch {
      throw new ApiError('登录失败，请到「我的」页重新登录', 401)
    }
  }

  const json = res.data || {}
  if (res.statusCode < 200 || res.statusCode >= 300) {
    const detail = json?.detail
    const msg =
      (typeof detail === 'object' ? detail?.msg : detail) ||
      json?.msg ||
      `请求失败（${res.statusCode}）`
    const code = typeof detail === 'object' ? detail?.code : undefined
    throw new ApiError(msg, res.statusCode, code)
  }

  // 业务失败也统一抛错（code != 200）
  if (json?.code !== undefined && json.code !== 200) {
    throw new ApiError(json?.msg || '请求失败', res.statusCode, json.code)
  }

  return json as T
}

/** GET /pay/cards 在售卡列表（办卡卡片/充值页档位数据源） */
export interface CardInfo {
  card_id: number
  name: string
  price: number
  price_yuan: number
  description: string
  coins: number   // 面值（币），如 "200元=320币" → 320
  kind?: string   // small=小额充币（10/20/30币） / member=会员充值（200/500/1000元）
}

export function getSaleCards(): Promise<{ data: CardInfo[] }> {
  return request('/pay/cards', { auth: false })
}

/** POST /pay/orders 办卡下单（false→4001 未开通；mock→直接发卡；true→返回支付参数） */
export function createCardOrder(cardId: number): Promise<{
  data: {
    mode: string
    order_no: string
    status: string
    card_name?: string
    payment?: any
  }
}> {
  return request('/pay/orders', { method: 'POST', data: { card_id: cardId } })
}

/** GET /pay/orders/{no} 轮询订单状态 */
export function getOrderStatus(orderNo: string): Promise<{
  data: { order_no: string; card_name: string; amount: number; status: string }
}> {
  return request(`/pay/orders/${orderNo}`)
}

export interface MyCardInfo {
  id: number
  card_id: number
  card_name: string
  order_no: string
  coins: number
  face_coins: number
  price_yuan: number
  refundable: boolean
  in_use: boolean
  status: string
  kind?: string      // member 会员卡（可能为空=旧数据按 member 处理）
  created_at: string
}

/** GET /pay/my/cards 我的卡列表（含剩余币/面值/支付金额/生效标记） */
export function getMyCards(): Promise<{ data: MyCardInfo[] }> {
  return request('/pay/my/cards')
}

/** GET /pay/my/balance 我的余币余额（持卡面值 + 签到奖励，充值后实时变化） */
export function getMyBalance(): Promise<{ data: { coins: number; cards_count: number } }> {
  return request('/pay/my/balance')
}

/** 签到状态（我的页钱包卡展示） */
export interface CheckinStatus {
  signed_today: boolean
  signed_now?: boolean
  streak_days: number
  total_coins: number
  checkin_count: number
}

/** GET /pay/my/checkin 签到状态（今日是否已签/连续天数/累计奖励） */
export function getCheckinStatus(): Promise<{ data: CheckinStatus }> {
  return request('/pay/my/checkin')
}

/** POST /pay/my/checkin 每日签到（送币计入余额，重复签幂等） */
export function doCheckin(): Promise<{ data: CheckinStatus }> {
  return request('/pay/my/checkin', { method: 'POST' })
}

/** GET /pay/redeem/qrcode 会员码二维码（指定生效卡时按卡出码）
 *  qr_ok=false 时表示后端未生成出图片（如运行环境缺 qrcode 依赖），
 *  此时前端展示 content 文本会员码 + 重试入口，不再无限"加载中" */
export function getRedeemQrcode(cardId?: number): Promise<{
  data: {
    content: string
    coins: number
    card_name: string
    card_coins: number
    image: string
    qr_ok?: boolean
    qr_error?: string
  }
}> {
  return request(cardId ? `/pay/redeem/qrcode?card_id=${cardId}` : '/pay/redeem/qrcode')
}

/** POST /pay/my/cards/{id}/use 使用某张卡（标记为生效中） */
export function useMyCard(cardId: number): Promise<{ code: number; msg: string }> {
  return request(`/pay/my/cards/${cardId}/use`, { method: 'POST' })
}

/** POST /pay/my/cards/{id}/refund 自助退卡（剩余币不足面值时后端拒绝） */
export function refundMyCard(cardId: number): Promise<{ code: number; msg: string; data: { refund_yuan: number } }> {
  return request(`/pay/my/cards/${cardId}/refund`, { method: 'POST' })
}

/**
 * POST /chat/vision 图片（可附文字）提问 → 视觉模型多模态回答
 * 带图提问后端强制走 deepseek-v4-flash-vision-exp（不读文本缓存）；
 * 纯文字（不带图）仍走 /chat/stream（FAQ→Redis 缓存，其余→RAG 文本模型）
 */
export function askVision(
  filePath: string,
  question: string = '',
  sessionId: string = '',
): Promise<{
  code: number
  msg?: string
  data: { answer: string; references: string[]; note?: string }
}> {
  return new Promise((resolve, reject) => {
    Taro.uploadFile({
      url: API_BASE + '/chat/vision',
      filePath,
      name: 'file',
      formData: { question, session_id: sessionId, source: 'miniprogram' },
      header: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      timeout: 120000, // 图片+知识库+视觉模型推理较慢，放宽超时
      success: (res) => {
        try {
          const json = JSON.parse(res.data || '{}')
          if (res.statusCode >= 200 && res.statusCode < 300 && json.code === 200) {
            resolve(json)
          } else {
            reject(new ApiError(json?.detail || json?.msg || '图片提问失败', res.statusCode, json.code))
          }
        } catch {
          reject(new ApiError('图片提问失败（响应解析错误）', res.statusCode))
        }
      },
      fail: () => reject(new ApiError('上传失败，请检查网络后重试', 0)),
    })
  })
}

// ============================================================
// 失物招领
// ============================================================
/** 照片的模型识别结果（多模态大模型看图后给的结构化特征） */
export interface LostPhotoInsight {
  ok: boolean              // 是否识别成功；false 时其余字段为空，前端退回手动填名
  suggested_name: string   // 建议的失物名称（如「绿色盾形挂件」）
  category: string
  color: string
  brand: string
  features: string[]       // 外观特征，最多 5 条
  visible_text: string
  confidence: number       // 0~1
  summary: string          // 一行摘要
  error?: string
}

export interface LostItem {
  id: number
  name: string
  photo_url: string   // /static/lost_found/xxx.jpg（需拼接 API_BASE）
  status: string      // lost 待认领 / claimed 已认领
  source: string      // user 游客报失 / merchant 商家登记
  reporter: string
  created_at: string
  /** 照片的模型识别结果（未识别/识别失败时各字段为空） */
  ai?: LostAiTags
}

/** 失物上的识别标签（列表返回的 ai 字段，用于商家端按特征比对） */
export interface LostAiTags {
  category: string
  color: string
  brand: string
  features: string[]
  visible_text: string
}

/** GET /lost-found/items 失物列表（认领弹窗数据源，默认仅待认领） */
export function getLostItems(): Promise<{ data: LostItem[] }> {
  return request('/lost-found/items?status=lost', { auth: false })
}

/**
 * POST /lost-found/analyze-photo 失物照片预识别（选完照片即调，用于自动填名）。
 *
 * 服务端**不落盘不入库**，所以用户选完又取消也不会留下垃圾文件。
 * 识别失败不会抛业务错（HTTP 仍 200，data.ok=false），调用方按"手动填写"降级即可；
 * 这里只对网络/HTTP 层错误 reject。
 */
export function analyzeLostPhoto(
  photoPath: string,
  name: string = '',
): Promise<{ code: number; msg: string; data: LostPhotoInsight }> {
  return new Promise((resolve, reject) => {
    Taro.uploadFile({
      url: API_BASE + '/lost-found/analyze-photo',
      filePath: photoPath,
      name: 'photo',
      formData: { name },
      header: {
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      // 预识别要走一次视觉模型，进程内首次调用含冷启动（实测可达 25s），
      // 所以这里的超时要比普通请求宽松，否则第一次用必然失败。
      timeout: 35000,
      success: (res) => {
        try {
          const json = JSON.parse(res.data || '{}')
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(json)
          } else {
            reject(new ApiError(json?.detail || json?.msg || '识别失败', res.statusCode, json.code))
          }
        } catch {
          reject(new ApiError('识别失败（响应解析错误）', res.statusCode))
        }
      },
      fail: () => reject(new ApiError('识别失败，请检查网络后重试', 0)),
    })
  })
}

/** POST /lost-found/items 用户报失（名称 + 照片 multipart 上传，时间后端自动生成）
 *
 * name 可以传空串：服务端会用照片的识别结果自动补全名称。
 */
export function reportLostItem(
  name: string,
  photoPath: string,
  reporter: string = '',
): Promise<{ code: number; msg: string; data: LostItem }> {
  return new Promise((resolve, reject) => {
    Taro.uploadFile({
      url: API_BASE + '/lost-found/items',
      filePath: photoPath,
      name: 'photo',
      formData: { name, reporter },
      header: {
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      timeout: 30000,
      success: (res) => {
        try {
          const json = JSON.parse(res.data || '{}')
          if (res.statusCode >= 200 && res.statusCode < 300 && json.code === 200) {
            resolve(json)
          } else {
            reject(new ApiError(json?.detail || json?.msg || '报失失败', res.statusCode, json.code))
          }
        } catch {
          reject(new ApiError('报失失败（响应解析错误）', res.statusCode))
        }
      },
      fail: () => reject(new ApiError('上传失败，请检查网络后重试', 0)),
    })
  })
}

// ============================================================
// 园区公告 / 今日热点（仅商家 PC 端发布，首页只读展示）
// ============================================================
export interface NoticeInfo {
  id: number
  kind: 'announcement' | 'hot'   // announcement 园区公告 / hot 今日热点
  title: string
  content: string
  created_at: string
}

/** GET /notices 公告/热点列表（仅上架内容） */
export function getNotices(): Promise<{ data: NoticeInfo[] }> {
  return request('/notices', { auth: false })
}

/** POST /user-feedback 游客意见反馈（已登录自动关联用户；/feedback 已被满意度评价占用） */
export function submitFeedback(content: string, nickname = ''): Promise<{ code: number; msg: string }> {
  return request('/user-feedback', { method: 'POST', data: { content, nickname } })
}


// ============================================================
// 个人资料（我的页 → 个人信息弹窗）
// ============================================================
/** PUT /auth/me 更新昵称 */
export function updateProfile(nickname: string): Promise<{ code: number; msg: string }> {
  return request('/auth/me', { method: 'PUT', data: { nickname } })
}

/** POST /auth/avatar 上传头像（chooseAvatar 临时文件 → 服务器存储，跨端同步） */
export function uploadAvatar(filePath: string): Promise<{ data: { avatar: string } }> {
  return new Promise((resolve, reject) => {
    Taro.uploadFile({
      url: API_BASE + '/auth/avatar',
      filePath,
      name: 'file',
      header: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      timeout: 30000,
      success: (res) => {
        try {
          const json = JSON.parse(res.data || '{}')
          if (res.statusCode >= 200 && res.statusCode < 300 && json.code === 200) {
            resolve(json)
          } else {
            reject(new ApiError(json?.detail || json?.msg || '头像上传失败', res.statusCode))
          }
        } catch {
          reject(new ApiError('头像上传失败（响应解析错误）', res.statusCode))
        }
      },
      fail: () => reject(new ApiError('上传失败，请检查网络后重试', 0)),
    })
  })
}


// ============================================================
// 游乐消费：多选项目合并扣币 → 一单一券包（一券 = 一项一次）
// ============================================================
export interface ParkProject {
  id: number
  name: string
  price_coins: number
  unit_desc: string
}

/** 单张游园券：到项目现场出示，工作人员扫一张核销一张 */
export interface PassItem {
  item_code: string
  order_no: string
  project_id: number | null
  project_name: string
  unit_coins: number
  status: 'unused' | 'used' | 'refunded' | string
  used_at: string
  created_at: string
}

export interface MyPasses {
  passes: PassItem[]
  /** 「下一张待玩」——我的游园码页顶部大字卡显示的就是它 */
  next_pass: PassItem | null
  unused_count: number
  used_count: number
  refunded_count: number
}

export interface ConsumeResult {
  order_no: string
  verify_code: string
  items: { name: string; qty: number; coins: number; unit_desc?: string }[]
  passes: { project_name: string; item_code: string }[]
  pass_count: number
  total_coins: number
  remaining_coins: number
}

export interface MyConsumption {
  order_no: string
  verify_code: string
  items_json: string   // 消费明细JSON字符串
  total_coins: number
  status: string   // unused 未核销 / used 已核销 / refunded 已退款
  /** 本单生成的券张数（一单一券包：项目数 × 数量） */
  pass_total: number
  /** 本单还剩几张待玩 */
  pass_unused: number
  created_at: string
}

/** GET /pay/projects 游乐项目价目表 */
export function getProjects(): Promise<{ data: ParkProject[] }> {
  return request('/pay/projects', { auth: false })
}

/** POST /pay/consume 游乐消费（多选项目合并扣币，生成唯一核销码） */
export function consumeItems(items: { project_id: number; qty: number }[]): Promise<{
  code: number
  msg: string
  data: ConsumeResult
}> {
  return request('/pay/consume', { method: 'POST', data: { items } })
}

/** GET /pay/passes/qrcode?c=CODE 游园券二维码（PNG base64，扫码直达员工核销页）
 *  qr_ok=false 表示后端未出图（缺 qrcode 依赖等），此时页面展示大字券码让工作人员手工输入 */
export function getPassQrcode(itemCode: string): Promise<{
  data: { item_code: string; content: string; image: string; qr_ok: boolean; qr_error?: string }
}> {
  return request(`/pay/passes/qrcode?c=${encodeURIComponent(itemCode)}`)
}

/** GET /pay/my/passes 我的游园码（待玩在前；不传 status = 全部） */
export function getMyPasses(status = '', limit = 50): Promise<{ data: MyPasses }> {
  const q = status ? `?status=${encodeURIComponent(status)}&limit=${limit}` : `?limit=${limit}`
  return request(`/pay/my/passes${q}`)
}

/** POST /pay/my/passes/refund 自助退券
 *  口径：只退**自己的**券、只退**未核销**的券——玩过的券由现场员工/商家判定后退，
 *  不能让用户在小程序里把已玩过的项目自己退掉。 */
export function refundMyPasses(payload: { codes?: string[]; order_no?: string }): Promise<{
  code: number
  msg: string
  data: {
    refunded_coins: number
    count: number
    items: { item_code: string; order_no: string; project_name: string; unit_coins: number }[]
    skipped: { item_code: string; reason: string }[]
  }
}> {
  return request('/pay/my/passes/refund', { method: 'POST', data: payload })
}

/** GET /pay/my/consumptions 我的消费记录（today=true 仅今日；不传=全部） */
export function getMyConsumptions(limit = 5, today = false): Promise<{ data: MyConsumption[] }> {
  return request(`/pay/my/consumptions?limit=${limit}${today ? '&today=true' : ''}`)
}

/** 我的消费统计（项目排行/办卡/按月；用于“我的”页横向柱状图） */
export interface ConsumptionStats {
  period?: { key?: string; label?: string; start?: string; end?: string }
  project_rank?: { name: string; qty: number; coins: number }[]
  monthly?: { month: string; coins: number; yuan: number }[]
  total_yuan?: number
  order_count?: number
}

export function getMyConsumptionStats(period = 'month'): Promise<{ data: ConsumptionStats }> {
  return request(`/pay/my/consumption-stats?period=${encodeURIComponent(period)}`)
}

// ============================================================
// 快捷提问（近 7 天热点问题 Top6，由后端统计 + 每 7 天自动刷新）
// ============================================================
export interface HotQuestion {
  question: string   // 完整问句（点击后直接发送）
  label: string      // ≤4 字按钮标签
  icon: string       // emoji 图标
  hit_count: number  // 近 7 天提问次数
}

/** GET /chat/hot-questions 近7天热点问题（失败时调用方回退本地默认 6 条） */
export function getHotQuestions(limit = 6): Promise<{ data: HotQuestion[] }> {
  return request(`/chat/hot-questions?limit=${limit}`, { auth: false, timeout: 8000 })
}

// ============================================================
// 收藏（AI 回答操作条「收藏」→ 我的页「我的收藏」）
// ============================================================
export interface FavoriteItem {
  id: number
  question: string
  answer: string
  /** 收藏附图（用户上传原图 / 地图静态图 / 图表），服务器 /static 路径或 http 地址 */
  images: string[]
  created_at: string
}

/**
 * POST /chat/favorites/upload 上传收藏附图
 * 小程序图片是本地临时路径（重启后失效），收藏前必须先落到服务器才能长期显示
 */
export function uploadFavoriteImage(filePath: string): Promise<{ data: { url: string } }> {
  return new Promise((resolve, reject) => {
    Taro.uploadFile({
      url: API_BASE + '/chat/favorites/upload',
      filePath,
      name: 'file',
      header: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      timeout: 30000,
      success: (res: any) => {
        try {
          const json = JSON.parse(res.data || '{}')
          if (res.statusCode >= 200 && res.statusCode < 300 && json?.code === 200) {
            resolve(json)
          } else {
            reject(new ApiError(json?.msg || json?.detail || '图片上传失败', res.statusCode))
          }
        } catch {
          reject(new ApiError('图片上传失败（响应解析错误）', res.statusCode))
        }
      },
      fail: () => reject(new ApiError('图片上传失败，请检查网络', 0)),
    })
  })
}

/** POST /chat/favorites 收藏一条问答（可带附图） */
export function addFavorite(
  question: string,
  answer: string,
  sessionId = '',
  images: string[] = [],
): Promise<{
  code: number
  msg: string
  data: { favorite_id: number }
}> {
  return request('/chat/favorites', {
    method: 'POST',
    data: { question, answer, session_id: sessionId, images },
    timeout: 30000,
  })
}

/** GET /chat/favorites 我的收藏列表 */
export function getFavorites(limit = 50, page = 1): Promise<{
  data: FavoriteItem[]
  total: number
}> {
  return request(`/chat/favorites?limit=${limit}&page=${page}`)
}

/** DELETE /chat/favorites/{id} 取消收藏 */
export function deleteFavorite(favoriteId: number): Promise<{ code: number; msg: string }> {
  return request(`/chat/favorites/${favoriteId}`, { method: 'DELETE' })
}

// ============================================================
// 用户画像（AI 记得我）：跟着账号走的长期记忆，换设备不丢
// ============================================================
/** 一个孩子：年龄 / 身高 / 性别 + 这个孩子自己的项目好恶与注意事项（均可缺） */
export interface ProfileKid {
  age?: number | null
  height?: number | null
  gender?: '男' | '女' | null
  /** v5：这个孩子喜欢玩的项目（每个孩子各一份，不是账号级） */
  likes?: string[] | null
  /** v5：这个孩子不喜欢的项目 */
  dislikes?: string[] | null
  /** v5：选填 —— 症状 / 注意事项（「怕高」「有哮喘」…） */
  symptoms?: string[] | null
}

export interface ProfileData {
  family?: {
    /** v3 唯一真源：每个孩子一条 */
    kids?: ProfileKid[]
    /** 后端由 kids 派生的兼容字段（只读） */
    kids_ages?: number[]
    kids_heights?: number[]
    kids_count?: number
  }
  interest?: { projects?: string[]; topics?: string[]; recent_questions?: string[] }
  context?: { first_seen?: string; last_seen?: string }
  /** 用户手改过的字段（界面上标记「已手动修改」） */
  manual?: Record<string, unknown>
}

/** GET /profile/options 画像弹窗的选项词表（后端下发，前端不硬编码） */
export interface ProfileOptions {
  projects: string[]
  /** v4：负向偏好同一份项目词表 */
  dislikes?: string[]
  /** v5：症状预置标签（选填，用户也可自己说别的） */
  symptoms?: string[]
  topics: string[]
  genders: string[]
  age: { min: number; max: number; step: number }
  height: { min: number; max: number; step: number }
  kids_max: number
}

export interface MyProfile {
  /** AI 自动抽取的原样数据 */
  profile: ProfileData
  /** 合并视图：手改值优先，界面按它渲染 */
  view: ProfileData
  tags: string[]
  /** 一行可读摘要，如「孩子年龄：6岁；孩子身高：1.2米」 */
  note: string
  msg_count: number
  last_active_at?: string | null
  has_data: boolean
  personalization_on: boolean
}

/** GET /profile/me 我的 AI 画像 */
export function getMyProfile(): Promise<{ code: number; data: MyProfile }> {
  return request('/profile/me')
}

/** PUT /profile/me 修正画像（字段传 null 表示删掉手改、恢复 AI 自动值） */
export function updateMyProfile(patch: {
  kids?: ProfileKid[] | null
  kids_count?: number | null
  projects?: string[] | null
  topics?: string[] | null
}): Promise<{ code: number; data: MyProfile }> {
  return request('/profile/me', { method: 'PUT', data: patch })
}

/** GET /profile/options 画像弹窗的选项词表 */
export function getProfileOptions(): Promise<{ code: number; data: ProfileOptions }> {
  return request('/profile/options')
}

/** DELETE /profile/me 清除 AI 对我的记忆（alsoMessages 连同对话原文一起清） */
export function clearMyProfile(alsoMessages = false): Promise<{
  code: number
  data: { deleted: number; messages_cleared: number }
}> {
  return request(`/profile/me?also_messages=${alsoMessages ? 1 : 0}`, { method: 'DELETE' })
}

/** PUT /profile/me/setting 个性化开关（关闭后不再写入画像） */
export function setPersonalization(on: boolean): Promise<{
  code: number
  data: { personalization_on: boolean }
}> {
  return request('/profile/me/setting', { method: 'PUT', data: { personalization_on: on } })
}
