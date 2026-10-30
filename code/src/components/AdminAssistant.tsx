// ============================================================
// 经营小助手 · 右下角悬浮弹窗（商家 PC 后台全局可用）
// ------------------------------------------------------------
// 管理员说一句话 → 后端把它变成真实操作 → 这里按时间线展示每一步，
// 高危操作弹确认卡；结束后显示确定性动作流水汇总。
// 挂载点：components/AdminLayout.tsx 的 <main> 之后
// ============================================================
import { useCallback, useEffect, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import {
  Bot, Send, Loader2, CircleCheck, CircleAlert, Trash2, Square, X,
  ShieldAlert, Sparkles, History, ImagePlus,
} from 'lucide-react';
import {
  runAssistant, approveAssistant, QUICK_PROMPTS, registerLostPhoto,
  type AssistantEvent, type Risk, type AssistantStat, type AssistantChart,
  type LostPhotoAi,
} from '@/lib/adminAssistant';

type Item =
  | { kind: 'user'; text: string }
  | { kind: 'step'; run: number; seq: number; tool: string; risk: Risk;
      status: 'running' | 'done' | 'failed'; brief?: string; elapsed?: number;
      report?: string; stats?: AssistantStat[]; charts?: AssistantChart[] }
  | { kind: 'photo'; state: 'uploading' | 'done' | 'failed'; preview: string;
      name?: string; ai?: LostPhotoAi; error?: string }
  | { kind: 'confirm'; seq: number; tool: string; preview: string; token: string;
      risk: Risk; state: 'waiting' | 'running' | 'done' | 'skipped' }
  | { kind: 'summary'; text: string }
  | { kind: 'notice'; content: string }
  | { kind: 'error'; content: string };

const RISK_CN: Record<Risk, string> = { read: '查询', write: '修改', danger: '高危' };
const RISK_CLS: Record<Risk, string> = {
  read: 'bg-slate-100 text-slate-600',
  write: 'bg-sky-100 text-sky-700',
  danger: 'bg-orange-100 text-orange-700',
};

// 数字卡配色（后端只给 tone 语义，颜色在前端定）
const TONE_CLS: Record<string, string> = {
  default: 'text-slate-800',
  money: 'text-[#26334D]',
  good: 'text-emerald-600',
  warn: 'text-orange-600',
};

// ---------- 查询类富结果的三块渲染 ----------
function StatCards({ stats }: { stats: AssistantStat[] }) {
  if (!stats.length) return null;
  return (
    <div className="mt-1.5 grid grid-cols-3 gap-1.5">
      {stats.map((s, i) => (
        <div key={i} className="rounded-md border border-slate-100 bg-white px-2 py-1.5">
          <div className="truncate text-[10px] text-slate-400" title={s.label}>{s.label}</div>
          <div className={`mt-0.5 truncate font-bold ${TONE_CLS[s.tone || 'default'] || TONE_CLS.default}`}>
            <span className="text-[13px]">{s.value}</span>
            {s.unit ? <span className="ml-0.5 text-[10px] font-normal text-slate-400">{s.unit}</span> : null}
          </div>
        </div>
      ))}
    </div>
  );
}

function ChartBlock({ charts }: { charts: AssistantChart[] }) {
  if (!charts.length) return null;
  return (
    <div className="mt-1.5 space-y-2">
      {charts.map((c, i) => (
        <div key={i} className="rounded-md border border-slate-100 bg-white p-1">
          <div className="px-1.5 pt-0.5 text-[10px] font-bold text-slate-500">{c.title}</div>
          <ReactECharts option={c.option} style={{ height: c.height || 200, width: '100%' }}
                        notMerge lazyUpdate />
        </div>
      ))}
    </div>
  );
}

function ReportBlock({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <details open className="mt-1.5 rounded-md border border-slate-100 bg-white">
      <summary className="cursor-pointer select-none px-2 py-1 text-[10px] font-bold text-slate-500">
        详细报告
      </summary>
      <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words px-2 pb-2
                      font-sans text-[11px] leading-relaxed text-slate-700">
        {text}
      </pre>
    </details>
  );
}

export default function AdminAssistant() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState('');
  const [allowDangerous, setAllowDangerous] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // 后端 seq 每轮 /run、/approve 都从 1 重新编号，不能当全局唯一键用；
  // 每个执行批发放一个自增 run 号，步骤卡按 (run, seq) 匹配——否则新一轮会把
  // 第一轮的步骤卡原地覆盖（表现为「新回答跑到最上面、和问题错位」）。
  const runIdRef = useRef(0);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [items, open]);

  // ---------- SSE 事件 → 时间线 ----------
  const handle = useCallback((e: AssistantEvent) => {
    setItems((prev) => {
      const next = [...prev];
      if (e.type === 'step') {
        const run = runIdRef.current;
        const i = next.findIndex((it) => it.kind === 'step' && it.run === run && it.seq === e.seq);
        const item: Item = { kind: 'step', run, seq: e.seq, tool: e.tool, risk: e.risk,
                             status: e.status, brief: e.brief, elapsed: e.elapsed,
                             report: e.report, stats: e.stats, charts: e.charts };
        if (i >= 0) next[i] = item;
        else next.push(item);
      } else if (e.type === 'confirm') {
        next.push({ kind: 'confirm', seq: e.seq, tool: e.tool, preview: e.preview,
                    token: e.token, risk: e.risk, state: 'waiting' });
      } else if (e.type === 'summary') {
        next.push({ kind: 'summary', text: e.text });
      } else if (e.type === 'notice') {
        next.push({ kind: 'notice', content: e.content });
      } else if (e.type === 'error') {
        next.push({ kind: 'error', content: e.content });
      }
      return next;
    });
    if (e.type === 'step') {
      setPhase(e.status === 'running' ? `正在执行 ${e.tool} …` : '');
    }
  }, []);

  // ---------- 发送一句话 ----------
  const submit = async (text?: string) => {
    const msg = (text ?? input).trim();
    if (!msg || busy) return;
    setItems((prev) => [...prev, { kind: 'user', text: msg }]);
    runIdRef.current += 1;
    setInput('');
    setBusy(true);
    setPhase('正在理解这句话要做什么 …');
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      await runAssistant(msg, allowDangerous, handle, ac.signal);
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        setItems((prev) => [...prev, { kind: 'notice', content: '已停止本次执行。' }]);
      } else {
        setItems((prev) => [...prev, { kind: 'error', content: e?.message || '执行失败' }]);
      }
    } finally {
      setBusy(false);
      setPhase('');
      abortRef.current = null;
    }
  };

  // ---------- 确认卡：执行 / 跳过 ----------
  const patchConfirm = (idx: number, state: 'waiting' | 'running' | 'done' | 'skipped') => {
    setItems((prev) => prev.map((it, i) =>
      i === idx && it.kind === 'confirm' ? { ...it, state } : it));
  };

  const approve = async (idx: number, token: string) => {
    if (busy) return;
    patchConfirm(idx, 'running');
    runIdRef.current += 1;   // 确认续跑是新一轮执行，seq 又从 1 开始
    setBusy(true);
    setPhase('正在执行已确认的操作 …');
    try {
      await approveAssistant(token, allowDangerous, handle);
      patchConfirm(idx, 'done');
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        setItems((prev) => [...prev, { kind: 'notice', content: '已停止本次执行。' }]);
        patchConfirm(idx, 'waiting');
      } else {
        setItems((prev) => [...prev, { kind: 'error', content: e?.message || '执行失败' }]);
        patchConfirm(idx, 'waiting');
      }
    } finally {
      setBusy(false);
      setPhase('');
    }
  };

  const stop = () => abortRef.current?.abort();
  const clearAll = () => {
    if (busy) return;
    setItems([]);
    setPhase('');
  };

  // ---------- 📷 失物照片直传（AI 识别 + 自动登记，不经模型） ----------
  const fileRef = useRef<HTMLInputElement | null>(null);

  const uploadLostPhoto = async (file: File) => {
    if (busy) return;
    const preview = URL.createObjectURL(file);
    setItems((prev) => [...prev, { kind: 'photo', state: 'uploading', preview }]);
    setBusy(true);
    setPhase('正在识别失物照片并登记 …');
    try {
      const item = await registerLostPhoto(file);
      setItems((prev) => prev.map((it) => (it.kind === 'photo' && it.state === 'uploading'
        ? { ...it, state: 'done', name: item.name, ai: item.ai } : it)));
    } catch (e: any) {
      setItems((prev) => prev.map((it) => (it.kind === 'photo' && it.state === 'uploading'
        ? { ...it, state: 'failed', error: e?.message || '上传失败' } : it)));
    } finally {
      setBusy(false);
      setPhase('');
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  const empty = items.length === 0;

  return (
    <>
      {/* ---------- 悬浮球 ---------- */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          title="经营小助手：说一句话，我直接办完再汇报"
          className="fixed bottom-6 right-6 z-50 flex h-14 w-14 items-center justify-center
                     rounded-full border-2 border-[#26334D] bg-white text-2xl shadow-xl
                     transition hover:scale-105 hover:shadow-2xl"
        >
          🎩
          <span className="absolute -right-0.5 -top-0.5 h-3 w-3 rounded-full bg-orange-500" />
        </button>
      )}

      {/* ---------- 抽屉 ---------- */}
      {open && (
        <div className="fixed bottom-0 right-0 z-50 flex h-[86vh] w-[440px] flex-col
                        rounded-tl-2xl border border-b-0 border-r-0 border-slate-200
                        bg-white shadow-2xl">
          {/* 顶部 */}
          <div className="flex items-start justify-between gap-2 rounded-tl-2xl
                          border-b border-slate-200 bg-[#26334D] px-4 py-3 text-white">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-bold">
                <Bot size={16} /> 经营小助手
              </div>
              <div className="mt-0.5 text-[11px] text-[#AEB8CC]">
                说一句话，我直接办完再汇报（不是聊天）
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button onClick={clearAll} disabled={busy} title="清空记录"
                      className="rounded p-1.5 text-[#AEB8CC] transition hover:bg-[#33405E]
                                 hover:text-white disabled:opacity-40">
                <Trash2 size={15} />
              </button>
              <button onClick={() => setOpen(false)} title="收起"
                      className="rounded p-1.5 text-[#AEB8CC] transition hover:bg-[#33405E] hover:text-white">
                <X size={16} />
              </button>
            </div>
          </div>

          {/* 授权开关 */}
          <label className="flex cursor-pointer items-center gap-2 border-b border-slate-100
                            bg-amber-50/60 px-4 py-2 text-[11px] text-slate-600">
            <input type="checkbox" checked={allowDangerous}
                   onChange={(e) => setAllowDangerous(e.target.checked)}
                   className="h-3.5 w-3.5 accent-orange-500" />
            <span className={allowDangerous ? 'font-bold text-orange-700' : ''}>
              本会话允许直接执行高危操作（退款 / 调币 / 删除，不再逐条确认）
            </span>
          </label>

          {/* 时间线 */}
          <div className="flex-1 overflow-y-auto px-4 py-3 text-sm">
            {empty && (
              <div className="mt-6 text-center text-xs leading-relaxed text-slate-400">
                <Sparkles className="mx-auto mb-2 text-slate-300" size={22} />
                试着说：「把 9.9 元那张小额充币下架」<br />
                「核销昨天还没核销的消费单」<br />
                「最近用户有哪些问题没答上来」
              </div>
            )}

            <div className="space-y-2">
              {items.map((it, i) => {
                if (it.kind === 'user') {
                  return (
                    <div key={i} className="flex justify-end">
                      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-[#26334D]
                                      px-3 py-2 text-xs leading-relaxed text-white">
                        {it.text}
                      </div>
                    </div>
                  );
                }
                if (it.kind === 'photo') {
                  return (
                    <div key={i} className="flex justify-end">
                      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-[#26334D]
                                      p-2 text-xs text-white">
                        <img src={it.preview} alt="失物照片"
                             className="max-h-40 rounded-lg border border-white/20" />
                        <div className="mt-1.5 px-1">
                          {it.state === 'uploading' && (
                            <div className="flex items-center gap-1 text-[#AEB8CC]">
                              <Loader2 size={12} className="animate-spin" /> 正在识别并登记 …
                            </div>
                          )}
                          {it.state === 'done' && (
                            <>
                              <div className="font-bold">
                                已登记失物：{it.name || '（未命名）'}
                              </div>
                              {!!(it.ai?.category || it.ai?.color || it.ai?.features?.length) && (
                                <div className="mt-1 flex flex-wrap gap-1">
                                  {[it.ai?.category, it.ai?.color, ...(it.ai?.features || [])]
                                    .filter(Boolean)
                                    .map((t, k) => (
                                      <span key={k} className="rounded bg-white/15 px-1.5 py-px
                                                              text-[10px] text-white/90">
                                        {t}
                                      </span>
                                    ))}
                                </div>
                              )}
                              <div className="mt-1 text-[10px] text-[#AEB8CC]">
                                已入「失物招领」，可在该页标记认领 / 删除
                              </div>
                            </>
                          )}
                          {it.state === 'failed' && (
                            <div className="leading-relaxed text-red-300">
                              {it.error || '上传失败'}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                }
                if (it.kind === 'step') {
                  // 查询类工具（后端 view=report）会带上完整报告 / 数字卡 / 图表。
                  // 有报告时不再重复显示 brief（brief 就是报告的首行）。
                  const hasRich = it.status === 'done'
                    && !!(it.report || it.stats?.length || it.charts?.length);
                  return (
                    <div key={i} className="rounded-lg border border-slate-100 bg-slate-50/70 px-2.5 py-2">
                      <div className="flex items-start gap-2">
                        <span className="mt-0.5 shrink-0">
                          {it.status === 'running' ? <Loader2 size={14} className="animate-spin text-sky-500" />
                            : it.status === 'done' ? <CircleCheck size={14} className="text-emerald-500" />
                            : <CircleAlert size={14} className="text-red-500" />}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5">
                            <span className="truncate font-mono text-[11px] text-slate-500">
                              {it.tool}
                            </span>
                            <span className={`shrink-0 rounded px-1 py-px text-[10px] ${RISK_CLS[it.risk]}`}>
                              {RISK_CN[it.risk]}
                            </span>
                            {typeof it.elapsed === 'number' && it.status !== 'running' && (
                              <span className="ml-auto shrink-0 text-[10px] text-slate-400">
                                {it.elapsed}s
                              </span>
                            )}
                          </div>
                          {(it.status === 'running' || (!hasRich && !it.report)) && (
                            <div className={`mt-0.5 text-xs leading-relaxed ${
                              it.status === 'failed' ? 'text-red-600' : 'text-slate-700'}`}>
                              {it.status === 'running' ? '正在执行 …' : (it.brief || '完成')}
                            </div>
                          )}
                        </div>
                      </div>

                      <StatCards stats={it.stats || []} />
                      <ChartBlock charts={it.charts || []} />
                      {it.status === 'done' && it.report ? <ReportBlock text={it.report} /> : null}
                    </div>
                  );
                }
                if (it.kind === 'confirm') {
                  return (
                    <div key={i} className="rounded-lg border-2 border-orange-300 bg-orange-50
                                            px-3 py-2.5">
                      <div className="flex items-center gap-1.5 text-xs font-bold text-orange-700">
                        <ShieldAlert size={14} /> 高危操作待确认
                      </div>
                      <div className="mt-1 text-xs leading-relaxed text-slate-700">
                        {it.preview || it.tool}
                      </div>
                      <div className="mt-2 flex items-center gap-2">
                        {it.state === 'waiting' && (
                          <>
                            <button onClick={() => void approve(i, it.token)} disabled={busy}
                                    className="rounded bg-orange-600 px-3 py-1 text-xs font-bold
                                               text-white transition hover:bg-orange-700
                                               disabled:opacity-50">
                              执行
                            </button>
                            <button onClick={() => patchConfirm(i, 'skipped')} disabled={busy}
                                    className="rounded border border-slate-300 px-3 py-1 text-xs
                                               text-slate-600 transition hover:bg-white disabled:opacity-50">
                              跳过
                            </button>
                            <span className="text-[10px] text-slate-400">5 分钟内有效</span>
                          </>
                        )}
                        {it.state === 'running' && (
                          <span className="flex items-center gap-1 text-xs text-slate-500">
                            <Loader2 size={12} className="animate-spin" /> 正在执行 …
                          </span>
                        )}
                        {it.state === 'done' && (
                          <span className="text-xs font-bold text-emerald-600">已执行 ✓</span>
                        )}
                        {it.state === 'skipped' && (
                          <span className="text-xs text-slate-400">已跳过（未执行）</span>
                        )}
                      </div>
                    </div>
                  );
                }
                if (it.kind === 'summary') {
                  return (
                    <div key={i} className="rounded-lg border border-slate-200 bg-white
                                            px-3 py-2.5 shadow-sm">
                      <div className="mb-1 flex items-center gap-1.5 text-xs font-bold text-[#26334D]">
                        <History size={13} /> 执行汇总
                      </div>
                      <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-slate-700">
                        {it.text}
                      </pre>
                    </div>
                  );
                }
                if (it.kind === 'notice') {
                  return (
                    <div key={i} className="rounded-lg bg-slate-50 px-3 py-2 text-xs
                                            leading-relaxed text-slate-500">
                      {it.content}
                    </div>
                  );
                }
                return (
                  <div key={i} className="rounded-lg border border-red-200 bg-red-50 px-3 py-2
                                          text-xs leading-relaxed text-red-600">
                    {it.content}
                  </div>
                );
              })}

              {phase && (
                <div className="flex items-center gap-2 px-1 py-1 text-[11px] text-slate-400">
                  <Loader2 size={12} className="animate-spin" /> {phase}
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          </div>

          {/* 快捷指令 */}
          {!busy && (
            <div className="flex flex-wrap gap-1.5 border-t border-slate-100 px-4 py-2">
              {QUICK_PROMPTS.map((q) => (
                <button key={q.label} onClick={() => void submit(q.text)}
                        className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1
                                   text-[11px] text-slate-600 transition hover:border-[#26334D]
                                   hover:text-[#26334D]">
                  {q.label}
                </button>
              ))}
            </div>
          )}

          {/* 输入区 */}
          <div className="border-t border-slate-200 px-3 py-2.5">
            <div className="flex items-end gap-2">
              <input ref={fileRef} type="file" accept="image/*" className="hidden"
                     onChange={(e) => {
                       const f = e.target.files?.[0];
                       e.target.value = '';
                       if (f) void uploadLostPhoto(f);
                     }} />
              <button onClick={() => fileRef.current?.click()} disabled={busy}
                      title="上传失物照片：AI 自动识别名称与特征并登记"
                      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg
                                 border border-slate-200 text-slate-500 transition
                                 hover:border-[#26334D] hover:text-[#26334D] disabled:opacity-40">
                <ImagePlus size={16} />
              </button>
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                rows={2}
                disabled={busy}
                placeholder="说一句话，例如：把 9.9 元那张小额充币下架（Enter 执行 / Shift+Enter 换行）"
                className="max-h-32 min-h-[44px] flex-1 resize-none rounded-lg border
                           border-slate-200 px-3 py-2 text-xs leading-relaxed outline-none
                           transition focus:border-[#26334D] disabled:bg-slate-50"
              />
              {busy ? (
                <button onClick={stop}
                        className="flex h-9 shrink-0 items-center gap-1 rounded-lg bg-slate-600
                                   px-3 text-xs font-bold text-white transition hover:bg-slate-700">
                  <Square size={13} /> 停止
                </button>
              ) : (
                <button onClick={() => void submit()} disabled={!input.trim()}
                        className="flex h-9 shrink-0 items-center gap-1 rounded-lg bg-[#26334D]
                                   px-3 text-xs font-bold text-white transition
                                   hover:bg-[#33405E] disabled:opacity-40">
                  <Send size={13} /> 执行
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
