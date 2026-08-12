# 本地 MPV 运行文件

将经过来源核验的干净 MPV Windows 运行文件完整解压到本目录。

- 本 README 由 Git 跟踪。
- `mpv.exe`、`mpv.com`、DLL 等实际运行文件由 `.gitignore` 排除。
- 不得复制项目根目录 `mpv/` 中的第三方整合包内容。
- 接入或升级运行文件时，必须创建或更新上一级的 `runtime-manifest.json`，
  登记原始下载文件、来源、版本、构建目标及 SHA-256。
- 发布材料尚未补齐前，本目录内容仅限本地开发使用。
