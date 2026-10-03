/** 新番分类复用媒体管理的来源更新/草稿恢复入口。 */
const actionKey = 'kumiplayer.media-v4.ongoing-source-action'

export interface OngoingSourceAction {
  root_id: string
  action: 'review' | 'new_txt'
  revision_id?: string
}

export function queueOngoingSourceAction(action: OngoingSourceAction): void {
  sessionStorage.setItem(actionKey, JSON.stringify(action))
  localStorage.removeItem('kumiplayer.media-v4.active-revision')
}

export function consumeOngoingSourceAction(): OngoingSourceAction | null {
  const raw = sessionStorage.getItem(actionKey)
  if (!raw) return null
  sessionStorage.removeItem(actionKey)
  try {
    const value = JSON.parse(raw) as Partial<OngoingSourceAction>
    if (typeof value.root_id !== 'string' || !value.root_id || !['review', 'new_txt'].includes(value.action || '')) return null
    return { root_id: value.root_id, action: value.action as OngoingSourceAction['action'],
      ...(typeof value.revision_id === 'string' ? { revision_id: value.revision_id } : {}) }
  } catch {
    return null
  }
}
