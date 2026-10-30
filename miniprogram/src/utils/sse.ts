// ============================================================
// 小程序 SSE 流式请求（后端协议与 PC 端完全一致：data: {json}\n\n）
// - Taro.request enableChunked + onChunkReceived 分块接收（基础库 ≥2.20.1）
// - 处理 UTF-8 多字节字符被分块截断的问题（中文高频出现）
// - 帧缓冲按 \n\n 拆分事件
// - 失败时由调用方降级为 POST /chat 非流式
// ============================================================
import Taro from '@tarojs/taro'
import { API_BASE } from './config'
import { getToken } from './auth'

export interface SseEvent {
  type: 'meta' | 'chunk' | 'status' | 'debug' | 'step' | 'action' | 'suggestions' | 'charts' | 'error' | 'done' | 'trace'
  content?: string
  cards?: any[]
  /** 可执行服务出口（业务 Agent 返回：去办卡/失物认领/报失登记等按钮） */
  services?: any[]
  /** 依据当前上下文的主动追问建议（杠杆3） */
  items?: string[]
  /** 消费统计等生成的图表 [{chart_id,title,url}] */
  charts?: any[]
  reference_doc?: string[]
  map_url?: string
  map_image?: string
  extra_info?: Record<string, any>
  elapsed?: number
  /** 真实思考步骤（后端 process_stream 逐步上抛中间结果） */
  step?: {
    id?: string
    phase?: string
    title?: string
    detail?: string
    meta?: Record<string, any>
    elapsed?: number
  }
  [k: string]: any
}

interface SseOptions {
  url: string
  data: any
  onEvent: (ev: SseEvent) => void
  onError: (err: Error) => void
  onComplete: () => void
  /** 流结束（含错误）是否也触发 onComplete，默认 true */
  alwaysComplete?: boolean
  /** 首包超时（毫秒）：发出请求后 N 毫秒内未收到任何数据则断开降级，默认 30000。
   *  防止断网/服务不可达时请求挂起（底层 timeout 只保底整体时长，不能及时反馈）；
   *  首包含检索耗时（Milvus+BM25+Reranker），30s 内未收到任何数据才判定为不可达 */
  firstChunkTimeout?: number
  /** 流中空闲超时（毫秒）：任意两次数据之间最大间隔，默认 45000。断网中途发生时及时报错 */
  idleTimeout?: number
}

/** 分块传输 UTF-8 字节 → 字符串（二进制安全；TextDecoder 不可用时走 decodeURIComponent 兜底） */
function bytesToString(bytes: Uint8Array): string {
  if (typeof TextDecoder !== 'undefined') {
    return new TextDecoder('utf-8').decode(bytes)
  }
  let s = ''
  for (let i = 0; i < bytes.length; i++) {
    s += '%' + bytes[i].toString(16).padStart(2, '0')
  }
  try {
    return decodeURIComponent(s)
  } catch {
    return ''
  }
}

/**
 * 找到 Uint8Array 中最后一个"完整 UTF-8 字符"的边界，
 * 返回 [完整部分, 截断剩余部分]（截断部分留待与下一 chunk 拼接）
 */
function splitAtCharBoundary(bytes: Uint8Array): [Uint8Array, Uint8Array] {
  // 从尾部最多回溯 4 字节找字符起始位（10xxxxxx 是续字节）
  for (let back = 0; back < 4 && back < bytes.length; back++) {
    const i = bytes.length - 1 - back
    const b = bytes[i]
    if ((b & 0xc0) === 0x80) continue // 续字节，继续回溯
    // b 是起始字节：判断该字符需要的总字节数
    const need = b >= 0xf0 ? 4 : b >= 0xe0 ? 3 : b >= 0xc0 ? 2 : 1
    const have = back + 1
    if (have < need) {
      // 最后一个字符被截断
      return [bytes.subarray(0, i), bytes.subarray(i)]
    }
    break
  }
  return [bytes, new Uint8Array(0)]
}

