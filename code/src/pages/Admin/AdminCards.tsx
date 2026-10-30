// ============================================================
// 管理端 - 卡产品管理：增删改价 / 上下架
// cards 表是价格权威来源：AI 只推荐 card_id，金额以下单时此表为准
// 数据源: GET /pay/cards, (管理端 CRUD 直接走后端 cards 接口)
// ============================================================
import { useEffect, useState, type FormEvent } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface CardItem {
  card_id: number;
  name: string;
  price: number;      // 分
  price_yuan: number;
  description: string;
}

interface AdminCardItem extends CardItem {
  status: string;
  sort: number;
}

const emptyForm = { name: '', price_yuan: '', description: '', sort: '0' };

export default function AdminCards() {
  const [cards, setCards] = useState<AdminCardItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState(emptyForm);

  // 后端 /pay/cards 仅返回在售卡；管理端需要全部 → 用在售 + 提示（正式管理接口可扩展）
  // 这里直接封装后端 cards 的管理 CRUD 接口调用
  const load = async () => {
    setLoading(true);
    try {
      const res = await request<{ data: any[] }>('/admin/cards');
      setCards(res.data || []);
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setShowForm(true);
  };

  const openEdit = (c: AdminCardItem) => {
    setEditingId(c.card_id);
    setForm({
      name: c.name,
      price_yuan: String(c.price / 100),
      description: c.description || '',
      sort: String(c.sort ?? 0),
    });
    setShowForm(true);
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const priceFen = Math.round(parseFloat(form.price_yuan || '0') * 100);
    if (!form.name.trim()) return toast.error('请输入卡名称');
    if (isNaN(priceFen) || priceFen < 0) return toast.error('价格格式不正确');

    const payload = {
      name: form.name.trim(),
      price: priceFen,
      description: form.description.trim(),
      sort: parseInt(form.sort || '0', 10) || 0,
    };
    try {
      if (editingId) {
        await request(`/admin/cards/${editingId}`, { method: 'PUT', body: payload });
        toast.success('已保存');
      } else {
        await request('/admin/cards', { method: 'POST', body: payload });
        toast.success('已创建');
      }
      setShowForm(false);
      load();
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    }
  };

  const toggleStatus = async (c: AdminCardItem) => {
    const next = c.status === 'on_sale' ? 'off_sale' : 'on_sale';
    try {
      await request(`/admin/cards/${c.card_id}`, {
        method: 'PUT',
        body: { status: next },
      });
      toast.success(next === 'on_sale' ? '已上架' : '已下架');
      load();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    }
  };

  const handleDelete = async (c: AdminCardItem) => {
    if (!window.confirm(`确认删除卡产品「${c.name}」？历史订单不受影响。`)) return;
    try {
      await request(`/admin/cards/${c.card_id}`, { method: 'DELETE' });
      toast.success('已删除');
      load();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">💳 卡产品管理</h1>
          <p className="mt-1 text-xs text-gray-400">
            卡名称即 AI 回答中的办卡推荐匹配词；价格为下单权威来源（单位：元，存储为分）
          </p>
        </div>
        <button
          onClick={openCreate}
          className="rounded-lg bg-orange-500 px-4 py-2 text-sm font-bold text-white shadow transition hover:bg-orange-600"
        >
          ＋ 新建卡产品
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">卡名称</th>
              <th className="px-4 py-3">价格</th>
              <th className="px-4 py-3">权益说明</th>
              <th className="px-4 py-3">排序</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : cards.length === 0 ? (
              <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400">暂无卡产品，点右上角创建（AI 回答提到卡名后会自动推荐办理卡片）</td></tr>
            ) : (
              cards.map((c) => (
                <tr key={c.card_id} className="border-t hover:bg-gray-50/60">
                  <td className="px-4 py-3 font-medium text-gray-700">{c.name}</td>
                  <td className="px-4 py-3 font-bold text-orange-500">¥{c.price_yuan}</td>
                  <td className="max-w-[280px] truncate px-4 py-3 text-gray-500">{c.description || '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{c.sort}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                      c.status === 'on_sale' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                    }`}>
                      {c.status === 'on_sale' ? '在售' : '下架'}
                    </span>
                  </td>
                  <td className="space-x-3 px-4 py-3 text-right text-xs font-bold">
                    <button onClick={() => openEdit(c)} className="text-sky-600 hover:text-sky-700">编辑</button>
                    <button onClick={() => toggleStatus(c)} className="text-amber-600 hover:text-amber-700">
                      {c.status === 'on_sale' ? '下架' : '上架'}
                    </button>
                    <button onClick={() => handleDelete(c)} className="text-red-500 hover:text-red-600">删除</button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 新建/编辑对话框 */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setShowForm(false)}>
          <form
            className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            onSubmit={onSubmit}
          >
            <h2 className="mb-4 text-lg font-bold">{editingId ? '编辑卡产品' : '新建卡产品'}</h2>
            <div className="space-y-3 text-sm">
              <div>
                <label className="mb-1 block text-gray-500">卡名称（与知识库/AI 回答中的叫法一致）</label>
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="如：储值卡300"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 outline-none focus:border-orange-400"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-gray-500">价格（元）</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    value={form.price_yuan}
                    onChange={(e) => setForm({ ...form, price_yuan: e.target.value })}
                    placeholder="300.00"
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
              <div>
                <label className="mb-1 block text-gray-500">权益说明</label>
                <textarea
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  rows={3}
                  placeholder="如：充值300元送30元，全场通用"
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
