// ============================================================
// 管理端 - Agent 参数配置：检索/分块/LLM 参数热更新（重启恢复默认）
// 数据源: GET /config/agent, POST /config/agent
// ============================================================
import { useEffect, useState, type FormEvent } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface RetrievalCfg { top_k: number; rerank_top: number; similarity_threshold: number; hybrid_alpha: number; }
interface ChunkingCfg { chunk_size: number; chunk_overlap: number; }
interface LlmCfg { model: string; temperature: number; }
interface ModelsCfg { embedding: string; reranker: string; }
interface AgentCfg {
  /** 后端声明"真正支持热更新"的键；其余键写进去也不会生效 */
  hot_updatable: string[];
  /** 只读键 → 生效方式说明 */
  readonly_reason: Record<string, string>;
  retrieval: RetrievalCfg;
  chunking: ChunkingCfg;
  llm: LlmCfg;
  models: ModelsCfg;
  cache: { ttl: number };
}

export default function AdminConfig() {
  const [cfg, setCfg] = useState<AgentCfg | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const res = await request<{ data: AgentCfg }>('/config/agent');
        setCfg(res.data);
      } catch (e: any) {
        toast.error(e.message || '加载配置失败');
      }
    })();
  }, []);

  const patchRetrieval = (k: keyof RetrievalCfg, v: string) => {
    if (!cfg) return;
    const num = parseFloat(v);
    setCfg({ ...cfg, retrieval: { ...cfg.retrieval, [k]: isNaN(num) ? 0 : num } });
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!cfg) return;
    setSaving(true);
    try {
      const res = await request<{ msg?: string }>('/config/agent', {
        method: 'POST',
        body: {
          top_k: cfg.retrieval.top_k,
          rerank_top: cfg.retrieval.rerank_top,
          similarity_threshold: cfg.retrieval.similarity_threshold,
          hybrid_alpha: cfg.retrieval.hybrid_alpha,
          chunk_size: cfg.chunking.chunk_size,
          chunk_overlap: cfg.chunking.chunk_overlap,
          llm_temperature: cfg.llm.temperature,
          cache_ttl: cfg.cache.ttl,
        },
      });
      toast.success(res?.msg || '配置已更新');
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (!cfg) return <div className="p-10 text-center text-gray-400">⚙️ 加载中...</div>;

  const isHot = (k: string) => (cfg.hot_updatable || []).includes(k);
  const roHint = (k: string, base?: string) =>
    isHot(k) ? base : cfg.readonly_reason?.[k] || '该参数不支持热更新，需改 config.py 后重启';

  const numField = (label: string, value: number, onChange: (v: string) => void,
                    hint?: string, step = '1', min?: number, max?: number,
                    hotKey?: string) => {
    const ro = !!hotKey && !isHot(hotKey);
    return (
      <div>
        <label className="mb-1 block text-sm text-gray-500">
          {label}
          {ro && <span className="ml-1 rounded bg-gray-100 px-1 text-[10px] text-gray-400">只读</span>}
        </label>
        <input
          type="number" value={value} step={step}
          min={min} max={max}
          readOnly={ro} disabled={ro}
          onChange={(e) => onChange(e.target.value)}
          className={`w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-orange-400${
            ro ? ' cursor-not-allowed bg-gray-100 text-gray-400' : ''
          }`}
        />
        {hint && <p className="mt-1 text-xs text-gray-400">{hint}</p>}
      </div>
    );
  };

  return (
    <div className="max-w-2xl space-y-5">
      <div>
        <h1 className="text-xl font-bold text-gray-800">⚙️ Agent 参数配置</h1>
        <p className="mt-1 text-xs text-gray-400">
          仅带「可热更」标记的参数支持在线修改并立即生效；其余为只读，需修改 config.py 后重启服务
        </p>
      </div>

      <form onSubmit={onSubmit} className="space-y-5">
        <fieldset className="rounded-xl border bg-white p-5 shadow-sm">
          <legend className="px-2 text-sm font-bold text-orange-500">🔎 检索参数</legend>
          <div className="grid grid-cols-2 gap-4">
            {numField('召回数量 TOP_K', cfg.retrieval.top_k, (v) => patchRetrieval('top_k', v), roHint('top_k', '混合检索最终返回条数'), '1', 1, 50, 'top_k')}
            {numField('精排保留 RERANK_TOP', cfg.retrieval.rerank_top, (v) => patchRetrieval('rerank_top', v), roHint('rerank_top', '送入 LLM 上下文的条数'), '1', 1, 20, 'rerank_top')}
            {numField('相似度阈值', cfg.retrieval.similarity_threshold, (v) => patchRetrieval('similarity_threshold', v), roHint('similarity_threshold', '低于此分过滤'), '0.05', 0, 1, 'similarity_threshold')}
            {numField('向量权重 α', cfg.retrieval.hybrid_alpha, (v) => patchRetrieval('hybrid_alpha', v), roHint('hybrid_alpha', '0=纯BM25，1=纯语义（可热更）'), '0.05', 0, 1, 'hybrid_alpha')}
          </div>
        </fieldset>

        <fieldset className="rounded-xl border bg-white p-5 shadow-sm">
          <legend className="px-2 text-sm font-bold text-sky-500">✂️ 分块参数</legend>
          <div className="grid grid-cols-2 gap-4">
            {numField('Chunk 大小（token）', cfg.chunking.chunk_size, (v) =>
              setCfg({ ...cfg, chunking: { ...cfg.chunking, chunk_size: parseInt(v) || 0 } }), roHint('chunk_size'), '1', 64, undefined, 'chunk_size')}
            {numField('Chunk 重叠（token）', cfg.chunking.chunk_overlap, (v) =>
              setCfg({ ...cfg, chunking: { ...cfg.chunking, chunk_overlap: parseInt(v) || 0 } }), roHint('chunk_overlap'), '1', 0, undefined, 'chunk_overlap')}
          </div>
        </fieldset>

        <fieldset className="rounded-xl border bg-white p-5 shadow-sm">
          <legend className="px-2 text-sm font-bold text-violet-500">🤖 LLM 与缓存</legend>
          <div className="grid grid-cols-2 gap-4">
            {numField('Temperature', cfg.llm.temperature, (v) =>
              setCfg({ ...cfg, llm: { ...cfg.llm, temperature: parseFloat(v) || 0 } }), roHint('llm_temperature', '越低越严谨'), '0.05', 0, 2, 'llm_temperature')}
            {numField('缓存 TTL（秒）', cfg.cache.ttl, (v) =>
              setCfg({ ...cfg, cache: { ...cfg.cache, ttl: parseInt(v) || 0 } }), roHint('cache_ttl'), '1', 0, undefined, 'cache_ttl')}
          </div>
        </fieldset>

        <div className="rounded-xl border border-dashed border-gray-300 bg-gray-50 p-4 text-xs text-gray-500">
          <b>当前模型：</b>LLM {cfg.llm.model} · Embedding {cfg.models.embedding} · Reranker {cfg.models.reranker}
        </div>

        <button
          type="submit" disabled={saving}
          className="w-full rounded-xl bg-orange-500 py-3 text-sm font-bold text-white shadow transition hover:bg-orange-600 disabled:opacity-60"
        >
          {saving ? '保存中...' : '💾 保存'}
        </button>
      </form>
    </div>
  );
}
