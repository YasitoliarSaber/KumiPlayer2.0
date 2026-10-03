# MPV 运行时演进记录

本文件简要记录 KumiPlayer 自有 MPV 功能是怎样逐步增加的。它用于保留清晰的开发历史，不记录冗长操作过程，也不代替第三方二进制清单。

## 更新规则

- 每个 MPV 相关编码任务完成后，由编码 AI 在最终审查与输出阶段顺便追加一条。
- 编码 AI 可以让一个只负责本文档的子代理根据实际 diff 和验证结果起草记录，最终内容仍由主编码 AI 核对。
- 简要说明增加、调整或移除了什么，以及验证结论。
- 配置依据只需注明“MPV 官方手册”或对应的项目自有需求。
- 二进制来源、构建目标、文件列表和 SHA-256 写入 `../runtime-manifest.json`，不在这里重复。
- 2026-09-24 起 MPV 文件统一放在项目根 `mpv/` 目录；本文档早期条目里的 `third_party/mpv/...` 与 `resources/mpv-runtime/...` 是当时的真实路径，保留原文不改。

## 简要模板

### YYYY-MM-DD · 更新标题

- **更新**：本次增加、调整或移除的内容。
- **依据**：MPV 官方手册或 KumiPlayer 自有需求。
- **验证**：针对性验证结论；未验证的项目必须明确说明。

## 演进记录

### 2026-09-24 · 组件盘点与文档校准（无功能变更）

- **更新**：全面盘点内置 MPV 当前实际加载的组件（用真实 `mpv.exe` + 生产参数跑一次，按 `-v` 日志核对加载清单），并据此修掉四处文档与实际不符之处：① `portable_config/script-opts/README.md` 原写“当前没有脚本配置项”，实际有 5 个 conf（uosc / thumbfast / stats / uosc_danmaku / kumiplayer_anime4k），已补成清单表；② `docs/THIRD_PARTY_NOTICES.md` 的 uosc_danmaku 说明原写“默认 API 服务器改为空，用户需自行配置”，与实际 conf（`api_server=https://danmaku-api.152468.xyz`、`fallback_server=https://dmku.hls.one`、弹幕键 `Alt+d` / 搜索键 `Ctrl+d`）不符，已按实际改写；③ `components-manifest.json` 的 uosc `bundled_files` 写的是 `scripts/uosc.lua`，实际是 `scripts/uosc/` 目录（含 lib/elements/intl/char-conv），已改；④ 开源清单里 Anime4K 的接入位置仍写可替换层，已改为自有层。
- **依据**：真实运行日志（`mpv --log-file … -v` 读取 `Loading lua script` / `Opened config file` / `change-list glsl-shaders` 三类记录）与 `mpv/components-manifest.json`、`portable_config/script-opts/uosc_danmaku.conf` 实际取值。
- **验证**：核对到实际加载的脚本共 15 个（mpv 二进制内嵌 `@*.lua` 7 个 + 可替换层 4 个 + 自有层 4 个；另有内嵌 `@stats.lua` 因 `load-stats-overlay=no` 未加载、`@osc.lua` 虽加载但被 uosc `main.lua:6` 的 `mp.set_property('osc','no')` 在运行时关闭），与配置开关一致；`components-manifest.json` 改后 JSON 仍可解析。**未做真机播放验证**（未在桌面端播放，行为未变，本次仅文档与登记校准）。

### 2026-09-24 · MPV 文件统一到 mpv/ 目录，自有层自包含

- **更新**：
  - **目录整合**：把原来分散在 `third_party/mpv/`（二进制、清单、许可证）与 `resources/mpv-runtime/`（配置源）两处的 MPV 文件全部并入项目根 `mpv/` 单一目录：`mpv/runtime/`（二进制）、`mpv/config/{portable_config,kumiplayer}/`（配置源两层）、`mpv/{runtime-manifest.json,runtime-manifest.schema.json,components-manifest.json}`、`mpv/licenses/`、`mpv/docs/`（原先在 `docs/03_运行时与发布/MPV/`）。`third_party/` 与 `resources/` 两个目录已不存在。
  - **自有层自包含**：Anime4K 着色器由可替换层 `portable_config/shaders/anime4k-v4.0.1/` 移入自有层 `config/kumiplayer/shaders/anime4k-v4.0.1/`。原因是 `kumiplayer_anime4k.lua` 原先用 `~~/shaders/anime4k-v4.0.1/` 解析，而 `~~` 指向当前 mpv 的**配置目录**（即 portable_config）——用户按设计把 portable_config 整体替换成第三方整合包后，Anime4K 菜单仍会出现但整条链加载失败。
  - **资源改绝对路径注入**：`kumiplayer_anime4k.lua` 新增 `shaders_dir` 脚本选项，后端在两种模式下都追加 `--script-opt=kumiplayer_anime4k-shaders_dir=<自有层 shaders 绝对路径>`；`~~/shaders/` 仅保留为手工裸跑 mpv（无注入）时的兜底。
  - **注入契约入档**：`mpv/README.md` 新增「自有层与外部整合包：注入契约」一节，按 MPV 官方的追加语义 / 覆盖语义列出各机制能否用于外部整合包档（`--script` / `--script-opt` 可叠加；`--config-dir` / `--include` / `--script-opts` 只限内置模式），并给出自有层逐项适用性表与「自有层脚本不得使用 `~~/`」的硬约束。
  - **引用同步**：`backend/app/core/runtime.py`（三个路径解析函数）、`backend/app/playback/mpv_runtime.py`（`--script-opt` 注入）、`scripts/stage_mpv_runtime.ps1`（源路径）、`.gitignore`（`/mpv/` 由整体忽略改为「二进制忽略、配置与清单跟踪」）、`components-manifest.json` 随包文件路径、8 个后端测试与 4 个配置文件内注释一并更新。安装包布局 `packaging/runtime/mpv/` 保持不变（`mpv.exe` 与 `portable_config/` 同级）。
  - **AGENTS.md 松绑**：按用户要求移除项目根 `AGENTS.md` 里的 MPV 硬约束条款（开发阶段路径经常变动），改为指向 `mpv/README.md` 作为唯一事实来源。
  - `configuration_version`：`mpv-runtime-config-2026-09-22-r18` → `mpv-runtime-config-2026-09-24-r19`。
- **依据**：用户 2026-09-24 需求（MPV 相关文件收拢到一个大目录；自有特征插件要能在外部播放器模式下继续生效且不影响整合包）；MPV 官方手册 `--script` / `--script-opt` / `--include` / `--config-dir` 语义。
- **验证**：后端定向测试全部通过 —— MPV 相关 7 个测试文件 53 条全绿（`test_mpv_anime4k_lua_mapping` / `test_mpv_anime4k_menu` / `test_mpv_input_conf` / `test_mpv_stats_zh` / `test_mpv_external_mode` / `test_runtime_environment` / `test_release_packaging`），MPV 集成与播放链路 6 个测试文件 118 条全绿（含 `test_mpv_kumiplayer_integration` / `test_mpv_progress_observer` / `test_config_api` / `test_v4_playback_*`）。新增两条回归：① `test_anime4k_shaders_are_injected_from_the_owned_layer`（断言生产参数里的注入路径等于自有层 `shaders/`，且可替换层不再存在 `shaders/anime4k-v4.0.1`）；② `test_real_mpv_mounts_anime4k_chain_from_injected_owned_layer`（真实 `mpv.exe` + 真实脚本，mode a 实测日志为 `shaders_dir=D:/…/mpv/config/kumiplayer/shaders/anime4k-v4.0.1/`、`applied mode=a quality=balanced shaders=6`，退出码 0、无 shader 报错）。**未做真机播放验证**（未在桌面端实际播放视频，未验证挂载盘素材）。
- **备注**：pytest 会话结束时会出现一条与本改动无关的 `PermissionError`（清理 `%TEMP%\pytest-current` 符号链接被拒），测试结果本身全部为通过。

### 2026-09-15 · 修复 script-opts 覆盖语义导致的注入失效

- **更新**：启动参数中三条 `--script-opts=` 改为追加式 `--script-opt=`（别名 `--script-opts-append`）。`--script-opts` 是覆盖语义，同一命令行上多次出现只有最后一次生效：实测生效值曾是 `[kumiplayer_anime4k.default_quality=balanced]`，即 **thumbfast 缓存目录与 Anime4K 默认模式被静默丢弃**；修复后为 `[thumbfast.thumbnail=…,kumiplayer_anime4k.default_mode=…,kumiplayer_anime4k.default_quality=…]`。同时实测确认 `--script` 为追加语义，多处 `--script=` 无需改动。未改任何配置文件或 Lua 脚本，`configuration_version` 保持 `mpv-runtime-config-2026-08-12-r14`。
- **依据**：MPV 官方手册 `--script-opts` / `--script-opt` 选项说明；第三方干净 MPV v0.41.0 本地实测（Lua 探针读 `options/script-opts`）。
- **验证**：mpv 相关后端测试全部通过（含新增"禁止覆盖式 `--script-opts`"回归）；用生产代码生成的参数喂给真实 mpv，三个键全部生效；`check_mpv_runtime(verify_files=True)` 四项校验均为 True。未做真机播放验证。

