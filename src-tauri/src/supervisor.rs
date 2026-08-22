//! 本地后端的发现、启动、探活与重启。
//!
//! 这一层不依赖 tauri：`lib.rs` 只负责把它接到 Tauri 命令与窗口事件上。
//! 拆开的直接原因是可测性——集成测试可以内联本模块验证启动拒绝、健康检查超时和
//! 重启失败恢复，而不必链接桌面运行时。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;

use crate::api_session::generate_api_token;
use crate::backend_job::KillOnCloseJob;
use crate::runtime_identity::{backend_is_compatible, runtime_id_for};

pub const BACKEND_HOST: &str = "127.0.0.1";
pub const BACKEND_PORT: u16 = 37821;

/// 健康检查的等待参数：总期限与轮询间隔。
///
/// 之所以把期限做成参数而不是内联字面量，是为了让「必须等满期限才放弃」这条行为
/// 能在测试里用毫秒级期限验证；生产路径继续使用下面的 30 秒常量。
#[derive(Clone, Copy, Debug)]
pub struct HealthTimings {
    pub total: Duration,
    pub interval: Duration,
}

impl HealthTimings {
    pub const fn new(total: Duration, interval: Duration) -> Self {
        Self { total, interval }
    }
}

/// 启动期等待后端就绪的参数。
pub const STARTUP_HEALTH_TIMINGS: HealthTimings =
    HealthTimings::new(Duration::from_secs(30), Duration::from_millis(500));

/// 重启期等待后端恢复的参数。
pub const RESTART_HEALTH_TIMINGS: HealthTimings =
    HealthTimings::new(Duration::from_secs(30), Duration::from_millis(300));

/// 重启时等待旧后端释放端口的上限。
pub const PORT_RELEASE_TIMEOUT: Duration = Duration::from_secs(3);

/// 一次重启用到的全部等待参数。
#[derive(Clone, Copy, Debug)]
pub struct RestartTimings {
    pub port_release: Duration,
    pub health: HealthTimings,
}

impl Default for RestartTimings {
    fn default() -> Self {
        Self {
            port_release: PORT_RELEASE_TIMEOUT,
            health: RESTART_HEALTH_TIMINGS,
        }
    }
}

/// 重启串行锁：显式串行化整个重启序列（停旧→等端口→起新→健康等待），
/// 不再依赖「同步命令恰好在主线程内联执行」的隐含事实；并发重启会互相
/// 杀掉对方刚启动的后端。
static RESTART_LOCK: Mutex<()> = Mutex::new(());

/// 桌面壳当前托管的后端进程（可能没有）。
pub struct BackendProcess(Mutex<Option<ManagedBackend>>);

impl BackendProcess {
    pub fn new(backend: Option<ManagedBackend>) -> Self {
        Self(Mutex::new(backend))
    }
}

/// 本次桌面会话的运行身份。
#[derive(Clone, Debug)]
pub struct RuntimeContext {
    kind: String,
    install_root: PathBuf,
    runtime_id: String,
    instance_id: String,
    api_token: String,
}

#[derive(Debug, Deserialize)]
struct BackendIdentity {
    app: String,
    #[serde(default)]
    runtime_kind: String,
    #[serde(default)]
    runtime_id: String,
    #[serde(default)]
    instance_id: String,
}

/// 加入了 kill-on-close 作业对象的后端进程。
pub struct ManagedBackend {
    child: Child,
    _job: KillOnCloseJob,
}

impl ManagedBackend {
    /// 终止后端并回收进程；作业对象在本结构被丢弃时连带清理子孙进程。
    pub fn shutdown(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Connect to the backend and read its desktop runtime identity.
fn health_identity() -> Option<BackendIdentity> {
    let request = b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if let Ok(mut stream) = TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT)),
        Duration::from_secs(1),
    ) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
        if stream.write(request).is_ok() {
            let mut buf = Vec::with_capacity(512);
            if stream.read_to_end(&mut buf).is_ok() {
                let response = String::from_utf8_lossy(&buf);
                let status_ok =
                    response.starts_with("HTTP/1.0 200") || response.starts_with("HTTP/1.1 200");
                if status_ok {
                    let body = response.split("\r\n\r\n").nth(1)?;
                    let identity: BackendIdentity = serde_json::from_str(body).ok()?;
                    if identity.app == "KumiPlayer" {
                        return Some(identity);
                    }
                }
            }
        }
    }
    None
}

