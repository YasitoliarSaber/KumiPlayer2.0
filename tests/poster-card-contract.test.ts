import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const card = readFileSync(new URL('../src/components/library/PosterCard.tsx', import.meta.url), 'utf8')

test('海报卡始终渲染标题占位：未解码或失败时不出现空白海报位（白卡）', () => {
  assert.match(card, /poster-placeholder-title/, '必须常驻渲染标题占位')
  assert.doesNotMatch(
    card,
    /\{imageUrl \? \(/,
    '不应再用 imageUrl 三元分支把占位与图片互斥',
  )
})

test('缩略图失败后不得重复请求原图（后端生成失败时已返回原图）', () => {
  assert.doesNotMatch(
    card,
    /setUseOriginalImage\(true\)/,
    '必须删除失败后切换到原图的重复请求',
  )
})

test('横图也使用按尺寸生成的缩略图（不因 isHorizontal 跳过）', () => {
  assert.doesNotMatch(
    card,
    /thumbnailWidth > 0 && !isHorizontal/,
    '横图此前跳过缩略图，等于把大背景图塞进小卡',
  )
  assert.match(card, /thumbnailWidth > 0 \? \{ thumbnailWidth \}/, '应按尺寸传 thumbnailWidth')
})
