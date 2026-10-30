// ============================================================
// 管理端 - 用户管理：搜索 + 筛选 + 分页 + 持卡/余币汇总
// 数据源:
//   GET /auth/users?keyword=&only_bound=&page=&limit=  用户列表（含来源标签与重复手机号告警）
//   GET /admin/user-cards/summary                      持卡数/余币合计（含签到奖励）
// 展示约定:
//   「账号」列以昵称为主信息，用户名（ph_/wx_ 前缀）降级为小字——它只是内部标识，
//   换绑手机号后不会跟着更新，当成主信息会让运营误以为"同一个手机号有多个账号"。
//   手机号列出现重复时标红告警（唯一约束下正常不该发生，属兜底监控）。
// ============================================================
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

const LIMIT = 20;

interface UserItem {
  id: number;
  username: string;
  nickname: string;
  phone?: string;
  role: string;
  created_at: string;
  source?: string;   // 微信 / 手机号 / PC 端（后端派生）
}

interface UserCardSummary {
  cards_count: number;
  coins: number;
  nickname?: string;
  username?: string;
}

interface UsersResponse {
  users: UserItem[];
  total: number;
  dup_phones: string[];
}

function sourceClass(source?: string) {
  if (source === '微信') return 'bg-emerald-50 text-emerald-600';
  if (source === '手机号') return 'bg-blue-50 text-blue-600';
  return 'bg-gray-100 text-gray-500';
}

