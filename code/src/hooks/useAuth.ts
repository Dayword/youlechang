import { useEffect, useState } from 'react';
import { getCurrentUser, clearCurrentUser } from '@/lib/auth';
import type { ICurrentUser } from '@/data/user';

export function useAuth() {
  const [currentUser, setCurrentUser] = useState<ICurrentUser | null>(() =>
    getCurrentUser(),
  );
  const [isReady, setIsReady] = useState(true);

  const refresh = () => {
    setCurrentUser(getCurrentUser());
  };

  const logout = () => {
    clearCurrentUser();
    setCurrentUser(null);
  };

  // 监听其他 tab / 同页不同组件的 storage 变化
  useEffect(() => {
    const handler = () => refresh();
    window.addEventListener('storage', handler);
    return () => window.removeEventListener('storage', handler);
  }, []);

  return { currentUser, isReady, refresh, logout };
}
