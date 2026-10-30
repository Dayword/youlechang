// ============================================================
// 登录门页 v2
// ------------------------------------------------------------
// 三种方式：
//   ① 微信一键登录（code 换 openid）→ 未绑手机号/未设密码则进入绑定步骤
//   ② 手机号 + 密码登录（未注册自动建号，密码 bcrypt 存 MySQL）
//   ③ 历史账号一键登录（点头像即进入，免密令牌 30 天有效）
// 说明：原先的「本机手机号一键登录」需企业认证主体，已移除；
//       「手机号验证码登录」也已移除，改为手机号+密码。
// ============================================================
import { useEffect, useRef, useState } from 'react'
import { View, Text, Button, Input, Image, ScrollView } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import {
  getToken, getUser, login, passwordLogin, rememberLogin, bindPhone,
  listLoginAccounts, removeLoginAccount, getLastAccount, hasRememberToken,
  sendPhoneCode, resetPassword, mergeTip,
  type MpUser, type LoginAccount, type LastAccount,
} from '@/utils/auth'
import { getAvatarUrl } from '@/utils/user'
import { PARK_NAME, PARK_PHONE } from '@/utils/config'
import './index.scss'

type Step = 'choose' | 'pwd' | 'bind' | 'reset'

export default function LoginPage() {
  const [step, setStep] = useState<Step>('choose')
  const [busy, setBusy] = useState(false)

  // 历史账号（服务端 device 维度 + 本地快照兜底）
  const [accounts, setAccounts] = useState<LoginAccount[]>([])
  const [last, setLast] = useState<LastAccount | null>(null)

  // 表单
  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [password2, setPassword2] = useState('')
  const [showPwd, setShowPwd] = useState(false)
  const [remember, setRemember] = useState(true)

  // 验证码（仅用于「忘记密码」与「手机号已被其他账号绑定 → 验证后合并」）
  const [smsCode, setSmsCode] = useState('')
  const [countdown, setCountdown] = useState(0)
  const [needVerify, setNeedVerify] = useState(false)
  const timerRef = useRef<any>(null)

  const validPhone = /^1[3-9]\d{9}$/.test(phone)

  useEffect(() => () => {
    if (timerRef.current) clearInterval(timerRef.current)
  }, [])

  const startCountdown = () => {
    setCountdown(60)
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setCountdown((c) => {
        if (c <= 1) {
          if (timerRef.current) clearInterval(timerRef.current)
          timerRef.current = null
          return 0
        }
        return c - 1
      })
    }, 1000)
  }

  /** 获取验证码（演示环境后端直接回显并自动填入） */
  const handleSendCode = async () => {
    if (countdown > 0) return
    if (!validPhone) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    try {
      const demo = await sendPhoneCode(phone)
      if (demo) {
        setSmsCode(demo)
        Taro.showToast({ title: `演示环境验证码：${demo}`, icon: 'none', duration: 4000 })
      } else {
        Taro.showToast({ title: '验证码已发送', icon: 'none' })
      }
      startCountdown()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '验证码发送失败', icon: 'none' })
    }
  }

  // 已登录且已绑手机号并设过密码 → 直接进首页；
  // 已登录但缺手机号/密码（如静默登录产生的账号）→ 直接进入绑定步骤补齐
  useDidShow(() => {
    const u: MpUser | null = getUser()
    if (!getToken() || !u) {
      setLast(getLastAccount())
      loadAccounts()
      return
    }
    if (u.phone && u.pwd_set) {
      Taro.switchTab({ url: '/pages/index/index' })
      return
    }
    setPhone(u.phone || '')
    setStep('bind')
  })

  const loadAccounts = async () => {
    const list = await listLoginAccounts()
    setAccounts(list)
  }

  // 登录成功后的去向：从应用内进入（页面栈>1）返回原页；直接打开时回首页
  const afterLogin = () => {
    const pages = Taro.getCurrentPages()
    if (pages.length > 1) Taro.navigateBack()
    else Taro.switchTab({ url: '/pages/index/index' })
  }

  // ---------- ① 微信一键登录 ----------
  const handleWechatLogin = async () => {
    if (busy) return
    setBusy(true)
    try {
      const user = await login()
      if (user.phone && user.pwd_set) {
        afterLogin()
      } else {
        // 未绑手机号或未设密码 → 补齐（手机号已绑定时预填）
        setPhone(user.phone || '')
        setStep('bind')
        Taro.showToast({ title: '请绑定手机号并设置密码', icon: 'none' })
      }
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '微信登录失败，请稍后重试', icon: 'none', duration: 4000 })
    } finally {
      setBusy(false)
    }
  }

  // ---------- ② 手机号 + 密码登录 ----------
  const handlePasswordLogin = async () => {
    if (busy) return
    if (!validPhone) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    if (password.length < 6) {
      Taro.showToast({ title: '密码至少 6 位', icon: 'none' })
      return
    }
    setBusy(true)
    try {
      const { user, merged } = await passwordLogin(phone, password, remember)
      Taro.showToast({ title: user.is_new ? '账号已创建，欢迎回来' : '登录成功', icon: 'success' })
      // 手机号账号是主账号：此微信号之前留下的空壳账号会被并入它，有迁移记录时告知用户
      const tip = mergeTip(merged)
      if (tip) Taro.showToast({ title: tip, icon: 'none', duration: 3000 })
      afterLogin()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '登录失败，请稍后重试', icon: 'none', duration: 3000 })
    } finally {
      setBusy(false)
    }
  }

  // ---------- ③ 历史账号一键登录（点一下头像 / 昵称） ----------
  const handleOneTap = async (userId: number, accountPhone: string) => {
    if (busy) return
    if (!hasRememberToken(userId)) {
      // 本机没有该账号的免密令牌（如刚退出登录）→ 引导输入密码
      setPhone(accountPhone || '')
      setPassword('')
      setStep('pwd')
      Taro.showToast({ title: '请输入密码登录', icon: 'none' })
      return
    }
    setBusy(true)
    try {
      await rememberLogin(userId)
      Taro.showToast({ title: '登录成功', icon: 'success' })
      afterLogin()
    } catch (e: any) {
      setPhone(accountPhone || '')
      setPassword('')
      setStep('pwd')
      Taro.showToast({ title: e?.message || '免密登录失败，请输入密码', icon: 'none', duration: 3000 })
    } finally {
      setBusy(false)
    }
  }

  const handleRemoveAccount = (acc: LoginAccount) => {
    Taro.showModal({
      title: '移除历史账号？',
      content: '将从本机登录列表中移除该账号，不影响账号本身的数据',
      confirmText: '移除',
      confirmColor: '#e4572e',
      success: async (r) => {
        if (!r.confirm) return
        try {
          await removeLoginAccount(acc)
          if (acc.id > 0) setAccounts((prev) => prev.filter((a) => a.id !== acc.id))
          else setLast(null)
          Taro.showToast({ title: '已移除', icon: 'none' })
        } catch (e: any) {
          Taro.showToast({ title: e?.message || '移除失败', icon: 'none' })
        }
      },
    })
  }

  // ---------- 绑定手机号（微信登录后补齐；手机号属于旧账号时验证后合并） ----------
  const handleBind = async () => {
    if (busy) return
    if (!validPhone) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    if (password.length < 6) {
      Taro.showToast({ title: '登录密码至少 6 位', icon: 'none' })
      return
    }
    if (password !== password2) {
      Taro.showToast({ title: '两次输入的密码不一致', icon: 'none' })
      return
    }
    if (needVerify && !smsCode.trim()) {
      Taro.showToast({ title: '请输入该手机号收到的验证码', icon: 'none' })
      return
    }
    setBusy(true)
    try {
      const res = await bindPhone(phone, password, '', smsCode.trim())
      Taro.showToast({ title: res.msg || '绑定成功', icon: 'success', duration: 2500 })
      afterLogin()
    } catch (e: any) {
      // 40901：该手机号已绑在另一个账号上 → 引导验证手机号后合并（解死锁）
      if (e?.code === 40901) {
        setNeedVerify(true)
        Taro.showToast({
          title: e?.message || '该手机号已绑定其他账号，请验证该手机号以合并',
          icon: 'none',
          duration: 4000,
        })
      } else {
        Taro.showToast({ title: e?.message || '绑定失败，请稍后重试', icon: 'none', duration: 3000 })
      }
    } finally {
      setBusy(false)
    }
  }

  // ---------- 忘记密码：验证码 + 新密码 → 重置并直接登录 ----------
  const handleReset = async () => {
    if (busy) return
    if (!validPhone) {
      Taro.showToast({ title: '请输入正确的 11 位手机号', icon: 'none' })
      return
    }
    if (!smsCode.trim()) {
      Taro.showToast({ title: '请输入验证码', icon: 'none' })
      return
    }
    if (password.length < 6) {
      Taro.showToast({ title: '新密码至少 6 位', icon: 'none' })
      return
    }
    if (password !== password2) {
      Taro.showToast({ title: '两次输入的密码不一致', icon: 'none' })
      return
    }
    setBusy(true)
    try {
      await resetPassword(phone, smsCode.trim(), password)
      Taro.showToast({ title: '密码已重置，已为你登录', icon: 'success' })
      afterLogin()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '重置失败，请稍后重试', icon: 'none', duration: 3000 })
    } finally {
      setBusy(false)
    }
  }

  const gotoReset = () => {
    setSmsCode('')
    setPassword('')
    setPassword2('')
    setStep('reset')
  }

  const handleForgot = () => {
    Taro.showModal({
      title: '忘记密码',
      content: `可用收到短信验证码的手机号自助重置密码；也可拨打园区电话 ${PARK_PHONE} 由工作人员协助。`,
      confirmText: '去重置',
      cancelText: '暂不',
      success: (r) => {
        if (r.confirm) gotoReset()
      },
    })
  }

  // 历史账号列表：服务端优先，其次本地快照（保证离线也有入口）
  const oneTapList: LoginAccount[] = accounts.length
    ? accounts
    : last
      ? [{
          id: -1,
          user_id: last.user_id,
          phone: last.phone,
          username: last.username,
          nickname: last.nickname,
          avatar: last.avatar,
          login_count: 0,
          last_login_at: '',
        }]
      : []

  const masked = (p?: string) =>
    p && p.length === 11 ? p.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2') : (p || '未绑定手机号')

  return (
    <View className='login-page'>
      {/* 装饰 */}
      <View className='bunting'>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <Text key={i} className={`flag f${i % 3}`} />
        ))}
      </View>
      <Text className='deco cloud'>☁️</Text>
      <Text className='deco wheel'>🎡</Text>

      {/* 欢迎区 */}
      <View className='login-hero'>
        <Text className='login-duck breathe'>🦆</Text>
        <Text className='login-title'>欢迎来到{PARK_NAME}</Text>
        <Text className='login-sub'>登录后可 AI 问答 · 每日签到 · 一键办卡</Text>
      </View>

      {step === 'choose' && (
        <View className='login-card'>
          <Button className='login-btn-main' hoverClass='press' onClick={handleWechatLogin} disabled={busy}>
            {busy ? '登录中...' : '💬 微信一键登录'}
          </Button>
          <Button
            className='login-btn-outline'
            hoverClass='press'
            onClick={() => {
              setPhone('')
              setPassword('')
              setStep('pwd')
            }}
            disabled={busy}
          >
            🔑 手机号密码登录
          </Button>

          {oneTapList.length > 0 && (
            <>
              <View className='login-divider'>
                <Text className='divider-line' />
                <Text className='divider-text'>本机登录过的账号</Text>
                <Text className='divider-line' />
              </View>
              <ScrollView scrollY className='acct-scroll'>
                {oneTapList.map((acc) => {
                  const avatar = getAvatarUrl({ avatar: acc.avatar } as MpUser)
                  const canOneTap = hasRememberToken(acc.user_id)
                  return (
                    <View key={`${acc.id}-${acc.user_id}`} className='acct-row'>
                      <View
                        className='acct-main'
                        hoverClass='press'
                        hoverStayTime={60}
                        onClick={() => handleOneTap(acc.user_id, acc.phone)}
                      >
                        {avatar ? (
                          <Image className='acct-avatar' src={avatar} mode='aspectFill' />
                        ) : (
                          <Text className='acct-avatar acct-avatar-emoji'>🦆</Text>
                        )}
                        <View className='acct-info'>
                          <Text className='acct-name'>{acc.nickname || '游客'}</Text>
                          <Text className='acct-phone'>{masked(acc.phone)}</Text>
                        </View>
                        <View className={`acct-badge ${canOneTap ? 'acct-badge-fast' : ''}`}>
                          <Text className='acct-badge-text'>{canOneTap ? '一键登录' : '输密码'}</Text>
                        </View>
                      </View>
                      <Text className='acct-del' onClick={() => handleRemoveAccount(acc)}>✕</Text>
                    </View>
                  )
                })}
              </ScrollView>
            </>
          )}

          <Text className='login-foot-note'>未注册的手机号将自动创建账号</Text>
        </View>
      )}

      {step === 'pwd' && (
        <View className='login-card'>
          <Text className='login-card-title'>手机号密码登录</Text>
          <Text className='login-card-sub'>未注册的手机号将自动创建账号，本次密码即登录密码</Text>

          <Text className='form-label'>手机号</Text>
          <Input
            className='form-input'
            type='number'
            maxlength={11}
            value={phone}
            onInput={(e) => setPhone(e.detail.value)}
            placeholder='请输入 11 位手机号'
          />

          <Text className='form-label'>密码</Text>
          <View className='pwd-row'>
            <Input
              className='form-input pwd-input'
              password={!showPwd}
              maxlength={32}
              value={password}
              onInput={(e) => setPassword(e.detail.value)}
              placeholder='至少 6 位'
            />
            <View className='pwd-eye' hoverClass='press' onClick={() => setShowPwd(!showPwd)}>
              <Text>{showPwd ? '隐藏' : '显示'}</Text>
            </View>
          </View>

          <View className='remember-row' onClick={() => setRemember(!remember)}>
            <View className={`checkbox ${remember ? 'checkbox-on' : ''}`}>
              {remember ? <Text className='checkbox-mark'>✓</Text> : null}
            </View>
            <Text className='remember-text'>记住这个账号，下次点头像直接登录</Text>
          </View>

          <Button className='login-btn-main' hoverClass='press' onClick={handlePasswordLogin} disabled={busy}>
            {busy ? '登录中...' : '登录 / 注册'}
          </Button>

          <View className='login-links'>
            <Text className='login-alt-inline' onClick={handleForgot}>忘记密码？</Text>
            <Text className='login-alt-inline' onClick={() => setStep('choose')}>‹ 返回其他方式</Text>
          </View>
        </View>
      )}

      {step === 'bind' && (
        <View className='login-card'>
          <Text className='login-card-title'>绑定手机号</Text>
          <Text className='login-card-sub'>微信身份已确认，绑定手机号并设置登录密码后即可进入</Text>

          <Text className='form-label'>手机号</Text>
          <Input
            className='form-input'
            type='number'
            maxlength={11}
            value={phone}
            onInput={(e) => {
              setPhone(e.detail.value)
              setNeedVerify(false)
            }}
            placeholder='请输入 11 位手机号'
          />

          <Text className='form-label'>设置登录密码</Text>
          <Input
            className='form-input'
            password
            maxlength={32}
            value={password}
            onInput={(e) => setPassword(e.detail.value)}
            placeholder='至少 6 位'
          />

          <Text className='form-label'>确认密码</Text>
          <Input
            className='form-input'
            password
            maxlength={32}
            value={password2}
            onInput={(e) => setPassword2(e.detail.value)}
            placeholder='再次输入密码'
          />

          {needVerify && (
            <>
              <View className='conflict-tip'>
                <Text className='conflict-tip-text'>
                  该手机号已绑定其他账号。验证该手机号后，你的微信会自动关联到该手机号的原账号——
                  持卡、余币、消费记录都留在原账号上，不会被搬走。
                </Text>
              </View>
              <Text className='form-label'>短信验证码</Text>
              <View className='code-row'>
                <Input
                  className='form-input code-input'
                  type='number'
                  maxlength={6}
                  value={smsCode}
                  onInput={(e) => setSmsCode(e.detail.value)}
                  placeholder='6 位验证码'
                />
                <View
                  className={`code-btn ${countdown > 0 ? 'code-btn-disabled' : ''}`}
                  hoverClass='press'
                  onClick={handleSendCode}
                >
                  <Text>{countdown > 0 ? `${countdown}s 后重发` : '获取验证码'}</Text>
                </View>
              </View>
            </>
          )}

          <Button className='login-btn-main' hoverClass='press' onClick={handleBind} disabled={busy}>
            {busy ? '处理中...' : needVerify ? '验证并合并账号' : '绑定并进入'}
          </Button>
          <Text className='login-alt' onClick={() => setStep('choose')}>‹ 返回选择登录方式</Text>
        </View>
      )}

      {step === 'reset' && (
        <View className='login-card'>
          <Text className='login-card-title'>重置登录密码</Text>
          <Text className='login-card-sub'>输入手机号并获取验证码，验证通过后重设登录密码</Text>

          <Text className='form-label'>手机号</Text>
          <Input
            className='form-input'
            type='number'
            maxlength={11}
            value={phone}
            onInput={(e) => setPhone(e.detail.value)}
            placeholder='请输入 11 位手机号'
          />

          <Text className='form-label'>短信验证码</Text>
          <View className='code-row'>
            <Input
              className='form-input code-input'
              type='number'
              maxlength={6}
              value={smsCode}
              onInput={(e) => setSmsCode(e.detail.value)}
              placeholder='6 位验证码'
            />
            <View
              className={`code-btn ${countdown > 0 ? 'code-btn-disabled' : ''}`}
              hoverClass='press'
              onClick={handleSendCode}
            >
              <Text>{countdown > 0 ? `${countdown}s 后重发` : '获取验证码'}</Text>
            </View>
          </View>

          <Text className='form-label'>新密码</Text>
          <Input
            className='form-input'
            password
            maxlength={32}
            value={password}
            onInput={(e) => setPassword(e.detail.value)}
            placeholder='至少 6 位'
          />

          <Text className='form-label'>确认新密码</Text>
          <Input
            className='form-input'
            password
            maxlength={32}
            value={password2}
            onInput={(e) => setPassword2(e.detail.value)}
            placeholder='再次输入新密码'
          />

          <Button className='login-btn-main' hoverClass='press' onClick={handleReset} disabled={busy}>
            {busy ? '处理中...' : '重置密码并登录'}
          </Button>
          <Text className='login-alt' onClick={() => setStep('pwd')}>‹ 返回密码登录</Text>
        </View>
      )}

      <Text className='login-foot'>🔒 密码仅以加密哈希存储，用于本园区服务</Text>
    </View>
  )
}
