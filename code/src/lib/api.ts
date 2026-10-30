// ============================================================
// 后端 API 统一请求封装
// - 自动携带 JWT（Authorization: Bearer）
// - 401 统一清除凭证并跳转登录页
// - 统一解析后端 {code, data, msg} 与 FastAPI detail 错误格式
// ============================================================
export const API_BASE: string =
  (import.meta as any).env?.VITE_API_BASE || `http://${window.location.hostname}:8000`;

const TOKEN_KEY = 'funpark_admin_token';

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  code?: number;

  constructor(message: string, status: number, code?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: any;
  /** 是否携带 token（默认 true） */
  auth?: boolean;
  /** 手动跳转登录前的回调（单测/特殊场景用） */
  onUnauthorized?: () => void;
}

export async function request<T = any>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = true, onUnauthorized } = options;

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (auth) {
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }

  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(`无法连接后端服务（${API_BASE}），请确认服务已启动`, 0);
  }

  // 会话失效：清凭证跳登录
  if (res.status === 401 && auth) {
    clearToken();
    if (onUnauthorized) onUnauthorized();
    else window.location.href = '/login';
    throw new ApiError('登录已过期，请重新登录', 401);
  }

  let json: any = {};
  try {
    json = await res.json();
  } catch {
    // 非 JSON 响应
  }

  if (!res.ok) {
    // FastAPI 错误: {detail: string} 或 {detail: {code, msg}}；业务错误: {code, msg}
    const detail = json?.detail;
    const msg =
      (typeof detail === 'object' ? detail?.msg : detail) ||
      json?.msg ||
      `请求失败（HTTP ${res.status}）`;
    const code = typeof detail === 'object' ? detail?.code : json?.code;
    throw new ApiError(msg, res.status, code);
  }

  return json as T;
}