### 2026-09-15 · 外部 MPV 整合包档（零注入）

- **更新**：新增 `player_mode`（`internal` / `external`）与 `external_mpv_path` 配置。`internal` 档保持原行为（`--config-dir` 指向 KumiPlayer 自有 portable_config，`--include` / `--script` 注入自有强制配置与脚本，可写状态隔离到 `data/mpv-state`）。新增 `external` 档：**不传** `--config-dir`、`--include`、`--script`、`--script-opts`，用户自备整合包的 `portable_config`、`input.conf`、脚本、着色器与界面完全自主，KumiPlayer 只追加会话参数（`--input-ipc-server`、`--start`、`--title`、`--force-media-title`、`--save-position-on-quit=no`、`--no-resume-playback`、`--autocreate-playlist=no` 与受控播放列表）。外部档路径无效时明确报错，不静默回退内置播放器。播放调校页新增「播放模式」与整合包 `mpv.exe` 选择入口。
- **依据**：用户 2026-09-15 需求（使用他人整合包时不得影响整合包内部）；MPV 官方手册命令行参数语义。
- **验证**：`backend/tests/test_mpv_external_mode.py` 6 条与 `tests/components/player-tuning-mode.test.tsx` 3 条通过；`npx tsc -b` 通过；全量后端仅剩 4 条既有失败。**未做真机验证**：未启动桌面端、未用真实外部整合包播放。

### 2026-09-15 · 工作区 script-opts 调参（缩略图与弹幕）

- **更新**：
  - `portable_config/script-opts/thumbfast.conf`：缩略图尺寸上限由 320×320 调整为 256×256，新增 `overlay_id=42`、`quit_after_inactivity=0`。
  - `portable_config/script-opts/uosc_danmaku.conf`：开启本地与网络 URL 弹幕自动加载（`autoload_local_danmaku=yes`、`autoload_for_url=yes`），字号 50→30，简繁转换由不转换改为转简体（`chConvert=1`），新增 `vf_fps=yes` 与 `fps=60/1.001`。
- **依据**：KumiPlayer 自有播放体验需求（用户侧调整）；这些键属于对应 mpv 脚本的 script-opts 配置面。
- **验证**：本条只记录工作区中已存在的配置改动（`git diff`，尚未提交）；未运行播放验证，未修改 MPV 二进制、脚本或运行时清单，未构建 EXE/安装包。实际生效情况需在桌面播放中确认。

### 2026-08-23 · V4 Asset 播放会话接回内置 MPV

- **更新**：播放接口从只返回 Asset 定位符改为校验 confirmed revision 中的 Work/Episode/Asset 关系，生成受管 `.strm` 会话文件并启动内置 MPV；恢复会话状态、优雅停止和 IPC 播放进度写入 V4 SQLite。
- **依据**：KumiPlayer V4 媒体身份边界与既有内置 MPV 运行时规范。
- **验证**：V4 播放会话与进度针对性测试通过；未修改 MPV 配置、脚本、二进制或运行时清单，未构建 EXE/安装包。

### 2026-08-12-r14 · 禁用 mpv 原生 OSD 进度条（滚轮 seek 不再弹粗条）

- **更新**：`portable_config/mpv.conf` 与自有层 `kumiplayer/mpv.conf` 均新增 `osd-bar=no`、`osd-on-seek=no`——滚轮/方向键调节进度时不再弹出 mpv 原生粗进度条，进度反馈统一由 uosc 时间轴（timeline）提供（参考旧整合包同款设置）。放在自有层保证用户替换整合包后仍生效。
- **依据**：KumiPlayer 用户体验需求（用户反馈滚轮调进度弹条影响体验）；MPV 官方手册 `--osd-bar`/`--osd-on-seek`；旧整合包参考（osd-bar=no、osd-on-seek=no）。
- **验证**：真实 mpv 烟测确认两选项加载生效零错误；MPV 相关定向测试 55 项全绿；自有层强制配置契约测试补充 osd 断言。

### 2026-08-12-r13 · MPV 文档全面治理（子智能体协作）

- **更新**：审查并完善全部 MPV 相关文档：① 重构 `MPV运行时演进记录.md`（删除 7 个重复标题、按时间倒序、22 条目保持原文）；② 重写 `third_party/mpv/README.md` 与 `docs/03 MPV README.md`（补 components-manifest/licenses/分层架构/查找入口）；③ 重写 `USER_GUIDE.md` 快捷键表（按实际 input.conf 38 条 + bindings.lua 6 条，修正"保留官方默认键位"错误表述）与设置页描述；④ 修正 `PROJECT.md` 根目录 mpv/ 定位与加载机制、`DISTRIBUTION.md` 安装目录树（补 kumiplayer/ 层）；⑤ 开源清单同步已接入状态与职责边界；⑥ `runtime.py` 的 `get_kumiplayer_mpv_plugins_dir` 改为指向自有层并同步测试。全部文档保持 UTF-8 无 BOM、LF。
- **依据**：子智能体审查报告（explore 逐文档对比实际代码/配置/清单）；MPV 分层架构 r12 后的实际状态。
- **验证**：11 个文档/代码文件编码体检全过（UTF-8 无 BOM、LF、零乱码）；后端 112 项测试全绿；真实 mpv 烟测自有层 5 脚本加载零错误。

### 2026-08-12-r12 · MPV 分层架构：可替换整合包层 + KumiPlayer 自有层分离

