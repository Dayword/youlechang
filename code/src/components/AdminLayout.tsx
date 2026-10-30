// ============================================================
// 管理端布局：左侧导航 + 管理员角色守卫 + Outlet
// 侧栏底部 = 当前账号（头像+昵称），点击进入「个人信息」
// ============================================================
import { useEffect, useState } from 'react';
import { Outlet, Navigate, useLocation, Link } from 'react-router-dom';
import { getAdminUser, isAdmin, logoutAdmin, refreshAdminUser, type AdminUser } from '@/lib/admin-auth';
import { API_BASE } from '@/lib/api';
import AdminAssistant from '@/components/AdminAssistant';

const MENUS = [
  { path: '/admin', label: '仪表盘', icon: '📊', exact: true },
  { path: '/admin/knowledge', label: '知识库管理', icon: '📚' },
  { path: '/admin/kb-agent', label: '知识库智能体', icon: '🤖' },
  { path: '/admin/questions', label: '问答记录', icon: '💬' },
  { path: '/admin/analytics', label: '问答分析', icon: '📊' },
  { path: '/admin/cards', label: '卡产品管理', icon: '💳' },
  { path: '/admin/orders', label: '办卡账单', icon: '🧾' },
  { path: '/admin/lost-found', label: '失物招领', icon: '🧸' },
  { path: '/admin/content', label: '内容管理', icon: '📢' },
  { path: '/admin/config', label: 'Agent 配置', icon: '⚙️' },
  { path: '/admin/users', label: '用户管理', icon: '👥' },
];

export default function AdminLayout() {
  const [user, setUser] = useState<AdminUser | null>(getAdminUser);
  const location = useLocation();

  // 进入后台后刷一次最新资料（头像/邮箱可能在个人信息页改过）
  useEffect(() => {
    if (!isAdmin()) return;
    refreshAdminUser().then((u) => {
      if (u) setUser(u);
    });
  }, []);

  // 未登录 → 登录页；非管理员 → 拒绝
  if (!user || !isAdmin()) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  const isActive = (m: (typeof MENUS)[number]) =>
    m.exact ? location.pathname === m.path : location.pathname.startsWith(m.path);

  const onProfile = location.pathname.startsWith('/admin/profile');
  const avatarUrl = user.avatar
    ? user.avatar.startsWith('http')
      ? user.avatar
      : API_BASE + user.avatar
    : '';

  return (
    <div className="flex min-h-screen bg-[#F5F6FA]">
      {/* 侧边栏 */}
      <aside className="fixed inset-y-0 left-0 z-40 flex w-56 flex-col bg-[#26334D] text-white">
        <div className="flex items-center gap-2 px-5 py-5 text-lg font-bold">
          <span className="text-2xl">🎡</span>
          <span>游乐园运营后台</span>
        </div>

        <nav className="mt-2 flex-1 space-y-1 overflow-y-auto px-3 pb-2">
          {MENUS.map((m) => (
            <Link
              key={m.path}
              to={m.path}
              className={`flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm transition ${
                isActive(m)
                  ? 'bg-[#3B4A6B] font-bold text-white'
                  : 'text-[#AEB8CC] hover:bg-[#303D59] hover:text-white'
              }`}
            >
              <span className="text-base">{m.icon}</span>
              {m.label}
            </Link>
          ))}
        </nav>

        {/* 底部：当前账号（点击进入个人信息） */}
        <div className="border-t border-[#33405E] px-3 py-3">
          <Link
            to="/admin/profile"
            className={`flex items-center gap-2.5 rounded-lg px-2 py-2 transition ${
              onProfile ? 'bg-[#3B4A6B]' : 'hover:bg-[#303D59]'
            }`}
            title="个人信息"
          >
            {avatarUrl ? (
              <img
                src={avatarUrl}
                alt="头像"
                className="h-8 w-8 shrink-0 rounded-full border border-[#4A5A7D] object-cover"
              />
            ) : (
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#4A5A7D] text-xs font-bold text-white">
                {(user.nickname || user.username || 'A').slice(0, 1).toUpperCase()}
              </span>
            )}
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs text-white">
                {user.nickname || user.username}
              </span>
              <span className="block truncate text-[11px] text-[#8B97B0]">
                {user.email || '个人信息'}
              </span>
            </span>
          </Link>

          <button
            onClick={logoutAdmin}
            className="mt-1 w-full rounded-lg px-2 py-1.5 text-left text-[11px] text-[#AEB8CC] transition hover:text-white"
          >
            ⏻ 退出登录
          </button>
        </div>
      </aside>

      {/* 主内容区 */}
      <main className="ml-56 flex-1 p-6">
        <Outlet />
      </main>

      {/* 右下角经营小助手（全局可用：说一句话直接办完再汇报） */}
      <AdminAssistant />
    </div>
  );
}
