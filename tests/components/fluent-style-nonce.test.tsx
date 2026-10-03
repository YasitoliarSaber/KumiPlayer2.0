import { render } from '@testing-library/react'
import { FluentProvider, Radio, RadioGroup, RendererProvider } from '@fluentui/react-components'
import { expect, test } from 'vitest'
import * as fluent from '../../src/design/fluentTheme'

test('动态控件、主题和嵌套弹窗样式继承桌面启动样式的 nonce', () => {
  const bootStyle = document.createElement('style')
  bootStyle.dataset.kumiBootStyles = ''
  bootStyle.nonce = 'test-style-nonce'
  document.head.append(bootStyle)
  try {
    const previousStyles = new Set(document.head.querySelectorAll('style'))
    const renderer = fluent.createKumiFluentRenderer(document)
    render(<RendererProvider renderer={renderer}><FluentProvider theme={fluent.getKumiFluentTheme('cinema')}>
      <RadioGroup><Radio value="completed" label="已完结" /></RadioGroup>
      <FluentProvider theme={fluent.getKumiFluentTheme('fluent')}><Radio value="ongoing" label="新番" /></FluentProvider>
    </FluentProvider></RendererProvider>)
    const tags = [...document.head.querySelectorAll('style')].filter(tag => !previousStyles.has(tag))
    expect(tags.length).toBeGreaterThan(2)
    expect(tags.every(tag => tag.nonce === 'test-style-nonce')).toBe(true)
    expect(renderer.styleElementAttributes?.nonce).toBe('test-style-nonce')
  } finally { bootStyle.remove() }
})

test('普通开发页面没有 nonce 时不生成或伪造安全标记', () => {
  const isolatedDocument = document.implementation.createHTMLDocument('Fixture')
  const renderer = fluent.createKumiFluentRenderer(isolatedDocument)
  expect(renderer.styleElementAttributes?.nonce).toBeUndefined()
})
