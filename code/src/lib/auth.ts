// EXPORTS: USERS_KEY, CURRENT_USER_KEY, getUsers, setUsers, addUser, getCurrentUser, setCurrentUser, clearCurrentUser, login, register, updateUser, validateEmail
import { scopedStorage } from '@lark-apaas/client-toolkit-lite';
import { MOCK_USERS } from '@/data/user';
import type { IUser, ICurrentUser } from '@/data/user';

const USERS_KEY = 'funpark_users';
const CURRENT_USER_KEY = 'funpark_currentUser';

function getUsers(): IUser[] {
  try {
    const raw = scopedStorage.getItem(USERS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function setUsers(users: IUser[]): void {
  scopedStorage.setItem(USERS_KEY, JSON.stringify(users));
}

function addUser(user: IUser): void {
  const users = getUsers();
  users.push(user);
  setUsers(users);
}

function getCurrentUser(): ICurrentUser | null {
  try {
    const raw = scopedStorage.getItem(CURRENT_USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as ICurrentUser;
  } catch {
    return null;
  }
}

function setCurrentUser(user: ICurrentUser): void {
  scopedStorage.setItem(CURRENT_USER_KEY, JSON.stringify(user));
}

function clearCurrentUser(): void {
  scopedStorage.removeItem(CURRENT_USER_KEY);
}

function validateEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function login(account: string, password: string): ICurrentUser | null {
  const users = getUsers();
  const found = users.find(
    (u) =>
      (u.username === account || u.email === account) &&
      u.password === password,
  );
  if (!found) return null;
  const currentUser: ICurrentUser = {
    id: found.id,
    username: found.username,
    email: found.email,
    createdAt: found.createdAt,
  };
  setCurrentUser(currentUser);
  return currentUser;
}

function register(params: {
  username: string;
  email: string;
  password: string;
}): {
  success: boolean;
  error?: string;
  user?: ICurrentUser;
} {
  const users = getUsers();
  if (users.some((u) => u.username === params.username)) {
    return { success: false, error: '用户名已被注册' };
  }
  if (users.some((u) => u.email === params.email)) {
    return { success: false, error: '邮箱已被注册' };
  }
  const newUser: IUser = {
    id: Date.now().toString(),
    username: params.username,
    email: params.email,
    password: params.password,
    createdAt: new Date().toISOString(),
  };
  addUser(newUser);
  const currentUser: ICurrentUser = {
    id: newUser.id,
    username: newUser.username,
    email: newUser.email,
    createdAt: newUser.createdAt,
  };
  setCurrentUser(currentUser);
  return { success: true, user: currentUser };
}

function updateUser(
  userId: string,
  updates: Partial<Pick<IUser, 'username' | 'email'>>,
): ICurrentUser | null {
  const users = getUsers();
  const idx = users.findIndex((u) => u.id === userId);
  if (idx === -1) return null;
  // 检查用户名/邮箱是否与其他人冲突
  if (updates.username) {
    const conflict = users.some(
      (u) => u.username === updates.username && u.id !== userId,
    );
    if (conflict) return null;
  }
  if (updates.email) {
    const conflict = users.some(
      (u) => u.email === updates.email && u.id !== userId,
    );
    if (conflict) return null;
  }
  users[idx] = { ...users[idx], ...updates };
  setUsers(users);
  const currentUser: ICurrentUser = {
    id: users[idx].id,
    username: users[idx].username,
    email: users[idx].email,
    createdAt: users[idx].createdAt,
  };
  setCurrentUser(currentUser);
  return currentUser;
}

const INIT_FLAG_KEY = 'funpark_init_done';

function initMockUsers(): void {
  try {
    const initialized = scopedStorage.getItem(INIT_FLAG_KEY);
    if (initialized) return;
    const existing = getUsers();
    if (existing.length === 0) {
      setUsers(MOCK_USERS);
    }
    scopedStorage.setItem(INIT_FLAG_KEY, '1');
  } catch {
    // ignore
  }
}

// 模块加载时自动初始化一次 mock 数据，方便演示
initMockUsers();

export {
  USERS_KEY,
  CURRENT_USER_KEY,
  getUsers,
  setUsers,
  addUser,
  getCurrentUser,
  setCurrentUser,
  clearCurrentUser,
  login,
  register,
  updateUser,
  validateEmail,
  initMockUsers,
};
