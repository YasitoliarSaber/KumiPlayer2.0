# MPV 运行时与发布（文档）

本目录记录 KumiPlayer 内置 MPV 播放器的运行时演进、开源组件与分发状态，是 MPV 相关工作的文档入口；二进制来源清单不在这里，由 `../runtime-manifest.json` 记录。

## 文件

| 文件 | 职责 |
|---|---|
| `README.md` | 本说明（文件/目录/分层架构/查找入口/修改纪律） |
| `MPV开源项目与功能待加入清单.md` | 记录基础运行时、待评估的开源项目、商用条件，以及 MPV 功能/预设/滤镜候选 |
| `MPV运行时演进记录.md` | 编码任务收尾时顺便更新的简要功能日志（最新：2026-09-24 目录整合与自有层自包含） |

> 2026-09-24 之前的历史条目里出现的 `third_party/mpv/...` 与 `resources/mpv-runtime/...` 是当时的真实路径，保留原文不改；现行路径以 `../README.md` 为准。

## 相关目录

| 路径 | 职责 |
|---|---|
| `../`（`mpv/`） | MPV 统一目录入口：职责表、分层、注入契约、修改纪律 |
| `../config/` | KumiPlayer 配置源，分两层：`portable_config/`（可替换层）与 `kumiplayer/`（自有层，含自有 shader 资源） |
| `../runtime/` | 干净官方 MPV v0.41.0 二进制与依赖 DLL（Git 忽略实际文件） |
| `../runtime-manifest.json` | 运行时清单：来源、版本、27 个文件逐项 SHA-256、配置版本、分发状态 |
| `../components-manifest.json` | 第三方组件登记（来源、commit、SHA-256、许可证、随包文件） |
| `../licenses/` | 第三方许可证正文 |
| `packaging/runtime/mpv/` | 安装包构建暂存副本（非源码，Git 忽略；由 `scripts/stage_mpv_runtime.ps1` 生成、`scripts/verify_installer_bundle.ps1` 校验） |

## 分层架构

MPV 运行时采用"干净二进制 + 可替换层 + 自有层"的分层设计：

- **可替换层** `../config/portable_config/`：用户可整体替换为第三方整合包。含 mpv.conf（禁用内置键位）、input.conf（通用播放键）、scripts/（uosc、thumbfast、stats 中文版、uosc_danmaku）、script-opts/、fonts/。
- **自有层** `../config/kumiplayer/`：不可替换，随应用分发，由后端 `--include` / `--script` / `--script-opt` 追加加载。含自有 mpv.conf（hwdec=auto-safe）、screenshot_to_video_dir.lua（截图目录）、kumiplayer_anime4k.lua（Anime4K 控制器）、kumiplayer_bindings.lua（快捷键绑定）、kumiplayer_uosc_menu.lua（右键菜单降级），以及自有 shader 资源 shaders/anime4k-v4.0.1/。
- **二进制层** `../runtime/`：干净官方 MPV v0.41.0，独立于配置层，来源与哈希见 `../runtime-manifest.json`。

自有层与外部整合包并存时的注入契约（哪些官方机制可安全叠加、哪些只限内置模式）见 `../README.md`。

## 按用途查找

| 要找的内容 | 入口 |
|---|---|
| MPV 基础二进制、来源、版本、包哈希和文件哈希 | `../runtime-manifest.json` |
| 第三方组件登记与许可证类型 | `../components-manifest.json` |
| 第三方许可证材料正文 | `../licenses/` |
| KumiPlayer 可替换层配置（mpv.conf / input.conf / 第三方脚本与字体） | `../config/portable_config/` |
| KumiPlayer 自有脚本与自有 shader（截图目录 / Anime4K / 快捷键 / 右键菜单） | `../config/kumiplayer/` |
| 后端播放参数加载机制 | `backend/app/playback/mpv_runtime.py` |
| 开源项目、许可证、致谢材料和功能候选 | `MPV开源项目与功能待加入清单.md` |
| 已完成的 MPV 配置/脚本功能变更 | `MPV运行时演进记录.md` |
| 用户可见的快捷键与截图行为 | `docs/USER_GUIDE.md` |

## 修改纪律

MPV 相关编码任务完成后，应在最终审查和输出阶段顺便向 `MPV运行时演进记录.md` 追加一条简短记录，只写本次增加、调整或移除了什么，以及验证结论。二进制来源、版本、文件列表和 SHA-256 由 `../runtime-manifest.json` 单独记录；配置、脚本或自有资源变化还须同步其中的 `configuration_version`。所有配置必须可追溯到 MPV 官方手册或项目自有需求，禁止从第三方整合包复制不明配置。未补齐许可证与验证材料前（`distribution_status=development-only`），MPV 运行时只能用于本地开发，不得进入公开安装包。
