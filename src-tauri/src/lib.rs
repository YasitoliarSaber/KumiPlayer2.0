use std::process::Command;

use tauri::{Manager, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

mod runtime_identity;
pub use runtime_identity::{backend_is_compatible, runtime_id_for};
mod api_session;
mod backend_job;
mod supervisor;
pub use supervisor::{
    data_dir_for, existing_backend_error, health_matches, protect_backend, restart_backend_with,
    start_backend, stop_backend, wait_for_health, BackendProcess, BackendSupervisor, HealthTimings,
    ManagedBackend, RestartTimings, RuntimeContext, SystemSupervisor, BACKEND_HOST, BACKEND_PORT,
    PORT_RELEASE_TIMEOUT, RESTART_HEALTH_TIMINGS, STARTUP_HEALTH_TIMINGS,
};

struct ApiSession(String);

#[tauri::command]
fn get_api_token(session: State<'_, ApiSession>) -> String {
    session.0.clone()
}

#[tauri::command]
fn restart_backend(
    process: State<'_, BackendProcess>,
    context: State<'_, RuntimeContext>,
) -> Result<(), String> {
    restart_backend_with(process.inner(), context.inner(), &SystemSupervisor)
}

#[tauri::command]
fn open_log_directory(context: State<'_, RuntimeContext>) -> Result<(), String> {
    let log_dir = data_dir_for(context.inner())?.join("logs").join("error");
    std::fs::create_dir_all(&log_dir).map_err(|error| format!("无法创建日志目录：{error}"))?;
    #[cfg(windows)]
    let mut command = Command::new("explorer.exe");
    #[cfg(not(windows))]
    let mut command = Command::new("xdg-open");
    command
        .arg(&log_dir)
        .spawn()
        .map_err(|error| format!("无法打开日志目录：{error}"))?;
    Ok(())
}

#[tauri::command]
fn save_diagnostics(content: String, context: State<'_, RuntimeContext>) -> Result<String, String> {
    if content.len() > 100_000 {
        return Err("诊断内容超过 100 KB 限制".to_string());
    }
    let diagnostics_dir = data_dir_for(context.inner())?
        .join("logs")
        .join("diagnostics");
    std::fs::create_dir_all(&diagnostics_dir)
        .map_err(|error| format!("无法创建诊断目录：{error}"))?;
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let path = diagnostics_dir.join(format!("kumiplayer-diagnostics-{timestamp}.txt"));
    std::fs::write(&path, content.as_bytes())
        .map_err(|error| format!("无法写入诊断文件：{error}"))?;
    Ok(path.to_string_lossy().into_owned())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 完整发行包优先使用内置后端；源码目录保留 Python 开发后备链路。
    let runtime_context = RuntimeContext::discover();
    let (backend, startup_error) = match start_backend(&runtime_context) {
        Ok(child) => (child, None),
        Err(error) => (None, Some(error)),
    };
    let expected_instance = backend
        .as_ref()
        .map(|_| runtime_context.instance_id().to_string());
    let health_context = runtime_context.clone();
    let api_token = runtime_context.api_token().to_string();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendProcess::new(backend))
        .manage(ApiSession(api_token))
        .manage(runtime_context.clone())
        .invoke_handler(tauri::generate_handler![
            get_api_token,
            restart_backend,
            open_log_directory,
            save_diagnostics
        ])
        .setup(move |app| {
            if let Some(error) = startup_error.as_ref() {
                app.dialog()
                    .message(format!(
                        "KumiPlayer 无法启动运行环境。\n\n{error}\n\n普通用户不需要安装 Python，请重新下载完整安装包；源码开发者可检查项目 backend/ 目录。"
                    ))
                    .kind(MessageDialogKind::Error)
                    .title("KumiPlayer 启动失败")
                    .blocking_show();
                app.handle().exit(1);
                return Ok(());
            }
            let app_handle = app.handle().clone();

            // 启动期健康检查：等满 STARTUP_HEALTH_TIMINGS 仍不就绪才判定失败。
            std::thread::spawn(move || {
                if wait_for_health(STARTUP_HEALTH_TIMINGS, || {
                    health_matches(&health_context, expected_instance.as_deref())
                }) {
                    return;
                }
                eprintln!("[KumiPlayer] Backend failed to start within 30s");
                app_handle
                    .dialog()
                    .message(format!(
                        "内置后端在 30 秒内未能就绪。请重新启动 KumiPlayer；若问题持续存在，请重新下载完整安装包，并检查 {BACKEND_PORT} 端口是否可用。"
                    ))
                    .kind(MessageDialogKind::Error)
                    .title("KumiPlayer 后端启动失败")
                    .blocking_show();
                app_handle.exit(1);
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<BackendProcess>();
                let _ = stop_backend(state.inner());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
