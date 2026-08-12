type LoadingStateProps = {
  label?: string;
  detail?: string;
};

/** 页面级短暂加载提示；不显示虚假的进度百分比。 */
export default function LoadingState({ label = '正在准备内容', detail = '请稍候' }: LoadingStateProps) {
  return (
    <div className="page-loading-wrap" role="status" aria-live="polite" aria-label={`${label}…`}>
      <div className="page-loading-orbit" aria-hidden="true">
        <span className="loader-reel loader-reel-left" />
        <span className="loader-beam" />
        <span className="loader-reel loader-reel-right" />
      </div>
      <div className="page-loading-copy">
        <strong>{label}<span aria-hidden="true">…</span></strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}
