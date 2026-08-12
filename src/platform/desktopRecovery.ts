import { invoke, isTauri } from '@tauri-apps/api/core';

const WINDOWS_PATH_PATTERN = /(?:[A-Za-z]:\\|\\\\)[^\r\n\t"']+/g;
const TOKEN_PATTERN = /\b(?:bearer\s+)?[A-Za-z0-9_-]{24,}\b/gi;

export function isDesktopRuntime(): boolean {
  return isTauri();
}

export async function restartDesktopBackend(): Promise<void> {
  if (!isTauri()) throw new Error('浏览器预览模式不能重启桌面后端');
  await invoke('restart_backend');
}

export async function openDesktopLogDirectory(): Promise<void> {
  if (!isTauri()) throw new Error('浏览器预览模式没有桌面日志目录');
  await invoke('open_log_directory');
}

export function redactDiagnosticText(value: string): string {
  return value
    .replace(WINDOWS_PATH_PATTERN, '[本地路径已隐藏]')
    .replace(TOKEN_PATTERN, '[敏感值已隐藏]');
}

export async function saveDesktopDiagnostics(content: string): Promise<string> {
  if (!isTauri()) throw new Error('浏览器预览模式不能导出桌面诊断');
  return invoke<string>('save_diagnostics', {
    content: redactDiagnosticText(content).slice(0, 100_000),
  });
}