export default function AdminUsers() {
  const [users, setUsers] = useState<UserItem[]>([]);
  const [total, setTotal] = useState(0);
  const [dupPhones, setDupPhones] = useState<string[]>([]);
  const [summary, setSummary] = useState<Record<string, UserCardSummary>>({});
  const [loading, setLoading] = useState(true);

  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');
  const [onlyBound, setOnlyBound] = useState(false);
  const [page, setPage] = useState(1);

  // 编辑弹窗：打开时快照一份表单，取消不影响列表
  const [editing, setEditing] = useState<UserItem | null>(null);
  const [form, setForm] = useState({ nickname: '', phone: '', role: 'user' });
  const [saving, setSaving] = useState(false);

  const totalPages = Math.max(1, Math.ceil(total / LIMIT));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const query =
        `/auth/users?keyword=${encodeURIComponent(keyword)}` +
        `&only_bound=${onlyBound ? 'true' : 'false'}&page=${page}&limit=${LIMIT}`;
      const [usersRes, summaryRes] = await Promise.all([
        request<{ data: UsersResponse }>(query),
        request<{ data: Record<string, UserCardSummary> }>('/admin/user-cards/summary')
          .catch(() => ({ data: {} as Record<string, UserCardSummary> })),
      ]);
      const data = usersRes.data || ({} as UsersResponse);
      setUsers(data.users || []);
      setTotal(data.total || 0);
      setDupPhones(data.dup_phones || []);
      setSummary(summaryRes.data || {});
    } catch (e: any) {
      toast.error(e.message || '加载失败');
      setUsers([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [keyword, onlyBound, page]);

  useEffect(() => { load(); }, [load]);

  const submitSearch = () => {
    setPage(1);
    setKeyword(keywordInput.trim());
  };

  const resetSearch = () => {
    setKeywordInput('');
    setKeyword('');
    setOnlyBound(false);
    setPage(1);
  };

  // ---------- 修改 / 删除 ----------
  const openEdit = (u: UserItem) => {
    setEditing(u);
    setForm({ nickname: u.nickname || '', phone: u.phone || '', role: u.role || 'user' });
  };

  const handleSave = async () => {
    if (!editing) return;
    // 只提交真正改过的字段：手机号/角色属敏感项，没动过就不要重写一遍
    const body: Record<string, string> = {};
    if (form.nickname.trim() !== (editing.nickname || '')) body.nickname = form.nickname.trim();
    if (form.phone.trim() !== (editing.phone || '')) body.phone = form.phone.trim();
    if (form.role !== editing.role) body.role = form.role;
    if (Object.keys(body).length === 0) {
      toast.info('没有修改任何内容');
      return;
    }
    setSaving(true);
    try {
      const res = await request(`/admin/users/${editing.id}`, { method: 'PUT', body });
      toast.success((res as any).msg || '修改成功');
      setEditing(null);
      load();
    } catch (e: any) {
      toast.error(e.message || '修改失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (u: UserItem) => {
    const name = u.nickname || u.username || `#${u.id}`;
    const ok = window.confirm(
      `确认删除账号「${name}」（ID ${u.id}）？\n\n` +
      '该账号的问答记录、用户画像、订单与游园卡、消费记录、签到与收藏将一并删除，且不可恢复。',
    );
    if (!ok) return;
    try {
      const res = await request(`/admin/users/${u.id}`, { method: 'DELETE' });
      toast.success((res as any).msg || '已删除');
      load();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-gray-800">👥 用户管理</h1>
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submitSearch(); }}
            placeholder="手机号 / 昵称 / 用户名 / ID"
            className="w-56 rounded-lg border px-3 py-2 text-sm outline-none focus:border-blue-400"
          />
          <button
            onClick={submitSearch}
            className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            搜索
          </button>
          <button
            onClick={() => { setOnlyBound((v) => !v); setPage(1); }}
            className={`rounded-lg border px-3 py-2 text-sm font-medium ${
              onlyBound
                ? 'border-blue-200 bg-blue-50 text-blue-600'
                : 'border-gray-200 bg-white text-gray-500 hover:bg-gray-50'
            }`}
          >
            {onlyBound ? '✓ 只看已绑定' : '只看已绑定'}
          </button>
          <button
            onClick={resetSearch}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-500 hover:bg-gray-50"
          >
            重置
          </button>
        </div>
      </div>

      {dupPhones.length > 0 && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
          ⚠️ 检测到重复手机号：{dupPhones.join('、')} —— 正常情况下同一手机号只属于一个账号，
          请检查这几条记录（可能是历史脏数据或唯一约束未生效）。
        </div>
      )}

      <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">ID</th>
              <th className="px-4 py-3">账号</th>
              <th className="px-4 py-3">手机号</th>
              <th className="px-4 py-3">角色</th>
              <th className="px-4 py-3">持卡</th>
              <th className="px-4 py-3">余币</th>
              <th className="px-4 py-3">注册时间</th>
              <th className="px-4 py-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : users.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400">
                {keyword || onlyBound ? '没有符合条件的账号' : '暂无用户'}
              </td></tr>
            ) : (
              users.map((u) => {
                const s = summary[String(u.id)];
                const isDup = !!u.phone && dupPhones.includes(u.phone);
                return (
                  <tr key={u.id} className="border-t hover:bg-gray-50/60">
                    <td className="px-4 py-3 text-gray-400">{u.id}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-700">{u.nickname || '未设置昵称'}</span>
                        <span className={`rounded-full px-2 py-0.5 text-xs ${sourceClass(u.source)}`}>
                          {u.source || 'PC 端'}
                        </span>
                      </div>
                      <div className="mt-0.5 text-xs text-gray-400">{u.username}</div>
                    </td>
                    <td className={`px-4 py-3 ${isDup ? 'font-bold text-red-600' : 'text-gray-600'}`}>
                      {u.phone || '-'}
                      {isDup && <span className="ml-2 rounded-full bg-red-50 px-2 py-0.5 text-xs">重复</span>}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                        u.role === 'admin' ? 'bg-orange-100 text-orange-600' : 'bg-gray-100 text-gray-500'
                      }`}>
                        {u.role === 'admin' ? '管理员' : '用户'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-700">
                      {u.role === 'admin' ? '-' : `${s?.cards_count ?? 0} 张`}
                    </td>
                    <td className="px-4 py-3 font-bold text-amber-600">
                      {u.role === 'admin' ? '-' : `🪙 ${s?.coins ?? 0}`}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">{u.created_at || '-'}</td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2">
                        <button
                          onClick={() => openEdit(u)}
                          className="rounded-lg border border-blue-200 px-2.5 py-1 text-xs font-medium text-blue-600 hover:bg-blue-50"
                        >
                          修改
                        </button>
                        <button
                          onClick={() => handleDelete(u)}
                          className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50"
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>共 {total} 个账号 · 第 {page} / {totalPages} 页</span>
        <div className="flex gap-2">
          <button
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40"
          >
            上一页
          </button>
          <button
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      </div>

      <p className="text-xs text-gray-400">
        💡 「账号」列为昵称，小字是内部用户名（换绑手机号后不会更新，属正常现象）。
        微信登录会自动建号，未绑定手机号的游客账号可用「只看已绑定」过滤。
        持卡明细、退卡与余币调整请到「办卡账单」页按用户搜索。
      </p>

      {/* 修改用户：昵称 / 手机号 / 角色（改密码不在此处，用户走自助重置） */}
      {editing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          onClick={() => !saving && setEditing(null)}
        >
          <form
            className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => { e.preventDefault(); handleSave(); }}
          >
            <h2 className="mb-1 text-lg font-bold text-gray-800">修改用户</h2>
            <p className="mb-4 text-xs text-gray-400">
              ID {editing.id} · {editing.username || '无用户名'}（用户名是内部标识，不可修改）
            </p>
            <div className="space-y-3 text-sm">
              <div>
                <label className="mb-1 block text-gray-500">昵称</label>
                <input
                  value={form.nickname}
                  onChange={(e) => setForm({ ...form, nickname: e.target.value })}
                  placeholder="如：大黄鸭粉丝"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-blue-400"
                />
              </div>
              <div>
                <label className="mb-1 block text-gray-500">手机号（唯一，留空表示解绑）</label>
                <input
                  value={form.phone}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                  placeholder="11 位手机号；清空表示解绑"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-blue-400"
                />
              </div>
              <div>
                <label className="mb-1 block text-gray-500">角色</label>
                <select
                  value={form.role}
                  onChange={(e) => setForm({ ...form, role: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-blue-400"
                >
                  <option value="user">用户</option>
                  <option value="admin">管理员（可进管理后台）</option>
                </select>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEditing(null)}
                className="rounded-lg border px-4 py-2 text-sm"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={saving}
                className="rounded-lg bg-blue-600 px-5 py-2 text-sm font-bold text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
