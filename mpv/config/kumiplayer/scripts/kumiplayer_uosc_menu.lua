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
--
-- 可用性探测有两条通道，**不得**再使用 `script-names`：
--   `script-names` 属性在 mpv v0.41.0 里根本不存在（`mpv --list-properties`
--   只列出 scripts / input-bindings / script-opts），读取结果恒为 nil，
--   会让本脚本永远判定"uosc 未加载"——右键菜单被无条件跳过（已复现的故障）。
--   ① uosc 加载后会主动广播 `uosc-version`（无需请求），据此置位；
--   ② 同步兜底：uosc 会注册自己的键位段，input-bindings 里的 owner 即脚本名。
-- 失败结果**不缓存**：右键可能发生在 uosc 注册键位之前，缓存 false 会让整个
-- 会话都打不开菜单。
local uosc_available = false

local function check_uosc_available()
    if uosc_available then
        return true
    end
    for _, binding in ipairs(mp.get_property_native("input-bindings") or {}) do
        if tostring(binding["owner"] or "") == "uosc" then
            uosc_available = true
            return true
        end
    end
    return false
end

mp.register_script_message("uosc-version", function()
    uosc_available = true
end)

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

-- 命令命名空间与取值白名单（F-013）。同一定义同时驱动菜单项与事件分发，
-- 避免两边各写一套枚举后又不一致。
local VALID_MODE_VALUES = {}
for _, mode in ipairs(MODES) do
    VALID_MODE_VALUES[mode.value] = true
end
local VALID_QUALITY_VALUES = {}
for _, quality in ipairs(QUALITIES) do
    VALID_QUALITY_VALUES[quality.value] = true
end
local VALID_SPEED_VALUES = {}
for _, speed in ipairs(SPEEDS) do
    VALID_SPEED_VALUES[speed] = true
end
-- 画面调节属性与步长白名单在 QUALITY_GROUPS 定义之后填充（见下方 fill_quality_whitelist）。
local QUALITY_PROP = {}
local DELTA_STEPS = { "-5", "5" }
local VALID_DELTA_VALUES = {}
for _, step in ipairs(DELTA_STEPS) do
    VALID_DELTA_VALUES[step] = true
end
-- 画面调节只接受菜单定义的有限步长，不执行自由文本命令；
-- 属性白名单与“画质调节”子菜单内的既有 MPV 属性一致（对比度/亮度/伽马/饱和度/色相）。

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
for _, group in ipairs(QUALITY_GROUPS) do
    QUALITY_PROP[group.prop] = group.label
end

local function build_image_quality_items()
    local items = {}
    for _, group in ipairs(QUALITY_GROUPS) do
        local current = mp.get_property_number(group.prop) or 0
        table.insert(items, {
            title = string.format("%s（当前 %+d）", group.label, math.floor(current)),
            value = "noop",
            muted = true,
        })
        for _, step in ipairs(DELTA_STEPS) do
            table.insert(items, {
                title = "  " .. step,
                value = "quality:add:" .. group.prop .. ":" .. step,
            })
        end
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

-- 请求 id：区分“已下发”与“已生效”，并让调用方能丢弃过期回执。
local request_counter = 0
local pending_request = nil

local function next_request_id()
    request_counter = request_counter + 1
    return script_name .. ":" .. tostring(request_counter)
end

-- 有界提示：只用于当前操作失败，不用来替代状态。（2 秒后自动消失）
local function notify(message)
    mp.osd_message(message, 2)
end

local function request_anime4k(kind, mode, quality)
    pending_request = { id = next_request_id(), kind = kind }
    mp.commandv("script-message-to", "kumiplayer_anime4k", kind, mode, quality, pending_request.id)
end

