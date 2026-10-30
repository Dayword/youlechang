import { Outlet, Navigate, useLocation } from 'react-router-dom';
import { getAdminUser } from '@/lib/admin-auth';
import { getToken } from '@/lib/api';

// 公开路由（无需登录）
const PUBLIC_PATHS = ['/login'];

function AuthLayout() {
  const location = useLocation();
  // 管理端认证：token + 用户信息同时存在视为已登录
  const currentUser = getToken() ? getAdminUser() : null;

  const isPublic = PUBLIC_PATHS.some(
    (p) => location.pathname === p || location.pathname.startsWith(p + '/'),
  );

  // 未登录且访问非公开页 → 跳登录
  if (!currentUser && !isPublic) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname }} />
    );
  }

  // 已登录且访问登录页 → 跳管理后台
  if (currentUser && isPublic) {
    return <Navigate to="/admin" replace />;
  }

  return <Outlet />;
}

export default AuthLayout;
