// ============================================================
// 思考过程时间线（真实思考轨迹版）：
// 步骤 = 后端 RAG/地图 Agent 链路上真实产生的中间结果
// （改写结果/各路召回数/精排保留数/校验通过数/耗时），
// 思考中逐条实时滚动，回答开始后折叠为可展开标签
// ============================================================
import { useState } from 'react'
import { View, Text, ScrollView } from '@tarojs/components'
import './ThinkingBox.scss'

/** 一条思考步骤（由后端 step 事件 / 兼容的 status 文本转换而来） */
export interface ThinkingStep {
  /** 步骤 id（用于 React key 稳定性） */
  key?: string
  /** 阶段标识：route/plan/rewrite/retrieve/merge/verify/generate/cache/map_tool/answer… */
  phase?: string
  /** 图标（未提供时按 phase 自动取默认） */
  icon?: string
  /** 短标题，如「并行检索」 */
  title?: string
  /** 展示文本（已包含真实数据） */
  text: string
  /** 该步耗时（秒，可选） */
  elapsed?: number
  /** 结构化原始数据（备用） */
  meta?: Record<string, any>
}

/** 各阶段默认图标 */
export const PHASE_ICONS: Record<string, string> = {
  route: '🗺️',
  plan: '🧭',
  rewrite: '✍️',
  retrieve: '🔎',
  merge: '🔀',
  verify: '🎯',
  generate: '🤖',
  cache: '⚡',
  map_tool: '📍',
  answer: '💬',
}

export default function ThinkingBox({
  steps,
  done,
}: {
  steps: ThinkingStep[]
  done: boolean
}) {
  const [open, setOpen] = useState(false)
  const showList = done ? open : true

  return (
    <View className='thinking-box'>
      <View className='thinking-header' onClick={() => setOpen(!open)}>
        {!done && <Text className='breathe'>🎡</Text>}
        <Text className='thinking-title'>
          {done ? `🧠 思考轨迹（${steps.length} 步）` : '🧠 思考中...'}
        </Text>
        {done && <Text className='thinking-toggle'>{open ? '▴ 收起' : '▾ 展开'}</Text>}
      </View>

      {showList && (
        <ScrollView scrollY className='thinking-list' scrollTop={100000}>
          {steps.map((s, i) => {
            const icon = s.icon || PHASE_ICONS[s.phase || ''] || '💭'
            return (
              <View key={s.key || i} className='thinking-item msg-enter'>
                <Text className='thinking-icon'>{icon}</Text>
                <Text className='thinking-text'>
                  {s.title && s.title !== s.text ? `${s.title}：` : ''}
                  {s.text}
                  {!!s.elapsed && s.elapsed > 0 && (
                    <Text className='thinking-elapsed'>{s.elapsed}s</Text>
                  )}
                </Text>
              </View>
            )
          })}
          {!done && <Text className='thinking-ellipsis breathe'>💭 思考中...</Text>}
        </ScrollView>
      )}
    </View>
  )
}
