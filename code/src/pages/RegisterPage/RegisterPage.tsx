import { useState, type FormEvent } from 'react';
import { motion } from 'framer-motion';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { register, validateEmail } from '@/lib/auth';
import ParkDecor from '@/components/ParkDecor';
import { useAutoAnimate } from '@formkit/auto-animate/react';

export default function RegisterPage() {
  const [form, setForm] = useState({
    username: '',
    email: '',
    password: '',
    confirmPassword: '',
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [errorParent] = useAutoAnimate({ duration: 250 });

  const handleChange = (field: keyof typeof form, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    if (errors[field]) {
      setErrors((prev) => {
        const next = { ...prev };
        delete next[field];
        return next;
      });
    }
  };

  const onSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const newErrors: Record<string, string> = {};

    if (!form.username.trim()) newErrors.username = '请输入用户名';
    else if (form.username.trim().length < 2)
      newErrors.username = '用户名至少2个字符';

    if (!form.email.trim()) newErrors.email = '请输入邮箱';
    else if (!validateEmail(form.email.trim()))
      newErrors.email = '邮箱格式不正确';

    if (!form.password) newErrors.password = '请输入密码';
    else if (form.password.length < 6) newErrors.password = '密码至少6位';

    if (!form.confirmPassword)
      newErrors.confirmPassword = '请再次输入密码';
    else if (form.password !== form.confirmPassword)
      newErrors.confirmPassword = '两次输入的密码不一致';

    setErrors(newErrors);
    if (Object.keys(newErrors).length > 0) return;

    setSubmitting(true);
    setTimeout(() => {
      const result = register({
        username: form.username.trim(),
        email: form.email.trim(),
        password: form.password,
      });
      setSubmitting(false);
      if (!result.success) {
        const field = result.error?.includes('用户名')
          ? 'username'
          : result.error?.includes('邮箱')
            ? 'email'
            : 'password';
        setErrors({ [field]: result.error || '注册失败' });
        toast.error(result.error || '注册失败，请重试');
        return;
      }
      toast.success(`注册成功！欢迎加入，${result.user?.username} 🎊`);
      window.location.href = '/';
    }, 500);
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-[#FFE5F0] via-[#FFF7E0] to-[#E0F4FF] px-4 py-8">
      <ParkDecor />

      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="relative z-10 w-full max-w-md"
      >
        <motion.div
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.2, duration: 0.5, type: 'spring' }}
          className="mb-6 text-center"
        >
          <div className="mb-3 text-6xl">🎠</div>
          <h1 className="bg-gradient-to-r from-[#FF6B9D] via-[#FFB347] to-[#6BCFFF] bg-clip-text text-3xl font-bold text-transparent md:text-4xl">
            欢乐游乐场
          </h1>
          <p className="mt-2 text-sm text-[#8B7B8B]">
            注册账号，一起开启欢乐之旅～
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3, duration: 0.5 }}
          className="rounded-[28px] border-4 border-white/80 bg-white/90 p-6 shadow-[0_12px_40px_rgba(255_107_157_0.2)] backdrop-blur-md md:p-8"
        >
          <h2 className="mb-6 text-center text-2xl font-bold text-[#5A3A5A]">
            注册 🎪
          </h2>

          <form onSubmit={onSubmit} className="space-y-3.5">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                用户名
              </label>
              <input
                type="text"
                value={form.username}
                onChange={(e) => handleChange('username', e.target.value)}
                placeholder="给自己取个可爱的名字吧"
                className="w-full rounded-2xl border-2 border-[#FFD9EC] bg-[#FFF8FC] px-4 py-3 text-[#5A3A5A] placeholder-[#C9A8C9] outline-none transition-all focus:border-[#FF6B9D] focus:bg-white focus:ring-4 focus:ring-[#FF6B9D]/20"
              />
              <div ref={errorParent} className="min-h-[18px]">
                {errors.username && (
                  <p className="pt-1 text-xs text-[#FF6B9D]">
                    {errors.username}
                  </p>
                )}
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                邮箱
              </label>
              <input
                type="email"
                value={form.email}
                onChange={(e) => handleChange('email', e.target.value)}
                placeholder="your@email.com"
                className="w-full rounded-2xl border-2 border-[#FFD9EC] bg-[#FFF8FC] px-4 py-3 text-[#5A3A5A] placeholder-[#C9A8C9] outline-none transition-all focus:border-[#FF6B9D] focus:bg-white focus:ring-4 focus:ring-[#FF6B9D]/20"
              />
              <div ref={errorParent} className="min-h-[18px]">
                {errors.email && (
                  <p className="pt-1 text-xs text-[#FF6B9D]">{errors.email}</p>
                )}
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                密码
              </label>
              <input
                type="password"
                value={form.password}
                onChange={(e) => handleChange('password', e.target.value)}
                placeholder="至少6位，越安全越好哦"
                className="w-full rounded-2xl border-2 border-[#FFD9EC] bg-[#FFF8FC] px-4 py-3 text-[#5A3A5A] placeholder-[#C9A8C9] outline-none transition-all focus:border-[#FF6B9D] focus:bg-white focus:ring-4 focus:ring-[#FF6B9D]/20"
              />
              <div ref={errorParent} className="min-h-[18px]">
                {errors.password && (
                  <p className="pt-1 text-xs text-[#FF6B9D]">
                    {errors.password}
                  </p>
                )}
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                确认密码
              </label>
              <input
                type="password"
                value={form.confirmPassword}
                onChange={(e) =>
                  handleChange('confirmPassword', e.target.value)
                }
                placeholder="再输入一次密码"
                className="w-full rounded-2xl border-2 border-[#FFD9EC] bg-[#FFF8FC] px-4 py-3 text-[#5A3A5A] placeholder-[#C9A8C9] outline-none transition-all focus:border-[#FF6B9D] focus:bg-white focus:ring-4 focus:ring-[#FF6B9D]/20"
              />
              <div ref={errorParent} className="min-h-[18px]">
                {errors.confirmPassword && (
                  <p className="pt-1 text-xs text-[#FF6B9D]">
                    {errors.confirmPassword}
                  </p>
                )}
              </div>
            </div>

            <motion.button
              type="submit"
              disabled={submitting}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.97 }}
              className="mt-4 w-full rounded-full bg-gradient-to-r from-[#FF6B9D] via-[#FF8C42] to-[#FFD93D] px-6 py-3.5 text-base font-bold text-white shadow-lg shadow-[#FF6B9D]/30 transition-all hover:shadow-xl hover:shadow-[#FF6B9D]/40 disabled:opacity-60"
            >
              {submitting ? '注册中...' : '立即注册 🎉'}
            </motion.button>
          </form>

          <div className="mt-6 text-center text-sm text-[#8B7B8B]">
            已有账号？
            <Link
              to="/login"
              className="ml-1 font-bold text-[#FF6B9D] transition-colors hover:text-[#FF8C42]"
            >
              去登录 →
            </Link>
          </div>
        </motion.div>
      </motion.div>
    </div>
  );
}
