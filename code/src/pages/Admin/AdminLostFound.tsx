// ============================================================
// 管理端 - 失物招领：游客报失 + 商家登记的失物统一管理
// 数据源:
//   POST   /admin/lost-found/items          商家登记失物（名称+照片 multipart，时间自动生成）
//   GET    /admin/lost-found/items          失物统一管理列表（分页 + 状态/来源筛选）
//   PUT    /admin/lost-found/items/{id}/status   审核通过/驳回、标记已认领/撤销
//   DELETE /admin/lost-found/items/{id}          删除失物（同步清理照片）
//   POST   /lost-found/analyze-photo        照片预识别（选完照片即调，自动填名 + 出特征标签）
//
// 审核流：游客报失落 pending，必须在「待审核」里通过（→ lost）才会出现在小程序的
// 失物认领列表；驳回则变 rejected，永不对外展示。商家自己登记的直接上架（lost）。
//
// 识别结果（ai）会随失物落库，列表里直接展示特征标签，
// 游客报失时商家可比对「颜色 / 类别 / 外观特征」更快找到对应失物。
// ============================================================
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { request, API_BASE, getToken } from '@/lib/api';

/** 照片的模型识别结果（后端 services/vision_service.py 产出） */
interface LostAiInsight {
  ok: boolean;
  suggested_name: string;
  category: string;
  color: string;
  brand: string;
  features: string[];
  visible_text: string;
  confidence: number;
  summary: string;
  error?: string;
}

/** 失物上已落库的识别标签 */
interface LostAiTags {
  category: string;
  color: string;
  brand: string;
  features: string[];
  visible_text: string;
}

interface LostItem {
  id: number;
  name: string;
  photo_url: string;   // /static/lost_found/xxx.jpg（需拼接 API_BASE）
  status: string;      // pending 待审核 / lost 待认领 / claimed 已认领 / rejected 已驳回
  source: string;      // user 游客报失 / merchant 商家登记
  reporter: string;
  created_at: string;
  ai?: LostAiTags;     // 照片识别特征（未识别时各字段为空）
}

/** 列表里的识别特征小标签 */
function AiTags({ ai }: { ai?: LostAiTags }) {
  if (!ai) return null;
  const chips = [ai.category, ai.color, ai.brand, ...(ai.features || []).slice(0, 3)].filter(Boolean);
  if (!chips.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {chips.map((c, i) => (
        <span key={i} className="rounded-full bg-slate-100 px-1.5 py-px text-[10px] text-slate-500">
          {c}
        </span>
      ))}
    </div>
  );
}

interface ListResp {
  data: {
    total: number;
    page: number;
    pages: number;
    items: LostItem[];
  };
}

const STATUS_TABS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '待审核' },
  { value: 'lost', label: '待认领' },
  { value: 'claimed', label: '已认领' },
  { value: 'rejected', label: '已驳回' },
];

/** 状态徽章：游客能看到的只有「待认领 / 已认领」，pending/rejected 仅后台可见 */
const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  pending: { label: '🕓 待审核', cls: 'bg-orange-100 text-orange-600' },
  lost: { label: '⏳ 待认领', cls: 'bg-amber-100 text-amber-600' },
  claimed: { label: '✅ 已认领', cls: 'bg-emerald-100 text-emerald-600' },
  rejected: { label: '🚫 已驳回', cls: 'bg-rose-100 text-rose-600' },
};

/** 各状态可执行的状态流转（删除按钮所有状态都有，单独渲染） */
const STATUS_ACTIONS: Record<string, { label: string; next: string; cls: string }[]> = {
  pending: [
    { label: '✓ 审核通过', next: 'lost', cls: 'border-emerald-200 text-emerald-600 hover:bg-emerald-50' },
    { label: '✗ 驳回', next: 'rejected', cls: 'border-rose-200 text-rose-500 hover:bg-rose-50' },
  ],
  lost: [
    { label: '✓ 标记已认领', next: 'claimed', cls: 'border-emerald-200 text-emerald-600 hover:bg-emerald-50' },
  ],
  claimed: [
    { label: '↺ 恢复待认领', next: 'lost', cls: 'border-amber-200 text-amber-600 hover:bg-amber-50' },
  ],
  rejected: [
    { label: '↺ 通过上架', next: 'lost', cls: 'border-emerald-200 text-emerald-600 hover:bg-emerald-50' },
  ],
};

