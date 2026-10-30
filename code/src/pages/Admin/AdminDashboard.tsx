// ============================================================
// 管理端 - 运营仪表盘
// 数据源: GET /admin/dashboard?days=7|30
// 组成：核心指标卡 + 意图分布（横向条形，实时聚合）+ 知识库盲区
//      + 响应性能 + 知识库规模 + 待处理
// 说明：意图分布已从"Redis 1 小时计数器"改为按 question_logs 实时聚合，
//       此前那张饼图长期只有一个"其他"（小程序走的 /chat/stream 从不计数）。
// ============================================================
import { useCallback, useEffect, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { RefreshCw, ArrowRight, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { request } from '@/lib/api';

interface IntentItem { intent: string; cnt: number }
interface GapItem { question: string; count: number; fallback_label: string }

interface DashboardData {
  days: number;
  total_questions: number;
  today_questions: number;
  today_new_users: number;
  today_paid_amount: number;
  intent_distribution: IntentItem[];
  intent_total: number;
  knowledge_base: {
    milvus_entities: number;
    bm25_documents: number;
    documents: number;
    chunks: number;
    last_updated: string;
  };
  gaps: {
    raw_total: number;
    distinct_total: number;
    by_type: Record<string, number>;
    top: GapItem[];
  };
  latency: {
    samples?: number; avg?: number; p50?: number; p90?: number; p95?: number;
    max?: number; under_10s_rate?: number; under_2s_rate?: number;
  };
  todo: { pending_orders: number; unused_consumptions: number; lost_items: number };
  components: { redis: string };
}

const RANGES = [
  { label: '近 7 天', days: 7 },
  { label: '近 30 天', days: 30 },
];

export default function AdminDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (d: number, silent = false) => {
    if (silent) setRefreshing(true);
    else setLoading(true);
    try {
      const res = await request<{ data: DashboardData }>(`/admin/dashboard?days=${d}`);
      setData(res.data);
    } catch (e: any) {
      toast.error(e?.message || '加载失败');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load(days);
  }, [days, load]);

  if (loading) {
    return <div className="p-10 text-center text-gray-400">加载中…</div>;
  }

  const kb = data?.knowledge_base;
  const gaps = data?.gaps;
  const lat = data?.latency || {};
  const todo = data?.todo;
  const intents = (data?.intent_distribution || []).filter((i) => i.cnt > 0);
  const intentTotal = data?.intent_total || 0;
  const otherCnt = (data?.intent_distribution || []).find((i) => i.intent === '其他')?.cnt || 0;
  const otherPct = intentTotal ? Math.round((otherCnt / intentTotal) * 100) : 0;
  const kbTotal = (kb?.documents || 0) + (kb?.chunks || 0);

  // ---------- 意图分布：横向条形（比甜甜圈更能看清长尾与占比） ----------
  const sortedIntents = [...intents].sort((a, b) => b.cnt - a.cnt);
  const intentOption = {
    grid: { left: 56, right: 96, top: 8, bottom: 8 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (p: any) => `${p[0].name}：${p[0].value} 次`,
    },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#F3F4F6' } },
      axisLabel: { fontSize: 11, color: '#9CA3AF' },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: sortedIntents.map((i) => i.intent),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontSize: 12, color: '#4B5563' },
    },
    series: [
      {
        type: 'bar',
        barWidth: 14,
        data: sortedIntents.map((i) => ({
          value: i.cnt,
          // 「其他」= 关键词规则没覆盖到的长尾，用灰色区分，提示可扩充
          itemStyle: {
            color: i.intent === '其他' ? '#D1D5DB' : '#26334D',
            borderRadius: [0, 4, 4, 0],
          },
        })),
        label: {
          show: true,
          position: 'right',
          fontSize: 11,
          color: '#6B7280',
          formatter: (p: any) =>
            `${p.value} 次 · ${intentTotal ? Math.round((p.value / intentTotal) * 100) : 0}%`,
        },
      },
    ],
  };

  return (
    <div className="space-y-5">
      {/* ---------- 标题 + 时间范围 ---------- */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">运营仪表盘</h1>
          <p className="mt-1 text-sm text-gray-500">
            园区问答与知识库的整体运行情况
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border bg-white p-0.5">
            {RANGES.map((r) => (
              <button
                key={r.days}
                onClick={() => setDays(r.days)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition ${
                  days === r.days ? 'bg-[#26334D] text-white' : 'text-gray-500 hover:text-gray-800'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <button
            onClick={() => load(days, true)}
            className="flex items-center gap-1.5 rounded-lg border bg-white px-3 py-2 text-xs text-gray-500 transition hover:text-gray-800"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
            刷新
          </button>
        </div>
      </div>

      {/* ---------- 指标卡 ---------- */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 2xl:grid-cols-6">
        <MetricCard label="累计提问" value={data?.total_questions ?? 0} unit="条"
                    sub={`今日 ${data?.today_questions ?? 0} 条`} />
        <MetricCard label="今日新增用户" value={data?.today_new_users ?? 0} unit="人"
                    sub="小程序注册" />
        <MetricCard label="今日收款" value={(data?.today_paid_amount ?? 0).toFixed(2)} unit="元"
                    sub="办卡订单实收" />
        <MetricCard label={`知识库盲区（近 ${days} 天）`}
                    value={gaps?.distinct_total ?? 0} unit="个问题"
                    sub={`兜底回答 ${gaps?.raw_total ?? 0} 条`}
                    tone={(gaps?.distinct_total ?? 0) > 0 ? 'warn' : 'ok'} />
        <MetricCard label="知识库规模" value={kbTotal} unit=""
                    sub={`${kb?.documents ?? 0} 篇文档 · ${kb?.chunks ?? 0} 个切片`} />
        <MetricCard label={`响应 P95（近 ${days} 天）`}
                    value={lat.p95 ?? '—'} unit={lat.p95 ? 's' : ''}
                    sub={`≤10s 达标 ${lat.under_10s_rate ?? 0}%`}
                    tone={(lat.p95 ?? 0) > 10 ? 'warn' : 'default'} />
      </div>

      {/* ---------- 意图分布 + 盲区 Top5 ---------- */}
      <div className="grid gap-5 xl:grid-cols-[1.35fr,1fr]">
        <div className="rounded-xl border bg-white p-5 shadow-sm">
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-bold text-gray-700">用户意图分布</h2>
            <span className="text-xs text-gray-400">
              近 {days} 天 · 共 {intentTotal} 条提问
            </span>
          </div>
          <p className="mb-3 text-xs text-gray-400">
            按关键词规则实时识别（票务 / 营业 / 设施 / 规则 / 应急 / 其他）
          </p>

          {sortedIntents.length > 0 ? (
            <>
              <ReactECharts option={intentOption} style={{ height: 300 }} notMerge />
              {otherPct >= 30 && (
                <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                  「其他」占 {otherPct}%（{otherCnt} 条），说明不少提问没被关键词规则覆盖，
                  可在「问答记录」里看看这些问法，必要时补充意图关键词。
                </p>
              )}
            </>
          ) : (
            <div className="flex h-40 items-center justify-center text-sm text-gray-400">
              近 {days} 天还没有问答记录
            </div>
          )}
        </div>

        {/* 知识库盲区 */}
        <div className="rounded-xl border bg-white p-5 shadow-sm">
          <div className="mb-1 flex items-center justify-between">
            <h2 className="font-bold text-gray-700">知识库盲区</h2>
            <Link to="/admin/kb-agent"
                  className="flex items-center gap-1 text-xs font-bold text-orange-600 hover:text-orange-700">
              去补齐 <ArrowRight size={12} />
            </Link>
          </div>
          <p className="mb-3 text-xs text-gray-400">
            模型没检索到资料、只能给建议的问题（近 {days} 天）
          </p>

          {gaps?.top?.length ? (
            <>
              <div className="mb-3 flex gap-2">
                <span className="rounded-lg bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
                  无资料建议 {gaps.by_type?.fallback_none ?? 0} 条
                </span>
                <span className="rounded-lg bg-sky-50 px-2 py-1 text-[11px] text-sky-700">
                  资料不足建议 {gaps.by_type?.fallback_partial ?? 0} 条
                </span>
              </div>
              <ul className="space-y-2">
                {gaps.top.map((g, i) => (
                  <li key={i} className="flex items-start justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2">
                    <span className="min-w-0 text-xs text-gray-700">
                      <span className="mr-1 text-gray-400">{i + 1}.</span>
                      {g.question}
                    </span>
                    <span className="shrink-0 whitespace-nowrap text-[11px] text-gray-400">
                      {g.count} 次 · {g.fallback_label}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <div className="flex h-32 flex-col items-center justify-center gap-1.5 text-sm text-gray-400">
              <CheckCircle2 size={20} className="text-emerald-500" />
              近 {days} 天没有"答不上来"的问题
            </div>
          )}
        </div>
      </div>

      {/* ---------- 性能 + 知识库/组件 + 待处理 ---------- */}
      <div className="grid gap-5 lg:grid-cols-3">
        <div className="rounded-xl border bg-white p-5 shadow-sm">
          <h2 className="mb-3 font-bold text-gray-700">响应性能</h2>
          {lat.samples ? (
            <dl className="space-y-2 text-xs">
              <Row k="样本数" v={`${lat.samples} 条`} />
              <Row k="平均耗时" v={`${lat.avg}s`} />
              <Row k="P50 / P90" v={`${lat.p50}s / ${lat.p90}s`} />
              <Row k="P95 / 最慢" v={`${lat.p95}s / ${lat.max}s`}
                   warn={(lat.p95 ?? 0) > 10} />
              <Row k="≤10s 达标率" v={`${lat.under_10s_rate}%`}
                   ok={(lat.under_10s_rate ?? 0) >= 95} />
              <Row k="≤2s（多为缓存命中）" v={`${lat.under_2s_rate}%`} />
            </dl>
          ) : (
            <p className="py-6 text-center text-xs text-gray-400">近 {days} 天暂无耗时样本</p>
          )}
        </div>

        <div className="rounded-xl border bg-white p-5 shadow-sm">
          <h2 className="mb-3 font-bold text-gray-700">知识库与组件</h2>
          <dl className="space-y-2 text-xs">
            <Row k="文档 / 切片" v={`${kb?.documents ?? 0} / ${kb?.chunks ?? 0}`} />
            <Row k="向量库（Milvus）" v={`${kb?.milvus_entities ?? 0} 条向量`} />
            <Row k="关键词索引（BM25）" v={`${kb?.bm25_documents ?? 0} 篇`}
                 warn={!!kb?.bm25_documents && kb.bm25_documents === 0} />
            <Row k="最近更新" v={kb?.last_updated || '—'} />
            <Row k="Redis 缓存" v={data?.components?.redis === 'connected' ? '已连接' : '未连接'}
                 ok={data?.components?.redis === 'connected'}
                 warn={data?.components?.redis !== 'connected'} />
          </dl>
          <Link to="/admin/knowledge"
                className="mt-3 inline-flex items-center gap-1 text-xs font-bold text-sky-600 hover:text-sky-700">
            知识库管理 <ArrowRight size={12} />
          </Link>
        </div>

        <div className="rounded-xl border bg-white p-5 shadow-sm">
          <h2 className="mb-3 font-bold text-gray-700">待处理</h2>
          <div className="space-y-2">
            <TodoRow to="/admin/orders?view=consumptions" label="待核销消费单"
                     count={todo?.unused_consumptions ?? 0} />
            <TodoRow to="/admin/orders" label="待处理办卡订单"
                     count={todo?.pending_orders ?? 0} />
            <TodoRow to="/admin/lost-found" label="待认领失物"
                     count={todo?.lost_items ?? 0} />
          </div>
          <p className="mt-3 text-[11px] text-gray-400">点击可直接跳转到对应管理页处理</p>
        </div>
      </div>
    </div>
  );
}

// ---------- 小部件 ----------
function MetricCard({
  label, value, unit, sub, tone = 'default',
}: {
  label: string; value: number | string; unit?: string; sub?: string;
  tone?: 'default' | 'warn' | 'ok';
}) {
  const color =
    tone === 'warn' ? 'text-amber-600' : tone === 'ok' ? 'text-emerald-600' : 'text-gray-800';
  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`mt-1.5 text-2xl font-bold ${color}`}>
        {value}
        {unit && <span className="ml-1 text-xs font-normal text-gray-400">{unit}</span>}
      </p>
      {sub && <p className="mt-1 truncate text-[11px] text-gray-400">{sub}</p>}
    </div>
  );
}

function Row({ k, v, ok, warn }: { k: string; v: string; ok?: boolean; warn?: boolean }) {
  const color = warn ? 'text-amber-600' : ok ? 'text-emerald-600' : 'text-gray-700';
  return (
    <div className="flex items-center justify-between">
      <dt className="text-gray-500">{k}</dt>
      <dd className={`font-medium ${color}`}>{v}</dd>
    </div>
  );
}

function TodoRow({ to, label, count }: { to: string; label: string; count: number }) {
  return (
    <Link
      to={to}
      className="flex items-center justify-between rounded-lg border px-3 py-2.5 transition hover:border-[#26334D] hover:bg-gray-50"
    >
      <span className="text-xs text-gray-700">{label}</span>
      <span className="flex items-center gap-2">
        <span className={`text-sm font-bold ${count > 0 ? 'text-amber-600' : 'text-gray-300'}`}>
          {count}
        </span>
        <ArrowRight size={12} className="text-gray-300" />
      </span>
    </Link>
  );
}
