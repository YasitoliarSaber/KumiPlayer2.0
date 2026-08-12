import { isTauri } from '@tauri-apps/api/core';

/** 使用 Tauri 原生目录选择器；浏览器预览返回 null，保留手动输入兜底。 */
export async function pickFolder(defaultPath?: string, title = '选择文件夹'): Promise<string | null> {
  if (!isTauri()) return null;
  const { open } = await import('@tauri-apps/plugin-dialog');
  const selected = await open({
    title,
    directory: true,
    multiple: false,
    recursive: true,
    defaultPath: defaultPath?.trim() || undefined,
  });
  return typeof selected === 'string' ? selected : null;
}

export async function pickFile(defaultPath?: string, title = '选择文件'): Promise<string | null> {
  if (!isTauri()) return null;
  const { open } = await import('@tauri-apps/plugin-dialog');
  const selected = await open({
    title,
    directory: false,
    multiple: false,
    defaultPath: defaultPath?.trim() || undefined,
    filters: [{ name: '可执行文件', extensions: ['exe'] }],
  });
  return typeof selected === 'string' ? selected : null;
}

/** 选择本机目录树 TXT（115 / 百度导出）；绝对路径后端合同只接受 .txt。 */
export async function pickDirectoryTreeFile(defaultPath?: string, title = '选择目录树 TXT'): Promise<string | null> {
  if (!isTauri()) return null;
  const { open } = await import('@tauri-apps/plugin-dialog');
  const selected = await open({
    title,
    directory: false,
    multiple: false,
    defaultPath: defaultPath?.trim() || undefined,
    filters: [{ name: '目录树文本', extensions: ['txt'] }],
  });
  return typeof selected === 'string' ? selected : null;
}
