-- KumiPlayer Anime4K 控制器
--
-- 职责：
-- 1. 定义六种官方模式（A/B/C/A+A/B+B/C+A）× 三种质量档位（light/balanced/high）
--    的 Anime4K v4 官方多 shader 链映射（链顺序来自官方模板与 v4.0.1 实际文件）；
-- 2. 文件载入时记录基础 glsl-shaders 列表；
-- 3. 根据永久默认值或当前视频临时覆盖，构建 Anime4K 附加链；
-- 4. 关闭 Anime4K 时恢复基础列表，不误删 KumiPlayer 自有 shader；
-- 5. 接收菜单调用与后端 IPC script-message；
-- 6. 文件切换时清除临时覆盖并重新采用永久默认值；
-- 7. 不写配置文件、不访问网络、不扫描媒体库、不持续轮询。

-- 脚本消息契约：
--   kumiplayer_anime4k set-session <mode> <quality>   右键临时切换（仅当前视频）
--   kumiplayer_anime4k clear-session                  清除临时覆盖（恢复永久默认）
--   kumiplayer_anime4k set-default <mode> <quality>   后端保存的下一视频默认值
--   kumiplayer_anime4k get-state                      查询当前状态
-- 值域 mode: off|a|b|c|a+a|b+b|c+a ; quality: light|balanced|high

local mp = require "mp"
local utils = require "mp.utils"

local ANIME4K_DIR = "~~/shaders/anime4k-v4.0.1/"

-- 模式链：官方 v4 多 shader 链（不含 Clamp_Highlights 与 AutoDownscalePre，统一追加）
-- 每个模式一个 {restore, upscale, extra} 结构，质量档位只替换 CNN 变体后缀。
local MODES = {
    a   = { kind = "restore" },          -- Restore -> Upscale -> Upscale
    b   = { kind = "restore_soft" },     -- Restore_Soft -> Upscale -> Upscale
    c   = { kind = "upscale_denoise" },  -- Upscale_Denoise -> Upscale
    ["a+a"] = { kind = "restore", enhanced = true },          -- Restore->Upscale->Restore->Upscale
    ["b+b"] = { kind = "restore_soft", enhanced = true },     -- Restore_Soft->Upscale->Restore_Soft->Upscale
    ["c+a"] = { kind = "upscale_denoise", enhanced = true },  -- Upscale_Denoise->Restore->Upscale
}

-- 质量档位：{first_restore, first_upscale, second_restore, last_upscale}
-- 施工说明 5.2 节：轻量 M/S、均衡 L/M、高质量 VL/M
local QUALITIES = {
    light    = { first = "M", second = "S" },
    balanced = { first = "L", second = "M" },
    high     = { first = "VL", second = "M" },
}

local VALID_MODES = { off = true, a = true, b = true, c = true, ["a+a"] = true, ["b+b"] = true, ["c+a"] = true }
local VALID_QUALITIES = { light = true, balanced = true, high = true }

local state = {
    default_mode = "off",
    default_quality = "balanced",
    session_mode = nil,     -- 当前视频临时模式（nil=用永久默认）
    session_quality = nil,  -- 当前视频临时质量
    base_shaders = {},      -- 脚本加载时记录的基础 glsl-shaders（只记录一次）
    base_captured = false,  -- 基础列表是否已记录
    applied = false,        -- 当前是否已应用 Anime4K
    applied_key = nil,      -- 已应用链的 "mode|quality"，用于避免重复重挂
}

-- 读取永久默认值。
-- 取值来自两处，由 mp.options 统一合并：配置文件
-- script-opts/kumiplayer_anime4k.conf，以及后端命令行注入的
-- --script-opt=kumiplayer_anime4k-default_mode=...（命令行覆盖配置文件）。
--
-- 键名前缀必须是 `<脚本名>-`（mp.options 内部用 identifier.."-" 匹配），
-- 不能写成点号 `kumiplayer_anime4k.default_mode`：点号形式会被静默忽略。
-- 也不能用 mp.get_opt("default_mode")：mp.get_opt 只按全键直查
-- （mpv 的 defaults.lua 中实现为 opts[key]），裸键永远取不到值——
-- 这正是此前「播放器调节页保存的 Anime4K 默认效果不生效」的根因。
local options = {
    default_mode = state.default_mode,
    default_quality = state.default_quality,
}

