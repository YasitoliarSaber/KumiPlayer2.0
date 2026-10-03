# MPV 开源项目与功能待加入清单

> 这是一份长期维护的候选登记册，用来保存 MPV 相关网址、许可证/商用判断、分发义务、依赖风险，以及未来想加入的功能、预设和滤镜。它记录“是否值得接入”和“是否具备发布条件”，不代表项目已经集成。
>
> 最后更新：2026-08-12

## 使用约定

- 每收到一个 MPV 相关网址，先登记项目与来源，再核对许可证、版本、依赖和分发条件。
- 商用结论只描述当前核对结果，不替代正式法律意见；发布前应以实际锁定的 tag/commit 和随包文件重新复核。
- `可商用` 表示当前未发现许可证禁止商业使用；`条件性可商用` 表示可以用于商业软件，但必须履行许可证、署名、源码或 NOTICE 等义务；`待确认` 表示证据不足，不能用于发布决策；`不建议` 表示许可证、依赖或维护风险暂不适合纳入发行包。
- 上游项目、可选脚本、主题、字体、缩略图工具、二进制文件和在线服务分别核对许可证；一个项目可商用，不等于它的全部依赖也可直接随包分发。
- KumiPlayer 的配置源仍归 `mpv/config/` 管理；不要从本机第三方 MPV 整合包复制不明配置、插件、字体、着色器或辅助工具。
- 接入前固定上游版本并记录来源、版本、SHA-256、许可证和实际随包文件；材料不完整时只用于本地开发，不进入公开安装包。

## 记录字段

新增项目时尽量补齐以下信息：

- 项目名称、网址、固定版本或 commit
- 项目用途与计划解决的问题
- 许可证、商用结论、许可证义务
- MPV 版本要求、运行平台和依赖
- 是否需要修改源码、配置或脚本
- 与 KumiPlayer 现有播放链路的集成位置
- 当前状态、下一步和核对日期
- 许可证原文、仓库 README、发行包清单等证据链接

## 当前基础运行时来源

### mpv（KumiPlayer 的基础播放二进制）

