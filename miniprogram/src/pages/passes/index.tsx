// ============================================================
// 我的游园码（一券 = 一项一次，一券一码）+ 消费明细
// ------------------------------------------------------------
// 解决的实际问题：用户一次买多个项目、拆开分几次玩时，
//   一单一码模型下工作人员看不到明细、忙起来记不住"他还有哪几个没玩"，
//   而整单一次核销更危险（玩了一半就把单结掉，用户失去剩余凭据）。
// 本页做法：
//   「我的券」顶部大字卡 = 当前要玩的那一张（项目名 + 大字券码 + 二维码），
//     工作人员扫一张只核销这一张；下方待玩 / 已玩 / 已退三分区，
//     "还剩哪几个没玩"一眼可见。
//   「消费明细」＝原「我的」页的消费记录，按整单列出券包进度
//     （原来藏在弹窗里、与券分成两个入口，用户对不上「我还有几张没玩」）。
// ============================================================
import { useEffect, useState } from 'react'
import { View, Text, Image, ScrollView } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import { isLoggedIn, requireLogin } from '@/utils/auth'
import {
  getMyPasses, getPassQrcode, getMyConsumptions, refundMyPasses,
  type MyPasses, type PassItem, type MyConsumption,
} from '@/utils/api'
import './index.scss'

const CONS_TABS = [
  { key: 'all', text: '全部' },
  { key: 'unused', text: '未核销' },
  { key: 'used', text: '已核销' },
  { key: 'refunded', text: '已退款' },
] as const

/**
 * items_json 是 TEXT 列，历史上可能写入非法 JSON（空串 / 手工改过的老数据）。
 * **绝不能把 JSON.parse 直接写在 JSX 里**：渲染期抛出的异常会打断这一帧，
 * 小程序所有页面共用同一个 JS 线程 —— 表现就是「弹窗关不掉、整页按钮全点不动」。
 * 解析失败一律当空明细处理，宁可少显示一行，也不能让整页卡死。
 */
