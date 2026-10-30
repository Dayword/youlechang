// ============================================================
// 办卡充值子页面（暖黄乐园主题，与首页/我的页同一套设计语言）：
// 顶部用户信息+余币余额胶囊 → 三角旗装饰 → 会员码主卡（提币码）→
// 底部「立即充值」渐变大按钮 → 弹出六个充值档位 → 点击模拟支付完成、
// 余币实时到账（未开通商户号阶段，不调起微信收银台）
// ============================================================
import { useState } from 'react'
import { View, Text, Button, Image } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import UserAvatar from '@/components/UserAvatar'
import { getUser, isLoggedIn, requireLogin, type MpUser } from '@/utils/auth'
import { refreshUser } from '@/utils/user'
import {
  getSaleCards, createCardOrder, getOrderStatus, getMyBalance, getMyCards,
  getRedeemQrcode, type CardInfo, type MyCardInfo,
} from '@/utils/api'
import './index.scss'

// 本地兜底档位：后端未启动/接口异常时仍可展示六档（card_id 与 cards 表种子数据一致，
// 后端恢复后可直接下单）
const FALLBACK_TIERS: CardInfo[] = [
  { card_id: 1, name: '10币', price: 1000, price_yuan: 10, description: '10个游戏币', coins: 10 },
  { card_id: 2, name: '20币', price: 2000, price_yuan: 20, description: '20个游戏币', coins: 20 },
  { card_id: 3, name: '30币', price: 3000, price_yuan: 30, description: '30个游戏币', coins: 30 },
  { card_id: 4, name: '200元=320币', price: 20000, price_yuan: 200, description: '充值200元得320币', coins: 320 },
  { card_id: 5, name: '500元=1000币', price: 50000, price_yuan: 500, description: '充值500元得1000币', coins: 1000 },
  { card_id: 6, name: '1000元=2500币', price: 100000, price_yuan: 1000, description: '充值1000元得2500币', coins: 2500 },
]

// 档位按每行 2 个分行（每行 flex 均分，规避安卓 WebView calc 换行变形）
const chunkRows = (list: CardInfo[], size = 2): CardInfo[][] => {
  const rows: CardInfo[][] = []
  for (let i = 0; i < list.length; i += size) rows.push(list.slice(i, i + size))
  return rows
}

