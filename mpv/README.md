# KumiPlayer 内置 MPV 播放器（mpv/）

KumiPlayer 的 MPV 相关文件**全部集中在本目录**：干净官方二进制、默认套件配置、KumiPlayer 自有层、运行时清单、第三方许可证与 MPV 文档。找 MPV 相关的任何东西都从这里开始，不再有第二处源。

> 目录整合于 2026-09-24：原 `third_party/mpv/` 与 `resources/mpv-runtime/` 已并入本目录，两个旧目录不再存在。

## 目录职责

| 路径 | 职责 | 随应用分发 | Git 跟踪 |
|---|---|---|---|
| `runtime/` | 干净官方 MPV v0.41.0 二进制与依赖 DLL（27 个运行文件），内置播放器本体 | ✅ | ❌ 实际文件忽略，仅跟踪 README |
| `config/portable_config/` | **可替换层**：KumiPlayer 默认套件（mpv.conf、input.conf、uosc / thumbfast / mpv-stats-zh / uosc_danmaku、script-opts、fonts） | ✅ | ✅ |
| `config/kumiplayer/` | **自有层**：KumiPlayer 自有强制配置、Lua 脚本与自有资源（`shaders/anime4k-v4.0.1/`），不可替换 | ✅ | ✅ |
| `runtime-manifest.json` | 二进制来源、版本、文件清单、逐文件 SHA-256、配置版本、`distribution_status` | ✅ | ✅ |
| `runtime-manifest.schema.json` | 上述清单的 JSON Schema（v1.0） | — | ✅ |
| `components-manifest.json` | 第三方组件登记（uosc / thumbfast / Anime4K / mpv-stats-zh / uosc_danmaku）的来源、版本、许可证、随包文件 | — | ✅ |
| `licenses/` | 第三方许可证正文（6 份） | — | ✅ |
| `docs/` | MPV 运行时演进记录、开源项目与功能候选清单、按用途查找入口 | — | ❌（`*.md` 全局忽略，`README.md` 除外） |

## 源码层 ↔ 安装包层

安装包由 `scripts/stage_mpv_runtime.ps1` 生成到 `packaging/runtime/mpv/`，布局与源码**不完全相同**：安装层要求 `mpv.exe` 与 `portable_config/` 同级（符合 mpv 自带的 portable_config 约定），源码层的 `config/` 外壳不进入安装包。

| 源码 | 安装包 |
|---|---|
| `mpv/runtime/*` | `runtime/mpv/*` |
| `mpv/config/portable_config/` | `runtime/mpv/portable_config/` |
| `mpv/config/kumiplayer/` | `runtime/mpv/kumiplayer/` |
| `mpv/runtime-manifest.json` | `runtime/mpv/runtime-manifest.json` |

后端按运行形态解析这些路径（`backend/app/core/runtime.py`）：源码模式读本目录，安装模式读桌面运行时目录下的 `mpv/`，可用环境变量（`KUMIPLAYER_MPV_RUNTIME_DIR` / `KUMIPLAYER_MPV_CONFIG_DIR` / `KUMIPLAYER_MPV_LAYER_DIR`）覆盖。

## 分层与加载机制

内置播放器启动时（`backend/app/playback/mpv_runtime.py`）：

```text
mpv.exe
  --config-dir=<portable_config>          # 可替换层：默认套件（用户可整体换成第三方整合包）
  --include=<kumiplayer/mpv.conf>         # 自有层强制配置（在整合包 mpv.conf 之后解析）
  --script=<kumiplayer/scripts/*.lua>     # 自有层脚本（与整合包 scripts/ 自动加载并行）
  --script-opt=thumbfast-thumbnail=<状态目录>
  --script-opt=kumiplayer_anime4k-default_mode=<off|a|b|c|a+a|b+b|c+a>
  --script-opt=kumiplayer_anime4k-default_quality=<fast|light|balanced|high>
  --script-opt=kumiplayer_anime4k-shaders_dir=<自有层 shaders 绝对路径>
```

要点：

- `--script-opt` 是**追加**语义（`--script-opts-append` 的别名），可重复；`--script-opts` 是**覆盖**语义，同一命令行出现多次时只有最后一次生效，前面的键会被静默丢弃。
- 脚本选项键名前缀必须是 `<脚本名>-`（短横），不能是点号：`mp.options` 内部按 `identifier.."-"` 匹配，点号形式会被静默忽略。
- 不读取用户全局 `%APPDATA%\mpv` 配置，也不读取任何第三方整合包配置；禁止传 `--no-config`（它会覆盖 `--config-dir`，把 KumiPlayer 配置一起丢掉）。

## 自有层与外部整合包：隔离契约（2026-09-26）

内置播放器之外，可直接选择本机 MPV 整合包的目录或可执行文件。外部播放器使用自己的配置、Anime4K、快捷键、截图和菜单；KumiPlayer 通过 JSON IPC 联动播放进度，无需向整合包注入插件。启动工作目录为播放器所在目录，兼容整合包的相对路径。检测播放器、打开配置目录和重新引导均遵循所选模式。

判定标准是 mpv 官方参数的**追加语义 vs 覆盖语义**：

