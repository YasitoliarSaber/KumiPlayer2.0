import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const css = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
const marker = '用户反馈（§14 / §14.7）'
const start = css.indexOf(marker)
const block = start >= 0 ? css.slice(start) : ''

test('关联作品 / 相关推荐：悬浮不再出现明显深色背景框', () => {
  assert.ok(start >= 0, 'index.css 末尾必须存在该覆盖块（否则被前面的 0.25 黑阴影压回去）')
  assert.match(
    block,
    /\.detail-related-card:is\(:hover, :focus-visible\)[\s\S]*?box-shadow: 0 8px 18px rgba\(28, 28, 28, 0\.04\) !important/,
    '悬浮阴影必须降到与剧集列表同量级',
  )
  assert.match(
    block,
    /\.detail-similar-card[\s\S]*?background: transparent !important/,
    '悬浮不得保留实心底色',
  )
})

test('关联作品 / 相关推荐：海报为竖版 2:3', () => {
  // 两条选择器在追加块里是**并列**的，其后统一给 aspect-ratio。
  assert.match(
    block,
    /\.detail-related-card \.detail-poster-artwork,[\s\S]*?\.detail-similar-card \.detail-poster-artwork \{\s*aspect-ratio: 2 \/ 3;/,
    '关联作品与相关推荐的海报都应为竖版 2:3',
  )
})
