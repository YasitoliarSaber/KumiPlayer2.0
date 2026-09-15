// KumiPlayer 播放器调节页面：Anime4K 永久默认效果设置
//
// 与右键菜单的关系：
// - 本页面保存的是“之后开始播放的新视频”的永久默认值；
// - 右键菜单只临时改变当前视频，下一视频恢复本页面设置的永久默认。

import { useEffect, useState } from 'react';
import { Button, Dropdown, Option, Spinner } from '@fluentui/react-components';
import { ArrowLeft, CheckCircle2, FolderOpen, TriangleAlert } from 'lucide-react';
import { configApi, type PublicConfig } from '../api/config';
import { pickFile } from '../platform/folderPicker';
import { useUiStore } from '../stores/ui';

type Anime4kMode = 'off' | 'a' | 'b' | 'c' | 'a+a' | 'b+b' | 'c+a';
type Anime4kQuality = 'light' | 'balanced' | 'high';
type PlayerMode = 'internal' | 'external';

const PLAYER_MODE_OPTIONS: Array<{ value: PlayerMode; label: string }> = [
  { value: 'internal', label: 'KumiPlayer 内置播放器' },
  { value: 'external', label: '外部 MPV 整合包' },
];

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
  const [playerMode, setPlayerMode] = useState<PlayerMode>('internal');
  const [externalPath, setExternalPath] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [openingConfigDir, setOpeningConfigDir] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [configFolderOpened, setConfigFolderOpened] = useState(false);

  useEffect(() => {
    let cancelled = false;
    configApi.getConfig()
      .then((data) => {
        if (cancelled) return;
        setConfig(data);
        setMode(data.mpv_anime4k_mode || 'off');
        setQuality(data.mpv_anime4k_quality || 'balanced');
        setPlayerMode(data.player_mode === 'external' ? 'external' : 'internal');
        setExternalPath(data.external_mpv_path || '');
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
      await configApi.patchConfig({
        mpv_anime4k_mode: mode,
        mpv_anime4k_quality: quality,
        player_mode: playerMode,
        external_mpv_path: externalPath.trim(),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setError(`保存失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const openConfigDir = async () => {
    setOpeningConfigDir(true);
    setError('');
    setConfigFolderOpened(false);
    try {
      await configApi.openMpvConfigDir();
      setConfigFolderOpened(true);
    } catch (err) {
      setError(`打开 MPV 配置文件夹失败：${(err as Error).message}`);
    } finally {
      setOpeningConfigDir(false);
    }
  };

  const chooseExternalPlayer = async () => {
    setError('');
    try {
      const picked = await pickFile(externalPath || undefined, '选择 MPV 可执行文件');
      if (picked) setExternalPath(picked);
    } catch (err) {
      setError(`选择 MPV 可执行文件失败：${(err as Error).message}`);
    }
  };

  return (
    <div className="player-tuning-page">
      <div className="player-tuning-header">
        <Button appearance="subtle" icon={<ArrowLeft size={16} />} onClick={() => goBack()}>返回</Button>
        <div>
          <span className="player-tuning-kicker">Anime4K 默认效果</span>
          <h2>播放器调节</h2>
        </div>
      </div>

      <section className="player-tuning-section">
        <h3>播放器状态</h3>
        <div className="player-tuning-status-row">
          <div className="player-tuning-status">
            {config ? (
              <span className="player-tuning-status-ok"><CheckCircle2 size={15} /> 配置已加载</span>
            ) : (
              <span className="player-tuning-status-warn"><TriangleAlert size={15} /> 配置不可用</span>
            )}
          </div>
          <Button appearance="secondary" icon={openingConfigDir ? <Spinner size="tiny" /> : <FolderOpen size={16} />} disabled={openingConfigDir} onClick={() => void openConfigDir()}>
            {openingConfigDir ? '正在打开…' : '打开 MPV 配置文件夹'}
          </Button>
        </div>
        <p className="player-tuning-note">
          此设置对之后开始播放的新视频生效；右键菜单中的调整只影响当前视频，不会改变这里的默认值。
        </p>
      </section>

      <section className="player-tuning-section">
        <h3>播放模式</h3>
        <div className="player-tuning-fields player-tuning-fields-wide">
          <div className="player-tuning-field">
            <label id="player-mode-label">使用哪个播放器</label>
            <Dropdown
              aria-labelledby="player-mode-label"
              inlinePopup
              value={PLAYER_MODE_OPTIONS.find((item) => item.value === playerMode)?.label || playerMode}
              selectedOptions={[playerMode]}
              onOptionSelect={(_, data) => {
                const value = data.optionValue as PlayerMode;
                if (value) setPlayerMode(value);
              }}
            >
              {PLAYER_MODE_OPTIONS.map((item) => (
                <Option key={item.value} value={item.value}>{item.label}</Option>
              ))}
            </Dropdown>
          </div>
          {playerMode === 'external' && (
            <div className="player-tuning-field">
              <label htmlFor="external-player-path">整合包 MPV 可执行文件</label>
              <div className="player-tuning-path-row">
                <input
                  id="external-player-path"
                  value={externalPath}
                  placeholder="例如：D:\03_ACGN\MPVlite\mpv\mpv.exe"
                  onChange={(event) => setExternalPath(event.target.value)}
                />
                <Button appearance="secondary" icon={<FolderOpen size={16} />} onClick={() => void chooseExternalPlayer()}>选择</Button>
              </div>
            </div>
          )}
        </div>
        <p className="player-tuning-note">
          {playerMode === 'internal'
            ? '使用 KumiPlayer 自带的干净 MPV 与自有播放配置，不需要你准备任何东西。'
            : '使用你自己的 MPV 整合包。KumiPlayer 不会修改该目录中的配置或脚本，只在自己播放时追加必要的会话参数（进度记录、标题与受控播放列表）；退出本应用后，该整合包仍然是原来的样子。'}
        </p>
      </section>

      <section className="player-tuning-section">
        <h3>Anime4K 默认效果</h3>
        {loading ? (
          <div className="player-tuning-loading"><Spinner size="tiny" /> 正在读取…</div>
        ) : (
          <div className="player-tuning-fields">
            <div className="player-tuning-field">
              <label id="anime4k-mode-label">模式</label>
              <Dropdown
                aria-labelledby="anime4k-mode-label"
                inlinePopup
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
              <label id="anime4k-quality-label">质量</label>
              <Dropdown
                aria-labelledby="anime4k-quality-label"
                inlinePopup
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
      {configFolderOpened && <div className="player-tuning-saved"><CheckCircle2 size={15} /> 已打开 MPV 配置文件夹</div>}

      <div className="player-tuning-actions">
        <Button appearance="primary" icon={saving ? <Spinner size="tiny" /> : <CheckCircle2 size={15} />} disabled={saving || loading} onClick={() => void save()}>
          保存默认设置
        </Button>
      </div>
    </div>
  );
}