const SOURCE_TABS = [
  { value: '', label: '全部来源' },
  { value: 'user', label: '游客报失' },
  { value: 'merchant', label: '商家登记' },
];

const resolveImg = (url: string) => (url && url.startsWith('http') ? url : API_BASE + url);

export default function AdminLostFound() {
  const [items, setItems] = useState<LostItem[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [source, setSource] = useState('');
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  // 登记表单
  const [name, setName] = useState('');
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState('');
  const [uploading, setUploading] = useState(false);
  // 照片预识别：选完图立刻让模型看图，自动把「失物名称」填好并给出特征标签
  const [analyzing, setAnalyzing] = useState(false);
  const [aiInsight, setAiInsight] = useState<LostAiInsight | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async (p = page, s = status, src = source, kw = keyword) => {
    setLoading(true);
    try {
      const res = await request<ListResp>(
        `/admin/lost-found/items?page=${p}&page_size=20&status=${encodeURIComponent(s)}&source=${encodeURIComponent(src)}&keyword=${encodeURIComponent(kw)}`
      );
      setItems(res.data.items || []);
      setTotal(res.data.total);
      setPages(Math.max(1, res.data.pages));
      setPage(res.data.page);
    } catch (e: any) {
      toast.error(e.message || '加载失物列表失败');
    } finally {
      setLoading(false);
    }
  }, [page, status, source, keyword]);

  useEffect(() => {
    load(1, status, source, keyword);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, source]);

  // 把照片交给多模态模型识别 → 自动填名 + 出特征标签（失败静默降级为手填）
  const recognize = async (file: File) => {
    setAnalyzing(true);
    setAiInsight(null);
    try {
      const form = new FormData();
      form.append('photo', file);
      form.append('name', name.trim());
      const res = await fetch(API_BASE + '/lost-found/analyze-photo', {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}` },
        body: form,
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || '识别失败');
      const insight: LostAiInsight | undefined = json?.data;
      if (insight?.ok) {
        setAiInsight(insight);
        // 只在商家还没自己填名字时才自动补，绝不动手填的内容
        setName((prev) => prev.trim() || insight.suggested_name || '');
      }
    } catch (e: any) {
      setAiInsight(null);
      console.warn('[失物识别]', e?.message);
    } finally {
      setAnalyzing(false);
    }
  };

  // 选择照片（校验类型与大小）
  const handlePickPhoto = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp'].includes(file.type)) {
      toast.error('仅支持图片文件（jpg/png/webp/gif/bmp）');
      e.target.value = '';
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      toast.error('照片过大，最大支持 10MB');
      e.target.value = '';
      return;
    }
    setPhotoFile(file);
    setPhotoPreview(URL.createObjectURL(file));
    void recognize(file);
  };

  const resetPhoto = () => {
    setPhotoFile(null);
    setPhotoPreview('');
    setAiInsight(null);
    setAnalyzing(false);
    if (fileRef.current) fileRef.current.value = '';
  };

  // 商家登记失物（multipart 上传，时间由后端自动生成）
  const handleUpload = async () => {
    if (!photoFile) {
      toast.error('请上传失物照片');
      return;
    }
    // 名称可留空：优先用 AI 建议名（服务端还会再兜一次）
    const trimName = name.trim() || (aiInsight?.ok ? aiInsight.suggested_name.trim() : '');
    if (!trimName) {
      toast.error('请填写失物名称（或换一张更清晰的照片让 AI 识别）');
      return;
    }
    setUploading(true);
    try {
      const form = new FormData();
      form.append('name', trimName);
      form.append('photo', photoFile);
      const res = await fetch(API_BASE + '/admin/lost-found/items', {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}` },
        body: form,
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = json?.detail;
        throw new Error((typeof detail === 'object' ? detail?.msg : detail) || '登记失败');
      }
      if (json?.code !== 200) throw new Error(json?.msg || '登记失败');
      toast.success(json.msg || '失物已登记');
      setName('');
      resetPhoto();
      load(1, status, source, keyword);
    } catch (e: any) {
      toast.error(e.message || '登记失败');
    } finally {
      setUploading(false);
    }
  };

  // 状态流转：审核通过 / 驳回 / 标记已认领 / 撤销
  const handleStatus = async (item: LostItem, next: string) => {
    try {
      const res = await request(`/admin/lost-found/items/${item.id}/status`, {
        method: 'PUT',
        body: { status: next },
      });
      toast.success((res as any).msg || '状态已更新');
      load();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    }
  };

  // 删除失物
  const handleDelete = async (item: LostItem) => {
    try {
      const res = await request(`/admin/lost-found/items/${item.id}`, { method: 'DELETE' });
      toast.success((res as any).msg || '已删除');
      load();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    }
  };

  const counts = {
    pending: items.filter((i) => i.status === 'pending').length,
    lost: items.filter((i) => i.status === 'lost').length,
    claimed: items.filter((i) => i.status === 'claimed').length,
  };

  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold text-gray-800">🧸 失物招领</h1>

      {/* 登记表单：商家上传失物（名称 + 照片，时间自动生成） */}
      <div className="rounded-xl border bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-sm font-bold text-amber-600">
          📦 登记失物（拾获物品入库后，游客可在小程序「失物认领」中查看）
        </h2>
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-64">
            <label className="mb-1 block text-xs text-gray-500">失物名称</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleUpload()}
              placeholder="如：粉色水杯、黑色钱包"
              className="w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-amber-400"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-gray-500">失物照片</label>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              onChange={handlePickPhoto}
              className="hidden"
            />
            {photoPreview ? (
              <div className="flex items-center gap-2">
                <div className="relative">
                  <img src={photoPreview} alt="预览" className="h-12 w-12 rounded-lg border object-cover" />
                  <button
                    onClick={resetPhoto}
                    className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-gray-700 text-xs text-white hover:bg-gray-900"
                  >×</button>
                </div>
                <div className="min-w-0 max-w-xs">
                  <div className="truncate text-xs text-gray-400">{photoFile?.name}</div>
                  {analyzing ? (
                    <div className="mt-0.5 text-xs text-amber-600">🔍 正在识别照片，稍后自动填名…</div>
                  ) : aiInsight?.ok ? (
                    <div className="mt-0.5 flex flex-wrap items-center gap-1">
                      <span className="text-xs font-bold text-emerald-600">✨ 已识别</span>
                      {[aiInsight.category, aiInsight.color, aiInsight.brand,
                        ...aiInsight.features.slice(0, 3)].filter(Boolean).map((c, i) => (
                        <span key={i}
                              className="rounded-full border border-amber-200 bg-amber-50 px-1.5 py-px text-[10px] text-amber-700">
                          {c}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            ) : (
              <button
                onClick={() => fileRef.current?.click()}
                className="flex h-12 w-48 items-center justify-center rounded-lg border-2 border-dashed text-sm text-gray-400 hover:border-amber-400 hover:text-amber-500"
              >
                📷 点击上传照片
              </button>
            )}
          </div>
          <button
            onClick={handleUpload}
            disabled={uploading || analyzing}
            className="rounded-lg bg-amber-500 px-5 py-2 text-sm font-bold text-white hover:bg-amber-600 disabled:opacity-60"
          >
            {analyzing ? '识别中...' : uploading ? '登记中...' : '登记失物'}
          </button>
          <p className="text-xs text-gray-400">
            ⏱ 登记时间由系统自动生成，与游客报失统一管理；传照片后 AI 会自动填好名称与特征
          </p>
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
                status === t.value ? 'bg-orange-500 font-bold text-white' : 'bg-white text-gray-600 hover:bg-gray-50'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex overflow-hidden rounded-lg border">
          {SOURCE_TABS.map((t) => (
            <button
              key={t.value}
              onClick={() => setSource(t.value)}
              className={`px-3 py-1.5 text-sm transition ${
                source === t.value ? 'bg-sky-500 font-bold text-white' : 'bg-white text-gray-600 hover:bg-gray-50'
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
            onKeyDown={(e) => e.key === 'Enter' && load(1, status, source, keyword)}
            placeholder="搜索名称 / 颜色 / 特征（如 绿色、水杯）"
            className="w-full rounded-lg border px-3 py-1.5 text-sm outline-none focus:border-orange-400"
          />
          <button
            onClick={() => load(1, status, source, keyword)}
            className="rounded-lg bg-orange-500 px-4 py-1.5 text-sm font-bold text-white hover:bg-orange-600"
          >
            搜索
          </button>
        </div>
        <button onClick={() => load()} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
          ↻ 刷新
        </button>
      </div>

      {/* 失物列表表格 */}
      <div className="overflow-x-auto overflow-hidden rounded-xl border bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-3">照片</th>
              <th className="px-4 py-3">失物名称</th>
              <th className="px-4 py-3">来源</th>
              <th className="px-4 py-3">报失人</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">登记时间</th>
              <th className="px-4 py-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="px-4 py-10 text-center text-gray-400">加载中...</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={7} className="px-4 py-10 text-center text-gray-400">暂无失物记录（游客报失与商家登记都会出现在这里）</td></tr>
            ) : (
              items.map((item) => (
                <tr key={item.id} className="border-t hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    {item.photo_url ? (
                      <img
                        src={resolveImg(item.photo_url)}
                        alt={item.name}
                        className="h-12 w-12 rounded-lg border object-cover"
                        onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2'; }}
                      />
                    ) : (
                      <span className="flex h-12 w-12 items-center justify-center rounded-lg bg-gray-100 text-xl">🧸</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-700">{item.name}</div>
                    <AiTags ai={item.ai} />
                  </td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                      item.source === 'user' ? 'bg-violet-100 text-violet-600' : 'bg-sky-100 text-sky-600'
                    }`}>
                      {item.source === 'user' ? '👤 游客报失' : '🏪 商家登记'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{item.reporter || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                      (STATUS_BADGE[item.status] || STATUS_BADGE.lost).cls
                    }`}>
                      {(STATUS_BADGE[item.status] || STATUS_BADGE.lost).label}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{item.created_at || '-'}</td>
                  <td className="px-4 py-3">
                    <span className="flex flex-wrap items-center gap-2">
                      {(STATUS_ACTIONS[item.status] || []).map((a) => (
                        <button
                          key={a.next}
                          onClick={() => handleStatus(item, a.next)}
                          className={`rounded-lg border px-2.5 py-1 text-xs font-bold transition ${a.cls}`}
                        >
                          {a.label}
                        </button>
                      ))}
                      <button
                        onClick={() => handleDelete(item)}
                        className="rounded-lg border border-rose-200 px-2.5 py-1 text-xs font-bold text-rose-500 hover:bg-rose-50"
                      >
                        删除
                      </button>
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <span>
          共 {total} 条记录（当前页 {counts.pending} 待审核 / {counts.lost} 待认领 / {counts.claimed} 已认领）
        </span>
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

      <p className="text-xs text-gray-400">
        💡 游客在小程序提交的报失会先进入「待审核」，点「审核通过」后才会出现在小程序的失物认领列表；
        「驳回」的内容永不对外展示。商家自己登记的直接上架，无需审核。
        「标记已认领」后该失物不再展示给游客认领。
        照片均由 AI 识别出颜色/类别/外观特征，搜索框支持按这些特征查找（如搜「绿色」找绿色物品）。
      </p>
    </div>
  );
}
