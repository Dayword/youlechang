// ============================================================
// 统一用户头像组件：AI 问答页 / 办卡充值页 / 我的页 共用
// - 有服务器头像 → 渲染图片（上传后三端同源同步）
// - 无头像 → 渲染兜底 emoji（👤 或自定义）
// ============================================================
import { View, Text, Image } from '@tarojs/components'
import { getAvatarUrl } from '@/utils/user'
import type { MpUser } from '@/utils/auth'

interface Props {
  user?: MpUser | null
  /** 兜底 emoji，默认 👤 */
  fallback?: string
  /** 容器类名（各页沿用自身样式：.avatar-user / .avatar / .profile-avatar …） */
  className?: string
  /** 图片类名（可省略，默认用 className + '-img'） */
  imgClassName?: string
  /** 兜底文本类名（可省略） */
  textClassName?: string
}

export default function UserAvatar({
  user, fallback = '👤', className = '', imgClassName = '', textClassName = '',
}: Props) {
  const url = getAvatarUrl(user)
  return (
    <View className={className}>
      {url ? (
        <Image
          className={imgClassName || `${className}-img`}
          src={url}
          mode='aspectFill'
        />
      ) : (
        <Text className={textClassName || `${className}-icon`}>{fallback}</Text>
      )}
    </View>
  )
}
