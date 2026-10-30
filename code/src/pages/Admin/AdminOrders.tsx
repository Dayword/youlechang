// ============================================================
// 管理端 - 办卡账单：用户充值订单账单 + 退卡 + 余币调整
// 数据源:
//   GET  /admin/orders                 统计 + 分页账单
//   POST /admin/orders/{no}/refund     退卡退款（订单+持卡失效，余币扣减）
//   PUT  /admin/user-cards/{id}/coins  调整持卡余币（线下补偿/纠错）
// ============================================================
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface OrderStats {
  paid_amount: number;        // 分
  refunded_amount: number;    // 分
  today_paid_amount: number;  // 分
  paid_count: number;
  refunded_count: number;
  pending_count: number;
}

interface OrderItem {
  order_no: string;
  user_id: number | null;
  username: string;
  nickname: string;
  card_name: string;
  amount_yuan: number;
  coins: number;
  uc_id: number | null;
  uc_status: string;
  status: string;   // paid / refunded / pending
  paid_at: string;
  created_at: string;
}

interface OrdersResp {
  data: {
    stats: OrderStats;
    total: number;
    page: number;
    pages: number;
    items: OrderItem[];
  };
}

interface ConsumptionPass {
  item_code: string;
  project_name: string;
  unit_coins: number;
  status: string;   // unused 未核销 / used 已核销 / refunded 已退款
  used_at: string;
}

interface ConsumptionItem {
  order_no: string;
  verify_code: string;
  nickname: string;
  phone: string;
  items: { name: string; qty: number; coins: number; unit_desc?: string }[];
  card_coins: number;
  bonus_coins: number;
  total_coins: number;
  status: string;   // unused 未核销 / used 已核销 / refunded 已退款
  pass_total: number;
  pass_unused: number;
  // 券包明细（一券 = 一项一次）：弹窗逐张列出、逐张核销/退款用
  pass_items: ConsumptionPass[];
  created_at: string;
}

const STATUS_TABS = [
  { value: '', label: '全部' },
  { value: 'paid', label: '已支付' },
  { value: 'refunded', label: '已退卡' },
  { value: 'pending', label: '待支付' },
];