pub fn health_matches(context: &RuntimeContext, expected_instance: Option<&str>) -> bool {
    let Some(identity) = health_identity() else {
        return false;
    };
    if !backend_is_compatible(
        &context.kind,
        &context.runtime_id,
        &identity.runtime_kind,
        &identity.runtime_id,
    ) {
        return false;
    }
    expected_instance
        .map(|expected| identity.instance_id == expected)
        .unwrap_or(true)
}

fn backend_port_in_use() -> bool {
    TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT)),
        Duration::from_millis(500),
    )
    .is_ok()
}

/// Find the project root (parent of tauri-shell/).
fn project_root() -> std::path::PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| {
            let mut dir = p.parent()?;
            // Walk up until we find the project root markers
            loop {
                if dir.join("backend").join("app").join("main.py").exists()
                    || dir.join("scripts").join("launcher_backend.py").exists()
                {
                    return Some(dir.to_path_buf());
                }
                dir = dir.parent()?;
            }
        })
        .unwrap_or_else(|| {
            // Fallback: assume we're running from the project root
            std::env::current_dir().unwrap_or_default()
        })
}

fn bundled_backend_path() -> Option<std::path::PathBuf> {
    let executable_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let candidate = executable_dir
        .join("runtime")
        .join("backend")
        .join("KumiPlayerBackend.exe");
    candidate.is_file().then_some(candidate)
}

impl RuntimeContext {
    /// 由运行模式与安装根组装身份；`runtime_id` 始终由两者推导，不接受外部传入。
    pub fn new(
        kind: impl Into<String>,
        install_root: PathBuf,
        instance_id: impl Into<String>,
        api_token: impl Into<String>,
    ) -> Self {
        let kind = kind.into();
        let runtime_id = runtime_id_for(&kind, &install_root);
        Self {
            kind,
            install_root,
            runtime_id,
            instance_id: instance_id.into(),
            api_token: api_token.into(),
        }
    }

    pub fn kind(&self) -> &str {
        &self.kind
    }

    pub fn runtime_id(&self) -> &str {
        &self.runtime_id
    }

    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    pub fn api_token(&self) -> &str {
        &self.api_token
    }

    /// discover 失败（如系统熵源不可用）时的占位身份：仅用于把启动错误
    /// 送入既有错误对话框流程，随后进程退出，不启动任何后端。
    pub fn fallback_for_error_dialog() -> Self {
        Self::new("source", project_root(), "", "")
    }

    pub fn discover() -> Result<Self, String> {
        let bundled = bundled_backend_path().is_some();
        let kind = if bundled { "bundled" } else { "source" }.to_string();
        let install_root = if bundled {
            std::env::current_exe()
                .ok()
                .and_then(|path| path.parent().map(Path::to_path_buf))
                .unwrap_or_else(project_root)
        } else {
            project_root()
        };
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let instance_id = format!("{}-{nonce:x}", std::process::id());
        let api_token = generate_api_token()?;
        Ok(Self::new(kind, install_root, instance_id, api_token))
    }

    fn apply_environment(&self, command: &mut Command, runtime_dir: &Path) {
        command
            .env("KUMIPLAYER_HOST", BACKEND_HOST)
            .env("KUMIPLAYER_PORT", BACKEND_PORT.to_string())
            .env("KUMIPLAYER_RUNTIME_DIR", runtime_dir)
            .env("KUMIPLAYER_RUNTIME_KIND", &self.kind)
            .env("KUMIPLAYER_RUNTIME_ID", &self.runtime_id)
            .env("KUMIPLAYER_INSTANCE_ID", &self.instance_id)
            .env("KUMIPLAYER_API_TOKEN", &self.api_token)
            .env("KUMIPLAYER_PARENT_PID", std::process::id().to_string())
            .env("KUMIPLAYER_AUTO_SHUTDOWN_ON_HEARTBEAT_TIMEOUT", "1")
            .env("KUMIPLAYER_HEARTBEAT_TIMEOUT", "120");
        if self.kind == "bundled" {
            command.env("KUMIPLAYER_INSTALL_DIR", &self.install_root);
        } else {
            command.env_remove("KUMIPLAYER_INSTALL_DIR");
        }
    }
}

