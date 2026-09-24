# 本地 MPV 运行文件

将经过来源核验的干净 MPV Windows 运行文件完整解压到本目录（官方 v0.41.0，27 个运行文件，含 `mpv.exe`、`mpv.com`、依赖 DLL 与注册脚本）。

- 本 README 由 Git 跟踪；`mpv.exe`、`mpv.com`、DLL 等实际运行文件由 `.gitignore` 排除。
- 只放来源可核验的官方文件；**禁止**把第三方 MPV 整合包（如本机其他目录里的 MPVlite / mpv-Yaozhi 等）的配置、插件、字体、着色器或辅助工具复制进来。整合包只作为形态与取值参考，不做来源。
- 接入或升级运行文件时，必须创建或更新上一级的 `runtime-manifest.json`，登记原始下载文件、来源、版本、构建目标及 SHA-256。
- 发布材料尚未补齐前，本目录内容仅限本地开发使用（`distribution_status=development-only`）。

上层目录职责与安装包映射见 `../README.md`。
