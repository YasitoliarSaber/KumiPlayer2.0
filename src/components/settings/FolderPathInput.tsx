import { useEffect, useId, useRef, useState } from 'react';
import { Button } from '@fluentui/react-components';
import { FolderOpen } from 'lucide-react';
import { pickFolder } from '../../platform/folderPicker';
import '../../styles/folder-path-input.css';

/** 文件夹选择只更新草稿；保存由所在设置行或导入流程负责。 */
export default function FolderPathInput({ label, value, onChange, disabled = false, inputId, placeholder = '' }: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  inputId?: string;
  placeholder?: string;
}) {
  const errorId = useId();
  const alive = useRef(true);
  const choosingRef = useRef(false);
  const [choosing, setChoosing] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const choose = async () => {
    if (choosingRef.current || disabled) return;
    choosingRef.current = true;
    setChoosing(true);
    setError('');
    try {
      const selected = await pickFolder(value, `选择${label}`);
      if (alive.current && selected) onChange(selected);
    } catch {
      if (alive.current) setError('无法打开文件夹选择器，请重试或手动输入路径。');
    } finally {
      choosingRef.current = false;
      if (alive.current) setChoosing(false);
    }
  };
  return <div className="settings-folder-field">
    <div className="settings-folder-input">
      <input id={inputId} aria-label={label} aria-describedby={error ? errorId : undefined} type="text" className="settings-input" value={value} placeholder={placeholder} title={value || undefined} disabled={disabled || choosing} onChange={event => onChange(event.target.value)} />
      <Button appearance="secondary" icon={<FolderOpen size={16} />} aria-label={`选择${label}`} disabled={disabled || choosing} onClick={() => void choose()}>浏览…</Button>
    </div>
    {error && <p id={errorId} role="alert" className="settings-folder-error">{error}</p>}
  </div>;
}
