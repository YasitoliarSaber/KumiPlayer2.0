use std::path::Path;

#[path = "../src/runtime_identity.rs"]
mod runtime_identity;

use runtime_identity::{backend_is_compatible, runtime_id_for};

#[test]
fn runtime_id_is_stable_for_the_same_windows_root() {
    let first = runtime_id_for("bundled", Path::new(r"D:\KumiPlayer"));
    let second = runtime_id_for("bundled", Path::new(r"d:/kumiplayer"));

    assert_eq!(first, second);
}

#[test]
fn runtime_id_ignores_a_trailing_separator() {
    // 安装根写不写尾分隔符只是调用方的书写差异，不能因此变成两个运行身份。
    let bare = runtime_id_for("bundled", Path::new("D:\\KumiPlayer"));
    let trailing = runtime_id_for("bundled", Path::new("D:\\KumiPlayer\\"));

    assert_eq!(bare, trailing);
}

#[test]
fn runtime_id_handles_non_ascii_install_roots() {
    // 中文安装路径是真实场景：身份必须稳定，且不能和相邻目录撞成同一个。
    let chinese = runtime_id_for("bundled", Path::new("D:\\软件\\KumiPlayer"));
    let same_again = runtime_id_for("bundled", Path::new("D:\\软件\\KumiPlayer"));
    let sibling = runtime_id_for("bundled", Path::new("D:\\软件\\KumiPlayer2"));

    assert_eq!(chinese, same_again);
    assert_ne!(chinese, sibling);
    assert_eq!(chinese.len(), 16, "运行身份必须始终是 16 位十六进制");
}

#[test]
fn runtime_identity_separates_install_source_and_other_roots() {
    let installed = runtime_id_for("bundled", Path::new(r"D:\KumiPlayer"));
    let other_install = runtime_id_for("bundled", Path::new(r"E:\KumiPlayer"));
    let source = runtime_id_for("source", Path::new(r"D:\KumiPlayer"));

    assert_ne!(installed, other_install);
    assert_ne!(installed, source);
    assert!(backend_is_compatible(
        "bundled", &installed, "bundled", &installed
    ));
    assert!(!backend_is_compatible(
        "bundled", &installed, "source", &source
    ));
    assert!(!backend_is_compatible("bundled", &installed, "bundled", ""));
}
