// ============================================================
// 管理端 - 知识库智能体
// 聊天式界面，把「知识库没答上来的问题」变成知识库资产：
//   发送指令 → 统计未命中 → 生成标准答案(≤N token) → 导出 Word
//   管理员改完 Word 回传 → 解析 → 切片 → 入库（立即可被检索）
// 接口：POST /admin/kb-agent/analyze (SSE) / POST /admin/kb-agent/ingest (SSE)
//      GET  /admin/kb-agent/download/{name} / GET /admin/kb-agent/docs
// ============================================================
import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { toast } from 'sonner';
import {
  Bot, Send, Paperclip, Download, FileText, Loader2, CircleCheck,
  CircleAlert, Sparkles, Trash2, Upload, Square, RefreshCw, X,
} from 'lucide-react';
import {
  analyzeGaps, ingestDocs, downloadDoc, listDocs, QUICK_PROMPTS,
  type KbEvent, type KbStep, type KbGapItem, type KbAnswerItem,
  type KbArtifact, type KbDocItem,
} from '@/lib/kbAgent';

// ---------- 会话消息模型 ----------
interface Msg {
  id: string;
  role: 'user' | 'agent';
  text: string;                       // agent 用 Markdown 渲染
  steps?: KbStep[];
  intentText?: string;
  dataset?: KbGapItem[] | null;
  answers?: KbAnswerItem[];
  progress?: { current: number; total: number; label?: string } | null;
  artifact?: KbArtifact | null;
  summary?: string;
  loading?: boolean;
  error?: string;
  ts: number;
}

const SESSION_KEY = 'funpark_kb_agent_session_v1';
const mdComponents = {
  p: ({ children }: any) => <p className="mb-2 leading-relaxed last:mb-0">{children}</p>,
  strong: ({ children }: any) => <strong className="font-bold text-orange-600">{children}</strong>,
  ol: ({ children }: any) => <ol className="my-1 ml-1 list-decimal list-outside space-y-1.5 pl-4">{children}</ol>,
  ul: ({ children }: any) => <ul className="my-1 ml-1 list-disc list-outside space-y-1.5 pl-4">{children}</ul>,
  li: ({ children }: any) => <li className="pl-1 leading-relaxed">{children}</li>,
  table: ({ children }: any) => <table className="my-2 w-full border-collapse text-xs">{children}</table>,
  th: ({ children }: any) => <th className="border border-gray-200 bg-gray-50 px-2 py-1 text-left font-bold">{children}</th>,
  td: ({ children }: any) => <td className="border border-gray-200 px-2 py-1">{children}</td>,
  hr: () => <hr className="my-2 border-gray-200" />,
  a: ({ href, children }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-sky-600 underline">{children}</a>
  ),
};

