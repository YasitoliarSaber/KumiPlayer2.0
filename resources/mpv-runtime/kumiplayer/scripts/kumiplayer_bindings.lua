-- KumiPlayer 快捷键弱绑定（分层架构：与整合包 input.conf 共存）
--
-- 职责：
-- 1. 注册 KumiPlayer 需要的、但整合包 input.conf 可能没有的快捷键；
-- 2. 使用 mp.add_key_binding 弱绑定：整合包 input.conf 的同名绑定自动优先
--    （MPV 官方手册：input.conf 覆盖脚本弱绑定），老手自定义不受影响；
-- 3. 安全屏蔽：Ctrl+v 剪贴板加载必须忽略（防绕过 KumiPlayer 受控队列）。
--
-- 注意：本脚本属于 KumiPlayer 自有层（resources/mpv-runtime/kumiplayer/），
-- 用户替换 portable_config 整合包后依然加载，保证 KumiPlayer 功能不受影响。

local mp = require "mp"

-- ── 安全屏蔽（绕过后端受控队列的风险键位）───────────────────────────
-- mpv 默认/整合包可能绑定 Ctrl+v 从剪贴板加载任意路径并追加到播放列表。
-- KumiPlayer 的播放列表由后端构建（同季度受控队列），进度回写按路径映射；
-- 剪贴板加载会绕过该队列且可能播放任意外部文件，故强制屏蔽。
-- 使用 add_forced_key_binding（强绑定）：整合包 input.conf 无法覆盖，
-- 保证安全边界不被第三方配置破坏（MPV 官方手册：forced 绑定覆盖 input.conf）。
mp.add_forced_key_binding("Ctrl+v", "kumiplayer-ignore-clipboard-load", function() end)

-- ── KumiPlayer 自有功能快捷键（弱绑定，整合包同名键优先）────────────
-- Anime4K 右键菜单（依赖 uosc；uosc 未加载时由 kumiplayer_uosc_menu.lua 跳过注册）
mp.add_key_binding("MBTN_RIGHT", "kumiplayer-open-context-menu", function()
    mp.commandv("script-message-to", "kumiplayer_uosc_menu", "open-anime4k-menu")
end)

-- 中文化统计页切换（依赖 stats.lua；整合包无 stats 时消息被忽略，不报错）
mp.add_key_binding("TAB", "kumiplayer-stats-toggle", function()
    mp.commandv("script-binding", "stats/display-stats-toggle")
end)

-- 控制台（mpv 内置 console.lua 通常存在；整合包无 console 时消息被忽略）
mp.add_key_binding("`", "kumiplayer-console", function()
    mp.commandv("script-binding", "console/enable")
end)

-- 截图（screenshot_to_video_dir.lua 弱绑定 F10/Alt+F10 保留；
-- 这里显式绑定同功能，双重保险，整合包同名键优先）
mp.add_key_binding("F10", "kumiplayer-screenshot-video", function()
    mp.command("no-osd screenshot video")
end)
mp.add_key_binding("Alt+F10", "kumiplayer-screenshot-subs", function()
    mp.command("screenshot subtitles")
end)

mp.msg.info("[kumiplayer_bindings] loaded")
