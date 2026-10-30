// ============================================================
// 管理端 - 内容管理：园区公告 / 今日热点 发布维护 + 游客反馈查看
// 公告与热点是小程序首页两个信息卡的唯一数据来源（商家发布 → 小程序只读展示）
// 数据源: GET/POST/PUT/DELETE /admin/notices, GET /admin/notices/feedback
// ============================================================
import { useEffect, useState, type FormEvent } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

type NoticeKind = 'announcement' | 'hot';

interface NoticeItem {
  id: number;
  kind: NoticeKind;
  title: string;
  content: string;
  status: string;   // on 上架 / off 下架
  sort: number;
  created_at: string;
}

interface FeedbackItem {
  id: number;
  nickname: string;
  content: string;
  created_at: string;
}

const KIND_LABEL: Record<NoticeKind, string> = {
  announcement: '园区公告',
  hot: '今日热点',
};

const emptyForm = { title: '', content: '', sort: '0' };

export default function AdminContent() {
  const [tab, setTab] = useState<NoticeKind | 'feedback'>('announcement');
  const [items, setItems] = useState<NoticeItem[]>([]);
  const [feedback, setFeedback] = useState<FeedbackItem[]>([]);
  const [feedbackTotal, setFeedbackTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState(emptyForm);

  const loadNotices = async () => {
    setLoading(true);
    try {
      const res = await request<{ data: { items: NoticeItem[] } }>(
        `/admin/notices?kind=${tab}&page_size=200`,
      );
      setItems(res.data?.items || []);
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  const loadFeedback = async () => {
    setLoading(true);
    try {
      const res = await request<{ data: { items: FeedbackItem[]; total: number } }>(
        '/admin/notices/feedback?page_size=100',
      );
      setFeedback(res.data?.items || []);
      setFeedbackTotal(res.data?.total || 0);
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (tab === 'feedback') loadFeedback();
    else loadNotices();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setShowForm(true);
  };

  const openEdit = (n: NoticeItem) => {
    setEditingId(n.id);
    setForm({ title: n.title, content: n.content, sort: String(n.sort ?? 0) });
    setShowForm(true);
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!form.title.trim()) return toast.error('请输入标题');

    const payload = {
      kind: tab as NoticeKind,
      title: form.title.trim(),
      content: form.content.trim(),
      sort: parseInt(form.sort || '0', 10) || 0,
    };
    try {
      if (editingId) {
        await request(`/admin/notices/${editingId}`, { method: 'PUT', body: payload });
        toast.success('已保存');
      } else {
        await request('/admin/notices', { method: 'POST', body: payload });
        toast.success('已发布，小程序首页立即可见');
      }
      setShowForm(false);
      loadNotices();
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    }
  };

  const toggleStatus = async (n: NoticeItem) => {
    const next = n.status === 'on' ? 'off' : 'on';
    try {
      await request(`/admin/notices/${n.id}`, {
        method: 'PUT',
        body: { kind: n.kind, title: n.title, content: n.content, sort: n.sort, status: next },
      });
      toast.success(next === 'on' ? '已上架' : '已下架');
      loadNotices();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    }
  };

  const handleDelete = async (n: NoticeItem) => {
    if (!window.confirm(`确认删除${KIND_LABEL[n.kind]}「${n.title}」？`)) return;
    try {
      await request(`/admin/notices/${n.id}`, { method: 'DELETE' });
      toast.success('已删除');
      loadNotices();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    }
  };

  const tabBtn = (key: NoticeKind | 'feedback', label: string) => (
    <button
      key={key}
      onClick={() => setTab(key)}
      className={`rounded-lg px-4 py-2 text-sm font-bold transition ${
        tab === key ? 'bg-orange-500 text-white shadow' : 'bg-white text-gray-500 hover:text-orange-500'
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">📢 内容管理</h1>
          <p className="mt-1 text-xs text-gray-400">
            园区公告 / 今日热点发布后，小程序首页信息卡实时展示；游客反馈来自小程序「意见反馈」
          </p>
        </div>
        {tab !== 'feedback' && (
          <button
            onClick={openCreate}
            className="rounded-lg bg-orange-500 px-4 py-2 text-sm font-bold text-white shadow transition hover:bg-orange-600"
          >
            ＋ 发布{KIND_LABEL[tab as NoticeKind]}
          </button>
        )}
      </div>

      <div className="flex gap-2">
        {tabBtn('announcement', '🏛️ 园区公告')}
        {tabBtn('hot', '🔥 今日热点')}
        {tabBtn('feedback', `💬 游客反馈${feedbackTotal ? ` (${feedbackTotal})` : ''}`)}
      </div>

      {/* 公告 / 热点列表 */}
      {tab !== 'feedback' && (
        <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500">
              <tr>
                <th className="px-4 py-3">标题</th>
                <th className="px-4 py-3">内容</th>
                <th className="px-4 py-3">排序</th>
                <th className="px-4 py-3">状态</th>
                <th className="px-4 py-3">发布时间</th>
                <th className="px-4 py-3 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-10 text-center text-gray-400">
                    暂无{KIND_LABEL[tab as NoticeKind]}内容，点右上角发布（小程序首页「{KIND_LABEL[tab as NoticeKind]}」卡片将展示最新一条）
                  </td>
                </tr>
              ) : (
                items.map((n) => (
                  <tr key={n.id} className="border-t hover:bg-gray-50/60">
                    <td className="px-4 py-3 font-medium text-gray-700">{n.title}</td>
                    <td className="max-w-[300px] truncate px-4 py-3 text-gray-500">{n.content || '-'}</td>
                    <td className="px-4 py-3 text-gray-500">{n.sort}</td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                        n.status === 'on' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                      }`}>
                        {n.status === 'on' ? '上架中' : '已下架'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-400">{n.created_at || '-'}</td>
                    <td className="space-x-3 px-4 py-3 text-right text-xs font-bold">
                      <button onClick={() => openEdit(n)} className="text-sky-600 hover:text-sky-700">编辑</button>
                      <button onClick={() => toggleStatus(n)} className="text-amber-600 hover:text-amber-700">
                        {n.status === 'on' ? '下架' : '上架'}
                      </button>
                      <button onClick={() => handleDelete(n)} className="text-red-500 hover:text-red-600">删除</button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 游客反馈（只读） */}
      {tab === 'feedback' && (
        <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500">
              <tr>
                <th className="px-4 py-3">游客</th>
                <th className="px-4 py-3">反馈内容</th>
                <th className="px-4 py-3">提交时间</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
              ) : feedback.length === 0 ? (
                <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400">暂无游客反馈</td></tr>
              ) : (
                feedback.map((f) => (
                  <tr key={f.id} className="border-t hover:bg-gray-50/60">
                    <td className="whitespace-nowrap px-4 py-3 font-medium text-gray-700">{f.nickname || '匿名游客'}</td>
                    <td className="px-4 py-3 text-gray-600">{f.content}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-gray-400">{f.created_at || '-'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 新建/编辑对话框 */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setShowForm(false)}>
          <form
            className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            onSubmit={onSubmit}
          >
            <h2 className="mb-4 text-lg font-bold">
              {editingId ? `编辑${KIND_LABEL[tab as NoticeKind]}` : `发布${KIND_LABEL[tab as NoticeKind]}`}
            </h2>
            <div className="space-y-3 text-sm">
              <div>
                <label className="mb-1 block text-gray-500">标题（小程序卡片首行展示）</label>
                <input
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  placeholder={tab === 'hot' ? '如：今日拾取钥匙一串' : '如：今日正常开园'}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-orange-400"
                />
              </div>
              <div>
                <label className="mb-1 block text-gray-500">正文内容（点击卡片弹窗展示全文）</label>
                <textarea
                  value={form.content}
                  onChange={(e) => setForm({ ...form, content: e.target.value })}
                  rows={3}
                  placeholder="详细内容，游客点击首页卡片后在此查看"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-orange-400"
                />
              </div>
              <div>
                <label className="mb-1 block text-gray-500">排序（越小越靠前）</label>
                <input
                  type="number"
                  value={form.sort}
                  onChange={(e) => setForm({ ...form, sort: e.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-orange-400"
                />
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" onClick={() => setShowForm(false)} className="rounded-lg border px-4 py-2 text-sm">
                取消
              </button>
              <button type="submit" className="rounded-lg bg-orange-500 px-5 py-2 text-sm font-bold text-white hover:bg-orange-600">
                保存
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