local function log_warn(message)
    mp.msg.warn("[kumiplayer_anime4k] " .. message)
end

local function read_injected_defaults()
    require("mp.options").read_options(options, "kumiplayer_anime4k")
    if VALID_MODES[options.default_mode] then
        state.default_mode = options.default_mode
    else
        log_warn("invalid default_mode in script-opts: " .. tostring(options.default_mode))
    end
    if VALID_QUALITIES[options.default_quality] then
        state.default_quality = options.default_quality
    else
        log_warn("invalid default_quality in script-opts: " .. tostring(options.default_quality))
    end
    mp.msg.info("[kumiplayer_anime4k] loaded with script-opts default_mode="
        .. state.default_mode .. " default_quality=" .. state.default_quality)
end
read_injected_defaults()

-- 从基础列表构建附加链：Append 方式保证不覆盖基础 shader
local function build_chain(mode, quality)
    local mode_cfg = MODES[mode]
    local q = QUALITIES[quality] or QUALITIES.balanced
    local chain = {}

    local function add(name)
        table.insert(chain, ANIME4K_DIR .. name)
    end

    -- 统一首段 Clamp_Highlights
    add("Anime4K_Clamp_Highlights.glsl")

    if mode_cfg.kind == "restore" or mode_cfg.kind == "restore_soft" then
        local restore_prefix = mode_cfg.kind == "restore_soft" and "Anime4K_Restore_CNN_Soft_" or "Anime4K_Restore_CNN_"
        add(restore_prefix .. q.first .. ".glsl")
        add("Anime4K_Upscale_CNN_x2_" .. q.first .. ".glsl")
        if mode_cfg.enhanced then
            -- A+A / B+B：二次 Restore 用较小变体
            add(restore_prefix .. q.second .. ".glsl")
            add("Anime4K_AutoDownscalePre_x2.glsl")
            add("Anime4K_AutoDownscalePre_x4.glsl")
            add("Anime4K_Upscale_CNN_x2_" .. q.second .. ".glsl")
        else
            add("Anime4K_AutoDownscalePre_x2.glsl")
            add("Anime4K_AutoDownscalePre_x4.glsl")
            add("Anime4K_Upscale_CNN_x2_" .. q.second .. ".glsl")
        end
    elseif mode_cfg.kind == "upscale_denoise" then
        -- C / C+A：Upscale_Denoise 作为首段
        add("Anime4K_Upscale_Denoise_CNN_x2_" .. q.first .. ".glsl")
        add("Anime4K_AutoDownscalePre_x2.glsl")
        add("Anime4K_AutoDownscalePre_x4.glsl")
        if mode_cfg.enhanced then
            -- C+A：Denoise -> Restore -> Upscale
            add("Anime4K_Restore_CNN_" .. q.second .. ".glsl")
            add("Anime4K_Upscale_CNN_x2_" .. q.second .. ".glsl")
        else
            add("Anime4K_Upscale_CNN_x2_" .. q.second .. ".glsl")
        end
    end

    return chain
end

local function current_mode()
    return state.session_mode or state.default_mode
end

local function current_quality()
    return state.session_quality or state.default_quality
end

