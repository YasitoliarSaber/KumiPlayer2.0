/**
 * P-004：统一来源视觉标识。
 *
 * providerVisuals 是 ProviderPicker、来源卡、OpenList 路由摘要的唯一映射；
 * 禁止在别处继续用单汉字/文字缩写充当图标。官方品牌资源无法合法本地复用，
 * 因此使用项目自有的差异化语义矢量 + 品牌色系，不伪造官方 Logo，离线可用。
 * 图标本身 aria-hidden，可访问名称始终由可见文字提供。
 */

export type ProviderVisualKey = 'local' | 'pan115' | 'baidu' | 'quark' | 'other' | 'openlist'

const PROVIDER_COLORS: Record<ProviderVisualKey, { from: string; to: string }> = {
  local: { from: '#5b6470', to: '#3f4550' },
  pan115: { from: '#2f7bff', to: '#1d5fd6' },
  baidu: { from: '#4a6cf7', to: '#2f47d0' },
  quark: { from: '#4da9ff', to: '#6a5cff' },
  other: { from: '#8a94a6', to: '#66707f' },
  openlist: { from: '#2f8f74', to: '#1f6b57' },
}

function ProviderGlyph({ provider }: { provider: ProviderVisualKey }) {
  switch (provider) {
    case 'local':
      return <path d="M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm1.5 3.5v9h11v-9h-11Z" />
    case 'pan115':
      // 115：云 + 数字标识，蓝白语义。
      return (
        <>
          <path d="M12 5c2.6 0 4.6 1.7 5 4a4.5 4.5 0 0 1-1 8.9H7.5A4.5 4.5 0 0 1 7 8a4.8 4.8 0 0 1 5-3Z" />
          <path d="M10.6 9.2 11.8 7h2.2l-1.7 4.4h-1.6l-.1-.4-2.2 3.2h3l.4-.6 1.2 1.2h1.8l-2.1-2.6 1.3-3.2h-2.1l-.6 1.5-1.9 3.2h1.6l1.3-2 1.1-1.9Z" transform="translate(0 -0.5)" />
        </>
      )
    case 'baidu':
      // 百度：熊掌抽象（四趾 + 掌垫），品牌蓝。
      return (
        <>
          <path d="M12 15.6c-2 0-3.6-1.2-3.6-3s1.6-3 3.6-3 3.6 1.2 3.6 3-1.6 3-3.6 3Z" />
          <path d="M5.6 10.2c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2Z" />
          <path d="M18.4 10.2c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2Z" />
          <path d="M8 6.6c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2Z" />
          <path d="M16 6.6c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2Z" />
        </>
      )
    case 'quark':
      // 夸克：原子/夸克环，蓝紫渐变语义。
      return (
        <>
          <ellipse cx="12" cy="12" rx="5.4" ry="2.3" transform="rotate(30 12 12)" />
          <ellipse cx="12" cy="12" rx="5.4" ry="2.3" transform="rotate(150 12 12)" />
          <circle cx="12" cy="12" r="1.9" />
        </>
      )
    case 'other':
      return <path d="M5 8a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8Zm2 0v8h10V8H7Z" />
    case 'openlist':
      return (
        <>
          <path d="M5 8.5A3.5 3.5 0 0 1 8.5 5h7A3.5 3.5 0 0 1 19 8.5v7a3.5 3.5 0 0 1-3.5 3.5h-7A3.5 3.5 0 0 1 5 15.5v-7Z" />
          <path d="M9 12a3 3 0 1 1 6 0 3 3 0 0 1-6 0Z" />
        </>
      )
  }
}

export function MediaProviderIcon({ provider, size = 22 }: { provider: ProviderVisualKey; size?: number }) {
  const colors = PROVIDER_COLORS[provider] ?? PROVIDER_COLORS.other
  const gradientId = `media-provider-${provider}`
  return (
    <svg
      className={`media-provider-icon provider-${provider}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={colors.from} />
          <stop offset="100%" stopColor={colors.to} />
        </linearGradient>
      </defs>
      <rect x="1.5" y="1.5" width="21" height="21" rx="5.5" fill={`url(#${gradientId})`} />
      <g fill="#ffffff" opacity="0.96">
        <ProviderGlyph provider={provider} />
      </g>
    </svg>
  )
}

export function providerVisualFor(value: string): ProviderVisualKey {
  if (value === 'local' || value === 'pan115' || value === 'baidu' || value === 'quark' || value === 'openlist') {
    return value
  }
  return 'other'
}
