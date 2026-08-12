import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, ChevronLeft, ChevronRight, Database, ExternalLink, FolderOpen, KeyRound, Play, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import { configApi, type MpvRuntimeStatus, type PublicConfig, type SetupCompletePayload } from '../api/config';
import { BANGUMI_ACCESS_TOKEN_URL, getTmdbCredentialError, TMDB_API_SETTINGS_URL } from '../config/credentials';
import { pickFolder } from '../platform/folderPicker';

interface FirstRunSetupProps {
  initialConfig: PublicConfig;
  onComplete: (config: PublicConfig) => void;
  mode?: 'first-run' | 'reconfigure';
  onCancel?: () => void;
}

const steps = ['欢迎', '播放器', '媒体与镜像', '验证完成'];

export default function FirstRunSetup({ initialConfig, onComplete, mode = 'first-run', onCancel }: FirstRunSetupProps) {
  const isReconfigure = mode === 'reconfigure';
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<SetupCompletePayload>({
    mirror_dir: initialConfig.mirror_dir || '',
    pan115_root: initialConfig.pan115_root || '',
    baidu_root: initialConfig.baidu_root || '',
    local_root: initialConfig.local_root || '',
    directory_tree_dir: initialConfig.directory_tree_dir || '',
    tmdb_bearer_token: '',
    bangumi_access_token: '',
  });
  const [mpvStatus, setMpvStatus] = useState<MpvRuntimeStatus | null>(null);
  const [checkingMpv, setCheckingMpv] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [error, setError] = useState('');

  const sourceCount = useMemo(
    () => [form.pan115_root, form.baidu_root, form.local_root].filter((value) => value?.trim()).length,
    [form.pan115_root, form.baidu_root, form.local_root],
  );

  const checkMpv = async () => {
    setCheckingMpv(true);
    setError('');
    try {
      setMpvStatus(await configApi.getMpvRuntime());
    } catch (reason) {
      setMpvStatus(null);
      setError(reason instanceof Error ? reason.message : '内置播放器检测失败');
    } finally {
      setCheckingMpv(false);
    }
  };

  // 首次进入播放器步骤时自动检查一次内置 MPV
  useEffect(() => {
    if (step === 1 && !mpvStatus) {
      void checkMpv();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  const update = (key: keyof SetupCompletePayload, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
    setError('');
  };

  const chooseDirectory = async (key: keyof SetupCompletePayload, title: string) => {
    const selected = await pickFolder(String(form[key] || ''), title);
    if (selected) update(key, selected);
  };

  const mpvReady = Boolean(mpvStatus?.available && mpvStatus.manifest_valid && mpvStatus.files_valid);

  const goNext = () => {
    if (step === 1 && !mpvReady) {
      setError('请先确认内置播放器可用');
      return;
    }
    if (step === 2) {
      if (!form.mirror_dir?.trim()) {
        setError('请选择镜像目录');
        return;
      }
      if (sourceCount === 0) {
        setError('请至少配置一个媒体来源');
        return;
      }
    }
    setError('');
    setStep((current) => Math.min(steps.length - 1, current + 1));
  };

  const finish = async () => {
    const tmdbCredentialError = getTmdbCredentialError(form.tmdb_bearer_token || '');
    if (tmdbCredentialError) {
      setError(tmdbCredentialError);
      return;
    }
    setFinishing(true);
    setError('');
    try {
      onComplete(await configApi.completeSetup(form));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '初始化失败，请检查配置');
    } finally {
      setFinishing(false);
    }
  };

  return (
    <main className="first-run-shell">
      <section className="first-run-window" aria-label={isReconfigure ? 'KumiPlayer 初始设置引导' : 'KumiPlayer 首次启动引导'}>
        <header className="first-run-header">
          <div className="first-run-brand"><img src="/brand/kumiplayer-app-icon.svg" alt="" /><strong>KumiPlayer</strong></div>
          <div className="first-run-header-actions">
            <span>{isReconfigure ? '重新配置初始设置' : '首次启动设置'}</span>
            {onCancel && <button type="button" onClick={onCancel}>退出引导</button>}
          </div>
        </header>

        <div className="first-run-layout">
          <nav className="first-run-steps" aria-label="设置步骤">
            {steps.map((label, index) => (
              <div key={label} className={`first-run-step ${index === step ? 'active' : ''} ${index < step ? 'done' : ''}`}>
                <span>{index < step ? <CheckCircle2 size={16} /> : index + 1}</span>
                <div><strong>{label}</strong><small>{index === step ? '正在设置' : index < step ? '已完成' : '稍后设置'}</small></div>
              </div>
            ))}
          </nav>

          <div className="first-run-content">
            {step === 0 && (
              <div className="first-run-panel first-run-welcome">
                <span className="first-run-hero-icon"><Sparkles size={28} /></span>
                <p className="first-run-eyebrow">{isReconfigure ? '重新检查 KumiPlayer 配置' : '欢迎使用 KumiPlayer'}</p>
                <h1>{isReconfigure ? '重新配置基础环境' : '先完成几项基础设置'}</h1>
                <p>真实视频始终保留在你的网盘或本地目录中。KumiPlayer 只创建可管理的镜像、元数据和播放记录。</p>
                <div className="first-run-principles">
                  <article><ShieldCheck size={20} /><div><strong>安装版已备好应用环境</strong><span>支持 Windows 10 / 11；后端、内置播放器和功能插件随软件安装，缺少 WebView2 时安装器会联网补齐。</span></div></article>
                  <article><ShieldCheck size={20} /><div><strong>不移动真实媒体</strong><span>路径检查只读取文件和目录信息。</span></div></article>
                  <article><Play size={20} /><div><strong>内置干净 MPV</strong><span>KumiPlayer 使用自己维护的播放器与配置，不读取或改写你的全局 MPV。</span></div></article>
                  <article><Database size={20} /><div><strong>配置可以随时修改</strong><span>完成后仍可在设置与媒体管理中调整。</span></div></article>
                </div>
              </div>
            )}

            {step === 1 && (
              <div className="first-run-panel">
                <p className="first-run-eyebrow">播放器</p>
                <h1>内置播放器</h1>
                <p>KumiPlayer 使用自带的内置 MPV 播放器，只加载自己的配置和脚本，不会读取或改写你的全局 MPV 配置。</p>
                {!mpvStatus && checkingMpv && (
                  <div className="first-run-result" role="status">
                    <strong>正在检测内置播放器…</strong>
                  </div>
                )}
                {mpvStatus && (
                  <div className={`first-run-result ${mpvReady ? 'success' : 'error'}`}>
                    <strong>{mpvReady ? '内置播放器已就绪' : '播放器运行时缺失或损坏'}</strong>
                    <span>{mpvStatus.message}</span>
                    <small>
                      {mpvStatus.version || '版本未知'}
                      {mpvStatus.architecture ? ` · ${mpvStatus.architecture}` : ''}
                      {mpvStatus.distribution_status === 'development-only' ? ' · 本地开发状态' : ''}
                    </small>
                  </div>
                )}
                <button className="first-run-secondary" onClick={checkMpv} disabled={checkingMpv}>{checkingMpv ? '正在检测…' : <><RefreshCw size={14} />重新检测</>}</button>
                {!mpvReady && <p className="first-run-help-link">请检查应用安装目录中的播放器文件是否完整。</p>}
              </div>
            )}

            {step === 2 && (
              <div className="first-run-panel">
                <p className="first-run-eyebrow">存储</p>
                <h1>镜像目录与媒体来源</h1>
                <p>镜像目录保存 .strm 和刮削元数据。下面只需配置你实际使用的来源，其他来源可以留空。</p>
                <SetupPathField label="镜像目录（必填）" value={form.mirror_dir} placeholder="选择用于保存镜像的文件夹" onChange={(value) => update('mirror_dir', value)} onPick={() => chooseDirectory('mirror_dir', '选择镜像目录')} />
                <div className="first-run-source-grid">
                  <SetupPathField label="115 网盘挂载根目录" value={form.pan115_root || ''} placeholder="例如 H:\\115open" onChange={(value) => update('pan115_root', value)} onPick={() => chooseDirectory('pan115_root', '选择 115 网盘挂载根目录')} compact />
                  <SetupPathField label="百度网盘挂载位置" value={form.baidu_root || ''} placeholder="例如 H:\\百度网盘" onChange={(value) => update('baidu_root', value)} onPick={() => chooseDirectory('baidu_root', '选择百度网盘挂载位置')} compact />
                  <SetupPathField label="本地媒体根目录" value={form.local_root || ''} placeholder="你的本地影视目录" onChange={(value) => update('local_root', value)} onPick={() => chooseDirectory('local_root', '选择本地媒体根目录')} compact />
                  <SetupPathField label="目录树文件目录（可选）" value={form.directory_tree_dir || ''} placeholder="保存网盘目录树 TXT 的文件夹" onChange={(value) => update('directory_tree_dir', value)} onPick={() => chooseDirectory('directory_tree_dir', '选择目录树文件目录')} compact />
                </div>
                {form.baidu_root?.trim() && (
                  <div className="first-run-path-example">
                    <strong>百度目录树会自动补齐作用域</strong>
                    <code>{form.baidu_root}\01动画\已完结\作品\Season 1\S01E01.mkv</code>
                    <span>导入 `01动画_文件目录.txt` 时自动识别 `01动画`，无需逐次设置。</span>
                  </div>
                )}
              </div>
            )}

            {step === 3 && (
              <div className="first-run-panel">
                <p className="first-run-eyebrow">最后检查</p>
                <h1>验证并完成</h1>
                <p>后端会重新验证内置播放器、镜像目录和媒体来源。任何一项失败都不会写入半完成配置。</p>
                <div className="first-run-summary">
                  <SummaryRow label="内置播放器" value={mpvStatus?.version || (mpvReady ? '已就绪' : '未就绪')} ok={mpvReady} />
                  <SummaryRow label="镜像目录" value={form.mirror_dir} ok={Boolean(form.mirror_dir?.trim())} />
                  <SummaryRow label="媒体来源" value={`已配置 ${sourceCount} 个来源`} ok={sourceCount > 0} />
                </div>
                <div className="first-run-credential-grid">
                  <article className="first-run-credential-card">
                    <div className="first-run-credential-title"><KeyRound size={18} /><div><strong>TMDB API 读取访问令牌</strong><small>可选 · 用于刮削元数据与图片</small></div></div>
                    <p>复制页面上方较长的“API 读取访问令牌”，不是下方的 API 密钥。</p>
                    <a href={TMDB_API_SETTINGS_URL} target="_blank" rel="noreferrer">前往 TMDB API 设置 <ExternalLink size={14} /></a>
                    <input aria-label="TMDB API 读取访问令牌" type="password" value={form.tmdb_bearer_token || ''} onChange={(event) => update('tmdb_bearer_token', event.target.value)} placeholder="粘贴 API 读取访问令牌" autoComplete="off" />
                  </article>
                  <article className="first-run-credential-card">
                    <div className="first-run-credential-title"><KeyRound size={18} /><div><strong>Bangumi 个人访问令牌</strong><small>可选 · 用于同步收藏与已看集数</small></div></div>
                    <p>在 Bangumi 官方页面创建 Access Token，再复制到这里。</p>
                    <a href={BANGUMI_ACCESS_TOKEN_URL} target="_blank" rel="noreferrer">前往 Bangumi 令牌页面 <ExternalLink size={14} /></a>
                    <input aria-label="Bangumi 个人访问令牌" type="password" value={form.bangumi_access_token || ''} onChange={(event) => update('bangumi_access_token', event.target.value)} placeholder="粘贴个人 Access Token" autoComplete="off" />
                  </article>
                </div>
                <p className="first-run-credential-security">凭据只保存在本机配置中；填写后会在完成设置前验证，留空也可以稍后配置。</p>
              </div>
            )}

            {error && <div className="first-run-error" role="alert">{error}</div>}
            <footer className="first-run-footer">
              <button className="first-run-back" onClick={() => setStep((current) => Math.max(0, current - 1))} disabled={step === 0 || finishing}><ChevronLeft size={17} />返回</button>
              {step < steps.length - 1 ? (
                <button
                  className="first-run-primary"
                  onClick={goNext}
                  disabled={(step === 1 && !mpvReady) || (step === 2 && (!form.mirror_dir?.trim() || sourceCount === 0))}
                >继续<ChevronRight size={17} /></button>
              ) : (
                <button className="first-run-primary" onClick={finish} disabled={finishing}>{finishing ? '正在验证…' : '验证并完成'}<CheckCircle2 size={17} /></button>
              )}
            </footer>
          </div>
        </div>
      </section>
    </main>
  );
}

function SetupPathField({ label, value, placeholder, onChange, onPick, actionLabel = '选择文件夹', compact = false }: { label: string; value: string; placeholder: string; onChange: (value: string) => void; onPick: () => void; actionLabel?: string; compact?: boolean }) {
  return (
    <label className={`first-run-path-field ${compact ? 'compact' : ''}`}>
      <span>{label}</span>
      <div><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} /><button type="button" onClick={onPick}><FolderOpen size={16} />{actionLabel}</button></div>
    </label>
  );
}

function SummaryRow({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return <div><span className={ok ? 'ok' : ''}>{ok ? <CheckCircle2 size={17} /> : '—'}</span><strong>{label}</strong><p>{value || '未配置'}</p></div>;
}
