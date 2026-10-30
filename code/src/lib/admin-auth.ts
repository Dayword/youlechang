// ============================================================
// 管理端认证：对接后端 /auth/login，JWT + 用户信息本地持久化
// ============================================================
import { request, setToken, clearToken } from '@/lib/api';

const USER_KEY = 'funpark_admin_user';

export interface AdminUser {
  id: number;
  username: string;
  nickname: string;
  role: string;
  /** 邮箱（个人信息页可改） */
  email?: string;
  /** 头像地址（/static/avatars/xxx，需拼 API_BASE 展示） */
  avatar?: string;
}

export function getAdminUser(): AdminUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AdminUser;
  } catch {
    return null;
  }
}

export function setAdminUser(user: AdminUser): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

/** 局部更新本地缓存的用户信息（头像/昵称/邮箱改完后同步侧栏展示） */
export function patchAdminUser(partial: Partial<AdminUser>): AdminUser | null {
  const cur = getAdminUser();
  if (!cur) return null;
  const next = { ...cur, ...partial };
  setAdminUser(next);
  return next;
}

export function clearAdminSession(): void {
  localStorage.removeItem(USER_KEY);
  clearToken();
}

export function isAdmin(): boolean {
  return getAdminUser()?.role === 'admin';
}

/**
 * 管理员登录：调用后端 /auth/login
 * 非 admin 角色抛错（管理端全部接口都要求管理员权限）
 */
export async function loginAdmin(
  username: string,
  password: string,
): Promise<AdminUser> {
  const res = await request<{ code: number; data: { token: string; user: AdminUser } }>(
    '/auth/login',
    { method: 'POST', body: { username, password }, auth: false },
  );
  const { token, user } = res.data;
  if (user.role !== 'admin') {
    throw new Error('该账号不是管理员，无法进入管理后台');
  }
  setToken(token);
  setAdminUser(user);
  return user;
}

/**
 * 用后端最新数据刷新本地用户缓存。
 * 登录响应里已经带上了基础字段，但头像/邮箱是登录后可能被改的，
 * 进入后台时刷一次，侧栏头像才不会一直是旧的（或空的）。
 */
export async function refreshAdminUser(): Promise<AdminUser | null> {
  try {
    const res = await request<{ code: number; data: AdminUser }>('/auth/me');
    setAdminUser(res.data);
    return res.data;
  } catch {
    return getAdminUser();
  }
}

export function logoutAdmin(): void {
  clearAdminSession();
  window.location.href = '/login';
}
