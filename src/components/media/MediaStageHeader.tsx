import type { ReactNode } from 'react';

type Props = {
  icon: ReactNode;
  eyebrow?: string;
  title: string;
  description: string;
  status?: ReactNode;
  action?: ReactNode;
};

export default function MediaStageHeader({
  icon,
  eyebrow,
  title,
  description,
  status,
  action,
}: Props) {
  return (
    <header className="media-stage-header">
      <div className="media-stage-heading">
        <span className="media-stage-icon" aria-hidden="true">{icon}</span>
        <div>
          {eyebrow && <span className="media-stage-eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      {(status || action) && <div className="media-stage-header-actions">{status}{action}</div>}
    </header>
  );
}
