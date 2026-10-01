// KumiPlayer 播放器调节页面：Anime4K 永久默认效果设置
//
// 与右键菜单的关系：
// - 本页面保存的是“之后开始播放的新视频”的永久默认值；
// - 右键菜单只临时改变当前视频，下一视频恢复本页面设置的永久默认。

import { useEffect, useState } from 'react';
import { Button, Input, Radio, RadioGroup, Select, Spinner } from '@fluentui/react-components';
import { ArrowLeft, CheckCircle2, FolderOpen, TriangleAlert } from 'lucide-react';
import { configApi, type PublicConfig, type MpvValidationResult } from '../api/config';
import { pickFile } from '../platform/folderPicker';
import { useUiStore } from '../stores/ui';
import '../styles/player-tuning.css';

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
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [testing, setTesting] = useState(false);
  const [validation, setValidation] = useState<MpvValidationResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    configApi.getConfig()
      .then((data) => {
        if (cancelled) return;
        setConfig(data);
        setMode(data.mpv_anime4k_mode || 'off');
        setQuality(data.mpv_anime4k_quality || 'balanced');
        setPlayerMode(data.player_mode === 'external' ? 'external' : 'internal');
        setExternalPath(data.external_mpv_path || (data.player_mode === 'external' ? data.mpv_path : '') || '');
      })
      .catch((err: Error) => {
        if (!cancelled) setError(`读取配置失败：${err.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [loadAttempt]);

  const busy = loading || saving || testing || openingConfigDir;
  const selectionChanged = !!config && (
    playerMode !== (config.player_mode === 'external' ? 'external' : 'internal')
    || (playerMode === 'external' && externalPath.trim() !== (config.external_mpv_path || config.mpv_path || ''))
  );
  const clearFeedback = () => {
    setSaved(false);
    setConfigFolderOpened(false);
    setValidation(null);
    setError('');
  };

  const save = async () => {
    if (!config || busy || (playerMode === 'external' && !externalPath.trim())) return;
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      const patch: Partial<PublicConfig> = {
        player_mode: playerMode,
        external_mpv_path: externalPath.trim(),
        ...(playerMode === 'internal' ? { mpv_anime4k_mode: mode, mpv_anime4k_quality: quality } : {}),
      };
      const updated = await configApi.patchConfig(patch);
      setConfig({ ...config, ...patch, ...updated });
      setSaved(true);
    } catch (err) {
      setError(`保存失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const openConfigDir = async () => {
    if (!config || busy || selectionChanged) return;
    setOpeningConfigDir(true);
    setError('');
    setConfigFolderOpened(false);
    try {
      await configApi.openMpvConfigDir(playerMode);
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
      if (picked) {
        clearFeedback();
        setExternalPath(picked);
      }
    } catch (err) {
      setError(`选择 MPV 可执行文件失败：${(err as Error).message}`);
    }
  };

  const testPlayer = async () => {
    if (!config || busy || (playerMode === 'external' && !externalPath.trim())) return;
    setTesting(true);
    clearFeedback();
    try {
      const result = playerMode === 'external'
        ? await configApi.testMpv(externalPath.trim(), 'external')
        : await configApi.testMpv();
      setValidation(result);
      if (result.ok && result.executable_path && playerMode === 'external') setExternalPath(result.executable_path);
    } catch (err) {
      setError(`检测失败：${(err as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="player-tuning-page player-tuning-page--compact">
      <div className="player-tuning-header">
        <Button appearance="subtle" icon={<ArrowLeft size={16} />} onClick={() => goBack()}>返回</Button>
        <div>
          <span className="player-tuning-kicker">播放设置</span>
          <h2>播放器调节</h2>
        </div>
      </div>

      <p className="player-tuning-note">选择播放器，再调整它的默认效果。保存后对新开始播放的视频生效。</p>
      {loading && <div className="player-tuning-loading" role="status"><Spinner size="tiny" /> 正在读取配置…</div>}
      {!loading && !config && <Button onClick={() => setLoadAttempt((value) => value + 1)}>重新读取</Button>}
      {config && !loading && <>
      <section className="player-tuning-section">
        <h3 id="player-mode-label">使用哪个播放器</h3>
        <RadioGroup aria-labelledby="player-mode-label" value={playerMode} disabled={busy}
          onChange={(_, data) => { clearFeedback(); setPlayerMode(data.value as PlayerMode); }}>
          {PLAYER_MODE_OPTIONS.map((item) => <Radio key={item.value} value={item.value} label={item.label} />)}
        </RadioGroup>
        <p className="player-tuning-note">
          {playerMode === 'internal'
            ? '使用 KumiPlayer 自带的 MPV 与播放配置，不需要你准备任何东西。'
            : '使用你自己的 MPV 整合包，KumiPlayer 不会修改该目录中的配置或脚本。'}
        </p>
          {playerMode === 'external' && (
            <div className="player-tuning-field">
              <label htmlFor="external-player-path">整合包 MPV 可执行文件</label>
              <div className="player-tuning-path-row">
                <Input
                  id="external-player-path"
                  value={externalPath}
                  disabled={busy}
                  placeholder="粘贴 mpv.exe 路径或整合包目录"
                  onChange={(_, data) => { clearFeedback(); setExternalPath(data.value); }}
                />
                <Button appearance="secondary" disabled={busy} icon={<FolderOpen size={16} />} onClick={() => void chooseExternalPlayer()}>选择</Button>
              </div>
              <p className="player-tuning-note">填写目录后可点击检测，自动定位其中的 mpv.exe。</p>
            </div>
          )}
        <div className="player-tuning-tools">
          <Button disabled={busy || (playerMode === 'external' && !externalPath.trim())} icon={testing ? <Spinner size="tiny" /> : undefined} onClick={() => void testPlayer()}>{testing ? '正在检测…' : '检测播放器'}</Button>
          <Button appearance="subtle" icon={<FolderOpen size={16} />} disabled={busy || selectionChanged} onClick={() => void openConfigDir()}>
            {openingConfigDir ? '正在打开…' : '打开 MPV 配置文件夹'}
          </Button>
        </div>
        {selectionChanged && <p className="player-tuning-note">播放器或路径已更改，请先保存再打开对应配置文件夹。</p>}
        {validation && <div className={validation.ok ? 'player-tuning-saved' : 'player-tuning-error'} role={validation.ok ? 'status' : 'alert'}>
          {validation.ok ? <CheckCircle2 size={15} /> : <TriangleAlert size={15} />}
          <span>{validation.message}{validation.version && !validation.message.includes(validation.version) ? ` · ${validation.version}` : ''}</span>
        </div>}
      </section>

      {playerMode === 'internal' ? (
      <section className="player-tuning-section">
        <h3>Anime4K 默认效果</h3>
        <p className="player-tuning-note">默认关闭。开启后可在播放中比较效果，右键菜单的调整仅影响当前视频。</p>
          <div className="player-tuning-fields">
            <div className="player-tuning-field">
              <label htmlFor="anime4k-mode">模式</label>
              <Select id="anime4k-mode" value={mode} disabled={busy} onChange={(_, data) => { clearFeedback(); setMode(data.value as Anime4kMode); }}>
                {MODE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </Select>
            </div>
            <div className="player-tuning-field">
              <label htmlFor="anime4k-quality">质量</label>
              <Select id="anime4k-quality" value={quality} disabled={busy || mode === 'off'} onChange={(_, data) => { clearFeedback(); setQuality(data.value as Anime4kQuality); }}>
                {QUALITY_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </Select>
            </div>
          </div>
        <details className="player-tuning-help-details">
        <summary>如何选择效果与质量</summary>
        <ul className="player-tuning-help">
          <li>“模式”选择的是<strong>算法族</strong>（Restore / Upscale_Denoise / 二次增强），不是性能档位。Mode C 使用 Upscale_Denoise 链，并不是最轻量的选项。</li>
          <li>“质量”是<strong>成本档位</strong>：它决定链中 CNN 模型的大小（轻量 M/S、均衡 L/M、高质量 VL/M）。降低质量能减轻 GPU 负担，但 4K 源片仍可能超出帧预算。</li>
          <li>当前默认是“关闭”，需要时再手动开启；播放中右键菜单可临时切换，只影响当前视频，不改这里的默认值。</li>
          <li>不确定时从 Mode A + 均衡质量开始，播放中比较 Mode B、Mode C，保留观感最好的一个。</li>
          <li>增强模式（A+A / B+B / C+A）的链更长、渲染开销更高，建议在显示放大至少 2× 时使用，否则可能过锐或劣化。</li>
          <li>播放 4K 视频出现掉帧时，先关闭 Anime4K 确认是否恢复流畅；仅降到“轻量”也可能不足。较低分辨率视频可再试轻量档或更短模式。“已应用”只代表着色器链已下发并被 MPV 接受，不代表当前 GPU 帧预算已经足够。</li>
        </ul>
        </details>
      </section>
      ) : <section className="player-tuning-section">
        <h3>画面效果与播放联动</h3>
        <p className="player-tuning-note">Anime4K 由整合包管理，请使用整合包自己的菜单或快捷键调节。</p>
        <p className="player-tuning-note">KumiPlayer 记录播放进度并支持续播，不追加内置 Anime4K、快捷键或截图脚本。</p>
      </section>}
      </>}

      {error && <div className="player-tuning-error" role="alert"><TriangleAlert size={15} /> {error}</div>}
      {saved && <div className="player-tuning-saved" role="status"><CheckCircle2 size={15} /> 已保存，之后播放的新视频将使用新默认值</div>}
      {configFolderOpened && <div className="player-tuning-saved" role="status"><CheckCircle2 size={15} /> 已打开 MPV 配置文件夹</div>}

      <div className="player-tuning-actions">
        <Button appearance="primary" icon={saving ? <Spinner size="tiny" /> : <CheckCircle2 size={15} />} disabled={busy || !config || (playerMode === 'external' && !externalPath.trim())} onClick={() => void save()}>
          保存默认设置
        </Button>
      </div>
    </div>
  );
}