- **更新**：将 MPV 配置拆为两层：① `portable_config/`（可替换层）——用户可整体替换为第三方整合包，默认随应用分发 KumiPlayer 套件（第三方组件 uosc/thumbfast/stats/uosc_danmaku/Anime4K shaders + 通用播放键位）；② 新增 `kumiplayer/`（KumiPlayer 自有层，不可替换）——自有脚本（screenshot_to_video_dir/kumiplayer_anime4k/kumiplayer_uosc_menu）+ 新建 `kumiplayer_bindings.lua`（快捷键弱绑定：MBTN_RIGHT/TAB/`/F10/Alt+F10，Ctrl+v 用强绑定保证安全）+ `mpv.conf`（--include 追加强制配置）。后端 `build_mpv_playback_args` 追加 `--include` 与 `--script` 加载自有层；`check_configuration`/`_ensure_scripts_present` 校验改为自有层；默认套件删除自有脚本避免重复加载；uosc 菜单脚本加“uosc 未加载自动降级”保护。
- **依据**：KumiPlayer 用户需求（新手开箱即用 / 老手替换整合包 / 自有功能不受影响）；MPV 官方手册 `--config-dir`/`--script`/`--include`/`add_key_binding` 弱绑定与 forced 绑定优先级。
- **验证**：模拟整合包替换烟测——旧整合包作 portable_config 时自有层 4 脚本全部加载、校验通过、uosc 降级保护生效；默认套件烟测无重复加载零错误；后端 111 项测试全绿（含自有层校验新逻辑）。

### 2026-08-12-r11 · 修复弹幕拉取失败（官方 API 改版 + 服务器下线）

- **更新**：弹幕拉取全面修复（此前任何视频都搜不到弹幕）：① `api_server` 从弹弹play 直连（`api.dandanplay.net`，已 403）改为官方代理 `danmaku-api.152468.xyz`——uosc_danmaku 官方 Issue #397 证实旧密钥泄露作废，直连被拒，官方唯一推荐路径是代理；② `fallback_server` 从 `api.danmu.icu`（DNS 已删除、域名易主）改为官方 README 列出的 `dmku.hls.one`（实测可用）；③ `fetch_danmaku` 改为无参数优先、`format=json` 回退——官方代理不接受 format=json（400），旧直连/danmu_api 需要该参数。同步更新 `script-opts/uosc_danmaku.conf` 与脚本 `modules/options.lua` 默认值。
- **依据**：uosc_danmaku 官方 README/Issue #397/#391；danmu.icu 实测 DNS 无记录；dmku.hls.one 实测 200 返回真实弹幕；官方代理实测搜索 6 条/剧集 26 集/弹幕 1237 条。
- **验证**：真实 mpv 端到端（与脚本相同 curl 方式）搜索成功 + 弹幕 1237 条加载成功；脚本零加载错误；MPV 相关测试全绿；components-manifest 已同步。

### 2026-08-12-r10c · 修复右键菜单子菜单 id 冲突并平铺 Anime4K 模式/质量

- **更新**：`kumiplayer_uosc_menu.lua` 修复两个问题：① 画质调节子菜单与 Anime4K“选择质量”误用同一 `submenu:quality` id，导致 uosc 菜单内容错乱（画质调节显示 Anime4K 项、选择质量显示画质参数）——现拆分为 `submenu:image-quality` / `submenu:anime4k-modes` / `submenu:anime4k-quality`；② Anime4K 模式/质量直接平铺为主菜单项（不再嵌套 Anime4K 子菜单），悬停展开路径从三级缩短为两级；③ 重写时遗漏的 `cmd:` 事件分支已恢复（字幕/音轨菜单项此前不生效）。
- **依据**：KumiPlayer 用户反馈（悬停展开、平铺模式/质量、质量概念与 Anime4K 档位对齐）；uosc 菜单按 id 缓存子菜单的机制。
- **验证**：真实 mpv 注入菜单事件实测 `cmd:` 单命令/多命令均生效、`mode:` 正常；新增回归断言（不得复用 submenu:quality）；MPV 相关定向测试 54 项全绿；components-manifest 同步 SHA-256。

### 2026-08-12-r10b · 精简右键菜单（仅保留画质/速度/字幕/音轨/Anime4K）

- **更新**：`kumiplayer_uosc_menu.lua` 移除播放/暂停、上下集、全屏、截图、控制台、退出等基础控制项（这些已有快捷键与 uosc 控制条覆盖），右键菜单仅保留：速度预设、画质调节、字幕、音轨、Anime4K 五个子菜单。值前缀契约与入口消息名不变。
- **依据**：KumiPlayer 用户要求（菜单只留需要经常切换的项，避免与快捷键/控制条重复）。
- **验证**：真实 mpv 脚本加载零错误；MPV 相关定向测试 54 项全绿（含新增"已移除项不得出现"断言）；components-manifest 同步 SHA-256。

### 2026-08-11-r10 · 右键菜单扩展为 KumiPlayer 通用控制菜单

- **更新**：`kumiplayer_uosc_menu.lua` 从“仅 Anime4K 菜单”扩展为完整右键控制菜单：主菜单含播放/暂停、上下集、速度预设、画质调节（对比度/亮度/伽马/饱和度/色相，含当前值与 ±5 步进）、字幕/音轨操作、Anime4K 子菜单（保留原能力）、全屏/截图/控制台/退出。全部基于 mpv 内置命令与 uosc 菜单 API（方案 C：零新增脚本、零许可证风险）；value 前缀契约扩展 `speed:`/`quality:add:`/`quality:reset`/`cmd:`。入口消息名 `open-anime4k-menu` 与 input.conf 绑定保持不变。
- **依据**：KumiPlayer 用户需求（右键快速控制，参照旧整合包 contextmenu 但避免 UNLICENSED 脚本）；uosc 5.13.0 菜单 API（active/separator/submenu/callback）；MPV 官方手册命令接口。
- **验证**：真实 mpv 注入 `menu-event` 实测 `set speed 1.5` 生效（属性读取确认）；脚本零加载错误；新增扩展菜单契约测试，MPV 相关 54 项全绿；components-manifest 同步新 SHA-256。

### 2026-08-11-r9b · 配置注释与格式规范化

- **更新**：`input.conf` 改为旧整合包风格——键位与命令空格对齐、每行绑定尾随中文注释；`stats.conf` 键位说明从默认 `i/I` 修正为显式 `TAB`（与 input-builtin-bindings=no 后的实际绑定一致）；`mpv.conf` 接入能力清单补充中文化 stats 与完全自定义键位。全部配置文件保持 UTF-8 无 BOM、LF 换行。
- **依据**：KumiPlayer 配置可读性与编码纪律（AGENTS.md UTF-8 章节）。
- **验证**：12 个配置文件编码/换行全检零问题；真实 mpv 解析 44 binds 零错误；MPV 相关定向测试 53 项全绿。

### 2026-08-11-r9 · 恢复旧整合包播放控制与画质调节键位

- **更新**：`input.conf` 按旧整合包布局恢复播放控制键位（SPACE/MBTN_LEFT 暂停、方向键±3s、WHEEL 精确±3s、UP/DOWN 音量、f/F11/MBTN_MID/Alt+Enter 全屏、ALT+MBTN_LEFT 平移、ALT+WHEEL 缩放、Shift+方向键精确跳转、F1/F2 字幕延迟、F5/F6 音频延迟、F12 字幕显隐、BS 速度重置、ESC 退全屏）与画质调节键位（1-8 对比度/亮度/伽马/饱和度、9/0 色相）。F3/F4/F7/F8 改用 mpv 内置 `cycle sub`/`cycle audio`（替代旧 input_plus 脚本命令，不依赖插件）。未恢复 F9 高级播放列表（依赖 playlist_osd，未接入）。
- **依据**：KumiPlayer 用户指定快捷键布局（沿用旧整合包）；画质属性为 mpv 内置（--contrast/--brightness/--gamma/--saturation/--hue）；MPV 官方手册 INPUT.CONF。
- **验证**：真实 mpv 解析 44 binds 零错误；MPV 相关定向测试 53 项全绿；画质键位无需额外插件（mpv 内置属性）。

### 2026-08-11 · 设置页新增“打开 MPV 配置目录”入口

- **更新**：设置页“播放 → 内置播放器”卡片新增“打开 MPV 配置目录”按钮，点击在资源管理器中打开 KumiPlayer 内置 MPV 的 `portable_config`（快捷设置入口）。后端新增 `POST /api/config/mpv-runtime/open-config`，只打开 KumiPlayer 自有配置目录，不接受前端任意路径（安全边界）。
- **依据**：KumiPlayer 用户需求（快速编辑 input.conf/mpv.conf/script-opts 等播放配置）。
- **验证**：后端 71 项测试通过 + 端点 TestClient 验证返回正确路径；前端 `tsc -b`、契约测试 176 项、`vite build` 全过。

### 2026-08-10-r9 · 接入 uosc_danmaku 弹幕插件

- **更新**：接入 uosc_danmaku（MIT，v2.2.0，Tony15246/uosc_danmaku）弹幕插件至 `scripts/uosc_danmaku/`，支持弹幕搜索、自动加载、弹幕开关、样式调节、弹幕源管理等功能。配合 danmu_api（AGPL-3.0，huangxd-/danmu_api）作为弹幕数据源，用户自行部署后通过 `uosc_danmaku.conf` 配置 API 地址。具体变更：
  - `modules/options.lua`：默认 `api_server` 改为空（用户自行配置 danmu_api 地址），启用 `auto_load=yes`，`user_agent` 改为 `KumiPlayer/2.0`，弹幕开关键改为 `Alt+d`（避免与 mpv 默认 `j` 键字幕切换冲突）。
  - `apis/dandanplay.lua`：`fetch_danmaku` URL 添加 `format=json` 参数确保 danmu_api 返回 JSON；`set_episode_id`/`match_anime`/`match_file` 添加空 API 服务器检查并提示用户配置；`add_danmaku_source_online` extcomment URL 添加 `format=json`。
  - `script-opts/uosc_danmaku.conf`：新建 KumiPlayer 专用配置，含 danmu_api 地址配置说明和弹幕显示/加载默认值。
  - `script-opts/uosc.conf`：`controls` 新增 `button:danmaku`（搜索弹幕）、`cycle:toggle_on:show_danmaku@uosc_danmaku`（弹幕开关）、`button:danmaku_styles`（弹幕样式）。
  - `input.conf`：新增 `Ctrl+d`（搜索弹幕）和 `Alt+d`（弹幕开关）显式绑定。
- **依据**：KumiPlayer 弹幕功能需求；uosc_danmaku 官方文档与 danmu_api API 规范（兼容弹弹 Play `/api/v2/` 接口）。
- **验证**：未执行运行时烟测（需用户部署 danmu_api 后端到端验证）；配置文件语法与 uosc 控件格式检查通过。danmu_api 由用户自行部署，不在 KumiPlayer 安装包中分发。

### 2026-08-10-r8 · 切换为完全自定义键位方案（禁用内置键位）

- **更新**：`mpv.conf` 新增 `input-builtin-bindings=no` 禁用 mpv 二进制内置键位（方向键/空格/音量等硬编码绑定）；`input.conf` 改为显式绑定方案：保留 `Ctrl+v ignore`（安全屏蔽）与 `MBTN_RIGHT`（Anime4K 右键菜单），新增脚本功能显式绑定（`TAB` stats 中文版切换、`` ` `` 控制台、`F10`/`Alt+F10` 截图）；播放控制键位留空待用户设计。不使用 `--no-input-default-bindings`（会连脚本弱绑定一起屏蔽，导致 stats/console 键位失效）。
- **依据**：KumiPlayer 完全自定义键位需求；参考旧整合包方案（`input-builtin-bindings=no` + input.conf 显式声明）；MPV 官方手册 `--input-builtin-bindings` 与 `--input-default-bindings` 的区别。
- **验证**：真实 mpv 烟测确认 input.conf 解析 6 binds 零错误、所有脚本正常加载；MPV 相关定向测试 53 项全绿（含新契约：mpv.conf 禁用内置、input.conf 显式绑定脚本功能）。

