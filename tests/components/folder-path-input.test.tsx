import { fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import FolderPathInput from '../../src/components/settings/FolderPathInput';
import { pickFolder } from '../../src/platform/folderPicker';

vi.mock('../../src/platform/folderPicker', () => ({ pickFolder: vi.fn() }));

test('取消与失败不改路径，并显示可重试错误', async () => {
  const onChange = vi.fn();
  vi.mocked(pickFolder).mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('dialog unavailable'));
  render(<FolderPathInput label="本地目录" value="J:/Media" onChange={onChange} />);
  fireEvent.click(screen.getByRole('button', { name: '选择本地目录' }));
  await waitFor(() => expect(screen.getByRole('button')).toBeEnabled());
  expect(onChange).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '选择本地目录' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('无法打开文件夹选择器');
  expect(onChange).not.toHaveBeenCalled();
});

test('切换连接后迟到的文件夹选择不能更新新连接', async () => {
  let finish!: (value: string) => void;
  vi.mocked(pickFolder).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const onChange = vi.fn();
  const view = render(<FolderPathInput label="挂载位置" value="K:/" onChange={onChange} />);
  fireEvent.click(screen.getByRole('button', { name: '选择挂载位置' }));
  view.unmount();
  await act(async () => finish('J:/'));
  expect(onChange).not.toHaveBeenCalled();
});
