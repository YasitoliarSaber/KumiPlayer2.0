-- KumiPlayer 右键菜单（基于 uosc 公开菜单接口）
--
-- 职责：
-- 1. Anime4K 模式/质量快速切换（会话级临时状态，不写永久配置）；
-- 2. 速度预设、画质调节（对比度/亮度/伽马/饱和度/色相，mpv 内置属性）；
-- 3. 字幕/音轨快速操作（mpv 内置命令）。
--
-- 交互：uosc 菜单原生支持鼠标悬停逐级展开子菜单，右滑即可选择，
-- 无需逐级点击进入。
--
-- 全部菜单项使用 mpv 内置命令或 KumiPlayer 自有脚本消息，不引入
-- 任何绕过后端受控队列的文件/列表加载类入口。

local mp = require "mp"
local utils = require "mp.utils"

local script_name = "kumiplayer_uosc_menu"

-- 分层架构降级保护：本脚本依赖 uosc（整合包层可能不包含）。
-- uosc 未加载时跳过右键菜单注册，不影响其他 KumiPlayer 自有功能；
-- 菜单入口脚本消息（open-anime4k-menu）保留但无动作，后端调用不报错。
local uosc_available = false
local function check_uosc_available()
    if uosc_available then
        return true
    end
    local clients = mp.get_property_native("script-names") or {}
    for _, name in ipairs(clients) do
        if tostring(name) == "uosc" then
            uosc_available = true
            return true
        end
    end
    return false
end

local MODES = {
    { value = "off", title = "关闭" },
    { value = "a", title = "Anime4K Mode A" },
    { value = "b", title = "Anime4K Mode B" },
    { value = "c", title = "Anime4K Mode C" },
    { value = "a+a", title = "Anime4K Mode A+A" },
    { value = "b+b", title = "Anime4K Mode B+B" },
    { value = "c+a", title = "Anime4K Mode C+A" },
}

local QUALITIES = {
    { value = "light", title = "轻量" },
    { value = "balanced", title = "均衡" },
    { value = "high", title = "高质量" },
}

local SPEEDS = { "0.25", "0.5", "0.75", "1.0", "1.25", "1.5", "1.75", "2.0" }

local state = {
    mode = "off",
    quality = "balanced",
    waiting_for_state = false,
}

-- ── 菜单项构建 ──────────────────────────────────────────────────────

local function build_mode_items()
    local items = {}
    for _, mode in ipairs(MODES) do
        table.insert(items, {
            title = mode.title,
            value = "mode:" .. mode.value,
            active = state.mode == mode.value,
        })
    end
    return items
end

local function build_quality_items()
    local items = {}
    for _, quality in ipairs(QUALITIES) do
        table.insert(items, {
            title = quality.title,
            value = "quality:" .. quality.value,
            active = state.quality == quality.value,
        })
    end
    return items
end

-- 速度子菜单：勾选当前速度
local function build_speed_items()
    local items = {}
    local current = tostring(mp.get_property_number("speed") or 1.0)
    for _, speed in ipairs(SPEEDS) do
        table.insert(items, {
            title = speed .. "x",
            value = "speed:" .. speed,
            active = math.abs((tonumber(current) or 1.0) - tonumber(speed)) < 0.001,
        })
    end
    return items
end

-- 画质调节子菜单：对比度/亮度/伽马/饱和度/色相（mpv 内置属性）
local QUALITY_GROUPS = {
    { label = "对比度", prop = "contrast" },
    { label = "亮度", prop = "brightness" },
    { label = "伽马", prop = "gamma" },
    { label = "饱和度", prop = "saturation" },
    { label = "色相", prop = "hue" },
}

local function build_image_quality_items()
    local items = {}
    for _, group in ipairs(QUALITY_GROUPS) do
        local current = mp.get_property_number(group.prop) or 0
        table.insert(items, {
            title = string.format("%s（当前 %+d）", group.label, math.floor(current)),
            value = "noop",
            muted = true,
        })
        table.insert(items, {
            title = "  -5",
            value = "quality:add:" .. group.prop .. ":-5",
        })
        table.insert(items, {
            title = "  +5",
            value = "quality:add:" .. group.prop .. ":5",
        })
        table.insert(items, { separator = true })
    end
    table.insert(items, {
        title = "全部重置",
        value = "quality:reset",
    })
    return items
end

-- 字幕子菜单
local function build_subtitle_items()
    return {
        {
            title = "隐藏/显示字幕",
            value = "cmd:cycle sub-visibility",
            active = not mp.get_property_bool("sub-visibility", true),
        },
        {
            title = "切换字幕轨",
            value = "cmd:cycle sub",
        },
        { separator = true },
        { title = "字幕延迟 -0.1s", value = "cmd:add sub-delay -0.1" },
        { title = "字幕延迟 +0.1s", value = "cmd:add sub-delay 0.1" },
        { title = "字幕字号 -0.1", value = "cmd:add sub-scale -0.1" },
        { title = "字幕字号 +0.1", value = "cmd:add sub-scale 0.1" },
        { title = "重置字幕", value = "cmd:set sub-pos 100;set sub-scale 1;set sub-delay 0" },
    }
end

