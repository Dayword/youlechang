// ============================================================
// 管理端 - 知识库管理：文档列表 / 上传异步入库（任务轮询）/ 删除 / 清缓存
// 数据源: GET /knowledge/list, POST /knowledge/upload, GET /knowledge/task/{id},
//         DELETE /knowledge/{doc}, POST /admin/cache/clear
// ============================================================
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { request } from '@/lib/api';

interface DocItem {
  name: string;
  size: number;
  modified: number;
  chunk_count: number;
}

interface IngestTask {
  status: string;        // pending / running / success / failed
  result?: { success: boolean; chunk_count?: number; error?: string };
}

export default function AdminKnowledge() {
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [taskStatus, setTaskStatus] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadDocs = async () => {
    try {
      const res = await request<{ data: DocItem[] }>('/knowledge/list');
      setDocs(res.data || []);
    } catch (e: any) {
      toast.error(e.message || '加载文档列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDocs();
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const handleUpload = async (file: File) => {
    setUploading(true);
    setTaskStatus('上传中...');
    try {
      // 上传用原生 fetch（multipart 不用 JSON header）
      const token = localStorage.getItem('funpark_admin_token') || '';
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(
        `${(window as any).__API_BASE__ || `http://${window.location.hostname}:8000`}/knowledge/upload`,
        { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form },
      );
      const json = await res.json();
      if (!res.ok) throw new Error(json?.detail || '上传失败');

      const taskId: string = json.data.task_id;
      // 轮询异步入库任务状态
      timerRef.current = setInterval(async () => {
        try {
          const t = await request<{ data: IngestTask }>(`/knowledge/task/${taskId}`);
          const st = t.data?.status || 'pending';
          setTaskStatus(`入库任务: ${st}`);
          if (st === 'success' || st === 'failed') {
            if (timerRef.current) clearInterval(timerRef.current);
            if (st === 'success') {
              const n = t.data.result?.chunk_count ?? 0;
              toast.success(`入库完成，切分 ${n} 个 chunk`);
            } else {
              toast.error(`入库失败: ${t.data.result?.error || '未知错误'}`);
            }
            setTaskStatus('');
            setUploading(false);
            loadDocs();
          }
        } catch {
          /* 轮询失败下次再试 */
        }
      }, 2000);
    } catch (e: any) {
      toast.error(e.message || '上传失败');
      setTaskStatus('');
      setUploading(false);
    }
  };

  const handleDelete = async (name: string) => {
    if (!window.confirm(`确认删除文档「${name}」？将同时从向量库与 BM25 索引移除。`)) return;
    try {
      await request(`/knowledge/${encodeURIComponent(name)}`, { method: 'DELETE' });
      toast.success('已删除');
      loadDocs();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    }
  };

  const handleClearCache = async () => {
    if (!window.confirm('确认清空所有问答/检索缓存？（知识库变更后建议执行）')) return;
    try {
      await request('/admin/cache/clear', { method: 'POST' });
      toast.success('缓存已清空');
    } catch (e: any) {
      toast.error(e.message || '清缓存失败');
    }
  };

  const fmtSize = (n: number) =>
    n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)}MB` : `${(n / 1024).toFixed(1)}KB`;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-800">📚 知识库管理</h1>
        <div className="flex gap-2">
          <button
            onClick={handleClearCache}
            className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-600 transition hover:bg-gray-50"
          >
            🗑️ 清空缓存
          </button>
          <button
            disabled={uploading}
            onClick={() => fileRef.current?.click()}
            className="rounded-lg bg-orange-500 px-4 py-2 text-sm font-bold text-white shadow transition hover:bg-orange-600 disabled:opacity-60"
          >
            {uploading ? '处理中...' : '⬆️ 上传文档'}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.txt,.docx,.doc"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleUpload(f);
              e.target.value = '';
            }}
          />
        </div>
      </div>

      {taskStatus && (
        <div className="rounded-lg border border-sky-200 bg-sky-50 px-4 py-2 text-sm text-sky-700">
          ⏳ {taskStatus}
        </div>
      )}

      <div className="overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">文档名</th>
              <th className="px-4 py-3">大小</th>
              <th className="px-4 py-3">Chunk 数</th>
              <th className="px-4 py-3">更新时间</th>
              <th className="px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : docs.length === 0 ? (
              <tr><td colSpan={5} className="px-4 py-10 text-center text-gray-400">暂无文档，点右上角上传第一个知识库文档</td></tr>
            ) : (
              docs.map((d) => (
                <tr key={d.name} className="border-t hover:bg-gray-50/60">
                  <td className="px-4 py-3 font-medium text-gray-700">📄 {d.name}</td>
                  <td className="px-4 py-3 text-gray-500">{fmtSize(d.size)}</td>
                  <td className="px-4 py-3">
                    <span className="rounded-full bg-sky-50 px-2 py-0.5 text-xs font-bold text-sky-600">
                      {d.chunk_count}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {d.modified ? new Date(d.modified * 1000).toLocaleString() : '-'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleDelete(d.name)}
                      className="text-xs font-bold text-red-500 transition hover:text-red-600"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-gray-400">
        💡 支持 PDF / TXT / DOCX / DOC，最大 50MB；PDF 走 OCR 解析，异步入库完成后自动清空缓存并重载问法库。
      </p>
    </div>
  );
}