-- uosc 回调模式事件入口：按 value 前缀分发。
-- 分发顺序是合同的一部分（F-013）：必须先处理更具体的 `quality:reset` /
-- `quality:add:<prop>:<delta>`，再处理 `quality:<枚举>`。历史缺陷是把
-- `quality:` 写成 `value:sub(1, 8)` 先判，于是 `quality:add:brightness:5` 和
-- `quality:reset` 会被当成质量枚举发给 Anime4K（被拒绝），而画面调节永远不生效，
-- 菜单本地 state.quality 还会被写成非法值后继续污染后续 mode 切换。
mp.register_script_message("menu-event", function(event_json)
    local event = utils.parse_json(event_json)
    if type(event) ~= "table" or event.type ~= "activate" then
        return
    end
    local value = tostring(event.value or "")
    local handled = true
    if value == "quality:reset" then
        -- 只重置本菜单管理的画面属性，不动 Anime4K 模式/质量。
        for _, group in ipairs(QUALITY_GROUPS) do
            mp.commandv("set", group.prop, 0)
        end
    elseif value:sub(1, 12) == "quality:add:" then
        local rest = value:sub(13)
        local prop, delta = rest:match("^([^:]+):(.+)$")
        if not prop or not QUALITY_PROP[prop] or not VALID_DELTA_VALUES[delta] then
            handled = false
            notify("无效的画面调节请求")
        else
            mp.commandv("add", prop, tonumber(delta))
            if mp.get_property_number(prop) == nil then
                notify(string.format("无法调节%s：命令被拒绝", QUALITY_PROP[prop]))
            end
        end
    elseif value:sub(1, 8) == "quality:" then
        local quality = value:sub(9)
        if not VALID_QUALITY_VALUES[quality] then
            handled = false
            notify("无效的 Anime4K 质量档位")
        else
            -- 不乐观写入本地 state：以 kumiplayer_anime4k 的回执为权威。
            request_anime4k("set-session", state.mode, quality)
        end
    elseif value:sub(1, 5) == "mode:" then
        local mode = value:sub(6)
        if not VALID_MODE_VALUES[mode] then
            handled = false
            notify("无效的 Anime4K 模式")
        else
            request_anime4k("set-session", mode, state.quality)
        end
    elseif value:sub(1, 6) == "speed:" then
        local speed = value:sub(7)
        if not VALID_SPEED_VALUES[speed] then
            handled = false
            notify("无效的播放速度")
        else
            mp.commandv("set", "speed", speed)
            if mp.get_property_number("speed") == nil then
                notify("无法设置播放速度：命令被拒绝")
            end
        end
    elseif value:sub(1, 4) == "cmd:" then
        -- cmd:<mpv 命令>，支持分号分隔的多命令。只供固定菜单项使用，
        -- 不得扩展成任意用户输入的执行入口（菜单 value 全部由本脚本生成）。
        for part in tostring(value:sub(5)):gmatch("[^;]+") do
            local trimmed = part:match("^%s*(.-)%s*$")
            if trimmed and trimmed ~= "" then
                mp.command(trimmed)
            end
        end
    else
        handled = false
    end
    if not handled then
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
-- 本函数是菜单状态的**唯一**写入点：失败/拒绝时保留旧值，绝不乐观写入。
mp.register_script_message("kumiplayer_anime4k-state", function(mode, quality, session_mode, session_quality, applied, request_id, ok, reason)
    state.mode = (mode ~= nil and mode ~= "") and mode or "off"
    state.quality = (quality ~= nil and quality ~= "") and quality or "balanced"
    local accepted = ok == nil or ok == "true" or ok == true
    if pending_request ~= nil and request_id ~= nil and request_id ~= "" and request_id == pending_request.id then
        pending_request = nil
        if not accepted then
            local detail = tostring(reason or "")
            if detail == "invalid_enum" then
                detail = "参数不被接受"
            elseif detail == "shader_missing" then
                detail = "着色器文件缺失"
            elseif detail == "list_mismatch" then
                detail = "着色器列表未生效"
            elseif detail == "" then
                detail = "请求被拒绝"
            end
            notify("未能应用 Anime4K：" .. detail)
        end
    end
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
