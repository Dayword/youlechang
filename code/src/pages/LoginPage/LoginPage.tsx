// ============================================================
// 管理端登录页（商务风格）
// - 左侧深蓝品牌区（与后台侧栏同色系 #26334D）+ 右侧白色登录卡片
// - 面向园区运营人员：无卡通装饰、无 emoji、无渐变，中性克制的视觉
// ============================================================
import { useState, type FormEvent } from 'react';
import { toast } from 'sonner';
import { Eye, EyeOff, Loader2, ShieldCheck } from 'lucide-react';
import { loginAdmin } from '@/lib/admin-auth';
import { ApiError } from '@/lib/api';

/** 左侧品牌区的能力要点（让管理员一眼知道后台能做什么） */
const CAPABILITIES = [
  { title: '知识库管理', desc: '上传文档、查看切片、维护检索索引' },
  { title: '问答记录与分析', desc: '真实游客提问、意图分布、响应耗时' },
  { title: '知识库智能体', desc: '统计答不上来的问题，补齐后回传入库' },
  { title: '卡产品与账单', desc: '储值卡配置、办卡订单、退款与核销' },
];

export default function LoginPage() {
  const [account, setAccount] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!account.trim()) {
      setError('请输入用户名');
      return;
    }
    if (!password) {
      setError('请输入密码');
      return;
    }

    setSubmitting(true);
    setError('');
    try {
      const user = await loginAdmin(account.trim(), password);
      toast.success(`欢迎回来，${user.nickname || user.username}`);
      window.location.href = '/admin';
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : '登录失败，请稍后重试';
      setError(msg);
      toast.error(msg);
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen bg-[#F5F6FA]">
      {/* ---------------- 左侧品牌区 ---------------- */}
      <aside className="relative hidden w-[42%] max-w-[560px] flex-col justify-between bg-[#26334D] px-10 py-12 text-white lg:flex">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-white/10 text-base">
              🎡
            </span>
            <span className="text-base font-bold tracking-wide">游乐园运营管理后台</span>
          </div>

          <h1 className="mt-14 text-2xl font-bold leading-snug text-white">
            RAG 智能客服
            <br />
            运营与知识库一体化管理
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-[#AEB8CC]">
            面向园区运营人员的后台系统：维护知识库、跟踪游客提问、
            持续把答不上来的问题补成标准答案。
          </p>

          <ul className="mt-10 space-y-5">
            {CAPABILITIES.map((c) => (
              <li key={c.title} className="flex gap-3">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-[#F97316]" />
                <div>
                  <p className="text-sm font-medium text-white">{c.title}</p>
                  <p className="mt-0.5 text-xs leading-relaxed text-[#8B97B0]">{c.desc}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <p className="text-xs text-[#6C7A96]">
          仅限授权管理人员使用 · 所有操作均有日志记录
        </p>
      </aside>

      {/* ---------------- 右侧登录区 ---------------- */}
      <main className="flex flex-1 items-center justify-center px-5 py-10">
        <div className="w-full max-w-[380px]">
          {/* 小屏顶部标识 */}
          <div className="mb-8 flex items-center gap-2 lg:hidden">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#26334D] text-sm text-white">
              🎡
            </span>
            <span className="text-sm font-bold text-gray-800">游乐园运营管理后台</span>
          </div>

          <h2 className="text-xl font-bold text-gray-800">管理员登录</h2>
          <p className="mt-1 text-sm text-gray-500">请使用管理员账号登录后台</p>

          <form onSubmit={onSubmit} className="mt-8 space-y-5">
            <div>
              <label htmlFor="account" className="mb-1.5 block text-sm font-medium text-gray-700">
                用户名
              </label>
              <input
                id="account"
                type="text"
                autoComplete="username"
                value={account}
                onChange={(e) => {
                  setAccount(e.target.value);
                  if (error) setError('');
                }}
                placeholder="请输入管理员用户名"
                className="w-full rounded-lg border border-gray-300 bg-white px-3.5 py-2.5 text-sm text-gray-800 outline-none transition placeholder:text-gray-400 focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
              />
            </div>

            <div>
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-gray-700">
                密码
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPwd ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    if (error) setError('');
                  }}
                  placeholder="请输入密码"
                  className="w-full rounded-lg border border-gray-300 bg-white px-3.5 py-2.5 pr-11 text-sm text-gray-800 outline-none transition placeholder:text-gray-400 focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
                <button
                  type="button"
                  onClick={() => setShowPwd((v) => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1.5 text-gray-400 transition hover:text-gray-600"
                  title={showPwd ? '隐藏密码' : '显示密码'}
                  aria-label={showPwd ? '隐藏密码' : '显示密码'}
                >
                  {showPwd ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>

            {/* 错误提示（固定高度，避免抖动） */}
            <div className="min-h-[18px]">
              {error && <p className="text-xs text-red-600">{error}</p>}
            </div>

            <button
              type="submit"
              disabled={submitting}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#26334D] px-4 py-2.5 text-sm font-bold text-white transition hover:bg-[#303D59] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting && <Loader2 size={15} className="animate-spin" />}
              {submitting ? '登录中…' : '登录'}
            </button>
          </form>

          <div className="mt-8 space-y-2 border-t border-gray-200 pt-5">
            <p className="flex items-center gap-1.5 text-xs text-gray-500">
              <ShieldCheck size={13} className="text-gray-400" />
              首次部署请及时修改默认管理员密码
            </p>
            <p className="text-xs text-gray-400">游客问答请使用微信小程序</p>
          </div>
        </div>
      </main>
    </div>
  );
}
