// ============================================================
// 轻量 Markdown 渲染（小程序无 DOM，react-markdown 不可用）
// 支持 AI 回答受控格式：## 标题 / **加粗** / - 列表 / 1. 列表 / `code` /
//                       段落 / ![alt](url) 图片（点击放大）
// 配色沿用 HomePage mdComponents（标题灰黑、强调橙、代码粉）
// ============================================================
import { View, Text, Image } from '@tarojs/components'
import Taro from '@tarojs/taro'
import { API_BASE } from '@/utils/config'
import './MarkdownView.scss'

/** 图片地址补全：/static/... 之类相对路径拼上后端地址 */
function fullUrl(url: string): string {
  const u = (url || '').trim()
  if (!u) return ''
  if (/^https?:\/\//i.test(u) || u.startsWith('data:')) return u
  return API_BASE + (u.startsWith('/') ? u : `/${u}`)
}

/** 行内解析：**bold** 与 `code` → [{text, bold, code}] */
function parseInline(line: string): Array<{ text: string; bold?: boolean; code?: boolean }> {
  const segs: Array<{ text: string; bold?: boolean; code?: boolean }> = []
  const regex = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = regex.exec(line)) !== null) {
    if (m.index > last) segs.push({ text: line.slice(last, m.index) })
    const token = m[0]
    if (token.startsWith('**')) segs.push({ text: token.slice(2, -2), bold: true })
    else segs.push({ text: token.slice(1, -1), code: true })
    last = m.index + token.length
  }
  if (last < line.length) segs.push({ text: line.slice(last) })
  return segs.length ? segs : [{ text: line }]
}

export default function MarkdownView({ content }: { content: string }) {
  const lines = (content || '').split('\n')
  const blocks: React.ReactNode[] = []

  let listItems: string[] = []
  let listOrdered = false

  const flushList = () => {
    if (!listItems.length) return
    const items = listItems
    blocks.push(
      <View key={`l${blocks.length}`} className={listOrdered ? 'md-ol' : 'md-ul'}>
        {items.map((it, i) => (
          <View key={i} className='md-li'>
            <Text className='md-li-marker'>{listOrdered ? `${i + 1}. ` : '· '}</Text>
            <View className='md-li-body'>
              {parseInline(it).map((s, j) => (
                <Text key={j} className={s.bold ? 'md-strong' : s.code ? 'md-code' : 'md-text'}>
                  {s.text}
                </Text>
              ))}
            </View>
          </View>
        ))}
      </View>
    )
    listItems = []
  }

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd()
    const trimmed = line.trim()

    if (!trimmed) {
      flushList()
      return
    }
    // 图片：![alt](url)（AI 回答里的地图静态图/图表；点击可放大）
    const img = trimmed.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/)
    if (img) {
      flushList()
      const src = fullUrl(img[2])
      if (src) {
        blocks.push(
          <View key={idx} className='md-img-wrap'>
            <Image
              className='md-img'
              src={src}
              mode='widthFix'
              onClick={() => Taro.previewImage({ urls: [src], current: src })}
            />
            {!!img[1] && <Text className='md-img-caption'>{img[1]}</Text>}
          </View>
        )
      }
      return
    }
    // 标题
    const h = trimmed.match(/^(#{1,3})\s+(.*)$/)
    if (h) {
      flushList()
      const cls = h[1].length === 1 ? 'md-h1' : h[1].length === 2 ? 'md-h2' : 'md-h3'
      blocks.push(
        <Text key={idx} className={cls}>{parseInline(h[2]).map((s) => s.text).join('')}</Text>
      )
      return
    }
    // 无序列表
    const ul = trimmed.match(/^[-*]\s+(.*)$/)
    if (ul) {
      if (listOrdered) flushList()
      listOrdered = false
      listItems.push(ul[1])
      return
    }
    // 有序列表
    const ol = trimmed.match(/^\d+[.、]\s*(.*)$/)
    if (ol) {
      if (!listOrdered) flushList()
      listOrdered = true
      listItems.push(ol[1])
      return
    }
    // 普通段落
    flushList()
    blocks.push(
      <View key={idx} className='md-p'>
        {parseInline(trimmed).map((s, j) => (
          <Text key={j} className={s.bold ? 'md-strong' : s.code ? 'md-code' : 'md-text'}>
            {s.text}
          </Text>
        ))}
      </View>
    )
  })
  flushList()

  return <View className='markdown-view'>{blocks}</View>
}
