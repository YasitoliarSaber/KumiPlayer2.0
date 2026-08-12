# Third-party MPV 运行时

本目录是 KumiPlayer 内置 MPV 播放器（干净官方二进制 v0.41.0）的**规范、清单与许可证入口**，不包含任何未经验证的二进制。

## 目录结构

```text
third_party/mpv/
├─ README.md                        本说明
├─ runtime-manifest.json            运行时清单（二进制 + 配置版本 + 分发状态）
├─ runtime-manifest.schema.json     运行时清单 JSON Schema（v1.0）
├─ components-manifest.json         第三方组件登记（uosc / thumbfast / Anime4K / mpv-stats-zh / uosc_danmaku）
├─ licenses/                        第三方许可证材料（6 份）
└─ runtime/
   ├─ README.md                     本地运行文件放置说明（Git 跟踪）
   └─ ...                           干净 MPV v0.41.0 二进制与依赖文件（Git 忽略，27 个运行文件）
```

## 三层职责

| 层 | 路径 | 职责 |
|---|---|---|
| 第三方二进制 | `third_party/mpv/runtime/` | 干净官方 MPV v0.41.0（x86_64-w64-mingw32）及依赖，来源/版本/SHA-256 登记于 runtime-manifest.json |
| 可替换层 | `resources/mpv-runtime/portable_config/` | 用户可整体替换为第三方整合包的配置/脚本/着色器/字体（mpv.conf、input.conf、uosc、thumbfast、Anime4K、uosc_danmaku 等） |
| KumiPlayer 自有层 | `resources/mpv-runtime/kumiplayer/` | 不可替换，随应用分发：自有 mpv.conf（hwdec=auto-safe）、截图目录脚本、Anime4K 控制器、快捷键绑定、右键菜单降级 |

## 清单文件

- **runtime-manifest.json**：遵循 `runtime-manifest.schema.json`（v1.0）。记录二进制来源/版本/文件列表/SHA-256、配置版本（当前 `mpv-runtime-config-2026-08-12-r12`）与 `distribution_status`。`distribution_status=development-only` 表示许可证与发布材料未完全补齐，仅限本地开发，不得进入公开安装包。
- **components-manifest.json**：第三方组件登记，含各组件许可证类型——uosc（LGPL-2.1）、thumbfast（MPL-2.0）、Anime4K（MIT）、mpv-stats-zh（MIT）、uosc_danmaku（MIT）。
- **licenses/**：6 份许可证材料正文（uosc-5.13.0/LICENSE.LGPL、thumbfast/MPL-2.0.txt、anime4k-v4.0.1/LICENSE、mpv-stats-zh/LICENSE.md、mpv-v0.41.0/LICENSE.GPL、mpv-v0.41.0/LICENSE.LGPL）。

## 硬性约束

- **禁止**复制根目录 `mpv/` 旧第三方整合包（含配置、插件、字体、着色器、辅助工具）到本目录或 `resources/mpv-runtime/`。
- 进入 `runtime/` 的二进制必须具有**明确来源、版本和 SHA-256**，并在运行时清单中登记。
- 每次接入或升级 MPV 二进制、配置或脚本，必须同步更新 `runtime-manifest.json`（配置版本与相关文件哈希），并在 `docs/03_运行时与发布/MPV/MPV运行时演进记录.md` 追加记录。
- **许可证材料未补齐前，不得进入公开安装包**（`distribution_status` 保持 `development-only`）。

## 当前状态

- **二进制**：官方 MPV v0.41.0（干净、27 个运行文件），来源与 SHA-256 已登记。
- **配置/脚本**：分层架构已落地（portable_config 可替换层 + kumiplayer 自有层），配置版本 r12；后端经 `--config-dir` + `--include` + `--script` 加载，不读取用户全局 MPV 配置。
- **打包**：`scripts/stage_mpv_runtime.ps1` 暂存两层配置、`scripts/verify_installer_bundle.ps1` 校验两层完整性。
- **分发**：`distribution_status=development-only`，许可证材料未完全补齐，暂不进正式安装包。
