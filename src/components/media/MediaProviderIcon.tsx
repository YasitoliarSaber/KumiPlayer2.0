/**
 * P-004：统一来源视觉标识。
 *
 * 来源名称始终由可见文字提供，图标使用 Fluent UI 的通用官方图标，
 * 不再在页面内绘制或伪造第三方网盘 Logo。
 */

import { Cloud24Regular, Folder24Regular } from '@fluentui/react-icons'

export type ProviderVisualKey = 'local' | 'pan115' | 'baidu' | 'quark' | 'other' | 'openlist'

export function MediaProviderIcon({ provider, size = 22 }: { provider: ProviderVisualKey; size?: number }) {
  const Icon = provider === 'local' ? Folder24Regular : Cloud24Regular
  return <Icon className={`media-provider-icon provider-${provider}`} width={size} height={size} aria-hidden="true" />
}

export function providerVisualFor(value: string): ProviderVisualKey {
  if (value === 'local' || value === 'pan115' || value === 'baidu' || value === 'quark' || value === 'openlist') {
    return value
  }
  return 'other'
}
