# KumiPlayer 2.0

KumiPlayer 是一款面向 Windows 的本地 / 网盘媒体库与 MPV 播放应用。它把本地目录、115 / 百度目录树 TXT 或 OpenList 路径转换为统一的 SourceEvidence，再由 V4 MediaGraph 建立 Work、Season、Episode、Edition 与 Asset；SQLite 是唯一媒体事实源，真实媒体的位置和文件名不会被移动或改写。

> 当前项目仍在开发阶段。`release\KumiPlayer.exe` 是依赖当前源码目录的开发版；交给普通用户使用时应构建完整安装包。

## 主要能力

| 场景 | KumiPlayer 可以做什么 |
|---|---|
| 导入媒体 | 扫描本地目录或目录树 TXT，统一生成 SourceEvidence |
| 识别整理 | 预览 ParsedFacts 与 MediaGraph，确认后生成不可变 revision |
| 后台执行 | confirmed revision 驱动镜像、元数据和 SQLite 投影任务，任务可恢复且幂等 |
| 元数据 | 以 TMDB 为主、AniList 为补充，结果只写入 Work / provider mapping |
| 浏览 | 首页推荐、分类、来源筛选、搜索、收藏、最近观看和虚拟化海报墙 |
| 作品详情 | 查看简介、演员、季度、剧集、关联作品和相关推荐 |
| 播放 | 使用 KumiPlayer 内置干净 MPV 与自有配置，按 Asset 保存进度和本地观看状态 |
| Bangumi | 保留本地账号会话、登录验证、用户信息和作品搜索 |
| 媒体维护 | 查看 V4 jobs、诊断投影、重新扫描并使用受保护的媒体状态重置 |

## 容易错过的功能

### 拖入目录树快速导入

除作品详情和设置界面外，可在 KumiPlayer 任意页面拖入单个 TXT 目录树。应用会把它送入“媒体管理 → 扫描来源 → 识别预览 → 确认 revision”流程；原始 TXT 只读，不会被写回或改名。

### 本地路径与 OpenList 网盘路径导入

本地路径和目录树 TXT 都先扫描为 SourceEvidence，再生成可审核的 MediaGraph 预览；确认后创建可恢复的 V4 jobs，依次完成镜像、资料补充和 Library Projection。OpenList 负责远端目录浏览与来源定位，导入仍进入同一条 revision 链。

- 可以离开媒体管理页面；返回时会恢复批次状态。
- 无法自动确认的识别结果会生成 review issue；未解决前不能确认 revision。
- OpenList 是网盘连接与远端路径入口，与 115 / 百度目录树 TXT 共用同一套 SourceEvidence 合同。

### 作品详情与多版本

作品详情页只读取 V4 Work、Season、Episode 和 Asset。季度编号、集号和版本关系由后端 MediaGraph 决定，前端不再自行合卡或重写编号；同一集的不同清晰度、压制组或来源会作为多个 Asset 保留。用户可以选择 Asset 播放、标记本地观看状态或打开可访问的来源目录。

### MPV 截图

- `F10`：无字幕、无顶部提示的纯净截图。
- `Alt+F10`：带字幕截图，并显示 MPV 原生截图路径提示。

截图自动保存到：

```text
桌面\动漫截图\作品名 - SxxExx - 剧集标题\HH-MM-SS.mmm.jpg
```

截图按刮削后的作品和剧集标题分目录，文件名是视频内精确到毫秒的时间，不添加 `_01` 序号。

更多操作见 [用户功能指南](docs/USER_GUIDE.md)。

## 快速开始

### 开发版 EXE

运行：

```text
release\KumiPlayer.exe
```

以下文件必须相邻：

```text
release\KumiPlayer.exe
release\WebView2Loader.dll
```

开发版会使用当前项目的 `backend/`、`data/`、`dist/` 和 `third_party/mpv/runtime/`，本机需要可用的 Python 后端环境。

### 普通用户安装版

运行安装包：

```text
release\KumiPlayer-Setup.exe
```

安装包包含冻结后的 Python 后端、KumiPlayer 内置播放器、自有播放配置和桌面程序。用户无需安装 Python、Node.js 或 Rust；首次安装建议联网，以便系统缺少 WebView2 时下载微软官方运行时。当前内置 MPV 仅用于本地开发，第三方许可证与发布材料尚未完成，正式安装包分发仍被拦截。

## 运行环境与本地配置

