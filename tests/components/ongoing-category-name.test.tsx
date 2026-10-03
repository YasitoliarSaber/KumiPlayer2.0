import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';
import { useUiStore } from '../../src/stores/ui';
import Sidebar from '../../src/components/shell/Sidebar';
import CategoryPage from '../../src/pages/CategoryPage';
import OngoingUpdateSettingsPanel from '../../src/components/settings/OngoingUpdateSettingsPanel';

vi.mock('../../src/stores/library', () => ({
  useLibraryStore: (selector: (state: unknown) => unknown) => selector({ works: [], history: [], loading: false, error: null }),
}));
vi.mock('../../src/stores/bangumi', () => ({ useBangumiStore: () => ({ user: null, hasStoredCredential: false }) }));
vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: { trackingSources: vi.fn().mockResolvedValue({ sources: [] }) } }));

beforeEach(() => {
  localStorage.clear();
  useUiStore.setState({ page: 'home', activeCategory: null, sidebarMode: 'expanded', source: 'all', ongoingCategoryName: '新番' });
});

test('分类名称默认为新番，自定义名称保存重载仍保留且分类键保持seasonal', async () => {
  expect(typeof useUiStore.getState().setOngoingCategoryName).toBe('function');
  useUiStore.getState().setOngoingCategoryName('追更剧集');
  useUiStore.getState().goCategory('seasonal');
  const persisted = localStorage.getItem('kumiplayer-ui');
  expect(JSON.parse(persisted || '{}').state.ongoingCategoryName).toBe('追更剧集');
  useUiStore.setState({ ongoingCategoryName: '临时值' });
  localStorage.setItem('kumiplayer-ui', persisted!);
  await useUiStore.persist.rehydrate();
  expect(useUiStore.getState().ongoingCategoryName).toBe('追更剧集');
  expect(useUiStore.getState().activeCategory).toBe('seasonal');
});

test('空白名称回默认，去除控制字符并按24个Unicode字符截断', () => {
  expect(typeof useUiStore.getState().setOngoingCategoryName).toBe('function');
  const setName = useUiStore.getState().setOngoingCategoryName;
  setName('  \n\t\u0000 ');
  expect(useUiStore.getState().ongoingCategoryName).toBe('新番');
  setName('  连\n载\u202e剧集  ');
  expect(useUiStore.getState().ongoingCategoryName).toBe('连载剧集');
  setName('🎬'.repeat(25));
  expect(useUiStore.getState().ongoingCategoryName).toBe('🎬'.repeat(24));
});

test('侧栏与分类标题和刷新按钮同步名称，渲染普通文本并保持原导航', async () => {
  useUiStore.setState({ ongoingCategoryName: '<b>追更剧集</b>' });
  render(<><Sidebar /><CategoryPage /></>);
  const navigation = screen.getByRole('button', { name: /<b>追更剧集<\/b>/ });
  fireEvent.click(navigation);
  expect(useUiStore.getState().activeCategory).toBe('seasonal');
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('<b>追更剧集</b>');
  expect(screen.getByRole('heading', { level: 1 }).querySelector('b')).toBeNull();
  expect(screen.getByRole('button', { name: '刷新<b>追更剧集</b>' })).toBeVisible();
  await act(async () => {});
});

test('设置分类名称只保存本机界面偏好，不请求后台或触发扫描', () => {
  const onSave = vi.fn();
  render(<OngoingUpdateSettingsPanel config={{}} onSave={onSave}
    categoryName="新番" onSaveCategoryName={name => useUiStore.getState().setOngoingCategoryName(name)} />);
  const input = screen.getByRole('textbox', { name: '分类名称' });
  fireEvent.change(input, { target: { value: '连载剧集' } });
  fireEvent.click(screen.getByRole('button', { name: '保存分类名称' }));
  expect(useUiStore.getState().ongoingCategoryName).toBe('连载剧集');
  expect(onSave).not.toHaveBeenCalled();
  expect(screen.getByRole('status')).toHaveTextContent('分类名称已保存');
});

test('旧UI偏好缺少名称时默认恢复，持久异常名称也会清洗', async () => {
  localStorage.setItem('kumiplayer-ui', JSON.stringify({ version: 4, state: { appearanceMode: 'mica' } }));
  await useUiStore.persist.rehydrate();
  expect(useUiStore.getState().ongoingCategoryName).toBe('新番');
  localStorage.setItem('kumiplayer-ui', JSON.stringify({ version: 4, state: { ongoingCategoryName: '连\u0000载' } }));
  await useUiStore.persist.rehydrate();
  expect(useUiStore.getState().ongoingCategoryName).toBe('连载');
});

test('分类名称本机保存失败不显示成功', () => {
  render(<OngoingUpdateSettingsPanel config={{}} onSave={vi.fn()} categoryName="新番"
    onSaveCategoryName={() => { throw new Error('模拟本机偏好写入失败'); }} />);
  fireEvent.change(screen.getByRole('textbox', { name: '分类名称' }), { target: { value: '追更剧集' } });
  fireEvent.click(screen.getByRole('button', { name: '保存分类名称' }));
  expect(screen.getByRole('alert')).toHaveTextContent('模拟本机偏好写入失败');
  expect(screen.queryByText('分类名称已保存')).not.toBeInTheDocument();
});
