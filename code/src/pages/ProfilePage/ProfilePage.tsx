import { useState, type FormEvent } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { format } from 'date-fns';
import { zhCN } from 'date-fns/locale';
import { useAuth } from '@/hooks/useAuth';
import { updateUser, validateEmail } from '@/lib/auth';
import ParkDecor from '@/components/ParkDecor';
import { User, Mail, Calendar, Edit3, Check, X } from 'lucide-react';
import { useAutoAnimate } from '@formkit/auto-animate/react';

export default function ProfilePage() {
  const navigate = useNavigate();
  const { currentUser, refresh, logout } = useAuth();
  const [isEditing, setIsEditing] = useState(false);
  const [form, setForm] = useState({
    username: currentUser?.username || '',
    email: currentUser?.email || '',
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [editParent] = useAutoAnimate({ duration: 300 });

  const handleLogout = () => {
    logout();
    toast.success('已退出登录，下次见～👋');
    window.location.href = '/login';
  };

  const startEdit = () => {
    setForm({
      username: currentUser?.username || '',
      email: currentUser?.email || '',
    });
    setErrors({});
    setIsEditing(true);
  };

  const cancelEdit = () => {
    setIsEditing(false);
    setErrors({});
  };

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const newErrors: Record<string, string> = {};

    if (!form.username.trim()) newErrors.username = '请输入用户名';
    else if (form.username.trim().length < 2)
      newErrors.username = '用户名至少2个字符';

    if (!form.email.trim()) newErrors.email = '请输入邮箱';
    else if (!validateEmail(form.email.trim()))
      newErrors.email = '邮箱格式不正确';

    setErrors(newErrors);
    if (Object.keys(newErrors).length > 0) return;

    if (!currentUser) return;

    setSubmitting(true);
    setTimeout(() => {
      const result = updateUser(currentUser.id, {
        username: form.username.trim(),
        email: form.email.trim(),
      });
      setSubmitting(false);
      if (!result) {
        setErrors({ username: '用户名或邮箱已被使用' });
        toast.error('修改失败，请检查信息');
        return;
      }
      refresh();
      setIsEditing(false);
      toast.success('个人信息已更新 ✨');
    }, 400);
  };

  const registerDate = currentUser?.createdAt
    ? format(new Date(currentUser.createdAt), 'yyyy年MM月dd日', {
        locale: zhCN,
      })
    : '-';

  return (
    <div className="relative min-h-screen bg-gradient-to-br from-[#FFF0E5] via-[#FFE5F0] to-[#F0E5FF]">
      <ParkDecor />

      {/* 顶部栏 - 与 HomePage 统一风格 */}
      <header className="fixed top-3 right-3 z-50 flex gap-2">
        <button
          onClick={() => navigate('/')}
          className="bg-white rounded-full px-4 py-2 shadow-lg border-2 border-pink-300 text-sm font-bold text-pink-600
                     hover:bg-pink-50 transition flex items-center gap-2 min-h-[48px]"
          title="返回首页"
        >
          🏠 首页
        </button>
        <button
          onClick={handleLogout}
          className="bg-white rounded-full px-4 py-2 shadow-lg border-2 border-gray-300 text-sm font-bold text-gray-600
                     hover:bg-gray-50 transition flex items-center gap-2 min-h-[48px]"
          title="退出登录"
        >
          🚪 退出
        </button>
      </header>

      {/* 主内容 */}
      <main className="relative z-10 mx-auto max-w-3xl px-4 py-8 md:px-6 md:py-12 pt-16">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
        >
          {/* 头像区 */}
          <div className="mb-8 text-center">
            <h1 className="mb-4 text-xl font-bold text-[#FF6B35]">🎪 个人中心</h1>
            <motion.div
              initial={{ scale: 0.5, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ delay: 0.2, type: 'spring', stiffness: 200 }}
              className="relative mx-auto mb-4 flex h-28 w-28 items-center justify-center rounded-full bg-gradient-to-br from-[#FF6B9D] via-[#FFD93D] to-[#6BCFFF] p-1 shadow-lg shadow-[#FF6B9D]/30 md:h-32 md:w-32"
            >
              <div className="flex h-full w-full items-center justify-center rounded-full bg-white text-5xl md:text-6xl">
                🧸
              </div>
              <motion.div
                className="absolute -bottom-1 -right-1 flex h-9 w-9 items-center justify-center rounded-full bg-[#FFD93D] text-lg shadow-md"
                animate={{ rotate: [0, 10, -10, 0] }}
                transition={{
                  duration: 2,
                  repeat: Infinity,
                  repeatDelay: 3,
                }}
              >
                ⭐
              </motion.div>
            </motion.div>
            <h2 className="text-2xl font-bold text-[#5A3A5A] md:text-3xl">
              {currentUser?.username}
            </h2>
            <p className="mt-1 text-sm text-[#8B7B8B]">{currentUser?.email}</p>
          </div>

          {/* 信息卡片 */}
          <div className="overflow-hidden rounded-[28px] border-4 border-white/80 bg-white/90 shadow-xl shadow-[#FF6B9D]/10 backdrop-blur-md">
            <div className="border-b border-[#FFE5F0] bg-gradient-to-r from-[#FFE5F0] to-[#FFF0E5] px-6 py-4 md:px-8">
              <div className="flex items-center justify-between">
                <h3 className="flex items-center gap-2 text-lg font-bold text-[#7A5A7A]">
                  <User className="h-5 w-5 text-[#FF6B9D]" />
                  个人信息
                </h3>
                {!isEditing && (
                  <motion.button
                    whileHover={{ scale: 1.08 }}
                    whileTap={{ scale: 0.92 }}
                    onClick={startEdit}
                    className="flex items-center gap-1.5 rounded-full bg-white px-3 py-1.5 text-xs font-semibold text-[#FF6B9D] shadow-sm ring-1 ring-[#FFD9EC] transition-all hover:bg-[#FFF8FC] md:text-sm"
                  >
                    <Edit3 className="h-3.5 w-3.5" />
                    编辑
                  </motion.button>
                )}
              </div>
            </div>

            <div ref={editParent} className="p-6 md:p-8">
              <AnimatePresence mode="wait">
                {!isEditing ? (
                  <motion.div
                    key="view"
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: 10 }}
                    transition={{ duration: 0.25 }}
                    className="space-y-5"
                  >
                    <InfoRow icon={<User className="h-5 w-5" />} label="用户名" value={currentUser?.username || '-'} color="#FF6B9D" />
                    <InfoRow icon={<Mail className="h-5 w-5" />} label="邮箱" value={currentUser?.email || '-'} color="#6BCFFF" />
                    <InfoRow icon={<Calendar className="h-5 w-5" />} label="注册时间" value={registerDate} color="#FFD93D" />
                  </motion.div>
                ) : (
                  <motion.form
                    key="edit"
                    initial={{ opacity: 0, x: 10 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: -10 }}
                    transition={{ duration: 0.25 }}
                    onSubmit={handleSubmit}
                    className="space-y-4"
                  >
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                        用户名
                      </label>
                      <input
                        type="text"
                        value={form.username}
                        onChange={(e) => {
                          setForm((prev) => ({
                            ...prev,
                            username: e.target.value,
                          }));
                          if (errors.username) {
                            setErrors((prev) => {
                              const next = { ...prev };
                              delete next.username;
                              return next;
                            });
                          }
                        }}
                        className="w-full rounded-2xl border-2 border-[#FFD9EC] bg-[#FFF8FC] px-4 py-3 text-[#5A3A5A] outline-none transition-all focus:border-[#FF6B9D] focus:bg-white focus:ring-4 focus:ring-[#FF6B9D]/20"
                      />
                      {errors.username && (
                        <p className="pt-1 text-xs text-[#FF6B9D]">
                          {errors.username}
                        </p>
                      )}
                    </div>
                    <div>
                      <label className="mb-1.5 block text-sm font-medium text-[#7A5A7A]">
                        邮箱
                      </label>
                      <input
                        type="email"
                        value={form.email}
                        onChange={(e) => {
                          setForm((prev) => ({
                            ...prev,
                            email: e.target.value,
                          }));
                          if (errors.email) {
                            setErrors((prev) => {
                              const next = { ...prev };
                              delete next.email;
                              return next;
                            });
                          }
                        }}
                        className="w-full rounded-2xl border-2 border-[#CFE8FF] bg-[#F5FAFF] px-4 py-3 text-[#5A3A5A] outline-none transition-all focus:border-[#6BCFFF] focus:bg-white focus:ring-4 focus:ring-[#6BCFFF]/20"
                      />
                      {errors.email && (
                        <p className="pt-1 text-xs text-[#FF6B9D]">
                          {errors.email}
                        </p>
                      )}
                    </div>

                    <div className="flex gap-3 pt-2">
                      <motion.button
                        type="submit"
                        disabled={submitting}
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.97 }}
                        className="flex flex-1 items-center justify-center gap-1.5 rounded-full bg-gradient-to-r from-[#FF6B9D] to-[#FF8C42] px-6 py-3 text-sm font-bold text-white shadow-md shadow-[#FF6B9D]/30 disabled:opacity-60"
                      >
                        <Check className="h-4 w-4" />
                        {submitting ? '保存中...' : '保存修改'}
                      </motion.button>
                      <motion.button
                        type="button"
                        onClick={cancelEdit}
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.97 }}
                        className="flex items-center justify-center gap-1.5 rounded-full bg-[#F5F0F8] px-6 py-3 text-sm font-semibold text-[#7A5A7A] ring-1 ring-[#E8DFF0]"
                      >
                        <X className="h-4 w-4" />
                        取消
                      </motion.button>
                    </div>
                  </motion.form>
                )}
              </AnimatePresence>
            </div>
          </div>

          {/* 统计卡片 */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.4, duration: 0.5 }}
            className="mt-6 grid grid-cols-3 gap-4 md:mt-8"
          >
            <StatCard emoji="🎡" label="游玩项目" value="3" color="#FF6B9D" />
            <StatCard emoji="🎟️" label="门票天数" value="7" color="#FFD93D" />
            <StatCard emoji="🏆" label="成就徽章" value="5" color="#6BCFFF" />
          </motion.div>

          {/* 退出登录 */}
          <button
            onClick={handleLogout}
            className="mt-8 w-full rounded-full bg-white/80 py-3.5 text-sm font-bold text-gray-500 shadow-sm border-2 border-gray-200 transition-all hover:bg-white hover:text-red-500 hover:border-red-200"
          >
            🚪 退出登录
          </button>
        </motion.div>
      </main>
    </div>
  );
}

interface InfoRowProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  color: string;
}

function InfoRow({ icon, label, value, color }: InfoRowProps) {
  return (
    <div className="flex items-center gap-4">
      <div
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl"
        style={{ backgroundColor: `${color}20`, color }}
      >
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-[#B090B0]">{label}</div>
        <div className="truncate text-base font-semibold text-[#5A3A5A]">
          {value}
        </div>
      </div>
    </div>
  );
}

interface StatCardProps {
  emoji: string;
  label: string;
  value: string;
  color: string;
}

function StatCard({ emoji, label, value, color }: StatCardProps) {
  return (
    <motion.div
      whileHover={{ y: -4, scale: 1.03 }}
      className="rounded-2xl border-2 border-white/80 bg-white/90 p-4 text-center shadow-md backdrop-blur-md"
      style={{ boxShadow: `0 4px 16px ${color}20` }}
    >
      <div className="mb-1 text-2xl">{emoji}</div>
      <div
        className="text-2xl font-bold tabular-nums"
        style={{ color }}
      >
        {value}
      </div>
      <div className="mt-0.5 text-xs text-[#8B7B8B]">{label}</div>
    </motion.div>
  );
}
