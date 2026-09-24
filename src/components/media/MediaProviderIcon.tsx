/** P-004：统一来源视觉标识，品牌资源随应用本地打包，不在运行时热链。 */

import { Cloud24Regular, Folder24Regular } from '@fluentui/react-icons'
import baiduLogo from '../../assets/provider-icons/baidu.svg'
import pan115Logo from '../../assets/provider-icons/pan115.jpg'
import quarkLogo from '../../assets/provider-icons/quark.svg'

export type ProviderVisualKey = 'local' | 'pan115' | 'baidu' | 'quark' | 'other' | 'openlist'

const BRAND_ASSETS: Partial<Record<ProviderVisualKey, string>> = {
  pan115: pan115Logo,
  baidu: baiduLogo,
  quark: quarkLogo,
}

export function MediaProviderIcon({ provider, size = 22 }: { provider: ProviderVisualKey; size?: number }) {
  const brandAsset = BRAND_ASSETS[provider]
  if (brandAsset) {
    // 宽高比自带的品牌图（夸克是 60/19 的横长条形）不能塞进 size×size 的方框：
    // 图会溢出容器、盖到紧跟其后的"夸克网盘"标签上（用户截图里的叠字）。
    // 容器尺寸按图片实际绘制尺寸给出，图标区宽度因此是可预测的。
    const wide = provider === 'quark'
    const width = wide ? size * (60 / 19) * 0.8 : size
    const height = wide ? size * 0.8 : size
    const imageStyle = { width: '100%', height: '100%', objectFit: 'contain' as const }
    return (
      <span
        className={`media-provider-icon media-provider-brand provider-${provider}`}
        style={{ width, height, flex: '0 0 auto' }}
        aria-hidden="true"
      >
        <img className="media-provider-brand-image" src={brandAsset} alt="" style={imageStyle} />
      </span>
    )
  }
  const Icon = provider === 'local' ? Folder24Regular : Cloud24Regular
  return <Icon className={`media-provider-icon provider-${provider}`} width={size} height={size} aria-hidden="true" />
}

export function providerVisualFor(value: string): ProviderVisualKey {
  if (value === 'local' || value === 'pan115' || value === 'baidu' || value === 'quark' || value === 'openlist') {
    return value
  }
  return 'other'
}
