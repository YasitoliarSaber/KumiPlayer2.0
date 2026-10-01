# KumiPlayer MPV 配置源（config）

本目录是 KumiPlayer 自有的 MPV **配置源**，分两层：`portable_config/` 是可被用户整体替换的默认套件层，`kumiplayer/` 是不可替换的自有层。二进制在 `../runtime/`，清单与许可证在 `../`。

## 目录职责（分层架构，2026-08-12 建立；2026-09-24 随 mpv/ 整合迁入本目录）

```text
config/
├─ README.md              本说明
├─ kumiplayer/            ★ KumiPlayer 自有层（不可替换，随应用分发）
│  ├─ mpv.conf            强制配置（hwdec=auto-safe / load-stats-overlay=no /
│  │                      osd-bar=no / osd-on-seek=no / directory-mode=ignore），
│  │                      经启动参数 --include 在整合包 mpv.conf 之后追加解析
│  ├─ scripts/            自有 Lua 脚本（--script 显式加载）
│  │  ├─ screenshot_to_video_dir.lua   截图自动建“作品-SxxExx-标题”目录
│  │  ├─ kumiplayer_anime4k.lua        Anime4K 控制器（后端 IPC 联动）
│  │  ├─ kumiplayer_bindings.lua       快捷键弱绑定（MBTN_RIGHT/TAB/`/F10/Alt+F10，
│  │  │                                 Ctrl+v 强绑定安全屏蔽）
│  │  └─ kumiplayer_uosc_menu.lua      右键菜单（依赖 uosc，缺失自动降级）
│  └─ shaders/           自有 shader 资源（绝对路径经 --script-opt 注入，
│     └─ anime4k-v4.0.1/  不使用 ~~/，保证替换整合包后 Anime4K 仍可用）
└─ portable_config/       ★ 可替换层（用户可整体替换为第三方整合包）
   ├─ mpv.conf            KumiPlayer 默认套件配置（含 input-builtin-bindings=no 等）
   ├─ input.conf          KumiPlayer 默认播放键位（SPACE/方向键/音量/画质等）
   ├─ scripts/            第三方组件：uosc / thumbfast / mpv-stats-zh（中文版）/ uosc_danmaku
   ├─ script-opts/        第三方组件配置（含 KumiPlayer 自有 Anime4K 静态默认值
   │                      kumiplayer_anime4k.conf，后端启动时经 --script-opt 追加注入覆盖）
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
  --script=<kumiplayer/scripts/*.lua>   # KumiPlayer 自有脚本（与整合包 scripts/ 并行；可重复）
  --script-opt=thumbfast-thumbnail=...  # 追加式注入，务必用 --script-opt
  --script-opt=kumiplayer_anime4k-default_mode=<off|a|b|c|a+a|b+b|c+a>
  --script-opt=kumiplayer_anime4k-default_quality=<fast|light|balanced|high>
  --script-opt=kumiplayer_anime4k-shaders_dir=<kumiplayer/shaders 绝对路径>
```

> ⚠️ 不要用 `--script-opts=` 注入多个键：它是**覆盖**语义，同一命令行上出现多次时
> 只有最后一次生效，前面的键会被静默丢弃（实测 `--script-opts=a=1 --script-opts=b=2`
> 的生效值是 `b=2`）。`--script-opt` 是 `--script-opts-append` 的别名，逐条追加。
> `--script` 则是追加语义，可安全重复。
>
> ⚠️ 键名前缀必须是 `<脚本名>-`（短横）：`mp.options` 内部按 `identifier.."-"` 匹配键名，
> 点号形式会被静默忽略。

## 与其他目录的关系

| 目录 | 职责 | 是否 Git 跟踪 |
|---|---|---|
| `mpv/config/` | KumiPlayer 自有配置、脚本与自有 shader 资源（本目录） | ✅ 是 |
| `mpv/runtime/` | 干净 MPV 二进制与依赖 DLL | ❌ 否（大二进制忽略） |
| `mpv/`（清单与许可证） | `runtime-manifest.json`、`components-manifest.json`、`licenses/` | ✅ 是 |
| `packaging/runtime/mpv/` | 安装包构建时生成的暂存副本 | ❌ 否（.gitignore 忽略） |

分层理由、自有层与外部整合包的注入契约（哪些机制可叠加、哪些只限内置模式）见上一级 `../README.md`。

## 修改约束

修改本目录任何配置、脚本或资源前，必须：

1. 配置项必须可追溯到 MPV 官方手册或项目自有需求；
2. 禁止从第三方整合包复制不明配置；
3. 在任务最终审查与输出阶段顺便更新 `../docs/MPV运行时演进记录.md`；
4. `../runtime-manifest.json` 已存在时，同步其中的配置版本；
5. KumiPlayer 自有脚本与自有资源必须放在 `kumiplayer/`，可替换层不承载自有逻辑；
6. 自有层脚本不使用 `~~/` 解析自有资源（`~~` 指向当前 mpv 的配置目录，替换整合包后会指错），改用 `--script-opt` 注入的绝对路径。
