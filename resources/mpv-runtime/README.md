# KumiPlayer MPV 运行时资源（mpv-runtime）

本目录是 **Git 跟踪的 KumiPlayer 自有 MPV 配置源**，与第三方整合包、发布暂存严格分离。

## 目录职责（分层架构，2026-08-12）

```text
mpv-runtime/
├─ README.md              本说明
├─ kumiplayer/            ★ KumiPlayer 自有层（不可替换，随应用分发）
│  ├─ mpv.conf            强制配置（hwdec=auto-safe / load-stats-overlay=no），
│  │                      通过启动参数 --include 在整合包 mpv.conf 之后追加
│  └─ scripts/            KumiPlayer 自有 Lua 脚本（--script 显式加载）
│     ├─ screenshot_to_video_dir.lua   截图自动建“作品-SxxExx-标题”目录
│     ├─ kumiplayer_anime4k.lua        Anime4K 控制器（后端 IPC 联动）
│     ├─ kumiplayer_bindings.lua       快捷键弱绑定（MBTN_RIGHT/TAB/`/F10/Alt+F10，
│     │                                 Ctrl+v 强绑定安全屏蔽）
│     └─ kumiplayer_uosc_menu.lua      右键菜单（依赖 uosc，缺失自动降级）
└─ portable_config/       ★ 可替换层（用户可整体替换为第三方整合包）
   ├─ mpv.conf            KumiPlayer 默认套件配置（含 input-builtin-bindings=no 等）
   ├─ input.conf          KumiPlayer 默认播放键位（SPACE/方向键/音量/画质等）
   ├─ scripts/            第三方组件：uosc / thumbfast / mpv-stats-zh（中文版）/ uosc_danmaku
   ├─ script-opts/        第三方组件配置（含 KumiPlayer 自有 Anime4K 静态默认值
   │                      kumiplayer_anime4k.conf，后端启动时经 --script-opts 注入覆盖）
   ├─ shaders/            Anime4K v4.0.1 官方着色器
   └─ fonts/              uosc 字体
```

## 分层设计说明

| 场景 | 行为 |
|---|---|
| 新手（默认） | 直接用 portable_config 默认套件，开箱即用 |
| 老手（替换整合包） | 把自己的整合包整体复制到 portable_config/（替换内容），KumiPlayer 自有功能（截图目录/进度标题/Anime4K 联动/快捷键）仍由 kumiplayer/ 自有层提供 |
| 替换后冲突 | KumiPlayer 快捷键为弱绑定，整合包 input.conf 同名键自动优先（老手自定义优先）；Ctrl+v 安全屏蔽为强绑定，不可覆盖 |

## 加载机制（后端启动参数）

```
mpv.exe
  --config-dir=<portable_config>        # 整合包层（用户可替换）
  --include=<kumiplayer/mpv.conf>       # KumiPlayer 强制配置追加
  --script=<kumiplayer/scripts/*.lua>   # KumiPlayer 自有脚本（与整合包 scripts/ 并行）
  --script-opts=...                     # 现有注入（thumbfast 缓存 / Anime4K 默认值）
```

## 与其他目录的关系

| 目录 | 职责 | 是否 Git 跟踪 |
|---|---|---|
| `resources/mpv-runtime/` | KumiPlayer 自有配置与脚本源（本目录） | ✅ 是 |
| `third_party/mpv/runtime/` | 干净 MPV 二进制与依赖 DLL | ❌ 否（大二进制忽略） |
| `packaging/runtime/mpv/` | 安装包构建时生成的暂存副本 | ❌ 否（.gitignore 忽略） |
| `mpv/`（根目录） | 第三方整合包参考（非正式基础） | ❌ 否 |

## 修改约束

修改本目录任何配置、脚本或版本前，必须：
1. 配置项必须可追溯到 MPV 官方手册或项目自有需求；
2. 禁止从第三方整合包复制不明配置；
3. 在任务最终审查与输出阶段，顺便更新 `docs/03_运行时与发布/MPV/MPV运行时演进记录.md`；
4. 实际运行时清单已经存在时，同步配置版本与相关文件哈希；
5. KumiPlayer 自有脚本必须放在 `kumiplayer/scripts/`（可替换层不承载自有逻辑）。