| 官方机制 | 语义 | 外部整合包下可用 | 说明 |
|---|---|---|---|
| `--script=<file>` | 追加，可重复 | ✅ | 只新增一个脚本，整合包自己的 `scripts/` 照常加载 |
| `--script-opt=<脚本名>-<键>=<值>` | 逐条追加 | ✅ | 只补一个脚本选项键 |
| `--include=<file>` | 追加解析但**覆盖**同名选项 | ❌ 仅内置模式 | 会改掉整合包 `mpv.conf` 的取值 |
| `--config-dir=<dir>` | 替换配置目录 | ❌ 仅内置模式 | 整合包自己的配置会完全不加载 |
| `--script-opts=` / `--input-conf=` | 覆盖 | ❌ 禁用 | 见上文覆盖语义说明 |

**当前实现不注入配置或脚本**：即使追加式脚本也可能覆盖快捷键或改变着色器，因此外部模式不传配置或脚本选项。上表仅解释官方机制，不代表允许默认启用外部注入。会话参数包含 IPC、窗口/媒体标题、受控播放列表、`--save-position-on-quit=no`、`--no-resume-playback`、`--autocreate-playlist=no`；另以 `--subs-fallback=no` 阻止无首选字幕时自动回退英语轨，仍可手动选取任意字幕。内外模式的续播起点均使用 `--{ --start=... 首个文件 --}` 文件局部参数组，避免下一集继承上一集的续播位置。

### 自有层逐项适用性

| 自有层内容 | 注入机制 | 内置模式 | 外部整合包 | 前置条件与风险 |
|---|---|---|---|---|
| `mpv.conf`（hwdec、load-stats-overlay、osd-bar、directory-mode） | `--include`（覆盖） | 注入 | 不注入 | 会覆盖整合包的解码与 OSD 取值 |
| `scripts/kumiplayer_anime4k.lua` | `--script` + `--script-opt` | 注入 | 可注入 | 依赖自有层 `shaders/anime4k-v4.0.1/`；必须同时注入 `kumiplayer_anime4k-shaders_dir`（脚本自 2026-09-24 起不再依赖 `~~/shaders/`） |
| `scripts/screenshot_to_video_dir.lua` | `--script` | 注入 | 谨慎 | 会改写 `screenshot-directory` / `screenshot-format` / `screenshot-template`，等于接管整合包的截图行为 |
| `scripts/kumiplayer_bindings.lua` | `--script` | 注入 | 可注入 | 功能键全为弱绑定（整合包 `input.conf` 优先）；仅 `Ctrl+v` 是强绑定屏蔽，会覆盖整合包同键 |
| `scripts/kumiplayer_uosc_menu.lua` | `--script` | 注入 | 可注入 | 依赖 uosc，缺失时自动跳过，不报错 |
| `shaders/anime4k-v4.0.1/` | 绝对路径 + `--script-opt` 注入 | ✅ | ✅ | 不使用 `~~/` |

**自有层脚本硬约束**：不得使用 `~~/`。`~~` 指向当前 mpv 的**配置目录**，在外部整合包档下会指向整合包自己的目录，导致读到整合包的文件或直接加载失败。自有资源一律用绝对路径经 `--script-opt` 注入。

> 上表的外部适用性仅供后续显式可选集成评估；当前所有自有层内容仅在内置模式加载。外部 Anime4K 完全由整合包管理，设置页不向外部模式写入内置画质选项。回归契约见 `backend/tests/test_mpv_external_mode.py`。

## 按用途查找

| 要找的内容 | 入口 |
|---|---|
| MPV 基础二进制、来源、版本、包哈希与逐文件哈希 | `runtime-manifest.json` |
| 第三方组件登记与许可证类型 | `components-manifest.json` |
| 内置播放器第三方组件、许可证与源码获取 | `THIRD_PARTY_NOTICES.md` |
| 第三方许可证正文 | `licenses/` |
| 默认套件配置与第三方脚本、着色器、字体 | `config/portable_config/` |
| KumiPlayer 自有脚本与自有 shader 资源 | `config/kumiplayer/` |
| 后端播放参数构造与运行时健康检查 | `backend/app/playback/mpv_runtime.py` |
| 开源项目、许可证、致谢材料与功能候选 | `docs/MPV开源项目与功能待加入清单.md` |
| 已完成的配置/脚本/交互变更 | `docs/MPV运行时演进记录.md` |
| 用户可见的快捷键与截图说明 | `docs/USER_GUIDE.md` |

## 修改纪律

1. 所有配置项必须能追溯到 MPV 官方手册（https://mpv.io/manual/stable/）或项目自有需求；**禁止**从第三方整合包复制不明配置、插件、字体、着色器或辅助工具（可作为形态参考，但落地的每一项都要能说清来源与理由）。
2. 修改配置或脚本后，在 `docs/MPV运行时演进记录.md` 追加一条简短记录：本次新增、调整或移除了什么，以及验证结论；二进制来源与 SHA-256 由 `runtime-manifest.json` 单独记录，配置变化还要同步其中的 `configuration_version`。
3. 接入或升级二进制、DLL、辅助运行文件时，必须更新符合 `runtime-manifest.schema.json` 的 `runtime-manifest.json`；进入 `runtime/` 的文件必须具备明确来源、版本与 SHA-256。
4. 自有层脚本必须放在 `config/kumiplayer/scripts/`，自有资源放在 `config/kumiplayer/` 内（不放进可替换层，否则用户替换整合包后自有功能会跟着丢失）。
5. 自有层脚本与配置保持 UTF-8 无 BOM、LF 行尾。
6. 未补齐发布材料（来源、版本、SHA-256、许可证）前，`distribution_status` 保持 `development-only`，不得进入公开安装包；`scripts/stage_mpv_runtime.ps1` 与 `scripts/verify_installer_bundle.ps1` 会双层拦截。