/// 反复探活直到后端就绪或等满期限；返回是否就绪。
///
/// 启动期与重启期共用同一段等待逻辑，避免两处循环各自漂移。
pub fn wait_for_health(timings: HealthTimings, mut probe: impl FnMut() -> bool) -> bool {
    let deadline = std::time::Instant::now() + timings.total;
    while std::time::Instant::now() < deadline {
        if probe() {
            return true;
        }
        std::thread::sleep(timings.interval);
    }
    false
}

/// 端口上已有 KumiPlayer 后端时，桌面壳给用户的拒绝理由。
///
/// 注意语义：身份一致代表「同一个安装已经在跑」，结果同样是拒绝启动而不是复用后端；
/// 身份不一致代表源码版与安装版抢同一个端口，必须明确点名对方，避免两者共用媒体库。
pub fn existing_backend_error(
    expected_kind: &str,
    expected_runtime_id: &str,
    actual_kind: &str,
    actual_runtime_id: &str,
) -> String {
    if backend_is_compatible(
        expected_kind,
        expected_runtime_id,
        actual_kind,
        actual_runtime_id,
    ) {
        return "当前 KumiPlayer 运行环境已经启动。请使用现有窗口，或完全退出后再重新打开。"
            .to_string();
    }
    format!(
        "端口 {BACKEND_PORT} 上已有另一个 KumiPlayer 运行环境（{}）。请先关闭它，避免源码版与安装版共用媒体库。",
        if actual_kind.is_empty() {
            "无法识别的旧版本"
        } else {
            actual_kind
        }
    )
}

/// 重启流程依赖的进程与探活能力。
///
/// 生产实现是 [`SystemSupervisor`]；抽成 trait 是为了让「启动失败」「始终不健康」
/// 这些恢复路径可以在不真起后端、不占用 37821 的前提下被测试覆盖。
pub trait BackendSupervisor {
    fn stop(&self, process: &BackendProcess) -> Result<(), String>;
    fn start(&self, context: &RuntimeContext) -> Result<Option<ManagedBackend>, String>;
    fn port_in_use(&self) -> bool;
    fn healthy(&self, context: &RuntimeContext, expected_instance: Option<&str>) -> bool;
    fn timings(&self) -> RestartTimings;
}

/// 真实进程与真实端口的监管实现。
pub struct SystemSupervisor;

impl BackendSupervisor for SystemSupervisor {
    fn stop(&self, process: &BackendProcess) -> Result<(), String> {
        stop_backend(process)
    }

    fn start(&self, context: &RuntimeContext) -> Result<Option<ManagedBackend>, String> {
        start_backend(context)
    }

    fn port_in_use(&self) -> bool {
        backend_port_in_use()
    }

    fn healthy(&self, context: &RuntimeContext, expected_instance: Option<&str>) -> bool {
        health_matches(context, expected_instance)
    }

    fn timings(&self) -> RestartTimings {
        RestartTimings::default()
    }
}

/// 重启后端：停旧、等端口释放、起新、确认健康；任一步失败都不得留下无人管理的后端。
pub fn restart_backend_with(
    process: &BackendProcess,
    context: &RuntimeContext,
    supervisor: &dyn BackendSupervisor,
) -> Result<(), String> {
    let _serial = RESTART_LOCK
        .lock()
        .map_err(|_| "后端重启锁已中毒".to_string())?;
    let timings = supervisor.timings();
    supervisor.stop(process)?;

    let port_deadline = std::time::Instant::now() + timings.port_release;
    while supervisor.port_in_use() && std::time::Instant::now() < port_deadline {
        std::thread::sleep(Duration::from_millis(100));
    }

    let child = supervisor
        .start(context)?
        .ok_or_else(|| "后端启动命令未创建进程".to_string())?;
    {
        let mut guard = process
            .0
            .lock()
            .map_err(|_| "后端进程状态已损坏".to_string())?;
        *guard = Some(child);
    }

    if wait_for_health(timings.health, || {
        supervisor.healthy(context, Some(context.instance_id()))
    }) {
        return Ok(());
    }

    // 新后端起来了却始终不健康：必须先清理，否则它会脱离桌面壳管理继续占着端口。
    supervisor.stop(process)?;
    Err("后端在 30 秒内未能恢复，请打开日志查看详细信息".to_string())
}