const uid = () => `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

function loadSession(): Msg[] {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    // 刷新后不允许残留 loading（流已断），统一收尾
    return Array.isArray(arr) ? arr.map((m: Msg) => ({ ...m, loading: false })) : [];
  } catch {
    return [];
  }
}

function fmtSize(n: number): string {
  if (!n) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

const STEP_ICON: Record<string, string> = {
  parse: '🧭', collect: '📊', answer: '✍️', docx: '📄', index: '🗂️',
};

export default function AdminKbAgent() {
  const [messages, setMessages] = useState<Msg[]>(loadSession);
  const [input, setInput] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [docs, setDocs] = useState<KbDocItem[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // ---------- 会话持久化 ----------
  useEffect(() => {
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify(messages.slice(-40)));
    } catch { /* 超配额忽略 */ }
  }, [messages]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, messages[messages.length - 1]?.answers?.length]);

  const refreshDocs = useCallback(async () => {
    setDocsLoading(true);
    try {
      setDocs(await listDocs());
    } finally {
      setDocsLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshDocs();
    return () => abortRef.current?.abort();
  }, [refreshDocs]);

  // ---------- 把 SSE 事件写进"当前 agent 气泡" ----------
  const patch = useCallback((id: string, fn: (m: Msg) => Msg) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? fn(m) : m)));
  }, []);

  const run = useCallback(
    async (userText: string, mode: 'analyze' | 'ingest', payload: { file?: File; replaceSource?: string }) => {
      const userMsg: Msg = { id: uid(), role: 'user', text: userText, ts: Date.now() };
      const agentId = uid();
      const agentMsg: Msg = { id: agentId, role: 'agent', text: '', steps: [], answers: [], ts: Date.now(), loading: true };
      setMessages((prev) => [...prev, userMsg, agentMsg]);
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;

      const onEvent = (e: KbEvent) => {
        switch (e.type) {
          case 'intent':
            patch(agentId, (m) => ({
              ...m,
              intentText: e.intent === 'ingest'
                ? `已识别：回传入库 → ${e.doc_name ?? ''}`
                : `已识别：统计未命中 → 生成标准答案 → 导出 Word（${e.days ? `近 ${e.days} 天` : '不限时间'} · 最多 ${e.limit} 条 · 每条 ≤${e.max_tokens} token）`,
            }));
            break;
          case 'step':
            patch(agentId, (m) => {
              const steps = [...(m.steps || [])];
              const i = steps.findIndex((s) => s.stage === e.stage);
              const next: KbStep = { stage: e.stage, status: e.status, title: e.title, detail: e.detail };
              if (i >= 0) steps[i] = next;
              else steps.push(next);
              return { ...m, steps };
            });
            break;
          case 'dataset':
            patch(agentId, (m) => ({ ...m, dataset: e.items }));
            break;
          case 'progress':
            patch(agentId, (m) => ({
              ...m,
              progress: { current: e.current, total: e.total, label: e.label },
            }));
            break;
          case 'answer':
            patch(agentId, (m) => ({ ...m, answers: [...(m.answers || []), e] }));
            break;
          case 'artifact':
            patch(agentId, (m) => ({ ...m, artifact: e }));
            break;
          case 'assistant':
            patch(agentId, (m) => ({ ...m, text: e.content }));
            break;
          case 'done':
            patch(agentId, (m) => ({
              ...m,
              loading: false,
              progress: null,
              // 注意：analyze 的 done.summary 里没有 chunks 字段（那是 ingest 的），
              // 之前统一按"入库 N 个切片"渲染，导致统计任务尾部错显"入库 0 个切片"。
              summary: mode === 'ingest'
                ? `用时 ${e.elapsed}s · 入库 ${e.summary?.chunks ?? 0} 个切片`
                : (e.summary?.processed != null
                    ? `用时 ${e.elapsed}s · 处理 ${e.summary.processed} 个问题`
                      + `（兜底回答记录 ${e.summary.raw_total ?? 0} 条 / 去重 ${e.summary.distinct ?? 0} 个）`
                    : `用时 ${e.elapsed}s`),
            }));
            break;
          case 'error':
            patch(agentId, (m) => ({ ...m, loading: false, progress: null, error: e.content }));
            toast.error(e.content);
            break;
        }
      };

      try {
        if (mode === 'analyze') {
          await analyzeGaps({ message: userText }, onEvent, controller.signal);
        } else if (payload.file) {
          await ingestDocs(payload.file, payload.replaceSource || '', onEvent, controller.signal);
        }
        if (mode === 'ingest') {
          refreshDocs();
          toast.success('已回传入库，知识库即刻生效');
        }
      } catch (err: any) {
        if (err?.name === 'AbortError') {
          patch(agentId, (m) => ({ ...m, loading: false, progress: null, error: '已停止本次任务' }));
          toast.message('已停止');
        } else {
          const msg = err?.message || '请求失败';
          patch(agentId, (m) => ({ ...m, loading: false, progress: null, error: msg }));
          toast.error(msg);
        }
      } finally {
        patch(agentId, (m) => ({ ...m, loading: false, progress: null }));
        setBusy(false);
        abortRef.current = null;
      }
    },
    [patch, refreshDocs],
  );

  // ---------- 发送 ----------
  const handleSend = () => {
    if (busy) return;
    if (file) {
      const f = file;
      setFile(null);
      if (fileRef.current) fileRef.current.value = '';
      run(`回传文档入库：${f.name}`, 'ingest', { file: f, replaceSource: f.name });
      return;
    }
    const text = input.trim();
    if (!text) {
      toast.message('请输入指令，或先选择要回传的 Word 文档');
      return;
    }
    setInput('');
    run(text, 'analyze', {});
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
  };

  const clearSession = () => {
    setMessages([]);
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch { /* ignore */ }
    toast.message('已清空本次会话');
  };

  const onDownload = async (name: string) => {
    try {
      await downloadDoc(name);
      toast.success('已开始下载，改完记得回传入库');
    } catch (e: any) {
      toast.error(e?.message || '下载失败');
    }
  };

  return (
    <div className="flex h-[calc(100vh-3rem)] gap-4">
      {/* ---------------- 主对话区 ---------------- */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-white shadow-sm">
        {/* 头部 */}
        <div className="flex items-center justify-between border-b bg-gradient-to-r from-orange-50 to-amber-50 px-4 py-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange-500 text-white">
              <Bot size={18} />
            </div>
            <div>
              <p className="text-sm font-bold text-gray-800">知识库智能体</p>
              <p className="text-xs text-gray-500">统计盲区 → 生成标准答案 → 导出 Word → 回传入库</p>
            </div>
          </div>
          <button
            onClick={clearSession}
            className="flex items-center gap-1 rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs text-gray-500 transition hover:bg-white hover:text-gray-700"
          >
            <Trash2 size={13} /> 清空会话
          </button>
        </div>

        {/* 消息流 */}
        <div className="flex-1 space-y-4 overflow-y-auto bg-[#FAFBFC] px-4 py-4">
          {messages.length === 0 && <EmptyGuide onPick={(t) => setInput(t)} />}

          {messages.map((m) =>
            m.role === 'user' ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-orange-500 px-3.5 py-2.5 text-sm text-white shadow-sm">
                  {m.text}
                </div>
              </div>
            ) : (
              <div key={m.id} className="flex gap-2.5">
                <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-orange-100 text-orange-600">
                  <Sparkles size={14} />
                </div>
                <div className="min-w-0 flex-1 space-y-2.5">
                  {m.intentText && (
                    <p className="text-xs text-gray-400">{m.intentText}</p>
                  )}

                  {/* 步骤卡片 */}
                  {!!m.steps?.length && (
                    <div className="space-y-1.5 rounded-xl border bg-white p-2.5">
                      {m.steps.map((s, i) => (
                        <div key={i} className="flex items-start gap-2 text-xs">
                          <span className="mt-px">
                            {s.status === 'running' ? (
                              <Loader2 size={13} className="animate-spin text-sky-500" />
                            ) : s.status === 'failed' ? (
                              <CircleAlert size={13} className="text-red-500" />
                            ) : (
                              <CircleCheck size={13} className="text-emerald-500" />
                            )}
                          </span>
                          <span className="shrink-0 text-gray-400">{STEP_ICON[s.stage] || '•'}</span>
                          <span className="font-medium text-gray-700">{s.title}</span>
                          {s.detail && <span className="min-w-0 flex-1 truncate text-gray-400">{s.detail}</span>}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 进度条 */}
                  {m.progress && (
                    <div className="rounded-xl border bg-white p-2.5">
                      <div className="mb-1.5 flex justify-between text-xs text-gray-500">
                        <span>正在生成标准答案…</span>
                        <span>{m.progress.current}/{m.progress.total}</span>
                      </div>
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
                        <div
                          className="h-full rounded-full bg-orange-400 transition-all"
                          style={{ width: `${Math.round((m.progress.current / Math.max(1, m.progress.total)) * 100)}%` }}
                        />
                      </div>
                      {m.progress.label && <p className="mt-1 truncate text-xs text-gray-400">{m.progress.label}</p>}
                    </div>
                  )}

                  {/* 统计结果表 */}
                  {!!m.dataset?.length && (
                    <div className="overflow-hidden rounded-xl border bg-white">
                      <div className="border-b bg-gray-50 px-3 py-2 text-xs font-bold text-gray-600">
                        📊 知识库盲区清单（{m.dataset.length} 条，按被提问次数排序）
                        <span className="ml-1.5 font-normal text-gray-400">
                          只含「模型没检索到资料 → 给建议」的兜底回答
                        </span>
                      </div>
                      <div className="max-h-64 overflow-y-auto">
                        <table className="w-full text-xs">
                          <thead className="bg-gray-50/70 text-left text-gray-500">
                            <tr>
                              <th className="px-3 py-1.5">#</th>
                              <th className="px-3 py-1.5">用户问题</th>
                              <th className="px-3 py-1.5 text-center">次数</th>
                              <th className="px-3 py-1.5">原回答</th>
                              <th className="px-3 py-1.5">最近提问</th>
                            </tr>
                          </thead>
                          <tbody>
                            {m.dataset.map((it, i) => (
                              <tr key={i} className="border-t">
                                <td className="px-3 py-1.5 text-gray-400">{i + 1}</td>
                                <td className="px-3 py-1.5 text-gray-700">{it.question}</td>
                                <td className="px-3 py-1.5 text-center font-bold text-orange-600">{it.count}</td>
                                <td className="whitespace-nowrap px-3 py-1.5">
                                  {it.fallback_type === 'fallback_partial' ? (
                                    <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[11px] text-sky-600">
                                      资料不足建议
                                    </span>
                                  ) : (
                                    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-600">
                                      无资料建议
                                    </span>
                                  )}
                                </td>
                                <td className="px-3 py-1.5 text-gray-400">{String(it.last_seen || '').slice(0, 16)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {/* 标准答案预览 */}
                  {!!m.answers?.length && (
                    <div className="overflow-hidden rounded-xl border bg-white">
                      <div className="border-b bg-gray-50 px-3 py-2 text-xs font-bold text-gray-600">
                        ✍️ 标准答案（{m.answers.length} 条，每条 ≤300 token）
                      </div>
                      <div className="max-h-80 divide-y overflow-y-auto">
                        {m.answers.map((a, i) => (
                          <div key={i} className="px-3 py-2.5">
                            <div className="flex items-start justify-between gap-2">
                              <p className="text-xs font-bold text-gray-700">
                                {i + 1}. {a.question}
                              </p>
                              <span className="shrink-0 whitespace-nowrap text-[11px] text-gray-400">
                                {a.tokens} token{a.truncated ? ' · 已截断' : ''}
                              </span>
                            </div>
                            <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-gray-600">
                              {a.answer || '（待补充）'}
                            </p>
                            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
                              {a.needs_review && (
                                <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-600">⚠ 需人工补充</span>
                              )}
                              {/* 补答依据 = 生成答案时现查到的资料，不是原回答的依据（原回答恰恰是没资料） */}
                              <span className="min-w-0 truncate text-gray-400">
                                补答依据（当前知识库）：{a.sources?.length ? a.sources.join('、') : '检索后仍无可用资料'}
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* 产物卡片 */}
                  {m.artifact && (
                    <div className="flex items-center justify-between gap-3 rounded-xl border border-orange-200 bg-orange-50/60 px-3 py-2.5">
                      <div className="flex min-w-0 items-center gap-2">
                        <FileText size={18} className="shrink-0 text-orange-500" />
                        <div className="min-w-0">
                          <p className="truncate text-xs font-bold text-gray-700">{m.artifact.name}</p>
                          <p className="text-[11px] text-gray-400">
                            {m.artifact.kind === 'docx'
                              ? `${fmtSize(m.artifact.size)} · ${m.artifact.items} 条待确认`
                              : `已入库 · ${m.artifact.items} 个切片`}
                          </p>
                        </div>
                      </div>
                      {m.artifact.kind === 'docx' && (
                        <button
                          onClick={() => onDownload(m.artifact!.name)}
                          className="flex shrink-0 items-center gap-1 rounded-lg bg-orange-500 px-3 py-1.5 text-xs font-bold text-white transition hover:bg-orange-600"
                        >
                          <Download size={13} /> 下载 Word
                        </button>
                      )}
                    </div>
                  )}

                  {/* 文字总结 */}
                  {m.text && (
                    <div className="rounded-xl border bg-white px-3.5 py-3 text-sm text-gray-700">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                        {m.text}
                      </ReactMarkdown>
                    </div>
                  )}

                  {/* 错误 */}
                  {m.error && (
                    <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{m.error}</p>
                  )}

                  {/* 加载中 */}
                  {m.loading && !m.progress && (
                    <div className="flex items-center gap-2 text-xs text-gray-400">
                      <Loader2 size={13} className="animate-spin" /> 处理中…
                    </div>
                  )}

                  {m.summary && <p className="text-[11px] text-gray-400">{m.summary}</p>}
                </div>
              </div>
            ),
          )}
          <div ref={bottomRef} />
        </div>

        {/* 附件提示 */}
        {file && (
          <div className="flex items-center justify-between gap-2 border-t bg-amber-50 px-4 py-2 text-xs text-amber-700">
            <span className="flex min-w-0 items-center gap-1.5">
              <Upload size={13} /> 待回传入库：<b className="truncate">{file.name}</b>（{fmtSize(file.size)}）
            </span>
            <button
              onClick={() => {
                setFile(null);
                if (fileRef.current) fileRef.current.value = '';
              }}
              className="shrink-0 text-amber-600 hover:text-amber-800"
            >
              <X size={14} />
            </button>
          </div>
        )}

        {/* 输入区 */}
        <div className="border-t bg-white px-4 py-3">
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {QUICK_PROMPTS.map((q) => (
              <button
                key={q.label}
                disabled={busy}
                onClick={() => setInput(q.text)}
                className="rounded-full border border-gray-200 px-2.5 py-1 text-[11px] text-gray-500 transition hover:border-orange-300 hover:bg-orange-50 hover:text-orange-600 disabled:opacity-50"
              >
                {q.label}
              </button>
            ))}
          </div>

          <div className="flex items-end gap-2">
            <input
              ref={fileRef}
              type="file"
              accept=".docx,.txt,.pdf"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0] || null;
                if (f) setFile(f);
              }}
            />
            <button
              onClick={() => fileRef.current?.click()}
              disabled={busy}
              title="选择改好的 Word 回传入库"
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-gray-200 text-gray-500 transition hover:border-orange-300 hover:text-orange-500 disabled:opacity-50"
            >
              <Paperclip size={17} />
            </button>

            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              rows={2}
              placeholder="例：帮我统计所有未检索到知识库的用户问题，基于知识库精细化回答，token 保持在 300 以内，并生成文档供我修改"
              className="min-h-[42px] flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-orange-400"
            />

            {busy ? (
              <button
                onClick={stop}
                className="flex h-10 shrink-0 items-center gap-1.5 rounded-lg bg-gray-700 px-4 text-sm font-bold text-white transition hover:bg-gray-800"
              >
                <Square size={14} /> 停止
              </button>
            ) : (
              <button
                onClick={handleSend}
                className="flex h-10 shrink-0 items-center gap-1.5 rounded-lg bg-orange-500 px-4 text-sm font-bold text-white transition hover:bg-orange-600"
              >
                <Send size={15} /> {file ? '回传入库' : '发送'}
              </button>
            )}
          </div>
          <p className="mt-1.5 text-[11px] text-gray-400">
            Enter 发送 · Shift+Enter 换行 · 点 📎 选择管理员改好的 Word 回传，智能体会自动解析 → 切片 → 入库
          </p>
        </div>
      </div>

      {/* ---------------- 右侧：生成的文档 ---------------- */}
      <aside className="hidden w-72 shrink-0 flex-col overflow-hidden rounded-xl border bg-white shadow-sm xl:flex">
        <div className="flex items-center justify-between border-b px-3.5 py-3">
          <p className="text-sm font-bold text-gray-800">📄 已生成的 Word</p>
          <button
            onClick={refreshDocs}
            className="text-gray-400 transition hover:text-orange-500"
            title="刷新"
          >
            <RefreshCw size={14} className={docsLoading ? 'animate-spin' : ''} />
          </button>
        </div>
        <div className="flex-1 space-y-2 overflow-y-auto p-3">
          {docs.length === 0 ? (
            <p className="py-8 text-center text-xs text-gray-400">
              {docsLoading ? '加载中…' : '还没有生成过文档'}
            </p>
          ) : (
            docs.map((d) => (
              <div key={d.name} className="rounded-lg border p-2.5">
                <p className="truncate text-xs font-medium text-gray-700" title={d.name}>
                  {d.name}
                </p>
                <div className="mt-1 flex items-center justify-between">
                  <span className="text-[11px] text-gray-400">
                    {fmtSize(d.size)} · {new Date(d.modified * 1000).toLocaleDateString('zh-CN')}
                  </span>
                  <button
                    onClick={() => onDownload(d.name)}
                    className="flex items-center gap-0.5 text-[11px] font-bold text-sky-600 hover:text-sky-700"
                  >
                    <Download size={11} /> 下载
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
        <div className="border-t bg-gray-50 px-3.5 py-2.5 text-[11px] leading-relaxed text-gray-400">
          提示：Word 里每个问题是一行 <code className="rounded bg-white px-1">## </code> 标题，
          请勿删除该标记 —— 回传时按它「一题一切片」。
        </div>
      </aside>
    </div>
  );
}

// ---------- 空态引导 ----------
function EmptyGuide({ onPick }: { onPick: (t: string) => void }) {
  const flow = [
    { icon: '📊', title: '1. 统计盲区', desc: '只挑模型没检索到资料、只好给建议的问题' },
    { icon: '✍️', title: '2. 生成标准答案', desc: '基于知识库现有资料，防幻觉、≤300 token' },
    { icon: '📄', title: '3. 导出 Word', desc: '一题一块，管理员直接改' },
    { icon: '🗂️', title: '4. 回传入库', desc: '解析 → 切片 → 向量库/关键词索引，即刻可检索' },
  ];
  return (
    <div className="mx-auto max-w-2xl py-6">
      <div className="mb-5 text-center">
        <div className="mx-auto mb-2.5 flex h-12 w-12 items-center justify-center rounded-2xl bg-orange-100 text-orange-500">
          <Bot size={24} />
        </div>
        <p className="text-base font-bold text-gray-800">你好，我是知识库智能体</p>
        <p className="mt-1 text-xs text-gray-500">
          把「用户问了但知识库答不上来」的问题，变成知识库里的标准答案
        </p>
      </div>

      <div className="mb-5 grid grid-cols-2 gap-2.5">
        {flow.map((f) => (
          <div key={f.title} className="rounded-xl border bg-white p-3">
            <p className="text-sm font-bold text-gray-700">
              {f.icon} {f.title}
            </p>
            <p className="mt-0.5 text-xs text-gray-500">{f.desc}</p>
          </div>
        ))}
      </div>

      <p className="mb-2 text-xs font-bold text-gray-500">试试这些指令：</p>
      <div className="space-y-2">
        {QUICK_PROMPTS.map((q) => (
          <button
            key={q.label}
            onClick={() => onPick(q.text)}
            className="w-full rounded-lg border bg-white px-3 py-2.5 text-left text-xs text-gray-600 transition hover:border-orange-300 hover:bg-orange-50"
          >
            <b className="mr-1.5 text-orange-600">{q.label}</b>
            <span className="text-gray-400">{q.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
