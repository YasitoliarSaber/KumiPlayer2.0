//! 桌面壳后端监管回归：拒绝跨运行环境复用、健康检查超时、重启失败恢复。
//!
//! 这些行为决定了「源码版与安装版不会盲目共用媒体库」和「重启失败不留孤儿后端」，
//! 属于产品级约束，必须与真实生产代码路径共用同一套逻辑，而不是在测试里另写一份。

// 内联源模块而不是链接 tauri_shell_lib：桌面运行时会把整个 WebView2 依赖链拖进
// 测试二进制，导致进程起不来。被内联的模块里有本测试用不到的启动辅助，故放开 dead_code。
#![allow(dead_code)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

#[path = "../src/api_session.rs"]
mod api_session;
#[path = "../src/backend_job.rs"]
mod backend_job;
#[path = "../src/runtime_identity.rs"]
mod runtime_identity;
#[path = "../src/supervisor.rs"]
mod supervisor;

use runtime_identity::backend_is_compatible;
use supervisor::{
    existing_backend_error, protect_backend, restart_backend_with, stop_backend, wait_for_health,
    BackendProcess, BackendSupervisor, HealthTimings, ManagedBackend, RestartTimings,
    RuntimeContext,
};

fn test_context() -> RuntimeContext {
    RuntimeContext::new(
        "source",
        std::path::PathBuf::from(r"D:\KumiPlayer"),
        "instance-under-test",
        "token-under-test",
    )
}

fn fast_timings() -> RestartTimings {
    RestartTimings {
        port_release: Duration::from_millis(50),
        health: HealthTimings::new(Duration::from_millis(200), Duration::from_millis(20)),
    }
}

// ---------------------------------------------------------------------------
// 1. backend_is_compatible 与它在 start_backend 里的实际后果
// ---------------------------------------------------------------------------

#[test]
fn an_unknown_local_identity_never_claims_a_listening_backend() {
    // 自身 runtime_id 尚未确定时，端口上的任何后端都不得被认作「自己人」，
    // 否则桌面壳会把陌生后端的数据目录当成自己的媒体库。
    assert!(!backend_is_compatible("bundled", "", "bundled", ""));
    assert!(!backend_is_compatible(
        "bundled",
        "",
        "bundled",
        "9f86d081884c7d65"
    ));
    assert!(!backend_is_compatible(
        "source",
        "",
        "source",
        "9f86d081884c7d65"
    ));
}

#[test]
fn the_same_runtime_is_reported_as_already_running_rather_than_reused() {
    // backend_is_compatible 返回 true 的后果不是复用，而是单实例拒绝。
    let context = test_context();
    let message = existing_backend_error(
        context.kind(),
        context.runtime_id(),
        context.kind(),
        context.runtime_id(),
    );

    assert!(
        message.contains("已经启动"),
        "同一运行环境应提示已经启动，实际：{message}"
    );
}

#[test]
fn a_foreign_runtime_is_rejected_and_named_in_the_error() {
    // 安装版占用端口时，源码版必须拒绝并说明对方身份，不得共用媒体库。
    let context = test_context();
    let message = existing_backend_error(
        context.kind(),
        context.runtime_id(),
        "bundled",
        "ffffffffffffffff",
    );

    assert!(
        message.contains("另一个 KumiPlayer 运行环境"),
        "跨运行环境应提示存在另一个环境，实际：{message}"
    );
    assert!(
        message.contains("bundled"),
        "错误信息应点明对方运行模式，实际：{message}"
    );
}

#[test]
fn a_legacy_backend_without_identity_is_labelled_explicitly() {
    // 旧版本后端不回显 runtime_kind，错误信息不能出现空括号。
    let context = test_context();
    let message = existing_backend_error(context.kind(), context.runtime_id(), "", "");

    assert!(
        message.contains("无法识别的旧版本"),
        "无身份后端应被显式标注，实际：{message}"
    );
}

// ---------------------------------------------------------------------------
// 2. 健康检查等待循环（启动期 30 秒与重启期 30 秒共用）
// ---------------------------------------------------------------------------

#[test]
fn health_wait_gives_up_only_after_the_configured_deadline() {
    let timings = HealthTimings::new(Duration::from_millis(250), Duration::from_millis(20));
    let started = Instant::now();

    let ready = wait_for_health(timings, || false);

    assert!(!ready, "后端始终不就绪时必须返回未就绪");
    assert!(
        started.elapsed() >= Duration::from_millis(250),
        "必须等满配置的期限才放弃，实际耗时 {:?}",
        started.elapsed()
    );
}

#[test]
fn health_wait_returns_immediately_once_the_backend_answers() {
    let timings = HealthTimings::new(Duration::from_secs(30), Duration::from_millis(20));
    let attempts = AtomicUsize::new(0);
    let started = Instant::now();

    let ready = wait_for_health(timings, || attempts.fetch_add(1, Ordering::SeqCst) >= 2);

    assert!(ready, "后端就绪后必须返回就绪");
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "就绪后不得继续等满期限，实际耗时 {:?}",
        started.elapsed()
    );
}