-- 音轨子菜单
local function build_audio_items()
    return {
        { title = "切换音轨", value = "cmd:cycle audio" },
        { separator = true },
        { title = "音量 -10", value = "cmd:add volume -10" },
        { title = "音量 +10", value = "cmd:add volume 10" },
        { title = "静音", value = "cmd:cycle mute", active = mp.get_property_bool("mute", false) },
        { separator = true },
        { title = "音频延迟 -0.1s", value = "cmd:add audio-delay -0.1" },
        { title = "音频延迟 +0.1s", value = "cmd:add audio-delay 0.1" },
        { title = "重置音频延迟", value = "cmd:set audio-delay 0" },
    }
end

-- 主菜单：Anime4K 模式/质量直接平铺（不再嵌套子菜单，减少展开路径）
local function build_main_items()
    return {
        {
            title = "速度",
            value = "submenu:speed",
            items = build_speed_items(),
        },
        {
            title = "画质调节",
            value = "submenu:image-quality",
            items = build_image_quality_items(),
        },
        {
            title = "字幕",
            value = "submenu:subtitles",
            items = build_subtitle_items(),
        },
        {
            title = "音轨",
            value = "submenu:audio",
            items = build_audio_items(),
        },
        { separator = true },
        {
            title = "Anime4K 模式",
            value = "submenu:anime4k-modes",
            items = build_mode_items(),
        },
        {
            title = "Anime4K 质量",
            value = "submenu:anime4k-quality",
            items = build_quality_items(),
        },
    }
end

local function render_menu()
    if not check_uosc_available() then
        mp.msg.verbose("[kumiplayer_uosc_menu] uosc 未加载，跳过菜单渲染")
        return
    end
    -- 必须使用 uosc 回调模式：JSON 顶层携带 callback 数组后，uosc 把菜单
    -- 事件（activate 等）发回本脚本；否则 uosc 会把菜单项的 value 当作
    -- mpv 命令执行（如 "mode:a"），导致点击无任何反应。
    mp.commandv("script-message-to", "uosc", "open-menu", utils.format_json({
        type = "kumiplayer-context",
        title = "KumiPlayer",
        callback = { script_name, "menu-event" },
        items = build_main_items(),
    }))
end

-- ── 事件处理 ────────────────────────────────────────────────────────

-- uosc 回调模式事件入口：按 value 前缀分发。
mp.register_script_message("menu-event", function(event_json)
    local event = utils.parse_json(event_json)
    if type(event) ~= "table" or event.type ~= "activate" then
        return
    end
    local value = tostring(event.value or "")
    if value:sub(1, 5) == "mode:" then
        local mode = value:sub(6)
        mp.commandv("script-message-to", "kumiplayer_anime4k", "set-session", mode, state.quality)
        state.mode = mode
    elseif value:sub(1, 8) == "quality:" then
        local quality = value:sub(9)
        mp.commandv("script-message-to", "kumiplayer_anime4k", "set-session", state.mode, quality)
        state.quality = quality
    elseif value:sub(1, 6) == "speed:" then
        local speed = value:sub(7)
        mp.commandv("set", "speed", speed)
    elseif value:sub(1, 12) == "quality:add:" then
        -- quality:add:<prop>:<delta>
        local rest = value:sub(13)
        local prop, delta = rest:match("^([^:]+):(.+)$")
        if prop and delta then
            mp.commandv("add", prop, tonumber(delta) or 0)
        end
    elseif value == "quality:reset" then
        for _, group in ipairs(QUALITY_GROUPS) do
            mp.commandv("set", group.prop, 0)
        end
    elseif value:sub(1, 4) == "cmd:" then
        -- cmd:<mpv 命令>，支持分号分隔的多命令
        for part in tostring(value:sub(5)):gmatch("[^;]+") do
            local trimmed = part:match("^%s*(.-)%s*$")
            if trimmed and trimmed ~= "" then
                mp.command(trimmed)
            end
        end
    else
        return
    end
    -- 与简单模式一致：激活后关闭菜单
    mp.commandv("script-message-to", "uosc", "close-menu", "kumiplayer-context")
end)

local function request_state_and_open()
    if not check_uosc_available() then
        mp.msg.verbose("[kumiplayer_uosc_menu] uosc 未加载，忽略右键菜单请求")
        return
    end
    state.waiting_for_state = true
    mp.commandv("script-message-to", "kumiplayer_anime4k", "get-state")
end

-- 监听 Anime4K 脚本的状态广播。消息名必须与 kumiplayer_anime4k.lua
-- 的广播名完全一致（kumiplayer_anime4k-state），否则菜单永远等不到
-- 状态回调、右键无反应。
mp.register_script_message("kumiplayer_anime4k-state", function(mode, quality)
    state.mode = mode or "off"
    state.quality = quality or "balanced"
    if state.waiting_for_state then
        state.waiting_for_state = false
        render_menu()
    end
end)

mp.register_script_message("open-anime4k-menu", request_state_and_open)

mp.register_event("file-loaded", function()
    mp.commandv("script-message-to", "kumiplayer_anime4k", "get-state")
end)

mp.msg.info("[kumiplayer_uosc_menu] loaded")