export default function AdminOrders() {
  const [stats, setStats] = useState<OrderStats | null>(null);
  const [items, setItems] = useState<OrderItem[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [refundNo, setRefundNo] = useState<string | null>(null);   // 退卡二次确认
  const [editing, setEditing] = useState<{ ucId: number; coins: string } | null>(null); // 调币行

  // 消费记录视图（游乐消费：多选项目合并扣币 → 核销码）
  // 支持 ?view=consumptions 直达（仪表盘「待核销消费单」一点即到）
  const [view, setView] = useState<'orders' | 'consumptions'>(
    new URLSearchParams(window.location.search).get('view') === 'consumptions'
      ? 'consumptions'
      : 'orders',
  );
  const [consItems, setConsItems] = useState<ConsumptionItem[]>([]);
  const [consTotal, setConsTotal] = useState(0);
  const [consPage, setConsPage] = useState(1);
  const [consPages, setConsPages] = useState(1);
  const [consLoading, setConsLoading] = useState(false);
  const [verifyInput, setVerifyInput] = useState('');
  const [verifying, setVerifying] = useState(false);
  // 「单独核销」弹窗：存单号而不是存对象，列表刷新后弹窗内容自动跟着更新
  const [passOrderNo, setPassOrderNo] = useState<string | null>(null);
  const [passSel, setPassSel] = useState<Record<string, boolean>>({});
  const [passBusy, setPassBusy] = useState(false);
  // 退款二次确认（不可逆）：把要执行的动作直接挂在确认框上，按钮只负责触发
  const [refundConfirm, setRefundConfirm] = useState<{ desc: string; run: () => Promise<void> } | null>(null);

  const load = useCallback(async (p = page, s = status, kw = keyword) => {
    setLoading(true);
    try {
      const res = await request<OrdersResp>(
        `/admin/orders?page=${p}&page_size=20&status=${encodeURIComponent(s)}&keyword=${encodeURIComponent(kw)}`
      );
      setStats(res.data.stats);
      setItems(res.data.items || []);
      setTotal(res.data.total);
      setPages(Math.max(1, res.data.pages));
      setPage(res.data.page);
    } catch (e: any) {
      toast.error(e.message || '加载账单失败');
    } finally {
      setLoading(false);
    }
  }, [page, status, keyword]);

  useEffect(() => {
    load(1, status, keyword);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const handleRefund = async (orderNo: string) => {
    try {
      const res = await request(`/admin/orders/${orderNo}/refund`, {
        method: 'POST',
        body: { reason: '商家后台退卡' },
      });
      toast.success((res as any).msg || '退卡成功');
      setRefundNo(null);
      load();
    } catch (e: any) {
      toast.error(e.message || '退卡失败');
    }
  };

  const loadConsumptions = useCallback(async (p = consPage) => {
    setConsLoading(true);
    try {
      const res = await request<{ data: { total: number; page: number; pages: number; items: ConsumptionItem[] } }>(
        `/admin/consumptions?page=${p}&page_size=20`
      );
      setConsItems(res.data.items || []);
      setConsTotal(res.data.total || 0);
      setConsPage(res.data.page || 1);
      setConsPages(Math.max(1, res.data.pages || 1));
    } catch (e: any) {
      toast.error(e.message || '加载消费记录失败');
    } finally {
      setConsLoading(false);
    }
  }, [consPage]);

  useEffect(() => {
    if (view === 'consumptions') loadConsumptions(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // 扫码/输码核销：券包单扫一张只核销这一张；无券的历史单才退回整单核销
  const handleVerifyByCode = async () => {
    const code = verifyInput.trim().toUpperCase();
    if (!code) { toast.error('请输入核销码'); return; }
    if (verifying) return;
    setVerifying(true);
    try {
      const res = await request<{ data: { kind?: string; order_no: string; item_code?: string; project_name?: string } }>(
        `/admin/consumptions/by-code/${encodeURIComponent(code)}`
      );
      const d = res.data || ({} as any);
      if (d.kind === 'item' && d.item_code) {
        // 券包订单：只核销扫到的这一张，同单其它券不动
        const vres = await request(`/admin/consumptions/items/${encodeURIComponent(d.item_code)}/verify`, { method: 'POST' });
        toast.success((vres as any).msg || `已核销：${d.project_name || ''} ×1`);
      } else {
        // 迁移前的历史单没有券行，只能整单核销
        const vres = await request(`/admin/consumptions/${encodeURIComponent(d.order_no)}/verify`, { method: 'PUT' });
        toast.success((vres as any).msg || '核销成功');
      }
      setVerifyInput('');
      loadConsumptions();
    } catch (e: any) {
      toast.error(e.message || '核销失败');
    } finally {
      setVerifying(false);
    }
  };

  const handleVerifyRow = async (orderNo: string) => {
    try {
      const res = await request(`/admin/consumptions/${orderNo}/verify`, { method: 'PUT' });
      toast.success((res as any).msg || '核销成功');
      loadConsumptions();
    } catch (e: any) {
      toast.error(e.message || '核销失败');
    }
  };

  // ---------- 券级操作（弹窗内：逐张 / 批量） ----------
  const openPassDialog = (c: ConsumptionItem) => {
    const sel: Record<string, boolean> = {};
    // 默认只勾「未核销」的：员工多数场景就是把还没玩的那几张核销掉
    (c.pass_items || []).forEach((p) => { if (p.status === 'unused') sel[p.item_code] = true; });
    setPassSel(sel);
    setPassOrderNo(c.order_no);
  };

  const doVerifyCodes = async (codes: string[]) => {
    if (!codes.length) { toast.error('请先勾选要核销的券'); return; }
    setPassBusy(true);
    try {
      const res = await request<{ msg: string }>('/admin/consumptions/items/verify', {
        method: 'POST',
        body: { codes },
      });
      toast.success(res.msg || '核销成功');
      await loadConsumptions();
    } catch (e: any) {
      toast.error(e.message || '核销失败');
      await loadConsumptions();
    } finally {
      setPassBusy(false);
    }
  };

  const doRefundCodes = async (codes: string[]) => {
    setRefundConfirm(null);
    setPassBusy(true);
    try {
      const res = await request<{ msg: string }>('/admin/consumptions/items/refund', {
        method: 'POST',
        body: { codes },
      });
      toast.success(res.msg || '退款成功');
      await loadConsumptions();
    } catch (e: any) {
      toast.error(e.message || '退款失败');
      await loadConsumptions();
    } finally {
      setPassBusy(false);
    }
  };

  // 无券的历史单只能走整单退款（该单全部扣币退回）
  const doRefundOrder = async (orderNo: string) => {
    setRefundConfirm(null);
    setPassBusy(true);
    try {
      const res = await request<{ msg: string }>(`/admin/consumptions/${encodeURIComponent(orderNo)}/refund`, { method: 'POST' });
      toast.success(res.msg || '退款成功');
      await loadConsumptions();
    } catch (e: any) {
      toast.error(e.message || '退款失败');
      await loadConsumptions();
    } finally {
      setPassBusy(false);
    }
  };

  const handleAdjust = async (ucId: number) => {
    const coins = Number(editing?.coins);
    if (!Number.isInteger(coins) || coins < 0) {
      toast.error('请输入非负整数币数');
      return;
    }
    try {
      const res = await request(`/admin/user-cards/${ucId}/coins`, {
        method: 'PUT',
        body: { coins, reason: '商家后台调整' },
      });
      toast.success((res as any).msg || '调整成功');
      setEditing(null);
      load();
    } catch (e: any) {
      toast.error(e.message || '调整失败');
    }
  };

  // ---------- 「单独核销」弹窗的派生数据 ----------
  // 弹窗只存单号，内容从列表里现算 —— 每次操作后 loadConsumptions() 一刷，弹窗里
  // 每一行的状态就跟着变，不会出现"点完核销还显示未核销"的错觉。
  const passTarget = passOrderNo ? consItems.find((x) => x.order_no === passOrderNo) || null : null;
  const passItems = passTarget?.pass_items || [];
  // 已核销的券只能退不能核销；已退款的券两个动作都不给
  const selVerifyCodes = passItems.filter((p) => passSel[p.item_code] && p.status === 'unused').map((p) => p.item_code);
  const selRefundCodes = passItems.filter((p) => passSel[p.item_code] && p.status !== 'refunded').map((p) => p.item_code);
  const allSelected = passItems.length > 0
    && passItems.every((p) => p.status === 'refunded' || passSel[p.item_code]);
  const toggleAll = () => {
    if (allSelected) { setPassSel({}); return; }
    const sel: Record<string, boolean> = {};
    passItems.forEach((p) => { if (p.status !== 'refunded') sel[p.item_code] = true; });
    setPassSel(sel);
  };
  const toggleOne = (code: string) => setPassSel((s) => ({ ...s, [code]: !s[code] }));

  const passStatusLabel = (s: string) => (s === 'used' ? '已核销' : s === 'refunded' ? '已退款' : '未核销');
  const passStatusClass = (s: string) => (
    s === 'used' ? 'bg-green-100 text-green-700'
      : s === 'refunded' ? 'bg-rose-100 text-rose-500'
        : 'bg-amber-100 text-amber-600'
  );

  const yuan = (fen: number) => `¥${(fen / 100).toFixed(2)}`;

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold text-gray-800">🧾 办卡账单</h1>

      {/* 视图切换：账单记录 / 游乐消费记录 */}
      <div className="flex gap-2">
        <button
          onClick={() => setView('orders')}
          className={`rounded-lg px-4 py-2 text-sm font-bold transition ${
            view === 'orders' ? 'bg-orange-500 text-white shadow' : 'bg-white text-gray-500 hover:text-orange-500'
          }`}
        >
          💳 充值账单
        </button>
        <button
          onClick={() => setView('consumptions')}
          className={`rounded-lg px-4 py-2 text-sm font-bold transition ${
            view === 'consumptions' ? 'bg-orange-500 text-white shadow' : 'bg-white text-gray-500 hover:text-orange-500'
          }`}
        >
          🎠 消费记录
        </button>
      </div>

      {view === 'orders' && (<>
      {/* 统计卡 */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">累计收款</p>
          <p className="mt-1 text-2xl font-bold text-emerald-600">
            {stats ? yuan(stats.paid_amount) : '-'}
          </p>
          <p className="mt-1 text-xs text-gray-400">{stats?.paid_count ?? 0} 笔成交</p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">今日收款</p>
          <p className="mt-1 text-2xl font-bold text-sky-600">
            {stats ? yuan(stats.today_paid_amount) : '-'}
          </p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">累计退款</p>
          <p className="mt-1 text-2xl font-bold text-rose-500">
            {stats ? yuan(stats.refunded_amount) : '-'}
          </p>
          <p className="mt-1 text-xs text-gray-400">{stats?.refunded_count ?? 0} 笔退卡</p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">待支付</p>
          <p className="mt-1 text-2xl font-bold text-amber-500">{stats?.pending_count ?? '-'}</p>
        </div>
      </div>

      {/* 筛选栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex overflow-hidden rounded-lg border">
          {STATUS_TABS.map((t) => (
            <button
              key={t.value}
              onClick={() => setStatus(t.value)}
              className={`px-3 py-1.5 text-sm transition ${
                status === t.value
                  ? 'bg-orange-500 font-bold text-white'
                  : 'bg-white text-gray-600 hover:bg-gray-50'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex flex-1 gap-2 sm:max-w-sm">
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load(1, status, keyword)}
            placeholder="搜索订单号 / 卡名 / 用户"
            className="w-full rounded-lg border px-3 py-1.5 text-sm outline-none focus:border-orange-400"
          />
          <button
            onClick={() => load(1, status, keyword)}
            className="rounded-lg bg-orange-500 px-4 py-1.5 text-sm font-bold text-white hover:bg-orange-600"
          >
            搜索
          </button>
        </div>
        <button
          onClick={() => load()}
          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50"
        >
          ↻ 刷新
        </button>
      </div>

      {/* 账单表格 */}
      <div className="overflow-x-auto overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">订单号</th>
              <th className="px-4 py-3">用户</th>
              <th className="px-4 py-3">卡名</th>
              <th className="px-4 py-3">金额</th>
              <th className="px-4 py-3">余币</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">支付时间</th>
              <th className="px-4 py-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-10 text-center text-gray-400">暂无账单记录</td></tr>
            ) : (
              items.map((o) => {
                const active = o.status === 'paid' && o.uc_status === 'active';
                return (
                  <tr key={o.order_no} className="border-t hover:bg-gray-50/60">
                    <td className="px-4 py-3 font-mono text-xs text-gray-500" title={o.order_no}>
                      {o.order_no.slice(0, 14)}…
                    </td>
                    <td className="px-4 py-3 text-gray-700">
                      {o.nickname || o.username || `用户${o.user_id ?? '-'}`}
                    </td>
                    <td className="px-4 py-3 font-medium text-gray-700">{o.card_name}</td>
                    <td className="px-4 py-3 font-bold text-gray-800">{o.amount_yuan} 元</td>
                    <td className="px-4 py-3">
                      {editing?.ucId === o.uc_id && active ? (
                        <span className="flex items-center gap-1">
                          <input
                            autoFocus
                            value={editing.coins}
                            onChange={(e) => setEditing({ ucId: o.uc_id!, coins: e.target.value })}
                            className="w-16 rounded border px-2 py-1 text-xs outline-none focus:border-orange-400"
                          />
                          <button
                            onClick={() => handleAdjust(o.uc_id!)}
                            className="rounded bg-emerald-500 px-2 py-1 text-xs font-bold text-white hover:bg-emerald-600"
                          >存</button>
                          <button
                            onClick={() => setEditing(null)}
                            className="rounded border px-2 py-1 text-xs text-gray-500 hover:bg-gray-50"
                          >取消</button>
                        </span>
                      ) : (
                        <span className="text-gray-700">
                          🪙 {o.coins}
                          {active && (
                            <button
                              onClick={() => setEditing({ ucId: o.uc_id!, coins: String(o.coins) })}
                              className="ml-1 text-xs text-orange-500 hover:underline"
                            >调</button>
                          )}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                        o.status === 'paid'
                          ? 'bg-emerald-100 text-emerald-600'
                          : o.status === 'refunded'
                            ? 'bg-rose-100 text-rose-500'
                            : 'bg-amber-100 text-amber-600'
                      }`}>
                        {o.status === 'paid' ? '已支付' : o.status === 'refunded' ? '已退卡' : '待支付'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">{o.paid_at || '-'}</td>
                    <td className="px-4 py-3">
                      {o.status === 'paid' ? (
                        refundNo === o.order_no ? (
                          <span className="flex items-center gap-1 text-xs">
                            <span className="text-gray-500">确认退款？</span>
                            <button
                              onClick={() => handleRefund(o.order_no)}
                              className="rounded bg-rose-500 px-2 py-1 font-bold text-white hover:bg-rose-600"
                            >确认</button>
                            <button
                              onClick={() => setRefundNo(null)}
                              className="rounded border px-2 py-1 text-gray-500 hover:bg-gray-50"
                            >取消</button>
                          </span>
                        ) : (
                          <button
                            onClick={() => setRefundNo(o.order_no)}
                            className="rounded-lg border border-rose-200 px-3 py-1.5 text-xs font-bold text-rose-500 hover:bg-rose-50"
                          >
                            退卡退款
                          </button>
                        )
                      ) : (
                        <span className="text-xs text-gray-300">—</span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <span>共 {total} 条记录</span>
        <div className="flex items-center gap-2">
          <button
            disabled={page <= 1}
            onClick={() => load(page - 1)}
            className="rounded-lg border px-3 py-1 disabled:opacity-40 hover:bg-gray-50"
          >
            上一页
          </button>
          <span>{page} / {pages}</span>
          <button
            disabled={page >= pages}
            onClick={() => load(page + 1)}
            className="rounded-lg border px-3 py-1 disabled:opacity-40 hover:bg-gray-50"
          >
            下一页
          </button>
        </div>
      </div>
      </>)}

      {view === 'consumptions' && (
        <div className="space-y-4">
          {/* 核销码输入 */}
          <div className="flex flex-wrap items-center gap-2 rounded-xl border bg-white p-4 shadow-sm">
            <span className="text-sm font-bold text-gray-700">🎫 核销：</span>
            <input
              value={verifyInput}
              onChange={(e) => setVerifyInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleVerifyByCode()}
              placeholder="输入游客出示的核销码"
              className="w-56 rounded-lg border px-3 py-1.5 text-sm font-mono uppercase outline-none focus:border-orange-400"
            />
            <button
              onClick={handleVerifyByCode}
              disabled={verifying}
              className="rounded-lg bg-orange-500 px-4 py-1.5 text-sm font-bold text-white hover:bg-orange-600 disabled:opacity-50"
            >
              {verifying ? '核销中...' : '立即核销'}
            </button>
            <span className="text-xs text-gray-400">券包订单扫一张只核销这一张；无券的历史单才会整单核销</span>
          </div>

          {/* 消费记录表 */}
          <div className="overflow-x-auto overflow-hidden rounded-xl border bg-white shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500">
                <tr>
                  <th className="px-4 py-3">核销码</th>
                  <th className="px-4 py-3">用户</th>
                  <th className="px-4 py-3">消费明细</th>
                  <th className="px-4 py-3">券进度</th>
                  <th className="px-4 py-3">卡内币</th>
                  <th className="px-4 py-3">奖励币</th>
                  <th className="px-4 py-3">合计</th>
                  <th className="px-4 py-3">状态</th>
                  <th className="px-4 py-3">时间</th>
                  <th className="px-4 py-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {consLoading ? (
                  <tr><td colSpan={10} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
                ) : consItems.length === 0 ? (
                  <tr><td colSpan={10} className="px-4 py-10 text-center text-gray-400">暂无消费记录（游客在首页「游乐消费」多选项目扣币后生成）</td></tr>
                ) : (
                  consItems.map((c) => {
                    // 券包订单按券的形态给不同入口：单张券直接核销/退款，多张券给
                    // 「整单核销」+「单独核销」两个选择（游客一次勾了好几个项目时用）。
                    const passes = c.pass_items || [];
                    const multi = passes.length > 1;
                    const single = passes.length === 1 ? passes[0] : null;
                    const allRefunded = c.status === 'refunded';
                    return (
                      <tr key={c.order_no} className="border-t hover:bg-gray-50/60">
                        <td className="px-4 py-3 font-mono text-sm font-bold tracking-wider text-gray-800">{c.verify_code}</td>
                        <td className="px-4 py-3 text-gray-700">
                          {c.nickname || '匿名游客'}
                          {c.phone && <div className="text-xs text-gray-400">{c.phone}</div>}
                        </td>
                        <td className="max-w-[280px] px-4 py-3 text-gray-600">
                          {c.items.map((it) => `${it.name}×${it.qty}`).join('、')}
                        </td>
                        <td className="px-4 py-3 text-gray-600">
                          {c.pass_total > 0 ? (
                            <span className="whitespace-nowrap">
                              还剩 <span className="font-bold text-amber-600">{c.pass_unused}</span> / {c.pass_total} 张
                            </span>
                          ) : (
                            <span className="text-xs text-gray-400">历史单</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-gray-700">🪙 {c.card_coins}</td>
                        <td className="px-4 py-3 text-gray-500">{c.bonus_coins ? `🪙 ${c.bonus_coins}` : '-'}</td>
                        <td className="px-4 py-3 font-bold text-amber-600">🪙 {c.total_coins}</td>
                        <td className="px-4 py-3">
                          <span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-bold ${
                            c.status === 'used'
                              ? 'bg-green-100 text-green-700'
                              : c.status === 'refunded'
                                ? 'bg-rose-100 text-rose-500'
                                : 'bg-amber-100 text-amber-600'
                          }`}>
                            {c.status === 'used' ? '已核销' : c.status === 'refunded' ? '已退款' : '未核销'}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-xs text-gray-500">{c.created_at || '-'}</td>
                        <td className="px-4 py-3 text-right">
                          {allRefunded ? (
                            <span className="text-xs text-gray-300">—</span>
                          ) : multi ? (
                            /* 一次勾了好几个项目：整体核销 / 单独核销二选一 */
                            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                              <button
                                onClick={() => handleVerifyRow(c.order_no)}
                                className="rounded-lg border border-orange-200 px-3 py-1.5 text-xs font-bold text-orange-500 hover:bg-orange-50"
                              >
                                整单核销
                              </button>
                              <button
                                onClick={() => openPassDialog(c)}
                                className="rounded-lg border border-sky-200 px-3 py-1.5 text-xs font-bold text-sky-600 hover:bg-sky-50"
                              >
                                单独核销（{passes.length}）
                              </button>
                            </span>
                          ) : (
                            /* 只买了一个项目 / 无券历史单：直接给核销 + 退款 */
                            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                              {c.status !== 'used' && (
                                <button
                                  onClick={() => handleVerifyRow(c.order_no)}
                                  className="rounded-lg border border-orange-200 px-3 py-1.5 text-xs font-bold text-orange-500 hover:bg-orange-50"
                                >
                                  核销
                                </button>
                              )}
                              <button
                                onClick={() => setRefundConfirm({
                                  desc: `退款：${single ? `${single.project_name} ×1（券码 ${single.item_code}）` : `消费单 ${c.order_no}`}`
                                    + `，共 ${single ? single.unit_coins : c.total_coins} 币`
                                    + `${single && single.status === 'used' ? '（这张已核销，仍可退）' : ''}`,
                                  run: () => (single ? doRefundCodes([single.item_code]) : doRefundOrder(c.order_no)),
                                })}
                                className="rounded-lg border border-rose-200 px-3 py-1.5 text-xs font-bold text-rose-500 hover:bg-rose-50"
                              >
                                退款
                              </button>
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="text-xs text-gray-400">
        💡 退卡后订单标记「已退卡」，对应持卡同步失效、用户余币实时扣减；「调」可修改持卡余币（线下补偿/纠错），用户余额随之变化。
        <br />
        游乐消费退款：<b>按券退</b>只退勾选的那几张，<b>已核销的券也能退</b>（游客刚上去就下来这类情况由现场判定）；
        退掉的币按原扣币比例退回用户的卡（卡内币回充生效卡，奖励币恢复可用），不可撤销。
      </p>

      {/* ---------- 「单独核销」弹窗：逐张券一行，每行单独核销/退款，支持批量勾选 ---------- */}
      {passTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setPassOrderNo(null)}>
          <div
            className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-lg font-bold text-gray-800">单独核销 / 退款</h3>
                <p className="mt-1 text-xs text-gray-500">
                  {passTarget.nickname || '匿名游客'}
                  {passTarget.phone ? ` · ${passTarget.phone}` : ''}
                  {' · '}共 {passItems.length} 张券 · 还剩 {passTarget.pass_unused} 张待玩
                </p>
              </div>
              <button onClick={() => setPassOrderNo(null)} className="text-lg leading-none text-gray-400 hover:text-gray-600">✕</button>
            </div>

            {/* 批量条：勾多少就核销/退款多少 */}
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl bg-gray-50 px-3 py-2">
              <label className="flex items-center gap-1.5 text-sm text-gray-600">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                全选
              </label>
              <span className="text-xs text-gray-400">已勾 {selRefundCodes.length} 张</span>
              <div className="flex-1" />
              <button
                disabled={passBusy || selVerifyCodes.length === 0}
                onClick={() => doVerifyCodes(selVerifyCodes)}
                className="rounded-lg bg-orange-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-orange-600 disabled:opacity-40"
              >
                批量核销（{selVerifyCodes.length}）
              </button>
              <button
                disabled={passBusy || selRefundCodes.length === 0}
                onClick={() => setRefundConfirm({
                  desc: `批量退款 ${selRefundCodes.length} 张券，共 `
                    + `${passItems.filter((p) => selRefundCodes.includes(p.item_code)).reduce((n, p) => n + p.unit_coins, 0)} 币`,
                  run: () => doRefundCodes(selRefundCodes),
                })}
                className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-bold text-white hover:bg-rose-600 disabled:opacity-40"
              >
                批量退款（{selRefundCodes.length}）
              </button>
            </div>

            {/* 逐张券一行（不合并 ×N，都是一项一次） */}
            <div className="mt-3 flex-1 space-y-2 overflow-y-auto pr-1">
              {passItems.length === 0 ? (
                <p className="py-10 text-center text-sm text-gray-400">
                  这单没有券明细（历史订单），只能用行内的「核销 / 退款」整单处理
                </p>
              ) : passItems.map((p) => (
                <div
                  key={p.item_code}
                  className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 ${
                    p.status === 'refunded' ? 'border-gray-100 bg-gray-50/70' : 'border-gray-200'
                  }`}
                >
                  <input
                    type="checkbox"
                    disabled={p.status === 'refunded'}
                    checked={!!passSel[p.item_code]}
                    onChange={() => toggleOne(p.item_code)}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-gray-700">
                      {p.project_name} <span className="text-gray-400">×1</span>
                    </p>
                    <p className="font-mono text-xs tracking-wider text-gray-500">{p.item_code}</p>
                  </div>
                  <span className="whitespace-nowrap text-sm font-bold text-amber-600">🪙 {p.unit_coins}</span>
                  <span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-bold ${passStatusClass(p.status)}`}>
                    {passStatusLabel(p.status)}
                  </span>
                  <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                    {p.status === 'unused' && (
                      <button
                        disabled={passBusy}
                        onClick={() => doVerifyCodes([p.item_code])}
                        className="rounded-lg border border-orange-200 px-2.5 py-1 text-xs font-bold text-orange-500 hover:bg-orange-50 disabled:opacity-40"
                      >
                        核销
                      </button>
                    )}
                    {p.status !== 'refunded' && (
                      <button
                        disabled={passBusy}
                        onClick={() => setRefundConfirm({
                          desc: `退款：${p.project_name} ×1（券码 ${p.item_code}），共 ${p.unit_coins} 币`
                            + `${p.status === 'used' ? '（这张已核销，仍可退）' : ''}`,
                          run: () => doRefundCodes([p.item_code]),
                        })}
                        className="rounded-lg border border-rose-200 px-2.5 py-1 text-xs font-bold text-rose-500 hover:bg-rose-50 disabled:opacity-40"
                      >
                        退款
                      </button>
                    )}
                  </span>
                </div>
              ))}
            </div>

            <p className="mt-3 rounded-lg bg-amber-50 p-2 text-xs text-amber-600">
              一券一项一次，每张券单独核销；<b>已核销的券也可以退款</b>（游客可能刚上去就下来），
              退掉的币按原扣币比例退回用户的卡，不可撤销。
            </p>
          </div>
        </div>
      )}

      {/* 退款二次确认（逐张 / 批量 / 整单共用） */}
      {refundConfirm && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4" onClick={() => setRefundConfirm(null)}>
          <div
            className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="mb-3 text-lg font-bold text-gray-800">确认退款？</h3>
            <div className="space-y-2 text-sm text-gray-600">
              <p>{refundConfirm.desc}</p>
              <p className="rounded-lg bg-amber-50 p-2 text-xs text-amber-600">
                退款后币按原扣币比例退回用户的卡（卡内币回充生效卡，奖励币恢复可用），不可撤销。
                已核销的券同样会退。
              </p>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setRefundConfirm(null)} className="rounded-lg border px-4 py-2 text-sm">取消</button>
              <button
                onClick={() => refundConfirm.run()}
                disabled={passBusy}
                className="rounded-lg bg-rose-500 px-5 py-2 text-sm font-bold text-white hover:bg-rose-600 disabled:opacity-50"
              >
                {passBusy ? '退款中...' : '确认退款'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
