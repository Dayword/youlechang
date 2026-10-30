// ============================================================
// 管理端 - 问答分析图谱：常见问题统计 + 用户满意度可视化
// 数据源: GET /admin/analytics/overview（一次返回指标卡 + 四张图数据）
//   - 常见问题 TOP10（横向柱状图）
//   - 意图分布（环形饼图）
//   - 近 7 天问答趋势（面积折线图）
//   - 满意度 1-5 星分布（柱状图）
//   - 响应耗时 P50/P90/P95 与 ≤10s 达标率（提速目标看板）
// 另有 GET/POST /admin/hot-questions* 维护小程序「快捷提问」6 个按钮的数据源
// ============================================================
import { useEffect, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface Latency {
  samples: number;
  avg?: number;
  p50?: number;
  p90?: number;
  p95?: number;
  max?: number;
  under_10s_rate?: number;
  under_2s_rate?: number;
  window_days?: number;
}

interface Overview {
  total_questions: number;
  today_questions: number;
  top_questions: { question: string; cnt: number }[];
  intent_distribution: { intent: string; cnt: number }[];
  trend: { date: string; count: number }[];
  satisfaction: {
    distribution: Record<string, number>; // {"1": n, ..., "5": n}
    total: number;
    average: number;
  };
  latency?: Latency;
}

/** 热点问题（近 7 天高频问题 Top6，小程序 AI 问答页快捷提问按钮的数据源） */
interface HotQuestion {
  question: string;
  label: string;
  icon: string;
  hit_count: number;
  raw_count?: number;
  source?: string;
}

interface HotQuestionsData {
  items: HotQuestion[];
  refreshed_at: string;
  stale: boolean;
  window_days: number;
  next_refresh_at: string;
  auto_count?: number;
}

// 图表统一配色（与运营后台主题呼应）
const PALETTE = ['#ff8c42', '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f43f5e', '#64748b'];

export default function AdminAnalytics() {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  // 热点问题（每 7 天自动统计批次，商家可手动刷新）
  const [hot, setHot] = useState<HotQuestionsData | null>(null);
  const [hotLoading, setHotLoading] = useState(true);
  const [hotRefreshing, setHotRefreshing] = useState(false);

  const load = () => {
    setLoading(true);
    request<{ data: Overview }>('/admin/analytics/overview?days=7')
      .then((res) => setData(res.data))
      .catch((e: any) => toast.error(e.message || '加载分析数据失败'))
      .finally(() => setLoading(false));
  };

  const loadHot = () => {
    setHotLoading(true);
    request<{ data: HotQuestionsData }>('/admin/hot-questions?limit=6&days=7')
      .then((res) => setHot(res.data))
      .catch((e: any) => toast.error(e.message || '加载热点问题失败'))
      .finally(() => setHotLoading(false));
  };

  /** 立即按近 7 天问答记录重新统计（等价于每 7 天自动刷新的那次任务） */
  const refreshHot = async () => {
    if (hotRefreshing) return;
    setHotRefreshing(true);
    try {
      const res = await request<{ msg: string; data: HotQuestionsData }>(
        '/admin/hot-questions/refresh',
        { method: 'POST' },
      );
      setHot(res.data);
      toast.success(res.msg || '热点问题已刷新');
    } catch (e: any) {
      toast.error(e.message || '刷新失败');
    } finally {
      setHotRefreshing(false);
    }
  };

  useEffect(() => {
    load();
    loadHot();
  }, []);

  // ---------- 图表配置 ----------
  const topOption = {
    title: { text: '🔥 用户常见问题 TOP10', left: 10, top: 6, textStyle: { fontSize: 14, color: '#374151' } },
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      formatter: (p: any) => `${p[0].name}<br/>提问次数：<b>${p[0].value}</b>`,
    },
    grid: { left: 10, right: 40, top: 44, bottom: 10, containLabel: true },
    xAxis: { type: 'value' as const, minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } } },
    yAxis: {
      type: 'category' as const,
      inverse: true,
      data: (data?.top_questions || []).map((q) =>
        q.question.length > 12 ? q.question.slice(0, 12) + '…' : q.question
      ),
      axisLabel: { color: '#4b5563' },
    },
    series: [{
      type: 'bar',
      data: (data?.top_questions || []).map((q) => q.cnt),
      barMaxWidth: 16,
      itemStyle: {
        color: {
          type: 'linear' as const, x: 0, y: 0, x2: 1, y2: 0,
          colorStops: [{ offset: 0, color: '#ffb26b' }, { offset: 1, color: '#ff8c42' }],
        },
        borderRadius: [0, 8, 8, 0],
      },
      label: { show: true, position: 'right' as const, color: '#ff8c42', fontWeight: 'bold' as const },
    }],
  };

  const intentOption = {
    title: { text: '🏷️ 问题意图分布（近30天）', left: 10, top: 6, textStyle: { fontSize: 14, color: '#374151' } },
    tooltip: { trigger: 'item' as const, formatter: '{b}<br/>{c} 次（{d}%）' },
    legend: { bottom: 0, icon: 'circle' as const, textStyle: { color: '#4b5563', fontSize: 11 } },
    color: PALETTE,
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '46%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      label: { formatter: '{b}\n{d}%', color: '#4b5563', fontSize: 11 },
      data: (data?.intent_distribution || []).map((i) => ({ name: i.intent, value: i.cnt })),
    }],
  };

  const trendOption = {
    title: { text: '📈 近 7 天问答趋势', left: 10, top: 6, textStyle: { fontSize: 14, color: '#374151' } },
    tooltip: { trigger: 'axis' as const, formatter: (p: any) => `${p[0].axisValue}<br/>问答量：<b>${p[0].value}</b>` },
    grid: { left: 10, right: 24, top: 44, bottom: 10, containLabel: true },
    xAxis: {
      type: 'category' as const,
      boundaryGap: false,
      data: (data?.trend || []).map((t) => t.date.slice(5)), // MM-DD
      axisLabel: { color: '#4b5563' },
    },
    yAxis: { type: 'value' as const, minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } } },
    series: [{
      type: 'line',
      smooth: true,
      data: (data?.trend || []).map((t) => t.count),
      symbolSize: 8,
      itemStyle: { color: '#3b82f6' },
      lineStyle: { width: 3, color: '#3b82f6' },
      areaStyle: {
        color: {
          type: 'linear' as const, x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(59,130,246,0.28)' }, { offset: 1, color: 'rgba(59,130,246,0.02)' }],
        },
      },
    }],
  };

  const satOption = {
    title: { text: '⭐ 用户满意度分布', left: 10, top: 6, textStyle: { fontSize: 14, color: '#374151' } },
    tooltip: { trigger: 'axis' as const, axisPointer: { type: 'shadow' as const }, formatter: (p: any) => `${p[0].name} 评价<br/>人数：<b>${p[0].value}</b>` },
    grid: { left: 10, right: 24, top: 44, bottom: 10, containLabel: true },
    xAxis: {
      type: 'category' as const,
      data: ['1星', '2星', '3星', '4星', '5星'],
      axisLabel: { color: '#4b5563' },
    },
    yAxis: { type: 'value' as const, minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } } },
    series: [{
      type: 'bar',
      barMaxWidth: 36,
      data: ['1', '2', '3', '4', '5'].map((k) => data?.satisfaction?.distribution?.[k] ?? 0),
      itemStyle: {
        borderRadius: [8, 8, 0, 0],
        // 1-3 星暖红提示、4-5 星好评金黄
        color: (p: any) => (p.dataIndex >= 3 ? '#ffb800' : '#f87171'),
      },
      label: { show: true, position: 'top' as const, color: '#6b7280', fontWeight: 'bold' as const },
    }],
  };

  const sat = data?.satisfaction;
  const latency = data?.latency;
  const happyRate = sat && sat.total > 0
    ? Math.round((((sat.distribution['4'] || 0) + (sat.distribution['5'] || 0)) / sat.total) * 100)
    : null;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-800">📊 问答分析</h1>
        <button
          onClick={load}
          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50"
        >
          ↻ 刷新
        </button>
      </div>

      {/* 指标卡 */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">累计问答</p>
          <p className="mt-1 text-2xl font-bold text-gray-800">{data?.total_questions ?? '-'}</p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">今日问答</p>
          <p className="mt-1 text-2xl font-bold text-sky-600">{data?.today_questions ?? '-'}</p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">平均满意度</p>
          <p className="mt-1 text-2xl font-bold text-amber-500">
            {sat ? `★ ${sat.average || 0}` : '-'}
          </p>
          <p className="mt-1 text-xs text-gray-400">共 {sat?.total ?? 0} 人评价</p>
        </div>
        <div className="rounded-xl border bg-white p-4 shadow-sm">
          <p className="text-xs text-gray-400">好评率（4-5星）</p>
          <p className="mt-1 text-2xl font-bold text-emerald-600">
            {happyRate !== null ? `${happyRate}%` : '-'}
          </p>
        </div>
      </div>

      {loading ? (
        <div className="rounded-xl border bg-white py-16 text-center text-gray-400 shadow-sm">图表加载中...</div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <div className="rounded-xl border bg-white p-2 shadow-sm">
            <ReactECharts option={topOption} style={{ height: 320 }} notMerge />
          </div>
          <div className="rounded-xl border bg-white p-2 shadow-sm">
            <ReactECharts option={intentOption} style={{ height: 320 }} notMerge />
          </div>
          <div className="rounded-xl border bg-white p-2 shadow-sm">
            <ReactECharts option={trendOption} style={{ height: 300 }} notMerge />
          </div>
          <div className="rounded-xl border bg-white p-2 shadow-sm">
            <ReactECharts option={satOption} style={{ height: 300 }} notMerge />
          </div>
        </div>
      )}

      {/* 响应耗时（提速达标看板：目标端到端 ≤10s、Redis 缓存命中 ≤2s） */}
      <div className="rounded-xl border bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-semibold text-gray-700">⏱️ 响应耗时（近 7 天）</p>
            <p className="mt-0.5 text-xs text-gray-400">
              统计每一轮问答的真实端到端耗时（问答日志 elapsed_seconds），用于验证提速目标是否守住
            </p>
          </div>
          <span className="rounded-full bg-orange-50 px-2.5 py-1 text-xs text-orange-500">
            样本 {latency?.samples ?? 0} 次
          </span>
        </div>

        {!latency || latency.samples === 0 ? (
          <p className="py-10 text-center text-sm text-gray-400">暂无耗时数据</p>
        ) : (
          <>
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {[
                { k: '平均', v: latency.avg, c: 'text-gray-800' },
                { k: 'P50', v: latency.p50, c: 'text-emerald-600' },
                { k: 'P90', v: latency.p90, c: 'text-sky-600' },
                { k: 'P95', v: latency.p95, c: 'text-indigo-600' },
                { k: '最慢', v: latency.max, c: 'text-rose-500' },
              ].map((it) => (
                <div key={it.k} className="rounded-lg border border-gray-100 bg-gray-50/60 p-3">
                  <p className="text-xs text-gray-400">{it.k}</p>
                  <p className={`mt-1 text-xl font-bold ${it.c}`}>
                    {it.v ?? '-'}
                    <span className="ml-0.5 text-xs font-normal text-gray-400">s</span>
                  </p>
                </div>
              ))}
              <div className="rounded-lg border border-gray-100 bg-gray-50/60 p-3">
                <p className="text-xs text-gray-400">样本数</p>
                <p className="mt-1 text-xl font-bold text-gray-800">{latency.samples}</p>
              </div>
            </div>

            <div className="mt-4 space-y-3">
              <RateBar
                label="端到端 ≤10s 达标率"
                hint="目标：常规问答（非缓存）10 秒内返回"
                rate={latency.under_10s_rate ?? 0}
                color="bg-gradient-to-r from-orange-400 to-amber-400"
                target={80}
              />
              <RateBar
                label="Redis 缓存命中 ≤2s 占比"
                hint="目标：命中缓存的问句 2 秒内返回"
                rate={latency.under_2s_rate ?? 0}
                color="bg-gradient-to-r from-sky-400 to-emerald-400"
                target={60}
              />
            </div>
          </>
        )}
      </div>

      {/* 近 7 天热点问题（小程序 AI 问答页快捷提问 6 按钮的数据源） */}
      <div className="rounded-xl border bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-semibold text-gray-700">🔥 近 7 天热点问题（小程序快捷提问按钮）</p>
            <p className="mt-0.5 text-xs text-gray-400">
              每 7 天按用户真实提问自动统计一次（同义问法已聚类），小程序 AI 问答页展示为 6 个快捷按钮；也可在此手动刷新
            </p>
          </div>
          <button
            onClick={refreshHot}
            disabled={hotRefreshing}
            className="rounded-lg border border-orange-200 bg-orange-50 px-3 py-1.5 text-sm text-orange-600 transition hover:bg-orange-100 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {hotRefreshing ? '刷新中...' : '↻ 立即刷新'}
          </button>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400">
          <span>本次统计：{hot?.refreshed_at || '-'}</span>
          <span>下次自动刷新：{hot?.next_refresh_at || '-'}</span>
          <span>统计窗口：近 {hot?.window_days ?? 7} 天</span>
          {hot?.stale ? (
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-600">⚠️ 数据已过期，建议刷新</span>
          ) : (
            <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-600">✅ 数据在有效期内</span>
          )}
        </div>

        {hotLoading ? (
          <p className="py-8 text-center text-sm text-gray-400">热点问题加载中...</p>
        ) : !hot?.items?.length ? (
          <p className="py-8 text-center text-sm text-gray-400">暂无热点问题数据，点右上角「立即刷新」统计一次</p>
        ) : (
          <ul className="mt-3 grid grid-cols-1 gap-2 lg:grid-cols-2">
            {hot.items.map((h, i) => (
              <li
                key={`${h.question}-${i}`}
                className="flex items-center gap-3 rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2.5"
              >
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-white text-xs font-bold text-gray-400 shadow-sm">
                  {i + 1}
                </span>
                <span className="shrink-0 rounded-full bg-orange-100 px-2 py-0.5 text-xs font-semibold text-orange-600">
                  {h.icon} {h.label}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-gray-700" title={h.question}>
                  {h.question}
                </span>
                <span className="shrink-0 text-xs text-gray-400">{h.hit_count} 次</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="text-xs text-gray-400">
        💡 数据来源：小程序/网页端全部问答日志（question_logs）与满意度评价（feedback）。小程序端在用户累计提问 ≥3 后弹出满意度调查，可选 1-5 星提交。
      </p>
    </div>
  );
}

/** 达标率进度条（0~100%） */
function RateBar({
  label, hint, rate, color, target,
}: { label: string; hint: string; rate: number; color: string; target: number }) {
  const pct = Math.max(0, Math.min(100, rate));
  return (
    <div>
      <div className="flex items-end justify-between">
        <div>
          <p className="text-sm text-gray-700">{label}</p>
          <p className="text-xs text-gray-400">{hint}</p>
        </div>
        <p className={`text-lg font-bold ${pct >= target ? 'text-emerald-600' : 'text-orange-500'}`}>
          {pct}%
        </p>
      </div>
      <div className="mt-1.5 h-2.5 w-full overflow-hidden rounded-full bg-gray-100">
        <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
