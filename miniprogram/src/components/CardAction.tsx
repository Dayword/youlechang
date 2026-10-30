// ============================================================
// 办卡卡片（AI 回答 action 事件渲染）：
// 点击【立即办理】→ POST /pay/orders 下单
//   - 未开通商户号阶段：模拟支付直接发卡到账（不调起微信收银台）
//   - WECHAT_PAY_ENABLED=true：wx.requestPayment 唤起微信支付
// 成功后回调 onSuccess（AI 聊天页：追加办理成功消息并跳转充值页）
// ============================================================
import { useState } from 'react'
import { View, Text } from '@tarojs/components'
import Taro from '@tarojs/taro'
import { createCardOrder, getOrderStatus, type CardInfo } from '@/utils/api'
import './CardAction.scss'

// 兼容别名：chat 页从组件导入 CardItem 类型
export type { CardInfo as CardItem }

export default function CardAction({
  card,
  onSuccess,
}: {
  card: CardInfo
  /** 支付/发卡成功后回调（AI 聊天页：追加办理成功消息并跳转充值页） */
  onSuccess?: (card: CardInfo) => void
}) {
  const [buying, setBuying] = useState(false)
  const [bought, setBought] = useState(false)

  const handleBuy = async () => {
    if (buying || bought) return
    setBuying(true)
    try {
      const res = await createCardOrder(card.card_id)
      const d = res.data

      if (d.mode === 'mock') {
        // 模拟支付：直接发卡到账
        setBought(true)
        Taro.showToast({ title: `办理成功，${card.coins || 0} 币已到账 🎉`, icon: 'none', duration: 2500 })
        onSuccess?.(card)
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
        } catch { /* 忽略单次轮询失败 */ }
        await new Promise((r) => setTimeout(r, 1500))
      }
      Taro.hideLoading()
      if (ok) {
        setBought(true)
        Taro.showToast({ title: `办理成功，${card.coins || 0} 币已到账 🎉`, icon: 'none', duration: 2500 })
        onSuccess?.(card)
      } else {
        Taro.showToast({ title: '支付结果确认中，稍后可在"我的-我的卡"查看', icon: 'none', duration: 3000 })
      }
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '下单失败，请稍后重试', icon: 'none' })
    } finally {
      setBuying(false)
    }
  }

  return (
    <View className='card-action msg-enter'>
      <View className='card-action-info'>
        <Text className='card-action-name'>💳 {card.name}</Text>
        {card.description ? <Text className='card-action-desc'>{card.description}</Text> : null}
        <Text className='card-action-price'>¥{card.price_yuan}</Text>
      </View>
      <View
        className={`card-action-btn ${bought ? 'disabled' : ''}`}
        onClick={handleBuy}
      >
        <Text>{bought ? '✓ 已办理' : buying ? '处理中...' : '立即办理'}</Text>
      </View>
    </View>
  )
}
