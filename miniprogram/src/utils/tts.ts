// ============================================================
// 语音朗读（TTS）— 微信同声传译插件（WechatSI）
// 需在小程序后台添加插件并 app.config.ts 声明后方可使用；
// 未开通/插件不可用时降级为提示，不影响其它功能。
// ============================================================
import Taro from '@tarojs/taro'

export function speakText(text: string) {
  const content = (text || '').replace(/[#*`>\[\]()_~]/g, ' ').replace(/\s+/g, ' ').slice(0, 300)
  if (!content) return

  let plugin: any = null
  try {
    plugin = (Taro as any).requirePlugin?.('WechatSI')
  } catch { plugin = null }
  if (!plugin || typeof plugin.textToSpeech !== 'function') {
    Taro.showToast({ title: '语音朗读需开通「同声传译」插件', icon: 'none' })
    return
  }

  try {
    plugin.textToSpeech(
      { lang: 'zh_CN', tts: true, content },
      (res: any) => {
        if (res && res.filename) {
          const innerCtx = Taro.createInnerAudioContext()
          innerCtx.src = res.filename
          innerCtx.autoplay = true
          innerCtx.onError(() => {
            Taro.showToast({ title: '语音播放失败', icon: 'none' })
            innerCtx.destroy()
          })
          innerCtx.onEnded(() => innerCtx.destroy())
        } else {
          Taro.showToast({ title: '语音生成失败，请稍后再试', icon: 'none' })
        }
      },
      () => {
        Taro.showToast({ title: '语音生成失败，请稍后再试', icon: 'none' })
      },
    )
  } catch (e) {
    Taro.showToast({ title: '语音朗读暂不可用', icon: 'none' })
  }
}
