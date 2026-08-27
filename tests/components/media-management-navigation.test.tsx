import { beforeEach, expect, test } from 'vitest'
import { useUiStore } from '../../src/stores/ui'

beforeEach(() => {
  localStorage.clear()
  useUiStore.setState({
    page: 'home',
    activeCategory: null,
    selectedWorkId: null,
    manageView: 'overview',
    navigationHistory: [],
    forwardHistory: [],
    canGoBack: false,
    canGoForward: false,
    query: '',
  })
})

test('媒体管理内部页面进入全局前进后退历史', () => {
  useUiStore.getState().goManage()
  expect(useUiStore.getState().manageView).toBe('overview')

  useUiStore.getState().goManageView('import')
  expect(useUiStore.getState()).toMatchObject({ page: 'manage', manageView: 'import', canGoBack: true })

  useUiStore.getState().goBack()
  expect(useUiStore.getState()).toMatchObject({ page: 'manage', manageView: 'overview', canGoForward: true })

  useUiStore.getState().goForward()
  expect(useUiStore.getState()).toMatchObject({ page: 'manage', manageView: 'import' })
})

test('从侧栏重新进入媒体管理时回到来源卡概览', () => {
  useUiStore.setState({ page: 'manage', manageView: 'maintenance' })
  useUiStore.getState().goHome()
  useUiStore.getState().goManage()

  expect(useUiStore.getState()).toMatchObject({ page: 'manage', manageView: 'overview' })
})