- 定位：KumiPlayer 当前内置 MPV 的上游基础项目；`mpv.exe`、`mpv.com` 和随包 DLL 属于第三方运行时，KumiPlayer 自有配置和脚本仍单独放在 `mpv/config/`。
- 官方网址：[mpv-player/mpv](https://github.com/mpv-player/mpv)
- 当前项目锁定版本：`v0.41.0`，`x86_64-w64-mingw32`；上游构建短哈希：`g41f6a6450`。
- 当前来源：MPV 官方 GitHub Actions CI；原始包：[mpv-v0.41.0-x86_64-w64-mingw32.zip](https://github.com/mpv-player/mpv/releases/download/v0.41.0/mpv-v0.41.0-x86_64-w64-mingw32.zip)
- 原始包 SHA-256：`a49811c0752c108b8260636f9c6f6fcb97406641c98b30f1e7b500dfb20177de`
- 许可证结构：官方 README 标注默认构建为 **GPLv2-or-later**；使用 `-Dgpl=false` 构建时为 **LGPLv2.1-or-later**。需要根据实际构建参数和随包依赖判断最终发行材料，不能只把 Windows 压缩包标注成一个未经核实的许可证。
- 商用结论：**原则上可用于商业软件，但当前发行材料未完成，暂不可作为公开安装包的合规结论**。GPL/LGPL 本身允许收费分发，但分发时要满足对应许可证的版权声明、许可证文本、源码或有效源码获取方式等义务；随包的 FFmpeg、libass、libplacebo、字体、编解码库和其他 DLL 还必须逐项核对。
- 当前运行文件：`mpv/runtime/`；完整文件清单和逐文件 SHA-256 以 `mpv/runtime-manifest.json` 为准。当前清单状态为 `development-only`，因此只允许本地开发使用。
- 致谢线索：上游 README 说明 mpv 基于 MPlayer 项目，项目早期代码库曾短暂经过 mplayer2；最终致谢名单还应根据实际 27 个运行文件和许可证材料补齐，不把依赖名称当成已经完成的版权声明。
- 当前状态：**已作为本地基础运行时接入；公开发布待材料补齐**
- 下一步：`LICENSE.GPL`、`LICENSE.LGPL` 已收集并存档至 `mpv/licenses/mpv-v0.41.0/`（2026-08-10，对应演进记录 r7）；剩余未完成：随包 DLL 逐项核对来源与许可证、`Copyright` 与 NOTICE 整合、mpv-stats-zh README/LICENSE 矛盾复核；全部完成后才能把 `distribution_status` 从 `development-only` 推进到发布状态。
- 核对日期：2026-08-09
- 证据：[官方仓库 README](https://github.com/mpv-player/mpv)、[GPLv2-or-later 文本](https://raw.githubusercontent.com/mpv-player/mpv/master/LICENSE.GPL)、[LGPLv2.1-or-later 文本](https://raw.githubusercontent.com/mpv-player/mpv/master/LICENSE.LGPL)、[v0.41.0 官方发布包](https://github.com/mpv-player/mpv/releases/download/v0.41.0/mpv-v0.41.0-x86_64-w64-mingw32.zip)

## 开源项目待加入清单

### 1. uosc

- 项目：MPV 的现代化用户界面/OSD 脚本
- 网址：[tomasklaen/uosc](https://github.com/tomasklaen/uosc)
- 许可证：[LGPL-2.1](https://raw.githubusercontent.com/tomasklaen/uosc/master/LICENSE.LGPL)（仓库 README 标注为 LGPL-2.1）
- 商用结论：**条件性可商用**。LGPL-2.1 允许将软件用于收费分发，但分发时必须履行对应的许可证和通知义务。当前结论针对 uosc 主仓库本身，不自动覆盖随 uosc 使用的其他脚本、二进制或在线服务。
- 主要分发义务：
  - 随发行包保留 LGPL-2.1 文本、版权/许可证声明和免责声明；
  - 保留 uosc 的对应源码；如果修改 uosc，标明修改内容和日期，并按 LGPL-2.1 提供修改后的对应源码；
  - 不把 KumiPlayer 的产品或作者与 uosc 上游建立未经授权的背书关系；
  - 对 thumbfast、ziggy、字体、安装辅助脚本和其他可选依赖逐项核对许可证，不能沿用 uosc 的结论。
- MPV 兼容性：uosc README 要求 MPV `0.33+`；项目当前运行时清单为 `0.41.0`，满足版本下限，但仍需在 KumiPlayer 的实际启动参数和窗口模式下回归验证。
- 关于“开源但加赞助二维码”的使用方式：uosc 本体是 **LGPL-2.1**，允许用在收费/商业软件中，也允许在 KumiPlayer 界面内展示自己的赞助二维码；LGPL 约束的是 uosc 自身代码的修改与再分发义务（保留许可证文本、提供修改源码），不阻止其所在产品界面展示赞助入口。赞助二维码属于 KumiPlayer 自己的界面元素，与 uosc 许可证无冲突。注意不要用“uosc 的作者接受赞助”这类暗示，避免误导与 uosc 构成背书关系。
- 计划集成位置：把锁定版本的 uosc 文件作为 KumiPlayer 自有 MPV 配置源的一部分，进入 `mpv/config/portable_config/scripts/`；脚本选项进入 `mpv/config/portable_config/script-opts/`。保留现有截图脚本，并单独验证快捷键、播放列表和 OSD 交互。
- 当前状态：**已接入**（2026-08-10，uosc 5.13.0，官方脚本组+字体已进入配置源；公开发布前仍需补齐发布材料）
- 核对日期：2026-08-09
- 证据：[仓库 README](https://github.com/tomasklaen/uosc)、[LGPL-2.1 原文](https://raw.githubusercontent.com/tomasklaen/uosc/master/LICENSE.LGPL)
- 备注：这是工程层面的初步合规判断，公开发布前应针对最终锁定版本和实际打包方式复核。

### 2. mpv_PlayKit（mpv-lazy）

- 项目：`mpv播放器折腾记录`，mpv Windows 配置、脚本、着色器、滤镜的整合方案与懒人包（mpv-lazy）
- 网址：[hooke007/mpv_PlayKit](https://github.com/hooke007/mpv_PlayKit)；[Wiki](https://github.com/hooke007/mpv_PlayKit/wiki)；懒人包说明：[discussions/194](https://github.com/hooke007/mpv_PlayKit/discussions/194)
- 定位：**整合包 / 懒人包，不是单一可复制脚本库**。README 明确警告“不要 `git clone` 后复制粘贴”，仓库只是按 mpv 常见配置目录组织，需要按需选取组件并阅读对应文档。
- 固定版本：main 分支持续维护（1057 commits）；懒人包通过 Releases 发布，版本周期不定；接入前需锁定具体 release/Assets 及对应 sha256。
- 许可证：仓库 [LICENSE.MD](https://raw.githubusercontent.com/hooke007/mpv_PlayKit/main/LICENSE.MD) 明确——“大量文件原始来自上游，分别使用不同协议；未列出的文件以最新文件内容为准，否则默认视作 **UNLICENSED**”。即**整个包没有统一许可证，每个脚本/着色器必须按各自上游单独核对**，不能把“mpv_PlayKit”当作一个可整体分发的许可实体。
- 商用结论：**待逐项核对，整体不建议直接随包分发**。组件来源分散（mpv、yt-dlp、VapourSynth、umpv、rossy/mpv-install 等），需分别看各自许可证；未标注文件默认 UNLICENSED，不能进入发布包。
- 许可证义务：同一套整合包不能用一个许可证结论覆盖；接入哪个组件就锁定哪个上游版本、核对哪个上游许可证，并在 NOTICE/致谢中逐项登记。
- MPV 版本要求：跟随 mpv 主线持续更新；懒人包 20260510 版标注基于 shinchiro 工具链、支持 win10+ x64（需 v3 微架构，约 2015 Haswell/Excavator），并移除了音视频编码器。
- 依赖与风险：
  - 内置脚本含 uosc（LGPL-2.1，已在清单登记）、thumb_engine（缩略图引擎，待单独核对）、input_plus、save_global_props、auto_load_fonts.js、auto_sub_fonts_dir.lua、cover_art_fallback.lua、stats.lua、stats_mediainfo.lua；
  - shaders 目录含 Anime4K、ESRGAN、ArtCNN、CuNNy、FSRCNNX、ACNet、DPID、Adaptive_sharpen、Deband、LUT、color 等大量着色器，均需分别核对来源与许可证；
  - 含 VapourSynth 便携方案（Python+VapourSynth+vs-plugins）、yt-dlp、umpv 单实例代理、installer 注册脚本；
  - 整体依赖重、组件多，逐项核对成本高，不能整体复制。
- 计划集成位置：**作为功能候选参考池，不整体接入**。若从其中选取组件（如缩略图、着色器、输入扩展），只把选中且完成许可证核对的上游文件放入 `mpv/config/portable_config/scripts|script-opts|shaders/`，并登记对应上游来源。
- 当前状态：**待评估（作为功能候选参考池登记）**
- 下一步：按需选取候选功能（首推缩略图预览、着色器选型），对每个候选回到其上游仓库单独核对版本、许可证、MPV 版本要求和依赖；不整体下载懒人包进运行时。
- 核对日期：2026-08-09
- 证据：[仓库根目录](https://github.com/hooke007/mpv_PlayKit)、[LICENSE.MD](https://raw.githubusercontent.com/hooke007/mpv_PlayKit/main/LICENSE.MD)、[portable_config 目录树](https://github.com/hooke007/mpv_PlayKit/tree/main/portable_config)、[Wiki 首页](https://github.com/hooke007/mpv_PlayKit/wiki)、[懒人包说明 discussion/194](https://github.com/hooke007/mpv_PlayKit/discussions/194)
- 备注：该项目的价值在于提供完整可参考的 mpv 配置/脚本/着色器组织方式和选型池；接入时必须逐组件核对，不能因为它“打包好了”就整体随包分发。

**mpv_PlayKit 组件许可证核对表（2026-08-09 实测仓库内容）**

| 组件 | 位置 | 上游 | 许可证 | 商用结论 | 义务/风险 |
|---|---|---|---|---|---|
| uosc | scripts/uosc/ | tomasklaen/uosc（COMMIT 4104053…） | LGPL-2.1 | 条件性可商用 | 保留 LICENSE、提供修改源码；内置 fzy.lua 为 MIT（Seth Warn） |
| thumb_engine | scripts/thumb_engine/ | po5/thumbfast 的 Fork | **MPL-2.0** | 条件性可商用 | 需提供对应源码、保留版权声明；Fork 代码要注明修改与日期 |
| stats.lua | scripts/stats.lua | mpv 官方 player/lua/stats.lua（COMMIT de0f2f9…） | 随 mpv（GPLv2+/LGPLv2.1+） | 随 mpv 结论 | 与 mpv 二进制同许可约束 |
| auto_load_fonts.js | scripts/auto_load_fonts.js | Hill-98/mpv-config | **MIT** | 可商用（免费/收费均可） | 保留版权声明即满足大部分义务；建议保留源码链接 |
| input_plus.lua | scripts/input_plus.lua | hooke007 自写 | **无声明 → 默认 UNLICENSED** | 不建议直接随包分发 | 商用需联系作者授权或替换实现 |
| stats_mediainfo.lua | scripts/stats_mediainfo.lua | hooke007 自写 | 无声明 → UNLICENSED | 不建议 | 同上 |
| save_global_props.lua | scripts/save_global_props.lua | 无来源标记 | 无声明 → UNLICENSED | 不建议 | 同上；存在同功能其他开源替代 |
| cover_art_fallback.lua | scripts/cover_art_fallback.lua | 基于 mpv issue 实现 | 无声明 → UNLICENSED | 不建议 | 同上 |
| auto_sub_fonts_dir.lua | scripts/auto_sub_fonts_dir.lua | 无来源标记 | 无声明 → UNLICENSED | 不建议 | 同上 |
| installer/ | installer/*.bat | rossy/mpv-install | **ISC** | 可商用 | ISC 为宽松许可，保留版权声明即可 |
| Anime4K shaders | shaders/Anime4K/ | bloc97/Anime4K | **MIT** | 可商用 | 保留版权声明 |
| 其余 400+ glsl | shaders/** | ESRGAN/ArtCNN/CuNNy/FSRCNNX/ACNet/DPID/SSim 等 | 多数不内嵌声明 | 待逐个回上游核对 | 来源分散，未核对前视作不可随包分发 |
| yt-dlp | 懒人包内 | yt-dlp/yt-dlp | Unlicense / 公有领域 | 可商用 | 依上游说明 |
| VapourSynth | 懒人包内（可选） | vapoursynth/vapoursynth | LGPL-2.1+ | 条件性可商用 | 若 KumiPlayer 不集成 VS 滤镜可忽略 |

### 3. Anime4K（动画画质增强着色器）

- 项目用途：实时动漫倍率/降噪/锐化着色器，提升动画视频画质
- 网址：[bloc97/Anime4K](https://github.com/bloc97/Anime4K)（21k+ stars）
- 固定版本：接入前锁定具体 release/tag 及对应文件哈希（master 持续更新）
- 许可证：**MIT**（[仓库标注](https://github.com/bloc97/Anime4K)）
- 商用结论：**可商用**（免费/收费均可，可修改）
- 许可证义务：MIT 只需保留版权声明与许可证文本；无强制开源要求；建议保留上游链接与 Source 来源
- MPV 版本要求：着色器为 GLSL hook，兼容 mpv 0.41.0；需实测 `gpu-next` 渲染下效果与性能
- 依赖与风险：从上游官方 Releases 取文件，不从 mpv_PlayKit 整包复制；动画场景默认启用需做性能评估（CNN 类 upscale 对 GPU 有要求）
- 计划集成位置：`config/kumiplayer/shaders/anime4k-v4.0.1/`（自有层；2026-09-24 由可替换层迁入）；如需默认启用，与 KumiPlayer 画质预设一起设计，不强制全局默认
- 官方安装依据：[GLSL / MPV Windows 安装说明](https://github.com/bloc97/Anime4K/blob/master/md/GLSL_Instructions_Windows_MPV.md)。接入时以该说明、对应官方 Release 与上游 MIT 许可证为准，不从任何第三方整合包提取 Anime4K 文件。
- 工程决策（2026-08-10）：采用 Anime4K 官方 v4 的多 shader 链方案；模式/次级模式、链顺序与性能约束以官方高级说明为准，不采用旧整合包的 RU/UR 预组合单文件方案。
- 当前状态：**已接入**（2026-08-10，v4.0.1，18 个官方 v4 glsl；2026-09-24 起归入自有层 `mpv/config/kumiplayer/shaders/anime4k-v4.0.1/`，由后端 `--script-opt` 注入绝对路径）
- 核对日期：2026-08-09
- 证据：[Anime4K 仓库](https://github.com/bloc97/Anime4K)、[GLSL 使用说明](https://github.com/bloc97/Anime4K/blob/master/md/GLSL_Instructions_Windows_MPV.md)

### 4. thumbfast / thumb_engine（缩略图预览）

- 项目用途：为 mpv 提供滚动/进度条的缩略图预览
- 网址：[po5/thumbfast](https://github.com/po5/thumbfast)（上游）；mpv_PlayKit 的 `thumb_engine` 是其 Fork
- 固定版本：接入时优先采用上游原版 thumbfast 并锁定 commit/tag；如需 Fork 功能则锁定 Fork 版本
- 许可证：**MPL-2.0**（[上游标注](https://github.com/po5/thumbfast)）
- 商用结论：**条件性可商用**
- 许可证义务（MPL-2.0 文件级弱 copyleft）：
  - 不修改原文件时可以整体随包分发，但需保留版权声明和 MPL-2.0 文本；
  - 若修改对应文件，修改后的文件需按 MPL-2.0 提供源码（可沿用原有覆盖方式）；
  - 使用 Fork（如 thumb_engine）时需注明修改与日期；
- MPV 版本要求：thumbfast 需 UI 脚本配合调用（uosc 内置集成）；mpv 0.41.0 支持
- 依赖与风险：缩略图生成依赖 mpv IPC/视频解码能力；需验证在 KumiPlayer 播放队列与 `.strm` 播放下的稳定性
- 计划集成位置：`mpv/config/portable_config/scripts/`（原版 thumbfast.lua）或与 uosc 一起集成；缩略图缓存写入运行状态目录
- 当前状态：**已接入**（2026-08-10，上游原版 commit 0f711de，thumbfast.lua 已进入配置源；缓存写 data/mpv-state/thumbfast/）
- 核对日期：2026-08-09
- 证据：[po5/thumbfast 仓库](https://github.com/po5/thumbfast)、[thumbfast.lua（MPL-2.0 声明）](https://github.com/po5/thumbfast/blob/master/thumbfast.lua)

### 5. mpv-stats.lua 中文翻译版（统计页面中文化）

- 项目用途：将 mpv 内置 stats.lua（英文统计页面）翻译为中文，并附带 Windows CPU/GPU 占用率统计模块
- 网址：[yosh-wang/mpv-stats.lua-zh-chinese-translation-](https://github.com/yosh-wang/mpv-stats.lua-zh-chinese-translation-)
- 固定版本：main 分支 @ 2026-07-12（stats.lua 112,050 字节，SHA-256 见 components-manifest）
- 许可证：**MIT**（仓库 LICENSE.md）；**注意矛盾**：仓库 README 顶部声明“🚫 严禁任何形式的商业使用”，与 LICENSE.md（MIT 允许商用）冲突；且未声明与官方 stats.lua（GPLv2+/LGPLv2.1+ 生态）的派生关系。开源 + 自愿赞助场景按 MIT 处理风险较低，但正式发布前应联系作者确认或如实标注。
- 商用结论：**待确认（开源/个人可用；发布前需复核）**
- 许可证义务：保留 MIT 版权声明（Copyright (c) 2026 yosh-wang）与许可证文本；随包提供基于官方 stats.lua 的修改源码（开源仓库即满足）；不暗示作者背书
- MPV 版本要求：基于官方 v0.41.0 stats.lua；需 `load-stats-overlay=no` 禁用内置英文版避免同名脚本被重命名为 stats2
- 依赖与风险：CPU/GPU 统计模块调用 `typeperf`/`nvidia-smi`/`powershell`（失败显示 N/A，不崩溃）；模糊翻译在边缘文案有误翻风险
- 计划集成位置：`mpv/config/portable_config/scripts/stats.lua` + `script-opts/stats.conf`
- 当前状态：**已接入**（2026-08-10，真实 mpv 烟测零错误；许可证文本已存档 `mpv/licenses/mpv-stats-zh/LICENSE.md`）
- 下一步：发布前联系作者确认 README 非商业声明与 LICENSE 矛盾的取舍；完成随包 NOTICE 整合
- 核对日期：2026-08-10
- 证据：[仓库](https://github.com/yosh-wang/mpv-stats.lua-zh-chinese-translation-)、[LICENSE.md](https://raw.githubusercontent.com/yosh-wang/mpv-stats.lua-zh-chinese-translation-/main/LICENSE.md)

## 功能、预设与滤镜候选池

这一节记录“想给当前基础 MPV 增加什么”，尚未登记许可证结论的项目不得直接视为可发布依赖。

| 类别 | 候选 | 网址 | 当前状态 | 备注 |
|---|---|---|---|---|
| 播放界面 / OSD | uosc | [GitHub](https://github.com/tomasklaen/uosc) | 已接入（2026-08-10） | uosc 5.13.0 官方脚本组+字体已进入配置源；上游 LGPL-2.1；内置 fzy.lua 为 MIT（Seth Warn）；可搭配 KumiPlayer 自有赞助二维码 |
| 缩略图预览 | thumbfast / thumb_engine | [GitHub](https://github.com/po5/thumbfast) | 已接入（2026-08-10） | 采用上游原版 thumbfast（commit 0f711de）已进入配置源；缓存写 data/mpv-state/thumbfast/；MPL-2.0 需履行源码提供义务 |
| 播放列表 / 队列 | KumiPlayer 受控队列适配 | — | 待设计 | 先保持应用侧队列与 MPV 播放状态同步，避免 UI 脚本绕过应用策略 |
| 画面滤镜 | Anime4K / mpv shader 方案 | [Anime4K](https://github.com/bloc97/Anime4K) 为主，其余 shader 分散 | 已接入（2026-08-10） | Anime4K v4.0.1 官方 18 个 v4 glsl 已进入配置源；上游 **MIT** 可商用；PlayKit 内其余 ESRGAN/ArtCNN/CuNNy 等 400+ glsl 多数不内嵌许可证声明，未核对前不得进入发布包 |
| 色彩与画质预设 | SDR / HDR / Anime 预设 | — | 待设计 | 需要区分项目默认配置、用户覆盖配置和设备能力检测 |
| 字幕 / 音轨 | 字幕样式、自动选轨、ASS 渲染预设 | — | 待设计 | 先基于 MPV 官方选项和项目需求，不直接引入未审查脚本 |
| 性能 | 硬件解码、缓存、帧步进预设 | — | 待验证 | 需要按硬件和媒体类型测试，不能把单一机器参数当成通用默认值 |
| 截图与记录 | 截图目录、命名、播放进度记录 | — | 部分已有 | 与现有 `screenshot_to_video_dir.lua`、播放事件链路保持兼容 |

## 新项目登记模板

复制下面的小节，替换编号和占位内容；在许可证未核对前保留“待确认”状态。

### N. <项目名称>

- 项目用途：
- 网址：
- 固定版本 / tag / commit：
- 许可证：
- 商用结论：待确认
- 许可证义务：
- MPV 版本要求：
- 依赖与风险：
- 计划集成位置：
- 当前状态：待评估
- 下一步：核对许可证、版本、依赖和实际归档内容
- 核对日期：
- 证据：

## 后续 MPV 工程化整理原则

后续会根据已经登记并实际接入的功能，对整个 MPV 相关目录做一次按职责和功能的工程化整理。目标是“按功能找得到、按来源分得开、按发布材料查得清”，而不是为了好看提前制造大量空目录。

### 稳定职责边界

| 责任 | 固定位置 | 整理要求 |
|---|---|---|
| KumiPlayer 播放行为 | `mpv/config/`（`kumiplayer/` + `portable_config/`） | 分层管理：`kumiplayer/` 为 KumiPlayer 自有层（强制配置 `mpv.conf` 与自有 Lua 脚本，不可替换，经 `--include`/`--script` 加载）；`portable_config/` 为可替换层（默认套件 `mpv.conf`、`input.conf`、第三方脚本/着色器/字体，用户可整体替换）；保持 `--config-dir` 的现有加载方式 |
| MPV 基础二进制 | `mpv/runtime/` | 只放来源已核验的 MPV 运行文件；版本、来源、SHA-256 统一由 `runtime-manifest.json` 管理 |
| 第三方许可证与致谢 | `mpv/` 及清单文档 | 按实际随包文件逐项登记，不把上游项目许可证自动套用到所有 DLL、字体或可选脚本 |
| 候选项目与功能索引 | 本清单 | 每项绑定网址、许可证结论、目标功能、计划路径、依赖、状态和验证要求 |
| 安装包暂存 | `packaging/runtime/mpv/` | 只作为构建生成/暂存位置，不作为源码编辑入口，不从这里反向复制文件 |
| 运行时状态 | `data/mpv-state/` | 只保存 cache、日志、watch-later 等运行数据，不纳入功能源码整理 |

### 按功能建立索引

新增能力统一归入以下功能域，再决定实际文件放置位置：

- 播放界面 / OSD：uosc、主题、提示和交互；
- 播放列表 / 队列 / 进度：应用受控队列、MPV IPC、进度回写；
- 截图 / 记录：截图目录、命名、播放事件和记录脚本；
- 字幕 / 音轨：字幕样式、ASS 渲染、自动选轨和切换快捷键；
- 画质 / 滤镜 / 预设：Anime4K、shader、色彩、SDR/HDR 和设备能力适配；
- 性能 / 解码：硬件解码、缓存、帧步进和异常回退；
- 发布 / 合规：运行时版本、依赖许可证、NOTICE、源码获取方式和完整性哈希。

每个已接入功能至少能反查到：功能记录、实际文件、MPV 配置入口、上游来源、许可证材料和最小验证命令。MPV 需要直接自动加载的入口仍遵守当前目录约束；在没有验证 MPV 加载语义前，不把脚本随意深层嵌套或改名。

### 后续整理顺序

1. 先盘点现有文件和播放链路，建立“功能 → 文件 → 加载入口”的映射；
2. 再按功能补齐来源、许可证和致谢材料，锁定版本后再接入；
3. 每次只迁移一个功能域，迁移后执行对应的配置读取、脚本加载、播放/IPC 和打包预检验证；
4. 确认行为不变后再清理旧路径，并同步 README、清单、演进记录和必要的运行时哈希；
5. 未被功能、来源或验证引用的文件不进入正式运行时，避免重新形成不可追溯的整合包。

## 状态定义

- `待评估`：已登记网址，但许可证或技术适配尚未完成核对。
- `已评估，待接入`：许可证和基本技术条件已核对，尚未进入代码/配置。
- `已接入`：已进入 KumiPlayer 配置源，并完成针对性验证。
- `暂缓`：技术、维护、性能或发布材料暂时不满足条件。
- `拒绝`：许可证、来源或安全风险不接受。

## mpv_PlayKit 整合包组件登记总表（2026-08-09 实测仓库 + 回上游核对）

> 说明：以下为从 hooke007/mpv_PlayKit（mpv-lazy）整合包中逐目录提取、并按文件头部上游引用回源核对的可用组件。当前阶段只登记，不接入。**未列出来源的脚本/着色器默认 UNLICENSED，不得进入发布包**；所有“剩余 400+ glsl”在未逐个回源前一律视作不可分发。

### 脚本类（scripts/）

| 组件 | 上游来源 | 许可证 | 商用结论 | 义务/风险 | 适合 KumiPlayer 的功能 |
|---|---|---|---|---|---|
| uosc | tomasklaen/uosc | LGPL-2.1 | 条件性可商用 | 保留 LICENSE+修改源码；勿暗示背书 | 现代 OSD/UI、播放列表、进度条 |
| thumb_engine | po5/thumbfast（Fork） | MPL-2.0 | 条件性可商用 | 改文件需提供源码；Fork 注明修改 | 进度条/滚动缩略图预览 |
| auto_load_fonts.js | Hill-98/mpv-config | MIT | 可商用 | 保留版权声明 | 自动加载视频目录字体 |
| stats.lua | mpv 官方 player/lua | 随 mpv（GPLv2+/LGPLv2.1+） | 随 mpv | 同 mpv 约束 | 播放统计 OSD（mpv 已内置等价物） |
| input_plus.lua | hooke007 自写 | 无声明→UNLICENSED | 不建议 | 商用需授权 | 快捷键扩展（可另找开源替代） |
| stats_mediainfo.lua | hooke007 自写 | UNLICENSED | 不建议 | 同上 | 媒体信息 OSD（可自行实现） |
| save_global_props.lua | 无来源标记 | UNLICENSED | 不建议 | 有同功能开源替代 | 退出保存属性（可选） |
| cover_art_fallback.lua | 基于 mpv issue | UNLICENSED | 不建议 | 同上 | 封面回退（KumiPlayer 前端已有封面） |
| auto_sub_fonts_dir.lua | 无来源标记 | UNLICENSED | 不建议 | 同上 | 字幕字体自动加载（可自行实现） |

### 着色器类（shaders/，按上游回源）

| 组件 | 上游来源 | 许可证 | 商用结论 | 义务/风险 | 适合 KumiPlayer 的功能 |
|---|---|---|---|---|---|
| Anime4K（全部） | bloc97/Anime4K | MIT | 可商用 | 保留版权声明 | 动画倍率/降噪/锐化（首选） |
| CuNNy | funnyplanter | 待确认（文件内版权 2024） | 待确认 | 回 funnyplanter 上游确认 | 神经网络缩放 |
| ESRGAN 系 | Xintao Wang / Real-ESRGAN，权重 W2xEX | 待确认（多为研究许可） | 待确认 | 权重需按 W2xEX/Real-ESRGAN 单独确认 | AI 超分（需较强 GPU） |
| ArtCNN | Joao Chrisostomo 等 | 文件内版权 2024 | 待确认 | 回源确认 | 艺术风缩放上采样 |
| DPID | merged/dpid | 见其 LICENSE.txt | 回源确认 | 需回源读 LICENSE.txt | 细节方向增强缩放 |
| SSim | WolframRhodium / MPDN | 以 MPDN/LICENSE 为准 | 回源确认 | 回源核对 | 缩放/降采样 |
| Adaptive_sharpen | bacondither/Adaptive-sharpen | 见其 LICENSE | 回源确认 | 回源核对 | 自适应锐化 |
| FSRCNNX | igv（igv/a015fc885d5c22e6891820ad89555637） | LGPL-3.0 或接近（回源确认） | 待确认 | 回源确认 | 高质量缩放（超分） |
| Deband | an3223 | 文件内版权 2023 | 待确认 | 回源确认 | 去色带（也可用 mpv 内置 deband） |
| AMD FSR/CAS | GPUOpen FidelityFX SDK | MIT | 可商用 | 保留版权声明 | FSR1 缩放、CAS 锐化、FSR 降噪 |
| FidelityFX Lens | GPUOpen FidelityFX SDK | MIT | 可商用 | 保留版权声明 | 镜头失真处理 |
| NVIDIA Image Scaling | NVIDIAGameWorks | 见其 licence.txt | 回源确认 | NVIDIA 许可证明确后确认 | NV 缩放/锐化 |
| Snapdragon GSR | SnapdragonStudios | 见 LICENSE | 回源确认 | 回源核对 | 安卓系缩放（PC 场景价值低） |
| RAISR | Intel（arxiv 1606.01299） | 版权 Intel，回源确认 | 待确认 | 回源确认 | 单帧超分 |
| MagicKernel | johncostella/magic | 见站点 | 回源确认 | 回源核对 | 缩放内核 |
| AXAA/FXAA 等 AA | BlueSkyDefender/Depth3D 等 | 回源确认 | 回源确认 | 回源核对 | 抗锯齿 |
| USM 锐化 | GEGL/GIMP（GNOME） | GPL/LGPL 相关 | 回源确认 | 回源核对 | 反锐化掩蔽 |

### 工具/依赖类（懒人包内含）

| 组件 | 上游 | 许可证 | 商用结论 | 义务/风险 | 用途 |
|---|---|---|---|---|---|
| yt-dlp | yt-dlp/yt-dlp | Unlicense/公有领域 | 可商用 | 保留声明 | 网络流解析（KumiPlayer 未必需） |
| VapourSynth | vapoursynth | LGPL-2.1+ | 条件性可商用 | 若集成需带义务 | 滤镜/帧服务（重依赖，非必须） |
| rossy/mpv-install | rossy/mpv-install | ISC | 可商用 | 保留版权声明 | Windows 文件关联注册（KumiPlayer 已有桌面壳） |
| umpv 单实例代理 | mpv 官方 TOOLS | 随 mpv | 随 mpv | 同 mpv 约束 | 单实例播放（KumiPlayer 已有播放服务管理） |

### 当前阶段结论

- 优先候选（许可证干净、可商用）：**Anime4K（MIT）、AMD FSR/CAS（MIT）、auto_load_fonts（MIT）、thumbfast（MPL-2.0）、uosc（LGPL-2.1 条件商用）**。

### 自愿赞助二维码合规结论（用户确认，2026-08-09）

KumiPlayer 在应用内展示**自己产品的自愿赞助二维码**（非强制收费、不限制功能），上述五类优先候选组件都可以在这种形式下使用。判断依据：这些组件在其各自开源许可证（MIT / MPL-2.0 / LGPL-2.1）下都允许用于商业/可接受资助的应用，赞助二维码属于 KumiPlayer 自身的界面与变现行为，不改变这些组件的开源属性，也不被许可证禁止。

| 组件 | 许可证 | 赞助二维码下可用 | 随包必须履行的义务 |
|---|---|---|---|
| Anime4K | MIT | ✅ 可商用可赞助 | 保留上游版权声明和 MIT 许可证文本；接入时记录来源与版本 |
| AMD FSR / CAS（FidelityFX SDK） | MIT | ✅ 可商用可赞助 | 保留 AMD 版权声明和 MIT 许可证文本；无需开源 KumiPlayer 自身代码 |
| auto_load_fonts（Hill-98） | MIT | ✅ 可商用可赞助 | 保留版权声明与 MIT 文本 |
| thumbfast | MPL-2.0 | ✅ 可商用可赞助 | 保留 MPL-2.0 文本与版权声明；若修改对应文件，需按 MPL 提供该文件的源码；不修改时可原样随包分发 |
| uosc | LGPL-2.1 | ✅ 可商用可赞助 | 保留 LGPL-2.1 文本与版权声明；若修改 uosc，需提供修改后的对应源码（可链接形式）；勿用“uosc 作者接受赞助”暗示背书 |

注意事项：
- 赞助二维码必须是 KumiPlayer 自己的收款/赞助入口，不能声称是任一上游组件作者的官方赞助页面。
- 自愿赞助 ≠ 封闭化：即使接入这些组件并接受赞助，这些组件仍需按各自许可证随包保留许可证材料，不能把它们改成闭源或移除版权声明。
- 本结论只在“自愿赞助 + 履行许可证义务”的前提下成立；若未来改为强收费或捆绑非自由组件，需重新评估。
- 其他待回源确认的组件（CuNNy/ArtCNN/FSRCNNX/DPID/SSim 等）在许可证核实前不得在本结论下使用，仍需逐一回上游确认。
- 需回源确认后再决策：CuNNy、ArtCNN、FSRCNNX、DPID、SSim、Adaptive_sharpen、Deband、NV、RAISR、USM 等。
- 不建议：hooke007 自写脚本（UNLICENSED）、以及所有无法回源的 glsl。
- 上述优先候选中，**Anime4K、thumbfast、uosc 已实际接入配置源**（2026-08-10，见清单正文对应条目）；**AMD FSR/CAS、auto_load_fonts 仍为登记结论，不代表已接入**；未接入项接入前仍需按上文“记录字段”逐一锁定版本、哈希与义务材料。

## mpv_PlayKit 深入阅读要点（2026-08-09 已克隆仓库通读）

> 以下为从本地克隆仓库（当前 main=commit 4921c67）完整通读后的配置设计要点，供未来接入 uosc/thumbfast/Anime4K 时参考。仅记录，不构成接入。

### 配置组织方式（值得 KumiPlayer 借鉴）

- **input.conf 拆分**：主 input.conf 留白，按功能拆分为 `input_easy.conf`（基础播放/窗口/视频均衡器）、`input_list.conf`（列表/队列）、`input_scripts.conf`（脚本功能）、`input_uosc.conf`（uosc 菜单命令），通过 `input-conf=~~/input_easy.conf` 等引用。
- **profiles.conf 预设化**：常规预设（pure 纯模式 / window_reset / DeBand+ / SWscaler 软件缩放）+ 条件预设（HDR转SDR 静态 H2S-STM / 动态 H2S-DTM、ontop_playback 暂停置顶、filename:match() 文件名触发、EXT_vpy 预览 VapourSynth 脚本）。条件预设使用 `profile-cond` + `profile-restore`，可对特定文件名/属性自动切换参数。
- **编码约定**：所有配置 UTF-8 + LF，注释格式 `# <可选值> [条件要求] 参数意义 （补充）`；`~~/` 指设置目录（对应 KumiPlayer 的 `--config-dir`）。

### 播放器核心参数参考（mpv 0.41.0 / gpu-next）

- 渲染：`vo=gpu-next`、`gpu-context=d3d11`、`d3d11-flip=yes`；双显卡用 `d3d11-adapter`/`vulkan-device` 指定。
- 硬解：`hwdec=no`（默认软解）或 `auto-safe`；`hwdec-codecs="h264,vc1,hevc,vp8,vp9,av1,prores,ffv1"`；`vd-lavc-dr=auto`。
- 画质：`scale=lanczos`、`dscale=hermite`、`sigmoid-upscaling=yes`、`linear-downscaling=yes`、`correct-downscaling=yes`；去色带用 `deband` 或 `[DeBand+]` 预设。
- 色彩/HDR：`icc-profile-auto`、`target-prim`、`target-trc`；HDR 直通 `target-colorspace-hint`；转 SDR 用 H2S-STM/H2S-DTM 预设。
- 字幕：`sub-auto=exact`、`sub-codepage=GB18030`（中文防乱码）、`blend-subtitles`、`sub-ass-override`；字体自动加载用 auto_load_fonts/auto_sub_fonts_dir。
- 音频：`ao=wasapi`、`volume-max=130`、`audio-exclusive=no`、`audio-pitch-correction=yes`。
- 播放列表：`autocreate-playlist`（自动加同目录文件）、`directory-filter-types`、`video-exts` 白名单——KumiPlayer 有受控队列策略，接入时需评估是否与 `--autocreate-playlist=no` 兼容。
- 缓存：`cache=auto`、`demuxer-max-bytes=150MiB`、`icc-cache`、`gpu-shader-cache`；缓存目录建议放运行状态目录（KumiPlayer 的 data/mpv-state）。

### uosc 交互设计参考（uosc.conf）

- 时间轴：`timeline_style=line`、热力图 `timeline_heatmap=overlay`、缓存/字符提示 `timeline_cache=yes`。
- 控制条：`controls=menu,stats,thumb_tog,gap,play_pause,...` 支持菜单/统计/缩略图开关/播放控制/音轨/字幕/章节/视频轨/流质量/速率/乱序/循环/上下集/全屏。
- 顶栏：`top_bar=no-border`、标题 `top_bar_title`。
- 菜单：`menu_item_height=36`、`menu_type_to_search=yes`（输入即搜索）。
- 通知：`flash_duration`、`animation_duration`、hover 元素（元素级 UI 而非整页覆盖）。
- 常用命令：`uosc/open-file`、`uosc/playlist`、`uosc/subtitles`、`uosc/audio`、`uosc/keybinds`、`uosc/show-in-directory` 等（右键菜单条目，见 input_uosc.conf）。

### thumb_engine 缩略图引擎参考（thumb_engine.conf）

- 后端：`backend=mpv`（默认）或 `ffmpeg`；mpv 后端可用已运行实例或独立进程。
- 尺寸：`max_width/max_height=320`、`rescale=0` 自动。
- 解码：`hwdec=yes` 优先硬解；软解 `sw_threads=2`；`min_duration=10` 只对长视频生效。
- 触发：`script-binding thumb_engine/thumb_rerun`（修复卡死）、`thumb_toggle`（开关）、`thumbnail_hwdec toggle`（切换硬解）。
- 网络/音频默认关闭；KumiPlayer 接入时应评估 .strm / 网盘播放时的缩略图来源。

### 安装器与工具（KumiPlayer 大多已有等价能力）

- `installer/`：mpv-register/unregister、mpv-install/uninstall（来自 rossy/mpv-install，ISC）、umpv-install；含 纯净/测试/跑分/输入 四种启动模式 bat（跑分用 mpv-BenchMark.conf）。
- umpv.conf：单实例管道（`\\.\pipe\umpv`），可兼容 SVP。KumiPlayer 的播放服务已管理播放会话，一般用不上。

### 给 KumiPlayer 的接入优先级参考（仍停留在登记阶段）

1. **uosc（LGPL-2.1）+ thumbfast/thumb_engine（MPL-2.0）**：现代 OSD、进度条缩略图——最优先，能显著改善播放体验；需把缓存放 data/mpv-state，且验证与 KumiPlayer 受控队列、`.strm` 播放兼容。
2. **Anime4K（MIT）+ AMD FSR/CAS（MIT）**：动画画质增强，接入画质预设（如 SDR/动画两套 profile）。
3. **auto_load_fonts（MIT）**：提升外挂字幕/ASS 字体兼容，参考其副字幕字体目录逻辑。《win10/11 mpv播放器通用教程》：https://hooke007.github.io/unofficial/mpv_start.html （已停止维护，仅作入门参考）

### 四个网址完整阅读记录（2026-08-09 补完，含全部 wiki 子页面）

按用户要求完整通读了用户提供的四个网址及其全部子页面，记录如下以便后续会话复用，避免重复抓取。

**A. https://github.com/hooke007/mpv_PlayKit**（仓库根目录，已本地克隆通读）
- 已读：README.MD、LICENSE.MD、umpv.conf、installer/ 全部 bat、portable_config/ 全部 conf（mpv.conf 379 行、input 全套、profiles.conf、menu.conf、script-opts.conf）、scripts/ 全部 lua/js、shaders/ 全部目录、vs/ 目录。

**B. https://github.com/hooke007/mpv_PlayKit/wiki**（首页 + 全部 7 个子页面导航）
- 首页重点：文档分散说明、本地文件树结构（portable_config 便携式 / %APPDATA%/mpv 常规式两种）、VapourSynth 便携版、yt-dlp、installer 位置。
- 子页面清单：`0_FAQ`、`1_installer`、`2_portable_config`、`2_CMD`、`3_FILTER`、`3_K7sfunc`、`4_GLSL`。

**C. https://hooke007.github.io/unofficial/mpv_start.html**（《win10/11 mpv播放器通用教程》，已停止维护）
- 已读：下载安装（shinchiro 编译半官方版）、注册、便携式配置、快捷键/input.conf 语法、vf 滤镜、glsl 着色器运行时启用（`change-list glsl-shaders toggle`）、VapourSynth 部署与 K7sfunc、FAQ（稳定版 vs 开发版、可移植性）。

**D. https://github.com/hooke007/mpv_PlayKit/discussions/194**（懒人包《新快速说明》）
- 已读：20260510 版要点（仅 win10+ x64、v3 微架构、需读写权限、移除音视频编码器）、主要特点（组件精简/开箱即用/参数简化）、注册方法、标准版预装组件（mpv/umpv/yt-dlp/input_plus/save_global_props/thumb_engine/uosc/LXGWWenKai 字体）、增强包（vsNV vs CUDA 滤镜）、新版 FAQ（7z≥23.00 解压、旧硬件换 no-v3 主程序、win7 需降级+python3.8）、查错（ESC 控制台、log-file 日志）。

**wiki 子页面要点：**
- `0_FAQ`（常见问题，ver.20260307）：mpv.exe 下载、懒人包定义、修改设置/快捷键方法、脚本安装目录、高性能模式、特定文件设置优先级（全局→include→目录→文件）、界面 uosc（#186）、高分屏 ui-scale、拖放、IME 快捷键冲突、声道平衡、多声道下混（避免 ad-lavc-downmix，用 mpv 下混）、人声增强、填满画面（panscan/video-aspect-override）。
- `1_installer`：第二方/第一方注册脚本、umpv 单实例、四个特殊启动模式 bat（测试/纯净/跑分/输入）及对应 conf。
- `2_portable_config`：便携式设置目录优先于 %AppData%/mpv；脚本表（thumb_engine/uosc/uosc_addones/auto_load_fonts.js/auto_sub_fonts_dir.lua/cover_art_fallback.lua/input_plus.lua/save_global_props.lua/stats.lua/stats_mediainfo.lua 及各自 # 讨论号与功能冲突说明）；vs 脚本表（去隔行/补帧/降噪/超分等）；配置文件表（mpv.conf/input.conf/input_uosc.conf/menu.conf/profiles.conf/script-opts.conf/fonts.conf）。
- `2_CMD`（指令交互）：控制台 ``` 开启/ENTER 执行/ESC 关闭；input.conf 语法=命令语法；选项↔属性、用户脚本命令（script-binding/script-message，建议带脚本全名）、滤镜命令（vf set/append/toggle/remove）。
- `3_FILTER`（用户滤镜）：format、FFmpeg lavfi 滤镜群、d3d11vpp（RTX VSR、需 d3d11+hwdec=d3d11va）、VapourSynth（K7sfunc + K7F ZEN 简化）。
- `3_K7sfunc`（vs 包装器 1.8.1）：模块化 vs 滤镜（格式控制/超分 SR/运动补偿 MEMC/去块降噪 DBLK&NR/其它 ETC/混合 MIX）；部署（Python+VapourSynth + `pip install k7sfunc`）；模块示例 ACNET_STD（Anime4KCPP）、ARTCNN_NV（akarin+vsmlrt）；FP16/CUDA/引擎优化参数。
- `4_GLSL`（用户着色器）：HOOK/PARAM/WHEN 基础、HOOK 顺序；各族简介：RAISR、ravu、SSim（仅配 mpv 内部 shader）、USM 锐化类、ETC 合集（去晕轮/降噪/抗振铃/胶片颗粒/超分等）；变体命名 `_r2/_lite/_ar/_pc` 等；仓库内 shader 大多做了修改与上游表现可能不一致。

**给后续会话的复用提示**：上述四个网址及 7 个 wiki 子页面的内容已通读并记录在此；如需接入具体组件，直接引用本记录 + 前面的许可证明细表与组件登记总表，不必重复整页抓取。