-- 应用 Anime4K 链（Append 追加，不覆盖基础）
local function apply_anime4k()
    local mode = current_mode()
    local quality = current_quality()
    if mode == "off" or not VALID_MODES[mode] then
        return
    end
    local key = mode .. "|" .. quality
    if state.applied and state.applied_key == key then
        -- 同一条链已经在生效：不要再 clr/append。
        -- mpv 的 glsl-shaders 是全局属性、会跨文件保持，切集时重复重挂整条链
        -- 只会带来无谓的着色器列表重建（起播顿挫的嫌疑点之一）。
        return
    end
    local chain = build_chain(mode, quality)
    -- 先清除本脚本可能已追加的旧链，再追加新链
    mp.commandv("change-list", "glsl-shaders", "clr", "")
    for _, shader in ipairs(chain) do
        mp.commandv("change-list", "glsl-shaders", "append", shader)
    end
    state.applied = true
    state.applied_key = key
    mp.msg.info("[kumiplayer_anime4k] applied mode=" .. mode .. " quality=" .. quality .. " shaders=" .. #chain)
end

-- 关闭 Anime4K：恢复基础 shader 列表
local function clear_anime4k()
    mp.commandv("change-list", "glsl-shaders", "clr", "")
    for _, shader in ipairs(state.base_shaders) do
        mp.commandv("change-list", "glsl-shaders", "append", shader)
    end
    state.applied = false
    state.applied_key = nil
    mp.msg.info("[kumiplayer_anime4k] restored base shaders")
end

-- 刷新当前视频的 Anime4K 状态
local function refresh()
    local mode = current_mode()
    if mode == "off" then
        if state.applied then
            clear_anime4k()
        end
    else
        apply_anime4k()
    end
end

-- 脚本消息入口
mp.register_script_message("set-session", function(mode, quality)
    if not VALID_MODES[mode] or not VALID_QUALITIES[quality] then
        log_warn("invalid set-session: " .. tostring(mode) .. " " .. tostring(quality))
        return
    end
    state.session_mode = mode
    state.session_quality = quality
    refresh()
end)

mp.register_script_message("clear-session", function()
    state.session_mode = nil
    state.session_quality = nil
    refresh()
end)

mp.register_script_message("set-default", function(mode, quality)
    if not VALID_MODES[mode] or not VALID_QUALITIES[quality] then
        log_warn("invalid set-default: " .. tostring(mode) .. " " .. tostring(quality))
        return
    end
    state.default_mode = mode
    state.default_quality = quality
    -- 仅更新默认值；当前视频保持不变，下一 file-loaded 生效
    mp.msg.info("[kumiplayer_anime4k] default updated: mode=" .. mode .. " quality=" .. quality)
end)

mp.register_script_message("get-state", function()
    mp.commandv("script-message", "kumiplayer_anime4k-state",
        current_mode(), current_quality(),
        state.session_mode or "nil", state.session_quality or "nil",
        tostring(state.applied))
end)

-- 记录“基础” shader 列表，且**只记录一次**（必须在 file-loaded 处理函数之前定义，
-- 否则闭包会把它解析成未定义的全局名）。
-- 早期实现放在 file-loaded 里每次重新记录，第二次起会把本脚本上一次追加的
-- Anime4K 链当成基础列表；此后切到 off 时 clear 完又把这些链“恢复”回来，
-- 表现为关闭 Anime4K 后画面依旧被处理（链残留）。
local function capture_base_shaders()
    if state.base_captured then
        return
    end
    state.base_captured = true
    state.base_shaders = {}
    local current = mp.get_property("glsl-shaders")
    if current then
        for item in (current .. "; "):gmatch("(.-);%s*") do
            if item ~= "" then
                table.insert(state.base_shaders, item)
            end
        end
    end
end

-- 事件：文件载入时清空临时覆盖，按永久默认应用
mp.register_event("file-loaded", function()
    capture_base_shaders()
    state.session_mode = nil
    state.session_quality = nil
    -- 不重置 state.applied：glsl-shaders 是全局属性、链会跨文件保持，
    -- 是否需要重挂交给 apply_anime4k() 用 applied_key 判断。
    refresh()
end)

mp.msg.info("[kumiplayer_anime4k] loaded, default mode=" .. state.default_mode .. " quality=" .. state.default_quality)