pub fn protect_backend(mut child: Child) -> Result<ManagedBackend, String> {
    match KillOnCloseJob::assign(&child) {
        Ok(job) => Ok(ManagedBackend { child, _job: job }),
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(error)
        }
    }
}

fn start_bundled_backend(context: &RuntimeContext) -> Result<Option<ManagedBackend>, String> {
    let Some(backend_executable) = bundled_backend_path() else {
        return Ok(None);
    };
    let runtime_dir = backend_executable
        .parent()
        .and_then(|path| path.parent())
        .ok_or_else(|| "内置后端目录结构无效".to_string())?;
    let mut command = Command::new(&backend_executable);
    context.apply_environment(&mut command, runtime_dir);
    let child = command
        .current_dir(runtime_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("内置后端无法启动，安装包可能不完整：{error}"))?;
    protect_backend(child).map(Some)
}

fn start_python_backend(context: &RuntimeContext) -> Result<ManagedBackend, String> {
    let root = project_root();
    let backend_dir = root.join("backend");
    if !backend_dir.join("app").join("main.py").is_file() {
        return Err(format!("未找到后端运行目录：{}", backend_dir.display()));
    }
    #[cfg(windows)]
    let python = find_python()?;
    #[cfg(not(windows))]
    let python = "python3".to_string();

    let mut command = Command::new(&python);
    context.apply_environment(&mut command, &root);
    let child = command
        .args([
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            BACKEND_HOST,
            "--port",
        ])
        .arg(BACKEND_PORT.to_string())
        .current_dir(&backend_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("无法启动 Python 后端：{error}"))?;
    protect_backend(child)
}

pub fn start_backend(context: &RuntimeContext) -> Result<Option<ManagedBackend>, String> {
    if let Some(identity) = health_identity() {
        return Err(existing_backend_error(
            &context.kind,
            &context.runtime_id,
            &identity.runtime_kind,
            &identity.runtime_id,
        ));
    }
    if backend_port_in_use() {
        return Err(format!(
            "本机端口 {BACKEND_PORT} 已被其他程序占用。请关闭占用程序后重试。"
        ));
    }
    if let Some(child) = start_bundled_backend(context)? {
        return Ok(Some(child));
    }
    start_python_backend(context).map(Some)
}

pub fn data_dir_for(context: &RuntimeContext) -> Result<PathBuf, String> {
    if context.kind == "bundled" {
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or_else(|| "无法确定 LocalAppData 目录".to_string())?;
        return Ok(local_app_data.join("KumiPlayer").join("data"));
    }
    Ok(context.install_root.join("data"))
}

pub fn stop_backend(process: &BackendProcess) -> Result<(), String> {
    let mut guard = process
        .0
        .lock()
        .map_err(|_| "后端进程状态已损坏".to_string())?;
    if let Some(mut backend) = guard.take() {
        backend.shutdown();
    }
    Ok(())
}

#[cfg(windows)]
fn find_python() -> Result<String, String> {
    // Try pythonw.exe first (no console window), then python.exe
    for name in &["pythonw.exe", "python.exe"] {
        if let Ok(path) = std::process::Command::new(name)
            .arg("-c")
            .arg("import fastapi, uvicorn, httpx, multipart, websockets; print('ok')")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
        {
            if path.success() {
                return Ok(name.to_string());
            }
        }
    }
    Err(
        "当前目录没有内置后端。仅源码开发模式需要 Python 3.12 和 backend/requirements.txt 依赖。"
            .to_string(),
    )
}