### 2026-08-10-r7 · 补齐全部第三方组件许可证材料

- **更新**：`third_party/mpv/licenses/` 新增 `mpv-stats-zh/LICENSE.md`（MIT，Copyright 2026 yosh-wang）与 `mpv-v0.41.0/LICENSE.GPL`/`LICENSE.LGPL`（mpv 基础二进制许可证文本）；components-manifest 中 mpv-stats-zh 补上 notices 指向；清单文档新增 stats 中文版登记条目；runtime-manifest 配置版本升至 r7。
- **依据**：KumiPlayer 开源准备需求；MPV 官方仓库 v0.41.0 的 LICENSE.GPL/LGPL 与 stats 中文版仓库 LICENSE.md。
- **验证**：三个既有许可证文件（anime4k MIT / thumbfast MPL-2.0 / uosc LGPL-2.1）由子智能体验证完整无截断；三个新增文件关键字与长度检查通过；两个 manifest JSON 有效；MPV 相关测试全绿。mpv-stats-zh 的 README 与 LICENSE 矛盾仍在发布前复核清单。

### 2026-08-10-r6 · 接入 mpv-stats.lua 中文翻译版统计页面

- **更新**：接入 yosh-wang/mpv-stats.lua-zh-chinese-translation-（main@2026-07-12）中文翻译版 `stats.lua` 至 `scripts/`，新增 `script-opts/stats.conf` 说明；`mpv.conf` 新增 `load-stats-overlay=no` 禁用内置英文 stats 脚本（同名脚本会被 mpv 重命名为 stats2 造成重复页面与键位冲突）。`i`/`I` 键位保持 mpv 默认（display-stats / display-stats-toggle）。
- **依据**：KumiPlayer 中文界面需求；MPV 官方手册 `--load-stats-overlay` 与脚本同名加载行为。
- **验证**：真实 mpv 烟测确认 stats 以正确名称加载、读取 stats.conf、14 个脚本零加载错误；`av://lavfi:sine` 播放 30 帧无 Lua 运行时错误。仓库 README 与 LICENSE.md 许可证矛盾已在 components-manifest 标注，发布前必须重新核对。

### 2026-08-10-r5 · 建立快捷键骨架并屏蔽剪贴板加载风险

- **更新**：重写 `input.conf` 为结构化骨架：保留 `MBTN_RIGHT`（Anime4K 右键菜单）与 mpv 官方默认键位（不重复声明），新增 `Ctrl+v ignore` 屏蔽 mpv 默认的剪贴板加载（`loadfile ${clipboard/text} append-play`）——该绑定会绕过 KumiPlayer 受控播放队列并可能播放任意外部文件；按功能分区补充可读注释（播放/跳转/音量/速度/窗口/字幕音轨/截图/OSD/队列/退出）。未新增任何功能快捷键。
- **依据**：KumiPlayer 受控队列安全需求；MPV 官方手册 INPUT.CONF（`ignore` 完全解绑）。
- **验证**：新增 `test_mpv_input_conf.py` 契约测试（MBTN_RIGHT 存在、`Ctrl+v` 被 ignore、无 loadfile/loadlist/playlist-remove）；真实 mpv `--input-test` 配置加载无解析错误；MPV 相关定向 pytest 通过。未打包、未运行全量测试。

### 2026-08-10 · 精简 Anime4K 右键菜单并改善起播基础配置

- **更新**：Anime4K 右键菜单只保留“选择模式”和“选择质量”两级，模式显示官方 `Anime4K Mode A/B/C/A+A/B+B/C+A` 原名与当前勾选；移除说明页、长提示与倍率提示。右键“关闭”改为仅当前视频的临时 off，不再回退并重新启用永久默认模式。uosc 时间轴改为全屏也可见的高对比条形；启用 `hwdec=auto-safe`，不可用时由 MPV 自动回退软件解码。
- **依据**：KumiPlayer 用户体验需求；MPV 官方手册的 `hwdec=auto-safe` 回退行为与 uosc 官方配置项。旧整合包仅用作时间轴样式参考，未复制其菜单脚本或危险功能。
- **验证**：`test_mpv_anime4k_menu.py`、Anime4K 链映射、设置与启动参数测试通过；内置 MPV 使用自有 `--config-dir` 的无媒体加载烟测退出码 0。仍需在桌面应用中人工确认本地视频和 `.strm` 的实际首帧耗时、进度条可读性与右键交互。

### 2026-08-10 · 接入受控 uosc、thumbfast 与 Anime4K 播放器增强

- **更新**：
  - 接入 uosc 5.13.0（LGPL-2.1）官方脚本组与字体，提供现代 OSD/时间轴/右键菜单能力；屏蔽文件/URL/队列/自更新入口（`uosc.conf` 受控配置）。
  - 接入 thumbfast（MPL-2.0，commit 0f711de）时间轴缩略图；缓存目录固定到 `data/mpv-state/thumbfast/`，`network=no`、`audio=no`。
  - 接入 Anime4K v4.0.1（MIT）官方多 shader 链：`kumiplayer_anime4k.lua` 控制器（6 模式 × 3 质量），`kumiplayer_uosc_menu.lua` 右键菜单（模式/质量/说明面板、悬停提示、增强模式 2x 提示）。
  - 新增“播放器调节”页面（Anime4K 永久默认值）与后端配置字段 `mpv_anime4k_mode/quality`，启动后经 IPC `set-default` 同步。
  - 加固 MPV 受控队列进度映射：带路径事件必须匹配受控队列才写入，未知外部路径忽略。
  - 新增 `third_party/mpv/components-manifest.json` 与 `licenses/`：组件来源、commit、SHA-256、许可证、随包文件与 NOTICE。
- **依据**：KumiPlayer 自有需求 + 施工说明（基于 uosc/thumbfast/Anime4K 官方文档与官方模板）；MPV 官方手册 `--config-dir`/`script-opts`/`change-list glsl-shaders` 行为。
- **验证**：真实内置 MPV 烟测确认四个脚本零加载错误、Anime4K 默认 off/balanced 生效；后端 `-k "mpv or playback or runtime or anime4k or config"` 全绿（唯一失败为并行窗口 CSS 契约漂移，与本任务无关）；前端 `tsc -b`、`vite build`、`test:contracts 149`、`test:components 48` 全过。

### 2026-08-10-r4 · 修复 Anime4K 右键菜单点击无响应（uosc 回调模式）

- **更新**：`scripts/kumiplayer_uosc_menu.lua` 改用 uosc 回调模式——菜单 JSON 顶层
  携带 `callback = { script_name, "menu-event" }` 后，uosc 把菜单事件（activate 等）
  发回本脚本；此前菜单项只带 `callback` 字段、value 为 `mode:a` 这类标识，而 uosc
  简单模式会把 value 当作 mpv 命令执行，导致点击任何选项都无反应。现在 menu-event
  按 value 前缀（`mode:`/`quality:`）转发 `set-session`，激活后关闭菜单。
- **依据**：uosc 5.13.0 `lib/menus.lua` 的 open_command_menu——简单模式下
  `run_command(event.value)` 执行菜单项 value；JSON 顶层携带 callback 数组才进入
  回调模式（`script-message-to <callback> <event-json>`）。
- **验证**：新增静态契约测试（回调模式、value 前缀、set-session 转发）与真实 mpv
  冒烟测试（测试辅助脚本在 file-loaded 时注入 menu-event，日志出现
  `applied mode=b`）；MPV 相关 53 项全部通过。`components-manifest.json` 已同步
  `kumiplayer_uosc_menu.lua` 的 SHA-256。未做真实窗口右键交互验证。

### 2026-08-10-r3 · 修复 uosc 窗口时间线与 Anime4K 菜单消息契约

