import { useEffect, useId, useRef, useState } from 'react';
import { Button, Input, Select, Spinner, Switch } from '@fluentui/react-components';
import type { PublicConfig } from '../../api/config';

type UpdateSettings = Pick<PublicConfig, 'ongoing_update_on_startup' | 'ongoing_update_interval_minutes'>;

const intervalOptions = [
  { value: 0, label: '关闭' }, { value: 15, label: '每 15 分钟' },
  { value: 30, label: '每 30 分钟' }, { value: 60, label: '每小时' },
  { value: 120, label: '每 2 小时' }, { value: 360, label: '每 6 小时' },
  { value: 720, label: '每 12 小时' }, { value: 1440, label: '每天' },
  { value: 10080, label: '每周' },
];

export default function OngoingUpdateSettingsPanel({ config, onSave, externalBusy = false, categoryName = '新番', onSaveCategoryName }: {
  config: UpdateSettings;
  onSave: (patch: UpdateSettings) => Promise<void>;
  externalBusy?: boolean;
  categoryName?: string;
  onSaveCategoryName?: (name: string) => string;
}) {
  const intervalId = useId();
  const nameId = useId();
  const [nameDraft, setNameDraft] = useState(categoryName);
  useEffect(() => setNameDraft(categoryName), [categoryName]);
  const pending = useRef(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const [failed, setFailed] = useState(false);
  const interval = config.ongoing_update_interval_minutes ?? 0;
  const options = intervalOptions.some(option => option.value === interval)
    ? intervalOptions : [...intervalOptions, { value: interval, label: `每 ${interval} 分钟` }].sort((a, b) => a.value - b.value);
  const disabled = saving || externalBusy;

  const saveName = () => {
    if (disabled || !onSaveCategoryName) return;
    try {
      setNameDraft(onSaveCategoryName(nameDraft));
      setFailed(false);
      setNotice('分类名称已保存');
    } catch (cause) {
      setFailed(true);
      setNotice(`保存失败：${cause instanceof Error ? cause.message : '请稍后重试'}`);
    }
  };

  const save = async (patch: UpdateSettings) => {
    if (pending.current || externalBusy) return;
    const minutes = patch.ongoing_update_interval_minutes;
    if (minutes !== undefined && (!Number.isInteger(minutes) || (minutes !== 0 && (minutes < 15 || minutes > 10080)))) {
      setFailed(true);
      setNotice('更新间隔应为关闭，或 15 到 10080 分钟。');
      return;
    }
    pending.current = true;
    setSaving(true);
    setNotice('');
    setFailed(false);
    try {
      await onSave(patch);
      setNotice('新番更新设置已保存');
    } catch (cause) {
      setFailed(true);
      setNotice(`保存失败：${cause instanceof Error ? cause.message : '请稍后重试'}`);
    } finally {
      pending.current = false;
      setSaving(false);
    }
  };

  return <div className="settings-field-list">
    <div className="settings-config-row">
      <label htmlFor={nameId}>分类名称</label>
      <Input id={nameId} value={nameDraft} placeholder="新番" maxLength={48} disabled={disabled}
        onChange={(_, data) => setNameDraft(Array.from(data.value).slice(0, 24).join(''))}
        onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); saveName(); } }} />
      <Button appearance="secondary" aria-label="保存分类名称" className="settings-ghost-btn fluent-settings-btn" disabled={disabled || !onSaveCategoryName} onClick={saveName}>保存</Button>
    </div>
    <p className="field-help">最多 24 个字符，留空恢复“新番”；名称只改变这台电脑的分类显示，追更内容也可以是其他剧集。</p>
    <div className="settings-toggle-row">
      <span>启动时更新新番</span>
      <Switch aria-label="启动时更新新番" checked={config.ongoing_update_on_startup ?? false} disabled={disabled}
        onChange={(_, data) => void save({ ongoing_update_on_startup: data.checked })} />
    </div>
    <div className="settings-config-row">
      <label htmlFor={intervalId}>新番更新间隔</label>
      <Select id={intervalId} value={String(interval)} disabled={disabled}
        onChange={(_, data) => void save({ ongoing_update_interval_minutes: Number(data.value) })}>
        {options.map(option => <option value={option.value} key={option.value}>{option.label}</option>)}
      </Select>
    </div>
    <p className="field-help">仅更新导入时选择“新番”的来源；纯 TXT 来源需要选择新导出的清单，刷新与识别结果在新番分类查看。</p>
    {saving && <div className="settings-note" role="status"><Spinner size="tiny" />正在保存新番更新设置…</div>}
    {notice && <div className={`settings-note${failed ? ' danger' : ''}`} role={failed ? 'alert' : 'status'}>{notice}</div>}
  </div>;
}
