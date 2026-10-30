// ============================================================
// 知识库智能体前端 API
// - analyze / ingest 走 SSE（fetch + ReadableStream，POST 无法用原生 EventSource）
// - 下载生成的 Word 需带 Authorization，故用 fetch → blob → a[download]
// ============================================================
import { API_BASE, ApiError, getToken, clearToken } from '@/lib/api';

// ---------- 事件类型（与后端 services/kb_agent.py 的 yield 一一对应） ----------
export interface KbStep {
  stage: string;              // parse / collect / answer / docx / index
  status: 'running' | 'done' | 'failed';
  title: string;
  detail?: string;
}

export interface KbGapItem {
  question: string;
  count: number;
  last_seen?: string;
  /** 原回答类型：fallback_none（无资料建议）/ fallback_partial（资料不足建议） */
  fallback_type?: string;
  fallback_label?: string;
  /** 原兜底回答的开头（便于人工判断该补什么） */
  answer_head?: string;
}

export interface KbAnswerItem {
  index: number;
  total: number;
  question: string;
  answer: string;
  sources: string[];
  needs_review: boolean;
  has_context: boolean;
  tokens: number;
  truncated: boolean;
  count: number;
}

export interface KbArtifact {
  kind: 'docx' | 'kb_doc' | string;
  name: string;
  url: string;
  size: number;
  items: number;
}

export type KbEvent =
  | { type: 'intent'; intent: string; reason?: string; days?: number; limit?: number;
      max_tokens?: number; generate?: boolean; export?: boolean;
      doc_name?: string; size?: number; replace_source?: string }
  | ({ type: 'step' } & KbStep)
  | { type: 'dataset'; title: string; items: KbGapItem[] }
  | { type: 'progress'; stage: string; current: number; total: number; label?: string }
  | ({ type: 'answer' } & KbAnswerItem)
  | ({ type: 'artifact' } & KbArtifact)
  | { type: 'assistant'; content: string }
  | { type: 'done'; elapsed: number; summary?: Record<string, any>; stats?: Record<string, any> }
  | { type: 'error'; content: string };

// ---------- SSE 读取 ----------
async function streamSse(
  path: string,
  init: RequestInit,
  onEvent: (e: KbEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken();
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      ...init,
      headers: {
        ...(init.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
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
    } catch { /* 非 JSON 响应 */ }
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
        onEvent(JSON.parse(payload) as KbEvent);
      } catch { /* 半包/非 JSON 行，忽略 */ }
    }
  }
}

// ---------- 1) 统计 → 标准答案 → Word ----------
export interface AnalyzePayload {
  message?: string;
  days?: number;
  limit?: number;
  max_tokens?: number;
  generate?: boolean;
  export?: boolean;
}

export function analyzeGaps(
  payload: AnalyzePayload,
  onEvent: (e: KbEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse(
    '/admin/kb-agent/analyze',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
    onEvent,
    signal,
  );
}

// ---------- 2) 回传 Word → 解析/切片/入库 ----------
export function ingestDocs(
  file: File,
  replaceSource: string,
  onEvent: (e: KbEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData();
  form.append('file', file);
  if (replaceSource) form.append('replace_source', replaceSource);
  // 注意：FormData 由浏览器自动带 boundary，绝不能手动设 Content-Type
  return streamSse('/admin/kb-agent/ingest', { method: 'POST', body: form }, onEvent, signal);
}

// ---------- 3) 下载 / 历史文档 ----------
export async function downloadDoc(name: string): Promise<void> {
  const token = getToken();
  const res = await fetch(`${API_BASE}/admin/kb-agent/download/${encodeURIComponent(name)}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new ApiError('下载失败，文件可能已被清理', res.status);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export interface KbDocItem {
  name: string;
  size: number;
  modified: number;
  url: string;
}

export async function listDocs(): Promise<KbDocItem[]> {
  const token = getToken();
  const res = await fetch(`${API_BASE}/admin/kb-agent/docs`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) return [];
  const j = await res.json().catch(() => null);
  return (j?.data as KbDocItem[]) || [];
}

// ---------- 快捷指令 ----------
export const QUICK_PROMPTS: { label: string; text: string }[] = [
  {
    label: '统计全部未命中问题',
    text: '帮我统计所有未检索到知识库的用户问题，统计完后，基于知识库进行精细化回答，token 保持在 300 以内，并生成文档，供我进行修改。',
  },
  {
    label: '近 7 天 · 前 10 条',
    text: '统计近 7 天未检索到知识库的问题，取前 10 个，基于知识库生成标准答案（token 300 以内）并生成 Word 文档。',
  },
  {
    label: '不限时间 · 前 30 条',
    text: '统计所有未检索到知识库的用户问题，最多处理 30 个，逐个基于知识库生成精细化标准答案，每条不超过 300 token，最后生成文档。',
  },
  {
    label: '只统计不生成文档',
    text: '只帮我统计所有未检索到知识库的用户问题，先不生成答案和文档。',
  },
];
