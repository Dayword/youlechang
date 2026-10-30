// ============================================================
// 经营小助手 前端 API（右下角弹窗）
// - run / approve 走 SSE（fetch + ReadableStream；POST 无法用原生 EventSource）
// - 事件类型与 agents/admin_assistant.py 的 yield 一一对应
// - 与 kbAgent.ts 的 streamSse 同一套路（各自持有，避免相互耦合）
// ============================================================
import { API_BASE, ApiError, getToken, clearToken } from '@/lib/api';

export type Risk = 'read' | 'write' | 'danger';

// ---------- 查询类工具的富结果（由后端工具层直接产出，模型无权参与） ----------
export interface AssistantStat {
  label: string;
  value: string | number;
  unit?: string;
  /** 仅用于配色：default 中性 / money 金额 / good 正常 / warn 待办 */
  tone?: 'default' | 'money' | 'good' | 'warn';
}

export interface AssistantChart {
  title: string;
  height?: number;
  /** 原生 ECharts option，直接交给 <ReactECharts option={...} /> */
  option: Record<string, any>;
}

export interface AssistantAction {
  seq: number;
  tool: string;
  args: Record<string, any>;
  risk: Risk;
  status: 'done' | 'failed' | 'need_confirm';
  brief: string;
  elapsed?: number;
  token?: string;
  preview?: string;
  /** 以下三项仅查询类（view=report）工具才有 */
  report?: string;
  stats?: AssistantStat[];
  charts?: AssistantChart[];
}

export type AssistantEvent =
  | {
      type: 'step';
      seq: number;
      tool: string;
      args: Record<string, any>;
      risk: Risk;
      status: 'running' | 'done' | 'failed';
      title?: string;
      brief?: string;
      elapsed?: number;
      report?: string;
      stats?: AssistantStat[];
      charts?: AssistantChart[];
    }
  | { type: 'confirm'; seq: number; tool: string; args: Record<string, any>;
      risk: Risk; preview: string; token: string }
  | { type: 'summary'; text: string; actions: AssistantAction[]; rounds: number; elapsed: number }
  | { type: 'notice'; content: string }
  | { type: 'error'; content: string }
  | { type: 'done'; elapsed: number; rounds?: number };

// ---------- SSE 读取 ----------
async function streamSse(
  path: string,
  body: any,
  onEvent: (e: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken();
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e: any) {
    if (e?.name === 'AbortError') throw e;
    throw new ApiError(`无法连接后端服务（${API_BASE}），请确认服务已启动`, 0);
  }

  if (res.status === 401) {
    clearToken();
    window.location.href = '/login';
    throw new ApiError('登录已过期，请重新登录', 401);
  }
  if (!res.ok) {
    let msg = `请求失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      msg = (typeof j?.detail === 'object' ? j?.detail?.msg : j?.detail) || j?.msg || msg;
    } catch {
      /* 非 JSON 响应 */
    }
    throw new ApiError(msg, res.status);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new ApiError('当前浏览器不支持流式响应，请更换浏览器', 0);

  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const payload = line.slice(6).trim();
      if (!payload) continue;
      try {
        onEvent(JSON.parse(payload) as AssistantEvent);
      } catch {
        /* 半包/非 JSON 行，忽略 */
      }
    }
  }
}

// ---------- 1) 一句话执行 ----------
export function runAssistant(
  message: string,
  allowDangerous: boolean,
  onEvent: (e: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse('/admin/assistant/run', { message, allow_dangerous: allowDangerous },
    onEvent, signal);
}

// ---------- 2) 确认卡续跑（只执行被确认的那一步） ----------
export function approveAssistant(
  token: string,
  allowDangerous: boolean,
  onEvent: (e: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse('/admin/assistant/approve', { token, allow_dangerous: allowDangerous },
    onEvent, signal);
}

// ---------- 快捷指令（覆盖商家端主要场景） ----------
export const QUICK_PROMPTS: { label: string; text: string }[] = [
  { label: '看今天经营数据', text: '看看今天的经营数据' },
  { label: '今日营收汇总', text: '看一下今天的营收汇总' },
  { label: '近7天营收', text: '看一下最近 7 天的营收汇总' },
  { label: '未核销消费单', text: '看看有哪些还没核销的消费单' },
  { label: '把未核销的核销掉', text: '把昨天还没核销的消费单全部核销掉' },
  { label: '统计知识库盲区', text: '统计一下最近用户没答上来的问题有哪些' },
  { label: '在售卡产品', text: '看看现在在售的卡产品都有哪些' },
  { label: '待认领失物', text: '有哪些还没被认领的失物' },
];

// ---------- 3) 失物照片直传（弹窗 📷 按钮） ----------
// 不经模型：直传 /admin/lost-found/items（name 留空），后端自己调视觉模型
// 识别 + 自动补名 + 照片落盘 + 特征入库，与「失物招领」页登记是同一条链路。
export interface LostPhotoAi {
  category?: string;
  color?: string;
  brand?: string;
  features?: string[];
  visible_text?: string;
}

export interface LostPhotoResult {
  id?: number;
  name?: string;
  photo_url?: string;
  ai?: LostPhotoAi;
}

export async function registerLostPhoto(file: File): Promise<LostPhotoResult> {
  const token = getToken();
  const fd = new FormData();
  fd.append('name', '');
  fd.append('photo', file);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/admin/lost-found/items`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      body: fd,
    });
  } catch {
    throw new ApiError(`无法连接后端服务（${API_BASE}），请确认服务已启动`, 0);
  }
  if (res.status === 401) {
    clearToken();
    window.location.href = '/login';
    throw new ApiError('登录已过期，请重新登录', 401);
  }
  const j = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(
      (typeof j?.detail === 'object' ? j?.detail?.msg : j?.detail) || j?.msg
        || `上传失败（HTTP ${res.status}）`,
      res.status,
    );
  }
  return (j?.data || {}) as LostPhotoResult;
}
