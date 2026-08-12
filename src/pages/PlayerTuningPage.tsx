// KumiPlayer 播放器调节页面：Anime4K 永久默认效果设置
//
// 与右键菜单的关系：
// - 本页面保存的是“之后开始播放的新视频”的永久默认值；
// - 右键菜单只临时改变当前视频，下一视频恢复本页面设置的永久默认。

import { useEffect, useState } from 'react';
import { Button, Dropdown, Option, Spinner } from '@fluentui/react-components';
import { ArrowLeft, CheckCircle2, TriangleAlert } from 'lucide-react';
import { configApi, type PublicConfig } from '../api/config';
import { useUiStore } from '../stores/ui';

type Anime4kMode = 'off' | 'a' | 'b' | 'c' | 'a+a' | 'b+b' | 'c+a';
type Anime4kQuality = 'light' | 'balanced' | 'high';

const MODE_OPTIONS: Array<{ value: Anime4kMode; label: string }> = [
  { value: 'off', label: '关闭' },
  { value: 'a', label: 'Anime4K Mode A' },
  { value: 'b', label: 'Anime4K Mode B' },
  { value: 'c', label: 'Anime4K Mode C' },
  { value: 'a+a', label: 'Anime4K Mode A+A' },
  { value: 'b+b', label: 'Anime4K Mode B+B' },
  { value: 'c+a', label: 'Anime4K Mode C+A' },
];

const QUALITY_OPTIONS: Array<{ value: Anime4kQuality; label: string }> = [
  { value: 'light', label: '轻量' },
  { value: 'balanced', label: '均衡' },
  { value: 'high', label: '高质量' },
];

export default function PlayerTuningPage() {
  const goBack = useUiStore((state) => state.goBack);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [mode, setMode] = useState<Anime4kMode>('off');
  const [quality, setQuality] = useState<Anime4kQuality>('balanced');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    configApi.getConfig()
      .then((data) => {
        if (cancelled) return;
        setConfig(data);
        setMode(data.mpv_anime4k_mode || 'off');
        setQuality(data.mpv_anime4k_quality || 'balanced');
      })
      .catch((err: Error) => {
        if (!cancelled) setError(`读取配置失败：${err.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const save = async () => {
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      await configApi.patchConfig({ mpv_anime4k_mode: mode, mpv_anime4k_quality: quality });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setError(`保存失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="player-tuning-page">
      <div className="player-tuning-header">
        <Button appearance="subtle" icon={<ArrowLeft size={16} />} onClick={() => goBack()}>返回</Button>
        <div>
          <span className="media-import-step-label">播放器调节</span>
          <h2>Anime4K 默认效果</h2>
        </div>
      </div>

      <section className="player-tuning-section">
        <h3>播放器状态</h3>
        <div className="player-tuning-status">
          {config ? (
            <span className="player-tuning-status-ok"><CheckCircle2 size={15} /> 配置已加载</span>
          ) : (
            <span className="player-tuning-status-warn"><TriangleAlert size={15} /> 配置不可用</span>
          )}
        </div>
        <p className="player-tuning-note">
          此设置对之后开始播放的新视频生效；右键菜单中的调整只影响当前视频，不会改变这里的默认值。
        </p>
      </section>

      <section className="player-tuning-section">
        <h3>Anime4K 默认效果</h3>
        {loading ? (
          <div className="player-tuning-loading"><Spinner size="tiny" /> 正在读取…</div>
        ) : (
          <div className="player-tuning-fields">
            <div className="player-tuning-field">
              <label>模式</label>
              <Dropdown
                value={MODE_OPTIONS.find((item) => item.value === mode)?.label || mode}
                selectedOptions={[mode]}
                onOptionSelect={(_, data) => {
                  const value = data.optionValue as Anime4kMode;
                  if (value) setMode(value);
                }}
              >
                {MODE_OPTIONS.map((item) => (
                  <Option key={item.value} value={item.value}>{item.label}</Option>
                ))}
              </Dropdown>
            </div>
            <div className="player-tuning-field">
              <label>质量</label>
              <Dropdown
                value={QUALITY_OPTIONS.find((item) => item.value === quality)?.label || quality}
                selectedOptions={[quality]}
                onOptionSelect={(_, data) => {
                  const value = data.optionValue as Anime4kQuality;
                  if (value) setQuality(value);
                }}
              >
                {QUALITY_OPTIONS.map((item) => (
                  <Option key={item.value} value={item.value}>{item.label}</Option>
                ))}
              </Dropdown>
            </div>
          </div>
        )}
      </section>

      <section className="player-tuning-section">
        <h3>如何选择</h3>
        <ul className="player-tuning-help">
          <li>不确定时按 <strong>Mode A → Mode B → Mode C</strong> 依次试听，保留观感最好的一个。</li>
          <li>增强模式（A+A / B+B / C+A）建议在显示放大至少 2× 时使用，否则可能过锐或劣化。</li>
          <li>播放掉帧时，优先把质量降为“均衡”或“轻量”，而不是关闭整个功能。</li>
        </ul>
      </section>

      {error && <div className="player-tuning-error" role="alert"><TriangleAlert size={15} /> {error}</div>}
      {saved && <div className="player-tuning-saved"><CheckCircle2 size={15} /> 已保存，之后播放的新视频将使用新默认值</div>}

      <div className="player-tuning-actions">
        <Button appearance="primary" icon={saving ? <Spinner size="tiny" /> : <CheckCircle2 size={15} />} disabled={saving || loading} onClick={() => void save()}>
          保存默认设置
        </Button>
      </div>
    </div>
  );
}
