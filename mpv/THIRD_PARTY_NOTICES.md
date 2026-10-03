# 内置播放器 第三方组件与许可证

KumiPlayer 的**内置播放器**是项目自带的 MPV（`mpv/runtime/` 27 个运行文件）加自有配置，**随安装包一起分发**。
本文件登记它的全部第三方组件、许可证与来源，供合规审阅与后续升级使用。

> 二进制本身不入 Git（见 `runtime-manifest.json` 与 `runtime/README.md`）；本文件与 `licenses/` 是必须随源码可见的合规材料。

## 1. 运行时组件（27 个文件）

来源：`mpv-v0.41.0-x86_64-w64-mingw32.zip`（MPV 官方 GitHub CI 构建，MinGW 版）。
**该构建启用 GPL 代码路径**（`mpv.com -v --version` 特性列表含 `gpl`；FFmpeg 构建配置含 `--enable-gpl`），因此运行时的主体按 **GPLv2+** 分发。

| 组件 | 随包文件 | 许可证 | 许可证正文 |
|---|---|---|---|
| mpv v0.41.0 | `mpv.exe`、`mpv.com`、`mpv-register.bat`、`mpv-unregister.bat` | GPL-2.0-or-later | `licenses/mpv-v0.41.0/LICENSE.GPL` |
| FFmpeg（git-2025-12-21） | `avcodec-62`、`avdevice-62`、`avfilter-11`、`avformat-62`、`avutil-60`、`swresample-6`、`swscale-9`（.dll） | GPL-2.0-or-later | `licenses/ffmpeg/COPYING.GPLv2`、`licenses/ffmpeg/LICENSE.md` |
| libass | `libass-9.dll` | ISC | `licenses/libass/COPYING` |
| dav1d | `libdav1d.dll` | BSD-2-Clause | `licenses/dav1d/COPYING` |
| libplacebo | `libplacebo-358.dll` | LGPL-2.1-or-later | `licenses/libplacebo/LICENSE` |
| GNU FriBidi | `libfribidi-0.dll` | LGPL-2.1-or-later | `licenses/fribidi/COPYING` |
| GNU libiconv | `libiconv-2.dll` | LGPL-2.1-or-later | `licenses/libiconv/COPYING.LIB` |
| FreeType | `libfreetype-6.dll` | FTL 或 GPLv2（双许可） | `licenses/freetype/LICENSE.TXT`、`licenses/freetype/FTL.TXT` |
| HarfBuzz | `libharfbuzz-0.dll` | MIT | `licenses/harfbuzz/COPYING` |
| Little CMS | `liblcms2.dll` | MIT | `licenses/lcms2/LICENSE` |
| shaderc | `libshaderc_shared.dll` | Apache-2.0 | `licenses/shaderc/LICENSE` |
| SPIRV-Cross | `libspirv-cross-c-shared.dll` | Apache-2.0 | `licenses/spirv-cross/LICENSE` |
| Vulkan-Loader | `vulkan-1.dll` | Apache-2.0 | `licenses/vulkan-loader/LICENSE.txt` |
| zlib | `zlib1.dll` | Zlib | `licenses/zlib/LICENSE` |
| GCC 运行库 | `libgcc_s_seh-1.dll`、`libssp-0.dll`、`libstdc++-6.dll` | GPL-3.0 **+ GCC Runtime Library Exception** | `licenses/gcc-runtime/COPYING3`、`licenses/gcc-runtime/COPYING.RUNTIME` |
| mingw-w64 运行库 | `libwinpthread-1.dll` | mingw-w64 许可 | `licenses/mingw-w64/COPYING` |
| LuaJIT | 静态链入 `mpv.exe` | MIT | `licenses/luajit/COPYRIGHT` |
| mujs | 静态链入 `mpv.exe` | ISC | `licenses/mujs/COPYING` |

## 2. 播放增强组件（配置层，随包分发）

| 组件 | 许可证 | 许可证正文 |
|---|---|---|
| uosc 5.13.0 | LGPL-2.1 | `licenses/uosc-5.13.0/LICENSE.LGPL` |
| thumbfast | MPL-2.0 | `licenses/thumbfast/MPL-2.0.txt` |
| Anime4K v4.0.1 | MIT | `licenses/anime4k-v4.0.1/LICENSE` |
| mpv-stats-zh | MIT（见第 4 节） | `licenses/mpv-stats-zh/LICENSE.md` |
| uosc_danmaku | MIT | `licenses/uosc_danmaku/LICENSE` |

版本、来源 URL 与 SHA-256 统一登记在 `components-manifest.json`。

## 3. 对应源码（GPL 义务）

内置播放器**未修改上游代码**，对应源码即上游源码：

- mpv：https://github.com/mpv-player/mpv/tree/v0.41.0
- 官方 Windows 构建脚本：https://github.com/mpv-player/mpv-winbuild-cmake
- FFmpeg：https://github.com/FFmpeg/FFmpeg
- 其余各组件上游地址见 `components-manifest.json` 与上表

若需要上述源码副本，可在本仓库开 Issue 索取。
KumiPlayer 自身以独立进程 + JSON IPC 驱动 `mpv.exe`，不链接 libmpv，属于 GPL 意义上的聚合而非衍生作品，因此 KumiPlayer 自身代码不因之适用 GPL。

## 4. 待处理事项

- **mpv-stats-zh**：上游仓库 2026-09-12 起把 LICENSE 从 MIT 改为「非商业使用许可」。本项目固定的版本取自 **2026-07-12（MIT 时期）**，脚本 SHA-256 为 `4F037D83…2302`（与清单一致，已复核），按当时获得的 MIT 许可分发；且该脚本派生自 mpv/Argon-/mpv-stats（LGPL-2.1+），派生部分仍受 LGPL 约束。**升级该脚本前必须重新评估上游许可。**
- **编解码专利**：H.264 / H.265 / AAC 等专利授权不由 GPL 覆盖，属独立事项，与是否使用 libmpv 无关。