// ---------------------------------------------------------------------------
// 3. restart_backend 的失败恢复路径
// ---------------------------------------------------------------------------

/// 可注入的监管者：记录 stop 调用次数，并按脚本决定 start / healthy 的结果。
struct ScriptedSupervisor<'a> {
    stops: &'a AtomicUsize,
    start: Box<dyn Fn() -> Result<Option<ManagedBackend>, String> + 'a>,
    healthy: bool,
    timings: RestartTimings,
}

impl BackendSupervisor for ScriptedSupervisor<'_> {
    fn stop(&self, process: &BackendProcess) -> Result<(), String> {
        self.stops.fetch_add(1, Ordering::SeqCst);
        stop_backend(process)
    }

    fn start(&self, _context: &RuntimeContext) -> Result<Option<ManagedBackend>, String> {
        (self.start)()
    }

    fn port_in_use(&self) -> bool {
        false
    }

    fn healthy(&self, _context: &RuntimeContext, _expected_instance: Option<&str>) -> bool {
        self.healthy
    }

    fn timings(&self) -> RestartTimings {
        self.timings
    }
}

#[test]
fn a_failed_start_surfaces_the_real_error_and_does_not_double_teardown() {
    let stops = AtomicUsize::new(0);
    let supervisor = ScriptedSupervisor {
        stops: &stops,
        start: Box::new(|| Err("无法启动 Python 后端：模拟失败".to_string())),
        healthy: false,
        timings: fast_timings(),
    };
    let process = BackendProcess::new(None);

    let result = restart_backend_with(&process, &test_context(), &supervisor);

    assert_eq!(
        result.unwrap_err(),
        "无法启动 Python 后端：模拟失败",
        "启动失败必须原样上抛底层原因，而不是笼统的重启失败"
    );
    assert_eq!(
        stops.load(Ordering::SeqCst),
        1,
        "启动都没成功，不应再执行一次清理"
    );
}

#[test]
fn a_start_that_creates_no_process_is_reported_instead_of_silently_succeeding() {
    let stops = AtomicUsize::new(0);
    let supervisor = ScriptedSupervisor {
        stops: &stops,
        start: Box::new(|| Ok(None)),
        healthy: true,
        timings: fast_timings(),
    };
    let process = BackendProcess::new(None);

    let result = restart_backend_with(&process, &test_context(), &supervisor);

    assert_eq!(
        result.unwrap_err(),
        "后端启动命令未创建进程",
        "没有进程却报成功会让应用停在「有窗口无后端」状态"
    );
}

#[cfg(windows)]
fn spawn_sentinel_backend() -> ManagedBackend {
    use std::os::windows::process::CommandExt;
    use std::process::{Command, Stdio};

    let child = Command::new("cmd")
        .args(["/C", "ping", "127.0.0.1", "-n", "30"])
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("哨兵进程应能启动");
    protect_backend(child).expect("哨兵进程应能加入进程保护作业")
}

#[cfg(windows)]
#[test]
fn a_backend_that_never_becomes_healthy_is_torn_down_before_reporting_failure() {
    // 这是最关键的一条：重启后新后端起来了但一直不健康，
    // 必须先杀掉它再报错，否则会留下一个脱离桌面壳管理的后端占着 37821。
    let stops = AtomicUsize::new(0);
    let supervisor = ScriptedSupervisor {
        stops: &stops,
        start: Box::new(|| Ok(Some(spawn_sentinel_backend()))),
        healthy: false,
        timings: fast_timings(),
    };
    let process = BackendProcess::new(None);

    let result = restart_backend_with(&process, &test_context(), &supervisor);

    assert!(
        result.unwrap_err().contains("未能恢复"),
        "健康检查超时必须报重启失败"
    );
    assert_eq!(
        stops.load(Ordering::SeqCst),
        2,
        "重启开始时停一次、超时清理再停一次，共两次；少一次就是留下孤儿后端"
    );
}

#[cfg(windows)]
#[test]
fn a_backend_that_becomes_healthy_is_kept_and_reported_as_success() {
    let stops = AtomicUsize::new(0);
    let supervisor = ScriptedSupervisor {
        stops: &stops,
        start: Box::new(|| Ok(Some(spawn_sentinel_backend()))),
        healthy: true,
        timings: fast_timings(),
    };
    let process = BackendProcess::new(None);

    let result = restart_backend_with(&process, &test_context(), &supervisor);

    assert!(result.is_ok(), "健康的后端必须被判定为重启成功");
    assert_eq!(
        stops.load(Ordering::SeqCst),
        1,
        "成功路径只应在开始时停一次旧后端"
    );

    // 收尾：测试自己负责清理留在 state 里的哨兵进程。
    stop_backend(&process).expect("哨兵进程应能被清理");
}
