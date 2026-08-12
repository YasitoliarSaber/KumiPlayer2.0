type TrackingScanResult = {
  display_title?: unknown;
  work_id?: unknown;
  added_episode_count?: unknown;
  added_count?: unknown;
};

function resultItems(result: Record<string, unknown>): TrackingScanResult[] {
  return Array.isArray(result.results)
    ? result.results.filter((item): item is TrackingScanResult => Boolean(item) && typeof item === 'object')
    : [];
}

export function formatTrackingScanNotice(result: Record<string, unknown>): string {
  const updates = resultItems(result).flatMap((item) => {
    const added = Number(item.added_episode_count ?? item.added_count ?? 0);
    if (!Number.isFinite(added) || added <= 0) return [];
    const title = String(item.display_title || item.work_id || '未命名作品').trim();
    return [`${title}新增 ${added} 集`];
  });
  const waitingReview = Number(result.waiting_review || 0);
  const failed = Number(result.failed || 0);
  const issues = [
    waitingReview > 0 ? `${waitingReview} 部未自动完成` : '',
    failed > 0 ? `${failed} 部来源不可用或处理失败` : '',
  ].filter(Boolean);

  if (updates.length > 0) {
    return `更新完成：${updates.join('；')}${issues.length ? `；${issues.join('，')}` : ''}`;
  }
  if (issues.length > 0) return `扫描完成：${issues.join('，')}`;
  return '扫描完成，未发现新增剧集';
}
