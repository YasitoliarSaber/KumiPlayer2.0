import '../../styles/first-run-refinement.css';

export default function StartupSplash() {
  return (
    <main className="startup-splash" role="status" aria-label="正在启动 KumiPlayer">
      <div className="startup-splash-mark" aria-hidden="true">
        <img src="/brand/kumiplayer-app-icon.svg" alt="" />
      </div>
      <p className="startup-splash-caption">正在打开媒体库…</p>
    </main>
  );
}