function parseItems(raw: string): { name: string; qty: number }[] {
  try {
    const v = JSON.parse(raw || '[]')
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

export default function Passes() {
  const [data, setData] = useState<MyPasses | null>(null)
  const [loading, setLoading] = useState(true)
  // 用户点待玩列表里的某一张时，把它顶到上面大字卡（不点就默认「下一张待玩」）
  const [picked, setPicked] = useState('')
  const [qr, setQr] = useState('')
  const [qrOk, setQrOk] = useState(true)
  // 券会一张张攒起来，全铺开会越滚越长：已玩/已退默认折叠，顶部给个「只看待玩」
  const [onlyUnused, setOnlyUnused] = useState(false)
  const [showUsed, setShowUsed] = useState(false)
  const [showRefunded, setShowRefunded] = useState(false)

  // 顶部双 tab：券 / 消费明细
  const [tab, setTab] = useState<'pass' | 'consume'>('pass')
  const [consList, setConsList] = useState<MyConsumption[]>([])
  const [consLoading, setConsLoading] = useState(false)
  const [consTab, setConsTab] = useState<'all' | 'unused' | 'used' | 'refunded'>('all')
  // 退款中：券码 / 单号（防连点重复提交）
  const [refunding, setRefunding] = useState('')

  const load = async () => {
    if (!isLoggedIn()) {
      setLoading(false)
      return
    }
    setLoading(true)
    try {
      const res = await getMyPasses()
      setData(res.data)
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '加载失败，请稍后重试', icon: 'none' })
    } finally {
      setLoading(false)
    }
  }

  const loadCons = async () => {
    if (!isLoggedIn()) return
    setConsLoading(true)
    try {
      const res = await getMyConsumptions(50)
      setConsList(res.data || [])
    } catch {
      setConsList([])
    } finally {
      setConsLoading(false)
    }
  }

  useDidShow(() => {
    if (!isLoggedIn()) {
      requireLogin('查看游园码')
      return
    }
    load()
    loadCons()
  })

  const passes = data?.passes || []
  const unused = passes.filter((p) => p.status === 'unused')
  const used = passes.filter((p) => p.status === 'used')
  const refunded = passes.filter((p) => p.status === 'refunded')

  const shown: PassItem | undefined =
    unused.find((p) => p.item_code === picked) || data?.next_pass || unused[0]

  const consShown = consTab === 'all' ? consList : consList.filter((c) => (c.status || 'unused') === consTab)

  // 换券就换码：只认当前这张券的二维码
  useEffect(() => {
    if (!shown) {
      setQr('')
      return
    }
    let alive = true
    getPassQrcode(shown.item_code)
      .then((r) => {
        if (!alive) return
        setQr(r.data?.image || '')
        setQrOk(!!r.data?.qr_ok)
      })
      .catch(() => {
        if (!alive) return
        setQr('')
        setQrOk(false)
      })
    return () => {
      alive = false
    }
  }, [shown?.item_code])

  const copyCode = (code: string) => {
    Taro.setClipboardData({ data: code })
  }

  // ---------- 自助退券 ----------
  // 只有「未核销」的券能自己退：玩过的项目让用户自己退掉等于白嫖，
  // 「刚上去就下来」这类要退已核销券的场景，由现场工作人员在员工端处理。
  const runRefund = async (payload: { codes?: string[]; order_no?: string }, key: string) => {
    if (refunding) return
    setRefunding(key)
    try {
      const res = await refundMyPasses(payload)
      Taro.showToast({ title: res.msg || '退款成功', icon: 'none' })
      setPicked('')
      await load()
      loadCons()
    } catch (e: any) {
      Taro.showToast({ title: e?.message || '退款失败，请稍后重试', icon: 'none' })
    } finally {
      setRefunding('')
    }
  }

  const askRefundPass = (p: PassItem) => {
    if (refunding) return
    Taro.showModal({
      title: '退掉这张券？',
      content: `${p.project_name} ×1，退 ${p.unit_coins} 币。`
        + '币会按原扣币比例退回你的卡，不可撤销。',
      confirmText: '确认退款',
      confirmColor: '#e8112d',
      success: (r) => { if (r.confirm) runRefund({ codes: [p.item_code] }, p.item_code) },
    })
  }

  const askRefundOrder = (c: MyConsumption) => {
    if (refunding) return
    const n = c.pass_unused || 0
    if (n <= 0) return
    Taro.showModal({
      title: '退掉未核销的券？',
      content: `这一单还有 ${n} 张券没玩，退款后币会按原扣币比例退回你的卡，不可撤销。`
        + '已经玩过的券不在这里退，需要退请找现场工作人员。',
      confirmText: '确认退款',
      confirmColor: '#e8112d',
      success: (r) => { if (r.confirm) runRefund({ order_no: c.order_no }, c.order_no) },
    })
  }

  const renderRow = (p: PassItem, dim: boolean) => (
    <View
      key={p.item_code}
      className={`pass-row${dim ? ' pass-row-dim' : ''}${p.item_code === shown?.item_code ? ' pass-row-on' : ''}`}
      onClick={() => {
        if (!dim) setPicked(p.item_code)
      }}
    >
      <View className='pass-row-main'>
        <Text className='pass-row-name'>{p.project_name} × 1</Text>
        <Text className='pass-row-sub'>
          {p.unit_coins} 币
          {p.status === 'used' && p.used_at ? ` · 已核销 ${p.used_at.slice(5, 16)}` : ''}
          {p.status === 'refunded' ? ' · 已退款' : ''}
        </Text>
      </View>
      <View className='pass-row-right'>
        <Text
          className='pass-row-code'
          onClick={(e) => { e.stopPropagation(); copyCode(p.item_code) }}
        >
          {p.item_code}
        </Text>
        {p.status === 'unused' && (
          <Text
            className={`pass-row-refund${refunding === p.item_code ? ' pass-row-refund-off' : ''}`}
            onClick={(e) => { e.stopPropagation(); askRefundPass(p) }}
          >
            {refunding === p.item_code ? '退款中…' : '退款'}
          </Text>
        )}
        {p.status === 'used' && (
          <Text className='pass-row-refund-tip'>找工作人员退</Text>
        )}
      </View>
    </View>
  )

  return (
    <View className='passes-page'>
      <View className='passes-head'>
        <Text className='passes-title'>我的游园码</Text>
        <Text className='passes-sub'>到项目现场出示，工作人员扫一张核销一张</Text>
      </View>

      {/* 双 tab：券和消费明细原本是两个入口（一个在首页 pill、一个在「我的」弹窗） */}
      {isLoggedIn() && (
        <View className='passes-tabs'>
          <View
            className={`passes-tab${tab === 'pass' ? ' passes-tab-on' : ''}`}
            onClick={() => setTab('pass')}
          >
            <Text>我的券{unused.length ? ` ${unused.length}` : ''}</Text>
          </View>
          <View
            className={`passes-tab${tab === 'consume' ? ' passes-tab-on' : ''}`}
            onClick={() => {
              setTab('consume')
              loadCons()
            }}
          >
            <Text>消费明细</Text>
          </View>
        </View>
      )}

      {loading ? (
        <View className='passes-empty'>加载中…</View>
      ) : !isLoggedIn() ? (
        <View className='passes-empty'>登录后查看你的游园码</View>
      ) : tab === 'consume' ? (
        /* ---------------- 消费明细（原「我的」页消费记录弹窗搬过来） ---------------- */
        <>
          <View className='cons-tabs'>
            {CONS_TABS.map((t) => (
              <View
                key={t.key}
                className={`cons-tab${consTab === t.key ? ' cons-tab-on' : ''}`}
                onClick={() => setConsTab(t.key)}
              >
                <Text>{t.text}</Text>
              </View>
            ))}
          </View>

          {consLoading && !consList.length ? (
            <View className='passes-empty'>加载中…</View>
          ) : consShown.length === 0 ? (
            <View className='cons-empty'>
              <Text className='cons-empty-emoji'>🧾</Text>
              <Text className='cons-empty-text'>
                {consList.length
                  ? '这个状态下暂时没有记录，换个筛选看看～'
                  : '还没有消费记录，去首页「游乐消费」选项目扣费吧～'}
              </Text>
            </View>
          ) : (
            <ScrollView scrollY className='cons-list' showScrollbar={false}>
              {consShown.map((c) => (
                <View key={c.order_no} className='cons-order'>
                  <View className='cons-order-main'>
                    {/* 一单一券包：券的进度才是用户真正关心的
                        （整单核销码展示在这里既看不懂，也不代表整单状态） */}
                    <Text className='cons-order-name'>🎢 券包 {c.pass_total || 0} 张</Text>
                    <Text className='cons-order-sub'>
                      {parseItems(c.items_json)
                        .map((it) => `${it.name}×${it.qty}`)
                        .join('、') || '消费'}
                    </Text>
                    {(c.pass_total || 0) > 0 && (
                      <Text className='cons-order-sub'>
                        {c.status === 'refunded'
                          ? '其余已退款'
                          : c.pass_unused > 0
                            ? `还剩 ${c.pass_unused} 张待玩`
                            : '已全部玩完'}
                      </Text>
                    )}
                  </View>
                  <View className='cons-order-right'>
                    <Text className='cons-order-coins'>-🪙 {c.total_coins}</Text>
                    <Text
                      className={`cons-order-status${c.status === 'used' ? ' cons-order-status-used' : c.status === 'refunded' ? ' cons-order-status-refunded' : ''}`}
                    >
                      {c.status === 'used' ? '已核销' : c.status === 'refunded' ? '已退款' : '未核销'}
                    </Text>
                    {/* 整单退未核销的券：临时有事一次买了 5 项全不玩了，不用一张张点 */}
                    {c.status !== 'refunded' && (c.pass_unused || 0) > 0 && (
                      <Text
                        className={`cons-order-refund${refunding === c.order_no ? ' cons-order-refund-off' : ''}`}
                        onClick={() => askRefundOrder(c)}
                      >
                        {refunding === c.order_no ? '退款中…' : `退未核销 ${c.pass_unused} 张`}
                      </Text>
                    )}
                  </View>
                </View>
              ))}
              <Text className='cons-foot-tip'>实时来自园区消费与退款，进入本页自动刷新</Text>
            </ScrollView>
          )}
        </>
      ) : !passes.length ? (
        <View className='passes-empty'>
          还没有游园券
          <Text className='passes-empty-hint'>去首页「游乐消费」选项目下单，买完这里就会出现</Text>
        </View>
      ) : (
        <>
          {shown ? (
            <View className='pass-hero'>
              <Text className='pass-hero-label'>当前要玩</Text>
              <Text className='pass-hero-name'>{shown.project_name} × 1</Text>
              <Text className='pass-hero-code' onClick={() => copyCode(shown.item_code)}>
                {shown.item_code}
              </Text>
              {qr ? (
                <Image className='pass-hero-qr' src={qr} mode='aspectFit' />
              ) : (
                <View className='pass-hero-qr pass-hero-qr-plain'>
                  <Text className='pass-hero-qr-tip'>
                    {qrOk ? '二维码加载中…' : '二维码暂不可用，请把上面的券码念给工作人员'}
                  </Text>
                </View>
              )}
              <Text className='pass-hero-tip'>
                还有 {unused.length} 张待玩 · 点一下券码可复制
              </Text>
              {/* 临时不玩了：现场没人、又不想跑一趟柜台时，用户自己把这张退掉 */}
              {shown.status === 'unused' && (
                <View
                  className={`pass-hero-refund${refunding === shown.item_code ? ' pass-hero-refund-off' : ''}`}
                  onClick={() => askRefundPass(shown)}
                >
                  <Text className='pass-hero-refund-t'>
                    {refunding === shown.item_code ? '退款中…' : '不玩了，退这张券'}
                  </Text>
                </View>
              )}
            </View>
          ) : (
            <View className='pass-hero pass-hero-done'>
              <Text className='pass-hero-name'>所有项目都玩完啦</Text>
              <Text className='pass-hero-tip'>想再玩就去首页「游乐消费」再买一次</Text>
            </View>
          )}

          <View className='pass-filter'>
            <View
              className={`pass-filter-btn${onlyUnused ? ' pass-filter-on' : ''}`}
              onClick={() => setOnlyUnused((v) => !v)}
            >
              <Text>只看待玩</Text>
            </View>
            <Text className='pass-filter-hint'>
              待玩 {unused.length} · 已玩 {used.length} · 已退 {refunded.length}
            </Text>
          </View>

          {unused.length > 0 && (
            <View className='pass-group'>
              <Text className='pass-group-title'>待玩 {unused.length} 张</Text>
              {unused.map((p) => renderRow(p, false))}
            </View>
          )}

          {/* 已玩/已退是历史，默认收起，点标题才铺开 */}
          {!onlyUnused && used.length > 0 && (
            <View className='pass-group'>
              <View className='pass-group-head' onClick={() => setShowUsed((v) => !v)}>
                <Text className='pass-group-title'>已玩 {used.length} 张</Text>
                <Text className='pass-group-caret'>{showUsed ? '收起 ▴' : '展开 ▾'}</Text>
              </View>
              {showUsed && used.slice(0, 20).map((p) => renderRow(p, true))}
            </View>
          )}

          {!onlyUnused && refunded.length > 0 && (
            <View className='pass-group'>
              <View className='pass-group-head' onClick={() => setShowRefunded((v) => !v)}>
                <Text className='pass-group-title'>已退 {refunded.length} 张</Text>
                <Text className='pass-group-caret'>{showRefunded ? '收起 ▴' : '展开 ▾'}</Text>
              </View>
              {showRefunded && refunded.slice(0, 20).map((p) => renderRow(p, true))}
            </View>
          )}
        </>
      )}
    </View>
  )
}
