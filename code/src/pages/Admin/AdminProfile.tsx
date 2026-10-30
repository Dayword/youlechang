// ============================================================
// 管理端 - 个人信息
// 三块：头像上传 / 基本信息（昵称·邮箱·用户名）/ 修改密码
// 接口：GET /auth/me、PUT /auth/me、POST /auth/avatar
// 注意：改用户名或密码后后端签发的新 token 才能生效（JWT.sub 就是用户名），
//       所以这两项保存成功后会强制重新登录。
// ============================================================
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Camera, Loader2, KeyRound, UserCog, ShieldAlert } from 'lucide-react';
import { API_BASE, getToken, request } from '@/lib/api';
import { clearAdminSession, patchAdminUser, type AdminUser } from '@/lib/admin-auth';

interface MeData extends AdminUser {
  phone?: string;
  created_at?: string;
}

const ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
const MAX_AVATAR = 5 * 1024 * 1024;

export default function AdminProfile() {
  const [me, setMe] = useState<MeData | null>(null);
  const [loading, setLoading] = useState(true);

  const [nickname, setNickname] = useState('');
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [savingInfo, setSavingInfo] = useState(false);

  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [savingPwd, setSavingPwd] = useState(false);

  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await request<{ data: MeData }>('/auth/me');
        setMe(res.data);
        setNickname(res.data.nickname || '');
        setEmail(res.data.email || '');
        setUsername(res.data.username || '');
        patchAdminUser(res.data);
      } catch (e: any) {
        toast.error(e?.message || '加载个人信息失败');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  /** 上传头像（multipart 不能走 request()，它固定了 JSON 头） */
  const uploadAvatar = async (file: File) => {
    if (!ALLOWED_TYPES.includes(file.type)) {
      toast.error('仅支持 JPG / PNG / WebP 图片');
      return;
    }
    if (file.size > MAX_AVATAR) {
      toast.error('图片最大 5MB，请压缩后再上传');
      return;
    }
    setUploading(true);
    try {
      const token = getToken();
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch(`${API_BASE}/auth/avatar`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      });
      const json = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(json?.detail || json?.msg || `上传失败（HTTP ${res.status}）`);
      }
      const url: string = json?.data?.avatar || '';
      setMe((m) => (m ? { ...m, avatar: url } : m));
      patchAdminUser({ avatar: url });
      toast.success('头像已更新');
    } catch (e: any) {
      toast.error(e?.message || '头像上传失败');
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  /** 保存基本信息：昵称 / 邮箱 / 用户名 */
  const saveInfo = async () => {
    if (!nickname.trim()) {
      toast.error('昵称不能为空');
      return;
    }
    setSavingInfo(true);
    try {
      const res = await request<{ msg: string; data: MeData; need_relogin: boolean }>(
        '/auth/me',
        { method: 'PUT', body: { nickname: nickname.trim(), email: email.trim(), username: username.trim() } },
      );
      patchAdminUser({
        nickname: res.data.nickname,
        email: res.data.email,
        username: res.data.username,
      });
      if (res.need_relogin) {
        toast.success('用户名已更新，请用新用户名重新登录');
        setTimeout(() => {
          clearAdminSession();
          window.location.href = '/login';
        }, 1000);
        return;
      }
      setMe(res.data);
      toast.success('信息已更新');
    } catch (e: any) {
      toast.error(e?.message || '保存失败');
    } finally {
      setSavingInfo(false);
    }
  };

  /** 修改密码（成功后强制重登） */
  const savePwd = async () => {
    if (!oldPwd || !newPwd) {
      toast.error('请填写原密码与新密码');
      return;
    }
    if (newPwd.length < 6) {
      toast.error('新密码长度至少 6 位');
      return;
    }
    if (newPwd !== confirmPwd) {
      toast.error('两次输入的新密码不一致');
      return;
    }
    if (newPwd === oldPwd) {
      toast.error('新密码不能与原密码相同');
      return;
    }
    setSavingPwd(true);
    try {
      const res = await request<{ msg: string; need_relogin: boolean }>('/auth/me', {
        method: 'PUT',
        body: { old_password: oldPwd, new_password: newPwd },
      });
      toast.success(res.msg || '密码已修改，请重新登录');
      setTimeout(() => {
        clearAdminSession();
        window.location.href = '/login';
      }, 1000);
    } catch (e: any) {
      toast.error(e?.message || '修改密码失败');
    } finally {
      setSavingPwd(false);
    }
  };

  if (loading) {
    return <div className="p-10 text-center text-gray-400">加载中…</div>;
  }

  const avatarUrl = me?.avatar ? (me.avatar.startsWith('http') ? me.avatar : API_BASE + me.avatar) : '';
  const initial = (me?.nickname || me?.username || 'A').slice(0, 1).toUpperCase();

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-gray-800">个人信息</h1>
        <p className="mt-1 text-sm text-gray-500">维护管理员账号资料与登录密码</p>
      </div>

      <div className="grid gap-5 lg:grid-cols-[300px,1fr]">
        {/* ---------------- 左：头像与账号信息 ---------------- */}
        <div className="space-y-5">
          <div className="rounded-xl border bg-white p-5 shadow-sm">
            <p className="mb-4 text-sm font-bold text-gray-700">头像</p>
            <div className="flex flex-col items-center">
              <div className="relative">
                {avatarUrl ? (
                  <img
                    src={avatarUrl}
                    alt="头像"
                    className="h-24 w-24 rounded-full border border-gray-200 object-cover"
                    onError={(e) => {
                      (e.currentTarget as HTMLImageElement).style.display = 'none';
                    }}
                  />
                ) : (
                  <div className="flex h-24 w-24 items-center justify-center rounded-full bg-[#26334D] text-2xl font-bold text-white">
                    {initial}
                  </div>
                )}
              </div>
              <input
                ref={fileRef}
                type="file"
                accept="image/jpeg,image/png,image/webp"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) uploadAvatar(f);
                }}
              />
              <button
                onClick={() => fileRef.current?.click()}
                disabled={uploading}
                className="mt-4 flex items-center gap-1.5 rounded-lg border border-gray-300 px-3.5 py-2 text-xs font-medium text-gray-600 transition hover:border-[#26334D] hover:text-[#26334D] disabled:opacity-60"
              >
                {uploading ? <Loader2 size={13} className="animate-spin" /> : <Camera size={13} />}
                {uploading ? '上传中…' : '更换头像'}
              </button>
              <p className="mt-2 text-[11px] text-gray-400">支持 JPG / PNG / WebP，≤ 5MB</p>
            </div>
          </div>

          <div className="rounded-xl border bg-white p-5 shadow-sm">
            <p className="mb-3 text-sm font-bold text-gray-700">账号信息</p>
            <dl className="space-y-2.5 text-xs">
              <div className="flex justify-between">
                <dt className="text-gray-500">角色</dt>
                <dd className="font-medium text-gray-700">
                  {me?.role === 'admin' ? '管理员' : me?.role}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-500">账号 ID</dt>
                <dd className="font-medium text-gray-700">{me?.id}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-500">手机号</dt>
                <dd className="font-medium text-gray-700">{me?.phone || '未绑定'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-gray-500">创建时间</dt>
                <dd className="font-medium text-gray-700">{String(me?.created_at || '').slice(0, 16)}</dd>
              </div>
            </dl>
          </div>
        </div>

        {/* ---------------- 右：基本信息 + 修改密码 ---------------- */}
        <div className="space-y-5">
          <div className="rounded-xl border bg-white p-5 shadow-sm">
            <div className="mb-4 flex items-center gap-2">
              <UserCog size={16} className="text-[#26334D]" />
              <p className="text-sm font-bold text-gray-700">基本信息</p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-sm font-medium text-gray-700">昵称</label>
                <input
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  placeholder="显示在侧栏的名字"
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-gray-700">邮箱</label>
                <input
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="用于接收通知（可留空）"
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
              </div>
              <div className="sm:col-span-2">
                <label className="mb-1.5 block text-sm font-medium text-gray-700">用户名</label>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
                <p className="mt-1.5 flex items-center gap-1.5 text-[11px] text-amber-600">
                  <ShieldAlert size={12} />
                  用户名是登录凭据，3-32 位字母/数字/下划线/点/中划线；修改后需要用新用户名重新登录
                </p>
              </div>
            </div>

            <div className="mt-5 flex justify-end">
              <button
                onClick={saveInfo}
                disabled={savingInfo}
                className="flex items-center gap-1.5 rounded-lg bg-[#26334D] px-4 py-2.5 text-sm font-bold text-white transition hover:bg-[#303D59] disabled:opacity-60"
              >
                {savingInfo && <Loader2 size={14} className="animate-spin" />}
                保存信息
              </button>
            </div>
          </div>

          <div className="rounded-xl border bg-white p-5 shadow-sm">
            <div className="mb-4 flex items-center gap-2">
              <KeyRound size={16} className="text-[#26334D]" />
              <p className="text-sm font-bold text-gray-700">修改密码</p>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <label className="mb-1.5 block text-sm font-medium text-gray-700">原密码</label>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={oldPwd}
                  onChange={(e) => setOldPwd(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-gray-700">新密码</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={newPwd}
                  onChange={(e) => setNewPwd(e.target.value)}
                  placeholder="至少 6 位"
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-gray-700">确认新密码</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={confirmPwd}
                  onChange={(e) => setConfirmPwd(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 px-3.5 py-2.5 text-sm outline-none transition focus:border-[#26334D] focus:ring-2 focus:ring-[#26334D]/10"
                />
              </div>
            </div>

            <p className="mt-3 text-[11px] text-gray-400">
              修改成功后需要重新登录；同时会吊销该账号在小程序端的免密登录令牌。
            </p>

            <div className="mt-4 flex justify-end">
              <button
                onClick={savePwd}
                disabled={savingPwd}
                className="flex items-center gap-1.5 rounded-lg bg-[#F97316] px-4 py-2.5 text-sm font-bold text-white transition hover:bg-[#EA6A0C] disabled:opacity-60"
              >
                {savingPwd && <Loader2 size={14} className="animate-spin" />}
                修改密码
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