- 支持 Windows 10 / 11；普通用户使用完整安装包时，无需另外配置开发环境。
- 源码开发需要 Node.js 22、Python 3.12、Rust stable；播放器使用项目内置的干净 MPV，不读取或改写用户全局 MPV 配置。
- 个人配置仅保存在本机。源码版默认使用项目 `data/`，安装版默认使用 `%LOCALAPPDATA%\KumiPlayer\data`。
- 主题、媒体目录等非敏感设置保存在 `config.json`；TMDB、Bangumi 等凭据保存在 Windows Credential Manager，不再以明文写入配置文件。
- 旧版 `config.json` 中的明文凭据会在读取时自动迁移；只有系统凭据写入成功后才清空旧值，避免迁移失败造成数据丢失。
- 凭据不会显示在公开界面、脱敏诊断或错误信息中；不要把真实 Token 写入公开问题报告。

## 启动失败时的恢复

如果桌面安全会话初始化失败、React 页面崩溃或本地后端未能就绪，KumiPlayer 会显示恢复界面，而不是留下空白窗口。可以直接：

- 重新加载应用。
- 重启 KumiPlayer 本地后端。
- 打开日志目录。
- 导出已脱敏的诊断文本。

正常退出时，Tauri 会终止自己启动的后端进程；Windows Job Object 和心跳超时是额外兜底，避免后端长期残留。

## 首次启动

首次启动包含四步：

1. 查看运行方式和数据保护说明。
2. 自动检测 KumiPlayer 内置播放器，确认版本与完整性。
3. 选择镜像目录和至少一个媒体来源。
4. 可选填写 TMDB API 读取访问令牌和 Bangumi 个人 Access Token。

TMDB 需要的是“API 读取访问令牌”，不是 32 位 API Key。以后可在“设置 → 应用与支持”重新打开引导，取消不会覆盖已有配置。

## 日常使用路径

### 建立媒体库

进入“媒体管理”后，按导入来源选择对应路径：

```text
本地目录 / TXT：媒体管理 → 扫描来源 → 生成识别预览 → 确认 revision
确认后：confirmed revision → mirror / scrape jobs → SQLite Library Projection
```

- TXT 和本地扫描都使用同一个 SourceEvidence → ParsedFacts → MediaGraph 流程。
- 季度或集数不明确的项目会生成 review issue，必须在确认前处理。
- confirmed revision 只产生 V4 jobs；任务输入固定为 revision/work/asset 身份，不会重新识别来源。
- 实际视频路径不可达时不会继续生成镜像。
- 自动刮削只按 Work 去重，并通过 provider mapping 保存外部 ID，不覆盖本地季度和集号。

### 播放和观看状态

- 详情页播放按钮把选定 Asset 的 playback locator 交给播放器；进度按 Episode + Asset 保存。
- 可以收藏作品、手动修改单集已看状态。
- 播放历史进入“最近观看”，收藏进入“我的收藏”。
- Bangumi 当前只负责账号会话、登录验证、用户信息和作品搜索，不参与本地 Work/Episode 身份写入。

## 数据安全

| 模式 | 数据目录 | 默认镜像建议 |
|---|---|---|
| 源码 / 开发 EXE | `<项目>\data` | `<项目>\data\mirror` |
| 安装版 | `%LOCALAPPDATA%\KumiPlayer\data` | `<安装目录>\mirror` |

KumiPlayer 不会主动移动或重命名真实媒体。构建、安装和升级不得覆盖现有 `data/`、镜像、NFO、图片、播放记录、快照或 Token。删除和清理功能必须先预览，再确认实际影响范围。

## 构建

开发 EXE：

```bat
build_tauri.bat
```

完整安装包：

```bat
build_installer.bat
```

两种构建都会检查 `37821` 端口和 KumiPlayer 运行状态。开发 EXE 构建执行 TypeScript、Vite、Rust/Tauri 和桌面产物验证；安装包额外检查冻结后端、内置 MPV 运行时、许可证发布门，以及 NSIS 内容。

## 验证

```bash
npm test
npm run build
npm run check:versions
uv sync --project backend --locked --extra dev
uv run --project backend python -m pytest backend/tests

cd src-tauri
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

## 文档

- [用户功能指南](docs/USER_GUIDE.md)
- [现行架构](docs/PROJECT.md)
- [项目目录结构](docs/PROJECT_STRUCTURE.md)
- [配置与数据目录](docs/01_项目基准/KumiPlayer_配置与数据目录.md)
- [Windows 分发说明](docs/DISTRIBUTION.md)
- [视觉风格与主题规范](docs/01_项目基准/KumiPlayer_视觉风格与主题规范_2026-07-08.md)
- [视频文件命名与识别边界](docs/01_项目基准/视频文件命名与目录组织模式.md)
- [文档总索引](docs/00_索引/KumiPlayer_文档总索引.md)

不要把真实 Token、Key、网盘路径或个人媒体目录写入公开文档、日志和问题报告。