export interface SseTask {
  abort: () => void
}

export function sseRequest(options: SseOptions): SseTask {
  const {
    url, data, onEvent, onError, onComplete, alwaysComplete = true,
    firstChunkTimeout = 30000, idleTimeout = 45000,
  } = options

  let pendingBytes: Uint8Array = new Uint8Array(0)
  let frameBuffer = ''
  let finished = false
  let gotFirstChunk = false

  // ---------- 超时看门狗：断网/服务不可达时及时断开，不再白等到底层 timeout ----------
  let watchdog: ReturnType<typeof setTimeout> | null = null
  const clearWatchdog = () => {
    if (watchdog) {
      clearTimeout(watchdog)
      watchdog = null
    }
  }
  const armWatchdog = () => {
    clearWatchdog()
    watchdog = setTimeout(() => {
      if (finished) return
      const msg = gotFirstChunk
        ? '网络不稳定，回答中断了，请重试'
        : '连接超时，请检查网络后重试'
      try {
        task.abort()
      } catch {
        /* ignore */
      }
      finish(new Error(msg))
    }, gotFirstChunk ? idleTimeout : firstChunkTimeout)
  }

  const finish = (err?: Error) => {
    if (finished) return
    finished = true
    if (err) onError(err)
    if (alwaysComplete) onComplete()
  }

  /** 处理一份完整的 SSE 帧（data: {...}） */
  const handleFrame = (frame: string) => {
    for (const line of frame.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const payload = trimmed.slice(5).trim()
      if (!payload) continue
      try {
        onEvent(JSON.parse(payload) as SseEvent)
      } catch {
        // 忽略无法解析的行
      }
    }
  }

  const task = Taro.request({
    url: API_BASE + url,
    method: 'POST',
    data,
    enableChunked: true,
    timeout: 180000,
    header: {
      'Content-Type': 'application/json',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    success: (res) => {
      clearWatchdog()
      if (res.statusCode >= 200 && res.statusCode < 300) {
        // 冲刷残余帧
        if (frameBuffer.trim()) handleFrame(frameBuffer)
        finish()
      } else {
        const detail = (res.data as any)?.detail
        const msg =
          (typeof detail === 'object' ? detail?.msg : detail) ||
          `流式请求失败（${res.statusCode}）`
        finish(new Error(msg))
      }
    },
    fail: (err) => {
      clearWatchdog()
      const raw = err?.errMsg || ''
      const msg = /timeout/i.test(raw)
        ? '连接超时，请检查网络后重试'
        : (raw || '网络连接失败，请检查网络后重试')
      finish(new Error(msg))
    },
  })

  // 启动首包看门狗（收到首个 chunk 后切换为流中空闲看门狗）
  armWatchdog()

  // 分块接收
  task.onChunkReceived((res: { data: ArrayBuffer }) => {
    if (finished || !res.data) return
    if (!gotFirstChunk) {
      gotFirstChunk = true
    }
    armWatchdog() // 每收到数据重置空闲看门狗
    const chunk = new Uint8Array(res.data)

    // 拼接上一轮截断的 UTF-8 残余字节
    let merged: Uint8Array
    if (pendingBytes.length) {
      merged = new Uint8Array(pendingBytes.length + chunk.length)
      merged.set(pendingBytes, 0)
      merged.set(chunk, pendingBytes.length)
    } else {
      merged = chunk
    }

    const [complete, rest] = splitAtCharBoundary(merged)
    pendingBytes = rest
    frameBuffer += bytesToString(complete)

    // 按 \n\n 拆帧，最后一段可能不完整留在缓冲
    const frames = frameBuffer.split('\n\n')
    frameBuffer = frames.pop() || ''
    for (const f of frames) handleFrame(f)
  })

  return {
    abort: () => {
      clearWatchdog()
      finished = true
      try {
        task.abort()
      } catch {
        /* ignore */
      }
    },
  }
}