export default function RechargePage() {
  const [user, setUser] = useState<MpUser | null>(getUser())
  const [cards, setCards] = useState<CardInfo[]>([])
  const [balance, setBalance] = useState(0)
  const [showSheet, setShowSheet] = useState(false)
  const [buyingId, setBuyingId] = useState<number | null>(null)
  const [doneIds, setDoneIds] = useState<number[]>([])
  const [qrImage, setQrImage] = useState('')
  // 会员码兜底：二维码生成失败时展示文本会员码 + 重试入口（不再无限"加载中"）
  const [qrText, setQrText] = useState('')
  const [qrError, setQrError] = useState('')
  const [qrLoading, setQrLoading] = useState(false)
  const [offline, setOffline] = useState(false)
  const [activeCard, setActiveCard] = useState<MyCardInfo | null>(null)

  useDidShow(() => {
    if (!isLoggedIn()) {
      requireLogin('充值与出示会员码')
      return
    }
    // 头像与「我的」页同源同步
    refreshUser().then((u) => setUser(u)).catch(() => {})
    loadCards()
    refreshBalance()
    loadActiveCard()
  })

  // 生效中的卡（我的页「使用」切换；仅一张卡时后端自动标记）
  const loadActiveCard = async () => {
    try {
      const res = await getMyCards()
      const list: MyCardInfo[] = res.data || []
      const active = list.find((c) => c.in_use) || null
      setActiveCard(active)
      loadQrcode(active ? active.id : undefined)
    } catch {
      loadQrcode()
    }
  }

  const loadCards = async () => {
    try {
      const res = await getSaleCards()
      const list = [...(res.data || [])].sort((a, b) => a.price - b.price)
      if (list.length > 0) {
        setCards(list)
        setOffline(false)
      } else {
        // 后端可达但无档位：展示本地兜底，避免弹层空置
        setCards(FALLBACK_TIERS)
        setOffline(true)
      }
    } catch {
      /* 后端未就绪：兜底档位兜底 + 明确提示（不再静默空弹层） */
      setCards(FALLBACK_TIERS)
      setOffline(true)
    }
  }

  /**
   * 拉取会员码二维码。
   * 失败必须给出明确反馈 + 可点击重试：原实现静默 catch，接口 500（如运行环境缺
   * qrcode 依赖）时页面会永远停在"会员码加载中..."，用户完全无法自助排查。
   */
  const loadQrcode = async (cardId?: number) => {
    if (qrLoading) return
    setQrLoading(true)
    setQrError('')
    try {
      const res = await getRedeemQrcode(cardId)
      const d = res.data
      setQrText(d?.content || '')
      if (d?.image) {
        setQrImage(d.image)
        setQrError('')
      } else {
        setQrImage('')
        setQrError(d?.qr_error || '会员码图片生成失败')
      }
    } catch (e: any) {
      setQrImage('')
      setQrError(e?.message || '会员码获取失败（后端未连接）')
    } finally {
      setQrLoading(false)
    }
  }

  const refreshBalance = async () => {
    try {
      const res = await getMyBalance()
      setBalance(res.data.coins || 0)
    } catch {
      /* 未登录/后端未就绪时保持 0 */
    }
  }

  const handleBuy = async (card: CardInfo) => {
    if (buyingId !== null || doneIds.includes(card.card_id)) return
    setBuyingId(card.card_id)
    try {
      const res = await createCardOrder(card.card_id)
      const d = res.data

      if (d.mode === 'mock') {
        // 模拟支付：直接发卡到账
        markDone(card.card_id)
        await refreshBalance()
        Taro.showToast({ title: `充值成功，${card.coins || 0} 币已到账 🎉`, icon: 'none', duration: 2500 })
        return
      }

      // 正式模式（WECHAT_PAY_ENABLED=true）：唤起微信支付
      await new Promise<void>((resolve, reject) => {
        Taro.requestPayment({
          timeStamp: d.payment.timeStamp,
          nonceStr: d.payment.nonceStr,
          package: d.payment.package,
          signType: d.payment.signType as any,
          paySign: d.payment.paySign,
          success: () => resolve(),
          fail: (err) => reject(new Error(err.errMsg?.includes('cancel') ? '已取消支付' : '支付失败')),
        })
      })

      Taro.showLoading({ title: '确认支付结果...' })
      let ok = false
      for (let i = 0; i < 10; i++) {
        try {
          const r = await getOrderStatus(d.order_no)
          if (r.data.status === 'paid') { ok = true; break }
        } catch { /* 忽略单次失败 */ }
        await new Promise((r2) => setTimeout(r2, 1500))
      }
      Taro.hideLoading()
      if (ok) {
        markDone(card.card_id)
        await refreshBalance()
        Taro.showToast({ title: `充值成功，${card.coins || 0} 币已到账 🎉`, icon: 'none', duration: 2500 })
      } else {
        Taro.showToast({ title: '支付确认中，请稍后查看', icon: 'none' })
      }
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '下单失败，请稍后重试', icon: 'none' })
    } finally {
      setBuyingId(null)
    }
  }

  const markDone = (id: number) => setDoneIds((prev) => [...prev, id])
  return (
    <View className='recharge-page'>
      {/* —— 装饰层：彩点 / 云 / 摩天轮水印 / 底部黄丘（填充中部留白） —— */}
      <View className='deco dot d1' />
      <View className='deco dot d2' />
      <View className='deco dot d3' />
      <View className='deco dot d4' />
      <View className='deco dot d5' />
      <Text className='deco cloud c1'>☁️</Text>
      <Text className='deco cloud c2'>☁️</Text>
      <Text className='deco wheel-mark'>🎡</Text>
      <Text className='deco balloon-mark'>🎈</Text>
      <View className='deco hill h1' />
      <View className='deco hill h2' />

      {/* —— 顶部：用户信息 + 余币余额胶囊 —— */}
      <View className='top-bar'>
        <UserAvatar
          user={user}
          className='avatar'
          imgClassName='avatar-img'
          textClassName='avatar-icon'
        />
        <Text className='user-name'>{user?.nickname || '游客'}</Text>
        <View className='balance'>
          <Text className='balance-num'>{balance}</Text>
          <Text className='balance-coin'>🪙</Text>
        </View>
      </View>

      {/* —— 三角旗装饰（左右两组，微旋转模拟垂挂弧线） —— */}
      <View className='bunting'>
        <View className='flag-group lg-l'>
          {[0, 1, 2, 3, 4].map((i) => (
            <Text key={`l${i}`} className={`flag f${i % 3}`} />
          ))}
        </View>
        <View className='flag-group lg-r'>
          {[0, 1, 2, 3, 4].map((i) => (
            <Text key={`r${i}`} className={`flag f${(i + 1) % 3}`} />
          ))}
        </View>
      </View>

      {/* —— 会员码主卡：在剩余空间垂直居中 —— */}
      <View className='qr-center'>
      <View className='qr-card'>
        <Text className='qr-title'>会员码</Text>
        <Text className='qr-sub'>向园区工作人员出示即可核验</Text>
        {activeCard && (
          <View className='qr-active-card'>
            <Text className='qr-active-text'>💳 生效卡：{activeCard.card_name} · 余 {activeCard.coins} 币</Text>
          </View>
        )}
        {qrImage ? (
          <Image className='qr-img' src={qrImage} showMenuByLongpress mode='aspectFit' />
        ) : qrError ? (
          /* 生成/获取失败：给出原因 + 重试 + 文本会员码兜底（工作人员可手工核验） */
          <View className='qr-placeholder qr-failed'>
            <Text className='qr-emoji'>⚠️</Text>
            <Text className='qr-hint'>会员码生成失败</Text>
            <Text className='qr-err'>{qrError}</Text>
            <View
              className='qr-retry'
              hoverClass='recharge-btn-hover'
              onClick={() => loadQrcode(activeCard ? activeCard.id : undefined)}
            >
              <Text className='qr-retry-text'>{qrLoading ? '重试中...' : '点击重试'}</Text>
            </View>
            {!!qrText && (
              <View className='qr-text-code'>
                <Text className='qr-text-label'>文本会员码（出示给工作人员）</Text>
                <Text className='qr-text-value' userSelect>{qrText}</Text>
              </View>
            )}
          </View>
        ) : (
          <View className='qr-placeholder'>
            <Text className='qr-emoji'>🦆</Text>
            <Text className='qr-hint'>{qrLoading ? '会员码加载中...' : '点击下方按钮重试'}</Text>
          </View>
        )}
        <Text className='qr-note'>核验后余额将存入当前账户</Text>
      </View>
      </View>

      {/* —— 底部：立即充值（渐变大胶囊，固定页底） + 注释 —— */}
      <View className='bottom-area'>
      <View
        className='recharge-btn'
        hoverClass='recharge-btn-hover'
        hoverStayTime={80}
        onClick={() => {
        if (!requireLogin('充值')) return
        setShowSheet(true)
      }}
      >
        <Text className='recharge-btn-text'>立即充值</Text>
      </View>

        <View className='page-footer'>
          <Text className='footer-text'>余额仅限本园园区游玩项目使用</Text>
        </View>
      </View>

      {/* 充值档位弹层（六个按钮） */}
      {showSheet && (
        <View className='sheet-mask' onClick={() => setShowSheet(false)}>
          <View className='sheet' onClick={(e) => e.stopPropagation()}>
            <View className='sheet-header'>
              <Text className='sheet-title'>选择充值档位</Text>
              <Text className='sheet-close' onClick={() => setShowSheet(false)}>×</Text>
            </View>

            {/* 档位分行渲染（每行两个 flex 均分，兼容所有机型） */}
            {chunkRows(cards).map((row, ri) => (
              <View className='tier-row' key={ri}>
                {row.map((c) => {
                  const done = doneIds.includes(c.card_id)
                  const buying = buyingId === c.card_id
                  return (
                    <View
                      key={c.card_id}
                      className={`tier ${done ? 'tier-done' : ''}`}
                      hoverClass='tier-hover'
                      hoverStayTime={80}
                      onClick={() => handleBuy(c)}
                    >
                      <Text className='tier-name'>
                        {done ? '✓ 已到账' : buying ? '处理中...' : c.name}
                      </Text>
                      {!done && !buying && (
                        <Text className='tier-price'>¥{c.price_yuan}</Text>
                      )}
                    </View>
                  )
                })}
                {row.length === 1 && <View className='tier-ghost' />}
              </View>
            ))}

            {offline && (
              <Text className='sheet-warn'>⚠️ 后端服务未连接，当前为默认档位，连接后可正常充值</Text>
            )}

            <Text className='sheet-tip'>💡 点击档位即模拟支付完成，余币实时到账</Text>

            <Button className='sheet-cancel' onClick={() => setShowSheet(false)}>
              取消
            </Button>
          </View>
        </View>
      )}
    </View>
  )
}
