# MPV 脚本配置

本目录只存放 KumiPlayer 自维护 Lua 脚本对应的 `script-opts` 配置。
（对应配置源位于 `mpv/config/portable_config/`，自有层条件见 `mpv/README.md`。）

当前实际生效的配置（按加载顺序无关，均为对应脚本自动读取）：

| 文件 | 属于 | 作用 |
|---|---|---|
| `uosc.conf` | 可替换层（第三方） | uosc 界面/时间轴/菜单参数，`pause_indicator=no`、`window_border_size=0` 等为本项目调优值 |
| `thumbfast.conf` | 可替换层（第三方） | 缩略图尺寸/后端进程策略；缓存目录由后端 `--script-opt=thumbfast-thumbnail=` 覆盖 |
| `stats.conf` | 可替换层（第三方） | 中文 stats 页面参数 |
| `uosc_danmaku.conf` | 可替换层（第三方） | 弹幕源、自动加载、简繁转换与弹幕键位 |
| `kumiplayer_anime4k.conf` | 本项目自有脚本 | Anime4K 静态默认值；运行时由后端 `--script-opt=kumiplayer_anime4k-default_*` 覆盖 |

以后新增文件时，应在任务收尾阶段简要更新
`mpv/docs/MPV运行时演进记录.md`；实际运行时清单已经存在时，
再同步配置版本和相关文件哈希。
