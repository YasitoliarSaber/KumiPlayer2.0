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
    const imageStyle = provider === 'quark'
      ? { width: size * (60 / 19) * 0.8, height: size * 0.8, objectFit: 'contain' as const }
      : { width: '100%', height: '100%', objectFit: 'contain' as const }
    return (
      <span
        className={`media-provider-icon media-provider-brand provider-${provider}`}
        style={{ width: size, height: size }}
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