- **更新**：
  - `script-opts/uosc.conf`：`scale=0` 改为 `scale=1`。窗口模式的时间线尺寸与鼠标命中区都乘以该系数，`scale=0` 会让底部进度条不可见且无法拖动（全屏走 `scale_fullscreen=1` 不受影响）；新增 `languages=zh-hans,en` 使 uosc 原生菜单（字幕/音轨/章节/播放列表等）使用简体中文。
  - `scripts/kumiplayer_uosc_menu.lua`：状态监听消息名由 `anime4k-state` 统一为 `kumiplayer_anime4k-state`，与 `kumiplayer_anime4k.lua` 的实际广播名一致；此前名称不一致导致右键菜单永远等不到状态回调。
  - 保留 `timeline_cache=yes`（uosc 原生缓存区段显示，网络播放时生效）与 `progress=always`。
- **依据**：uosc 5.13.0 配置说明（scale 为窗口模式缩放系数，languages 支持 zh-hans）；MPV `demuxer-cache-state` 由 uosc 原生读取。
- **验证**：新增回归测试 `test_mpv_anime4k_menu.py::test_anime4k_state_message_name_matches_broadcast`（发送/监听消息名必须一致，故意改错任一端即失败）与 `test_uosc_window_scale_and_language`；`backend/tests/test_mpv_anime4k_menu.py` 10 项全部通过。`components-manifest.json` 已更新 `uosc.conf` 与 `kumiplayer_uosc_menu.lua` 的 SHA-256；`runtime-manifest.json` 配置版本升至 `mpv-runtime-config-2026-08-10-r3`。未运行真实窗口交互验证与打包。

### 2026-08-09 · 对齐内置 MPV 的构建提示与配置说明

- **更新**：修正 `build_installer.bat` 对内置 MPV 运行时的构建和安装提示；更新 `mpv.conf`、`input.conf`，明确它们已由 `--config-dir` 接入播放链路；同步 `runtime-manifest.json` 的配置版本号。
- **依据**：MPV 官方手册的 `--config-dir` 行为与 KumiPlayer 自有内置运行时需求。
- **验证**：`uv run --project backend python -m pytest -q backend/tests/test_mpv_kumiplayer_integration.py backend/tests/test_runtime_environment.py backend/tests/test_release_packaging.py` 退出码 0，全部通过；未构建 EXE 或安装包。

### 2026-08-04 · 修复内置 MPV --cache-dir 不受支持导致所有播放失败

- **更新**：
  - `build_mpv_playback_args` 不再生成 `--cache-dir=<data/mpv-state/cache>`，改为生成 `--demuxer-cache-dir=<data/mpv-state/cache>`（MPV 官方手册支持的 demuxer 磁盘缓存目录选项，语义一致）。
  - 构造启动参数时防御性创建 `data/mpv-state`、`cache`、`watch_later` 目录，避免目录缺失导致缓存写入异常。
  - 新增真实二进制冒烟测试：用完整启动参数播放 `av://lavfi:sine`（`--force-window=no --frames=1 --no-terminal`），断言退出码为 0，防止再次出现“参数构造通过但真实二进制不接受”的回归。
- **依据**：MPV 官方手册选项列表（`mpv --list-options` 中无 `--cache-dir`，仅有 `--demuxer-cache-dir` 等）；根因实测：内置 v0.41.0 二进制收到 `--cache-dir` 报 `Error parsing option cache-dir (option not found)` 并以退出码 1 终止。
- **验证**：冒烟测试以真实 `third_party/mpv/runtime/mpv.exe` 完整参数播放测试源，退出码 0；后端定向测试 `test_mpv_kumiplayer_integration.py` 与 `test_playback_service.py` 全部通过。

### 2026-08-02 · 接入内置 MPV v0.41.0 并改为默认播放器

- **更新**：
  - 接入干净 MPV v0.41.0（x86_64-w64-mingw32），运行时位于 `third_party/mpv/runtime/`，清单见 `third_party/mpv/runtime-manifest.json`。
  - 播放默认使用内置 MPV，通过 `--config-dir` 只加载 KumiPlayer 自有配置，不读取用户全局或旧整合包配置。
  - 可写状态（cache / log / watch-later）隔离到 `data/mpv-state`（源码模式）。
  - 首次引导不再要求用户选择 MPV，改为自动检测内置播放器；设置页展示版本、架构与完整性。
  - 截图脚本由配置目录 `scripts/` 自动加载，不再 `--scripts-append`；旧 `resources/mpv-plugins/` 已移除。
  - 暂存器与安装包验证在 `development-only` 状态拦截正式发布。
- **依据**：MPV 官方手册 `--config-dir` / `--no-config` 行为（https://mpv.io/manual/stable/#program-behavior）。
- **验证**：27 个运行文件 SHA-256 与清单一致；`mpv.com --version` 成功；`--config-dir` 实测重定向 home 路径并自动加载 `scripts/`；后端路径解析、健康检查与播放参数针对性测试通过；TypeScript/Vite 构建通过。未运行全量后端测试、未构建 EXE 或安装包。

### 2026-08-01 · 建立 MPV 运行时骨架

- **更新**：建立配置源、第三方二进制和打包暂存三层目录；创建空白 `mpv.conf`、`input.conf` 和 `script-opts`；将 KumiPlayer 自有截图脚本复制到新的正式配置源。尚未接入 MPV 二进制。
- **依据**：KumiPlayer 自有运行时治理需求。
- **验证**：目录和 Schema 可读取；新旧截图脚本字节一致，编码与换行符合要求；未运行播放、构建或打包验证。

### 2026-09-16 · 修复右键菜单打不开 + 关闭暂停指示器

- **更新**：
  - `kumiplayer_uosc_menu.lua` 的 uosc 可用性探测不再使用 `script-names`：该属性在 mpv v0.41.0 的属性列表里**不存在**（`mpv --list-properties` 只列出 `scripts` / `input-bindings` / `script-opts`），读取结果恒为 nil，导致 `check_uosc_available()` 永远返回 false、右键请求被无条件忽略。改为双通道探测：`input-bindings` 里 `owner == "uosc"` 的同步判断，加上监听 uosc 主动广播的 `uosc-version`；并且**不缓存失败结果**（右键可能早于 uosc 注册键位，缓存 false 会让整个会话都打不开菜单）。
  - `script-opts/uosc.conf` 新增 `pause_indicator=no`：关闭 uosc 默认（`flash`）在暂停/恢复时于画面中央闪现的大号播放/暂停图标。
  - `third_party/mpv/components-manifest.json` 同步这两个文件的新 SHA-256；`third_party/mpv/runtime-manifest.json` 配置版本 `r14` → `r15` 并追加说明。
- **依据**：本机 `mpv --list-properties` 实测属性清单；uosc 5.13.0 源码 `scripts/uosc/main.lua`（`pause_indicator` 默认 `'flash'`）与 `elements/PauseIndicator.lua`。
- **验证**：真实内置 mpv 探针（走生产参数构造、无窗口）修复前后对比——修复前日志出现 `uosc 未加载，忽略右键菜单请求` 且无菜单动作，修复后该行 0 次并实际发出 `script-message-to uosc open-menu {...}`；新增真机回归用例（旧代码下失败、修复后通过，已做负控制）与 uosc.conf 契约断言；`backend/tests/test_mpv_kumiplayer_integration.py` 全绿。未运行全量后端测试、未构建 EXE 或安装包。

### 2026-09-21 · 以内置配置审校补强字幕、网络流与受控队列边界

