import { Component, type ErrorInfo, type ReactNode } from 'react';
import RecoveryView from './RecoveryView';

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  error: Error | null;
  componentStack: string;
}

export default class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null, componentStack: '' };

  static getDerivedStateFromError(error: Error): Partial<AppErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[KumiPlayer] React render failure', error, info.componentStack);
    this.setState({ componentStack: info.componentStack ?? '' });
  }

  render() {
    const { error, componentStack } = this.state;
    if (error) {
      return (
        <RecoveryView
          title="界面运行异常"
          message="当前页面无法继续显示。媒体文件和媒体库数据没有被修改。"
          detail={[error.stack || error.message, componentStack].filter(Boolean).join('\n')}
        />
      );
    }
    return this.props.children;
  }
}
