// ============================================================
// 管理端 - 问答记录：分页表格 + 关键词搜索（question_logs 持久化数据源）
// 数据源: GET /admin/questions?page=&page_size=&keyword=
// ============================================================
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface QuestionLog {
  id: number;
  created_at: string;
  question: string;
  search_query: string;
  answer: string;
  answer_full_len: number;
  references: string[];
  elapsed_seconds: number;
  session_id: string;
  source: string;
}

interface PageData {
  total: number;
  page: number;
  page_size: number;
  pages: number;
  items: QuestionLog[];
}

const SOURCE_LABEL: Record<string, string> = {
  web: '🖥️ PC',
  miniprogram: '📱 小程序',
};

export default function AdminQuestions() {
  const [data, setData] = useState<PageData | null>(null);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [expanded, setExpanded] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (p: number, kw: string) => {
    setLoading(true);
    try {
      const res = await request<{ data: PageData }>(
        `/admin/questions?page=${p}&page_size=20&keyword=${encodeURIComponent(kw)}`,
      );
      setData(res.data);
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(page, keyword);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const onSearch = () => {
    setPage(1);
    load(1, keyword);
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-gray-800">💬 问答记录</h1>
        <div className="flex gap-2">
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onSearch()}
            placeholder="搜索问题/答案关键词..."
            className="w-64 rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-orange-400"
          />
          <button
            onClick={onSearch}
            className="rounded-lg bg-orange-500 px-4 py-2 text-sm font-bold text-white transition hover:bg-orange-600"
          >
            搜索
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">时间</th>
              <th className="px-4 py-3">来源</th>
              <th className="px-4 py-3">用户问题</th>
              <th className="px-4 py-3">耗时</th>
              <th className="px-4 py-3 text-right">详情</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : !data || data.items.length === 0 ? (
              <tr><td colSpan={5} className="px-4 py-10 text-center text-gray-400">暂无问答记录</td></tr>
            ) : (
              data.items.map((it) => (
                <>
                  <tr key={it.id} className="border-t hover:bg-gray-50/60">
                    <td className="px-4 py-3 text-xs text-gray-500">{it.created_at}</td>
                    <td className="px-4 py-3 text-xs">{SOURCE_LABEL[it.source] || it.source}</td>
                    <td className="max-w-[420px] px-4 py-3">
                      <p className="truncate font-medium text-gray-700">{it.question}</p>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">{it.elapsed_seconds}s</td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => setExpanded(expanded === it.id ? null : it.id)}
                        className="text-xs font-bold text-sky-600 hover:text-sky-700"
                      >
                        {expanded === it.id ? '收起' : '查看'}
                      </button>
                    </td>
                  </tr>
                  {expanded === it.id && (
                    <tr key={`${it.id}-detail`} className="border-t bg-orange-50/40">
                      <td colSpan={5} className="px-4 py-3">
                        <p className="mb-1 text-xs text-gray-500">
                          <b>检索改写:</b> {it.search_query || '-'}
                        </p>
                        <p className="mb-1 text-xs text-gray-500">
                          <b>参考来源:</b> {it.references.length ? it.references.join('、') : '-'}
                        </p>
                        <p className="whitespace-pre-wrap text-xs text-gray-700">
                          {it.answer || '(空)'}
                        </p>
                      </td>
                    </tr>
                  )}
                </>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      {data && data.pages > 1 && (
        <div className="flex items-center justify-between text-sm text-gray-500">
          <span>共 {data.total} 条 · 第 {data.page}/{data.pages} 页</span>
          <div className="flex gap-2">
            <button
              disabled={data.page <= 1}
              onClick={() => setPage((p) => p - 1)}
              className="rounded-lg border px-3 py-1.5 transition hover:bg-gray-50 disabled:opacity-40"
            >
              上一页
            </button>
            <button
              disabled={data.page >= data.pages}
              onClick={() => setPage((p) => p + 1)}
              className="rounded-lg border px-3 py-1.5 transition hover:bg-gray-50 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