- **更新**：以第三方整合包（MPVlite）作为对照样本审校 KumiPlayer 自有 MPV 配置，按 mpv 官方选项补齐有实际收益的项，不照抄其排布与个人化设置：
  - `portable_config/mpv.conf` 新增字幕策略：`sub-auto=fuzzy`（外挂字幕常带发布组与语言后缀，默认 `exact` 只认完全同名文件会漏挂）、`slang=sc,chs,zh-cn,zh-hans,tc,cht,zh-hant,zh-hk,zh-mo,zh-tw,jp,ja,en`（简体优先，避免自动选中评论轨或纯日文字幕）、`subs-with-matching-audio=no`（默认 `yes` 在音轨语言与字幕语言匹配时自动关字幕，中配/原声双轨时表现为“字幕莫名不显示”）。
  - `portable_config/mpv.conf` 新增 `cache=yes`：KumiPlayer 素材主要来自网盘/OpenList，播放定位符可能是映射盘（`K:\` 等）或 http(s) 流；`cache=auto` 是否启用取决于 mpv 对文件系统的判定，属不确定行为，强制启用可保证高延迟挂载盘仍有预读。**刻意不抬高 `demuxer-max-bytes`**：默认 150 MiB 配合 `cache-secs`（默认上限 1 小时）对 1080p 动画已相当于上百秒缓冲，放大到 GB 级只增加内存占用。
  - `kumiplayer/mpv.conf`（强制层）新增 `directory-mode=ignore`：禁止 mpv 把目录参数展开为播放列表，避免绕过季度受控队列导致进度按路径回写失配；与 `kumiplayer_bindings.lua` 的 `Ctrl+v` 强制屏蔽、启动参数 `--autocreate-playlist=no` 构成三层防护。
  - `portable_config/script-opts/uosc.conf` 新增 `chapter_range_patterns`：在 uosc 内置英文模式与默认日文模式之外补充中文写法（片头/片头曲、片尾/片尾曲），用于时间线片头片尾着色。
  - 同文件既有的 `border=no` / `window-dragging=yes` / `window-corners=round`（2026-09-17 起已存在但此前未记录）本次一并登记。
  - **未采纳**的整合包项与原因：`save-position-on-quit=yes`、`autocreate-playlist=same` 与受控进度回写/受控队列冲突；`idle=yes` 会在播放结束后残留窗口；`volume-max=100`、`keepaspect-window=no`、`hr-seek-framedrop=no`、`image-subs-hdr-peak=video` 属个人偏好且收益不明确；`screenshot-*` 已由自有截图脚本在运行时设定；`demuxer-max-bytes=1000MiB` 见上。整合包 `thumbfast.conf` 的 `tnpath`/`sw_threads`/`binpath`/`min_duration`/`precise`/`quality`/`frequency` 在我们捆绑的 thumbfast（commit 0f711de）中并不存在，照抄会触发 unknown key 且静默无效。
- **依据**：MPV 官方手册对应选项（`--sub-auto` / `--slang` / `--subs-with-matching-audio` / `--cache` / `--directory-mode`）与本机 mpv v0.41.0 真实选项表；uosc 5.13.0 选项表与 `lib/utils.lua` 的章节范围识别逻辑（英文模式为内置，日文模式为默认配置）；KumiPlayer 受控队列与网盘播放需求。
- **验证**：脚本化逐项核对全部配置——两层 `mpv.conf` 的 8+5 个核心选项、`uosc.conf` 44 键、`thumbfast.conf` 键、`uosc_danmaku.conf` 23 键均存在于对应真实选项表（零非法项）；用 `build_mpv_playback_args` 的生产参数喂给真实 mpv v0.41.0 读取生效值，确认 `sub-auto=fuzzy`、完整 `slang`、`subs-with-matching-audio=no`、`cache=yes`、`directory-mode=ignore` 均生效，`demuxer-max-bytes` 保持默认 150 MiB；mpv 日志无选项解析错误、uosc 无 unknown key 警告；三个改动文件保持 UTF-8 无 BOM + LF。**未做**真机窗口播放与真实网盘挂载盘实测，未改动任何脚本与二进制。

### 2026-09-21 · 修复 Anime4K 永久默认值与 thumbfast 缓存目录注入失效

- **更新**：修复「播放器调节页保存的 Anime4K 默认效果不生效」，根因是两处**互相独立**的缺陷：
  - `backend/app/playback/mpv_runtime.py`：三条注入由点号形式（`--script-opt=thumbfast.thumbnail=`、`kumiplayer_anime4k.default_mode=`、`kumiplayer_anime4k.default_quality=`）改为短横形式 `thumbfast-thumbnail` / `kumiplayer_anime4k-default_mode` / `kumiplayer_anime4k-default_quality`。mpv 官方 `player/lua/options.lua` 用 `identifier.."-"` 匹配键名，点号形式被**静默忽略**。
  - `resources/mpv-runtime/kumiplayer/scripts/kumiplayer_anime4k.lua`：`read_injected_defaults()` 由 `mp.get_opt("default_mode")` 改为 `mp.options.read_options(options, "kumiplayer_anime4k")`。`mp.get_opt` 是**全键直查**（`defaults.lua` 中实现为 `opts[key]`），裸键恒为 nil——即使命令行键名修正，脚本侧仍读不到值。顺带让该脚本真正读取 `script-opts/kumiplayer_anime4k.conf`（此前该配置文件对脚本完全无效）。
  - 回归防护：新增 `test_script_opt_keys_must_use_dash_prefix_not_dot`（键名不得含点号）与 `test_anime4k_lua_reads_defaults_through_mp_options`（禁止 `mp.get_opt`、必须 `read_options`，断言前先剥离 Lua 注释）；原两条把点号形式写死的断言改为短横形式。
- **依据**：mpv 官方 v0.41.0 源码 `player/lua/options.lua`（`local prefix = identifier.."-"`，读取属性 `options/script-opts`）与 `player/lua/defaults.lua`（`mp.get_opt(key)` 即 `opts[key]`）；MPV 官方手册 `--script-opts` / `--script-opt` 的覆盖与追加语义；本机真实 mpv v0.41.0 实测。
- **验证**：真实 mpv v0.41.0 + `build_mpv_playback_args` 生产参数——修复前脚本报 `default mode=off`（而用户实配为 `c+a`），修复后为 `default mode=c+a quality=balanced`；加载真实视频源后日志出现 `applied mode=c+a quality=balanced shaders=6` 且 `applied=true`；追加 `--script-opt=kumiplayer_anime4k-default_mode=b` 能正确覆盖为 b（验证命令行优先级高于配置文件）。`backend/tests -k mpv` 84 项全绿。**未做**真机窗口播放，未构建 EXE/安装包。
- **附带修复**：thumbfast 的缩略图缓存目录同因点号形式失效（实际回退到 `%TEMP%\thumbfast.out<pid>`，而非 `data/mpv-state/thumbfast`），本次一并恢复。
- **遗留（未改代码）**：`_sync_anime4k_default_to_active_mpv`（`backend/app/api/config.py`）仍是空实现，`send_mpv_script_message`（`backend/app/playback/mpv_ipc.py`）全仓无调用者；因此在**已经运行的** MPV 会话中修改默认值不会影响该会话的下一集，只对之后新启动的播放器进程生效（KumiPlayer 每次播放都会重启 MPV 进程，用户可见行为正确）。

### 2026-09-22 · 修复三项播放回归：快捷键缺失、窗口 1px 白边、卡顿（cache 回退）

- **更新**：针对上一轮改动后的真机反馈逐项定位并修复：
  - **很多快捷键失效** —— `portable_config/input.conf` 由 38 条补到 58 条，新增旧整合包同款的**纯 mpv 内置命令**键位：`,`/`.` 逐帧、`[`/`]` 播放列表上/下一个、`-`/`=` 倍速、`CTRL+SHIFT+←/→` 章节、`CTRL+↑/↓` 字幕位移、`SHIFT+↑/↓` 字幕缩放、`SHIFT+F1/F2` 次字幕延迟、`A` 音调修正、`B` 去色带、`F` 保持宽高比、`H` HDR 直通、`L` AB 循环、`m` 静音。根因：`mpv.conf` 早已设 `input-builtin-bindings=no`（mpv 内置单键绑定全部关闭），**凡未在 `input.conf` 显式声明的单键都无反应**；整合包有 92 条而本层只有 38 条。实测绑定总数 60 → 80。
  - **画面四周 1px 白边** —— 根因是 Windows 11 的 DWM 给「无 `WS_CAPTION` 但有 `WS_THICKFRAME`」的窗口绘制可见边框（实测窗口样式 `0x140E0000`、`DWMWA_VISIBLE_FRAME_BORDER_THICKNESS == 1`），`--window-corners=donotround` 无法消除（实测仍为 1）。新增 `_strip_dwm_border()`（`backend/app/playback/mpv.py`），在既有窗口线程里对 mpv 窗口调用 `DwmSetWindowAttribute(DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)`。同时关闭 uosc 自己那圈 1px 边框（`script-opts/uosc.conf` 新增 `window_border_size=0`；uosc 该边框默认为纯黑，见 `elements/WindowBorder.lua`）。
  - **播放卡顿** —— 回退上一轮擅自新增的 `cache=yes`，保持 mpv 默认 `cache=auto`。针对性实测（1080p 素材 + Anime4K Mode C+A + `hwdec=auto-safe`，通过 IPC 读 `frame-drop-count`/`estimated-vf-fps`）三种组合**均为 0 丢帧且无差异**，说明卡顿与 Anime4K 负载、硬解方式无关；`cache=yes` 也拿不出收益证据，属未经实测的猜测性改动，故回退，改为「按实测再调」。
  - **顺带修复**：`kumiplayer_anime4k.lua` 的基础 shader 列表原本在每次 `file-loaded` 重新记录，第二次起会把本脚本上一轮追加的 Anime4K 链当成"基础"，导致切到关闭后链残留（画面仍被处理）；改为脚本加载时**只记录一次**，并用 `applied_key` 避免同一条链在每次切集重复 `clr`+`append`。
  - **顺带移除**：`_allow_foreground_activation()` 中模拟 Alt 键的 `keybd_event` 手法——Vista 起 Windows 只认真实输入，实测无法解除前台锁定，还会把一个 Alt 事件丢给当时的前台窗口。
- **依据**：MPV 官方手册（`--input-builtin-bindings`、`--cache`、`--border`、`--window-corners`）；本机 mpv v0.41.0 实测（`input-bindings` 全量导出、窗口样式与 DWM 属性读取、丢帧计数 IPC 采样）；整合包 `input.conf` 键位对照。
- **验证**：绑定审计共 80 条，20 个新增键位全部注册（`CTRL+SHIFT+←` 被 mpv 规范化为 `Shift+Ctrl+LEFT`，非缺失）；真实 mpv 加载生产参数无选项/脚本错误，Anime4K 仍为 `applied mode=c+a quality=balanced shaders=6`、`applied=true`；`DwmSetWindowAttribute` 返回 **S_OK**（该属性本机构建不支持 GET，返回 E_INVALIDARG，故白边需真机肉眼确认）；`backend/tests -k mpv` 84 项全绿。**未做**真机窗口播放与挂载盘实测、未构建 EXE/安装包。
- **已知未解**：mpv 由后端（非前台进程）启动时无法成为系统前台窗口——实测调用现有聚焦逻辑后 `is_foreground` 仍为 false，键盘输入要等第一次点击才生效。修法需在 Tauri 侧（前台进程）做 `AllowSetForegroundWindow` / `SetForegroundWindow` 交接，本轮未改。

### 2026-09-22 · 修复字幕不自动读取（slang 命中原声语言导致一个字幕都不选）

- **更新**：`portable_config/mpv.conf` 的 `slang` 由 `sc,chs,zh-cn,zh-hans,tc,cht,zh-hant,zh-hk,zh-mo,zh-tw,jp,ja,en` 改为 `zh,zh-cn,zh-hans,zh-hant,zh-tw,zh-hk,sc,tc,chs,cht,en`——**去掉 ja/jp**，并在配置里写明原因。
- **根因（实测）**：`slang` 里一旦包含**音轨语言**（动画原声是日语）而字幕里又没有该语言，mpv 会**一条字幕都不选**（`sid=no`），表现为"字幕不能正常自动读取"。用真实素材（VCB 版《房间露营》1080p，外挂 `.sc.ass`/`.tc.ass`）逐项测量：
  - `slang=sc,chs,zh-cn,zh-hans,tc,cht` → 选中 1 条 ✔
  - `slang=zh,sc,tc` / `slang=zh-hk,zh-mo` / `slang=zh-hant,zh-hk,zh-mo,zh-tw` → 选中 1 条 ✔
  - `slang=ja`（音轨是日语、无日语字幕）→ **选中 0 条** ✘
  - `slang=jp,ja,en` / 含 `ja` 的任意列表（包括改前的原值）→ **选中 0 条** ✘
  - `slang=en`（无英语字幕、音轨非英语）→ 选中 1 条 ✔（说明触发条件是"命中原声语言"，不是"列表里有不存在的语言"）
  - 注意 `sub-auto=fuzzy` 本身是好的：三种 `slang` 下都能**找到** 2 条外挂字幕，坏的只是"选中"环节。
- **依据**：MPV 官方手册 `--slang`（语言优先级）与 `--sid=auto`（按语言自动选轨）；`--sub-auto=fuzzy`；本机真实素材实测（`track-list` 的 `lang`/`selected` 与 `sid` 属性）。
- **验证**：改用新 `slang` 后，同一目录 **3 集**（第 01/05/SP 集）实测均为 `sid=1`、`SUB[1] lang=sc selected=true`、`SUB-COUNT=2 SELECTED=1`；`sub-auto=fuzzy` / `subs-with-matching-audio=no` 保持原值不变。**未做**真机窗口内肉眼确认。
- **已澄清的两项（无需改动）**：
  - 鼠标中键全屏：用户确认是当时处于全屏导致的观感混淆，键位本身正常（绑定审计中 `MBTN_MID cycle fullscreen` 注册在 `input.conf` 归属下）。
  - uosc 右上角窗口按钮：uosc 这几个按钮直接发 mpv 命令（`elements/TopBar.lua`：`cycle window-maximized` / `cycle window-minimized` / `quit`）。用生产参数 + IPC 实测：`cycle window-maximized` → `window-maximized=true`，再切回 `false`；`cycle fullscreen` → `true`；`set fullscreen no` → `false`；`cycle window-minimized` → `true`——在当前 `border=no` + `window-corners=round` 下全部生效，配置层无问题。

### 2026-09-22 · 对照 MPVlite 整合包优化交互手感（网盘读缓冲 / 窗口缩放 / 缩略图后端）

- **更新**：以 `D:\03_ACGN\MPVlite\mpv` 为对照样本，逐项比对核心配置与 uosc/thumbfast 选项后，采纳其中**与交互手感直接相关**的取值：
  - `portable_config/mpv.conf` 新增网盘/挂载盘读缓冲四件套：`cache=yes`、`demuxer-readahead-secs=5`、`demuxer-max-bytes=1000MiB`、`demuxer-max-back-bytes=100MiB`。
  - `portable_config/mpv.conf` 新增 `keepaspect-window=no`：无边框窗口（`border=no`）缩放宽高时不再吸附视频宽高比，拖拽边缘不会"一格一格跳"。
  - `script-opts/thumbfast.conf`：`spawn_first` 由 `no` 改为 `yes`——悬停时间轴预览不再等后端进程启动；同时**清掉该文件里重复声明的 `quit_after_inactivity`**（同一键写了两遍）。
- **依据与判断**：MPV 官方手册 `--cache` / `--demuxer-readahead-secs` / `--demuxer-max-bytes` / `--demuxer-max-back-bytes` / `--keepaspect-window`；thumbfast 同版本（commit 0f711de）的 `spawn_first` 选项；整合包同款取值作为对照。
  **更正 2026-09-21 的结论**：当时以 1080p **合成素材**测出"三种组合均 0 丢帧、`cache=yes` 无收益"并据此回退——该测例是本地/合成素材，**缓冲字节上限根本不会触发**，因此对高延迟挂载盘无效。对挂载盘而言，后向缓冲字节数正是"往回拖能否离线重放"的决定因素。本轮按整合包取值重新采用。
- **验证**：真实 mpv v0.41.0 + 生产参数读取生效值——`cache=yes`、`cache-secs=3600000`、`demuxer-readahead-secs=5`、`demuxer-max-bytes=1048576000`、`demuxer-max-back-bytes=104857600`、`keepaspect-window=no` 全部生效；thumbfast 解析出 `spawn_first=yes`、`quit_after_inactivity=0`（不再重复）、`thumbnail` 仍为后端注入的 `data/mpv-state/thumbfast`；全量选项复检零非法项、日志无选项/脚本错误。**未验证**：本轮作业时 `K:\` 未挂载，**未能在真实挂载盘上实测读缓冲收益**；也未做真机肉眼手感确认。
- **对照后明确不采纳**（含原因）：`idle=yes`（会在播放结束后残留窗口，与受控队列/自动退出冲突）；`save-position-on-quit=yes`、`autocreate-playlist=same`（与 IPC 进度回写、季度受控队列冲突）；`screenshot-directory`（写死个人路径，且我们的自有截图脚本已在运行时设定格式/质量/模板）；`refine=text_width,sorting`（uosc 明确标注为"用性能换精度"，对我们只有成本）；`use_trash=yes` / `show_hidden_files=yes` / `default_directory={drives}` / `load_types` 等（服务于 uosc 文件浏览菜单，我们刻意不暴露该入口，且 `use_trash` 触碰数据保护红线）；`thumbnail.tnpath` / `sw_threads` / `binpath` / `min_duration` / `precise` / `quality` / `frequency`（**我们捆绑的 thumbfast（commit 0f711de）没有这些键**，照抄会刷 unknown key 且静默无效）；`volume-max=100`、`image-subs-hdr-peak=video`、`osd-duration=1000`、`sid=auto`（前者是限制、后三者与默认值等价或无收益）。

### 2026-09-28 · 右键菜单命令空间与 Anime4K 应用状态（F-012 / F-013）

- **更新**：
  - `config/kumiplayer/scripts/kumiplayer_uosc_menu.lua`：`menu-event` 分发顺序改为先处理更具体的 `quality:reset`、`quality:add:<prop>:<delta>`，再处理 `quality:<light|balanced|high>` 枚举；`mode:` 只接受 `MODES` 里的合法值；新增取值白名单（`VALID_MODE_VALUES` / `VALID_QUALITY_VALUES` / `VALID_SPEED_VALUES` / `QUALITY_PROP` / `VALID_DELTA_VALUES`），`DELTA_STEPS = {"-5","5"}` 同时驱动菜单项与校验；非法输入不再下发并给出 2 秒有界 OSD 提示；Anime4K 请求改走统一入口 `request_anime4k()` 并携带请求 id，本地 `state` 只在状态回执到达时写入（不再乐观写入），失败回执保留旧状态。
  - `config/kumiplayer/scripts/kumiplayer_anime4k.lua`：`applied` 的语义收紧为「已安装且 `glsl-shaders` 读回一致」——应用前检查每个 shader 文件存在（`utils.file_info`），应用后读回列表比对，缺文件或列表不一致时不报 `applied` 并回滚到基础列表；`set-session` / `clear-session` / `set-default` / `get-state` 支持可选 `request_id`，并统一通过 `broadcast_state()` 回执 `applied` / `ok` / `reason`；`get-state` 的原有广播名与参数顺序保持不变（新增参数追加在末尾）。
- **依据**：用户报告的"画面调节点了没效果、随后切 Anime4K 模式也被拒绝"；静态追踪确认 `value:sub(1, 8) == "quality:"` 会命中 `quality:add:*` 与 `quality:reset`，把 `add:brightness:5` 当质量枚举发给 Anime4K（被拒绝）并把非法值写进菜单 `state.quality`，后续 `mode:` 请求继续携带污染值。Anime4K 官方 v4 链与质量档位映射未改动。
- **验证**：隔离探针 `.context/tasks/media-player-audit-2026-09-27/probe_menu_lua.py`（内置 MPV 的 Lua 引擎 + 记录型 `mp` 替身，`--no-config --load-scripts=no --vo=null --ao=null`，不加载媒体）24 项断言全部通过：画面调节命中 mpv 属性且不到达 Anime4K、reset 只归零画面属性、合法枚举带请求 id、请求发出后本地状态不变且成功回执才生效、拒绝回执有界提示、非法枚举/非法 delta/未知属性/非法速度不下发、合法链读回一致才报 `applied`（c/balanced = 5 个 shader）、同链重复请求不重挂、列表读回不一致与缺文件均报失败并回滚基础列表、`clear-session` 恢复基础列表、Anime4K 拒绝非法枚举并带回执。同探针在加载阶段曾抓到 `kumiplayer_uosc_menu.lua` 的一处 `pm.` 拼写错误（会让整个菜单脚本加载失败），已修复并新增 `backend/tests/test_mpv_anime4k_menu.py::test_lua_scripts_load_in_isolated_mpv` 作为回归。**未验证**：真机播放中点按菜单的端到端体验与 Anime4K 实际渲染帧预算（见 F-011/F-012 取证门）。

### 2026-09-28 · 内置初始窗口尺寸与缩略图后端绝对路径（F-008 / F-009）

- **更新**：
  - `backend/app/playback/mpv_runtime.py`（内置模式专用，外部整合包 argv 完全不变）新增四项启动参数：`--autofit=70%x60%`、`--geometry=50%:50%`、`--auto-window-resize=no`、`--fullscreen=no`。目的是让每次新建内置播放器窗口都按显示器工作区的中等尺寸、居中、非全屏启动，不沿用上一次窗口的尺寸/位置；同一窗口切集不自动改尺寸；用户手动 resize/maximize/fullscreen 仍可覆盖本次会话。
  - 同文件新增 `--script-opt=thumbfast-mpv_path=<已校验的内置 mpv.exe 绝对路径>`。thumbfast 的默认值是 `mpv_path=mpv`，在 Windows 上先读 `user-data/frontend/process-path`（那是 mpv.net/ImPlay 之类前端才提供的属性，KumiPlayer 不提供），取不到就退化为按 PATH 查找 `mpv`；解析不到时缩略图后端 spawn 失败并在屏幕上打出通用错误。内置模式改用后端已经校验过的绝对路径，消除“能不能找到播放器”这个变量。选项名取自 thumbfast 自身 options 表（第三方脚本未改动）。
- **依据**：MPV 官方手册 `--autofit` / `--geometry` / `--auto-window-resize` / `--fullscreen`；thumbfast 自身 `mpv_path` 选项与 `spawn()` 的失败分支。用户反馈：新开窗口几乎铺满屏幕、全屏切换观感不明显；本地电影播放出现 `ERROR! cannot create mpv subprocess`。
- **验证**：
  - 窗口策略：真实内置 MPV + 生产参数播放合成源，用 Win32 `GetWindowRect` + `GetMonitorInfoW` 读取实际矩形与所在显示器工作区，连开两次均为 1229×691 @ 2048×1152 工作区（宽 60%、高 60%，≤70%×60%），两次都居中且完全落在工作区内；`--autofit` 先命中高度上限，宽高比保持 16:9。**未验证**：多屏、不同 DPI 缩放、竖屏与超宽片场景。
  - 缩略图后端：隔离探针在真实 MPV 上发送合法的 `thumb` 请求后，用 Win32 Toolhelp 枚举子进程——注入绝对路径时子进程被创建且无 spawn 错误日志；把 `mpv_path` 指向不存在的文件时子进程为 0 且出现 thumbfast 内部的 `mpv subprocess create failed`。**未复现**用户那次的具体失败，也未在合成素材下产出缩略图文件（真实媒体条件下的产物验证仍未完成）；第三方脚本中的失败提示 OSD 文本是硬编码的，本次未改动。
- **未采纳**：没有为初始尺寸再叠一层 Win32 窗口矩形补偿（实测官方 `autofit + geometry` 已满足要求，第二尺寸权威只会带来冲突）；没有修改第三方 `thumbfast.lua` 正文（分层纪律：可替换层不做本项目定制）。

### 2026-09-29 · 右键菜单首帧不等待状态往返（r22）

- 对照本地两套 MPVlite 配置中的 `contextmenu.lua`：它们用 mpv 官方 `menu-data` / `context-menu` 命令，并在打开时刷新菜单状态。KumiPlayer 仍使用现有 uosc 公开 `open-menu` / `update-menu` 接口，保留已有菜单、中文、子菜单和回执合同；没有复制整合包脚本或替换可换层。
- `kumiplayer_uosc_menu.lua` 现在先按最近一次权威状态广播打开菜单，再异步请求最新 Anime4K 状态；回执仅用 `update-menu` 更新勾选项，不重置当前菜单。改变的是首帧等待顺序，不改 Anime4K 设置或用户的外部整合包。
- 官方依据：https://mpv.io/manual/stable/#context-menu ，https://mpv.io/manual/stable/#menu-data ；uosc 接口：https://github.com/tomasklaen/uosc/wiki/Menu-API 。真实 MPV 回归先证实旧代码在 `get-state` 回调后才发 `open-menu`，新代码打开请求先于刷新请求；测试结果与界面取证见同任务 `03-execution.md`。

### 2026-10-02 · HDR 时间轴预览兼容内置 MPV（r23）

- 用户再次反馈《摇曳露营》剧场版出现 `ERROR! cannot create mpv subprocess`。报错由 thumbfast 预览脚本产生；此前注入绝对 MPV 路径仅解决找不到程序的问题。当前内置二进制没有 FFmpeg `zscale`，原脚本对 BT.2020 视频构造的 HDR 链会在解析启动参数时退出。用 PQ / HLG 合成源复现原链返回码 1，确认不是主播放器启动失败。
- `thumbfast.lua` 新增可选 `tone_mapping_backend` 与 `quiet_failures`，受控配置启用 `gpu` 与静默降级。HDR 预览使用 MPV 原生 Vulkan GPU 滤镜并在预览子进程中指定 BT.709 / sRGB / hable；主播放器不注入这些输出参数。GPU/API 不可用时立即重试一次不带 HDR 映射的普通预览，重试仍失败就关闭当前文件的预览并仅记录退出状态；切文件恢复尝试与色调映射，旧文件回调仍有代际保护。上游默认 zscale 和独立前端配置提示保留。兼容重试的 HDR 预览色彩可能不准确，不影响主视频渲染。
- 官方依据：https://mpv.io/manual/stable/#video-filters-gpu （原生 GPU 滤镜有实验性限制，因此设置逐文件兼容重试）；第三方脚本的原始来源、许可证与 upstream hash 保留，清单标记本地改动，不更新二进制。
- 验证：新增回归 7 项通过，真实内置 MPV 编码 PQ / HLG / SDR 缩略图，另用隔离 HDR Matroska 验证实际 thumbfast 子进程和 `.bgra` 叠加产物；故障模拟覆盖单次重试、持续失败停止、下一文件恢复、旧回调隔离、SDR 启动失败静默停止。未执行 EXE 打包，未验证用户具体剧场版的端到端播放。
