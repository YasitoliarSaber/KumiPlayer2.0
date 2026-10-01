-- KumiPlayer Anime4K 控制器
--
-- 职责：
-- 1. 定义六种官方模式（A/B/C/A+A/B+B/C+A）与四种质量档位；轻量及以上
--    使用 Anime4K v4 多 shader 链（链顺序来自官方模板与 v4.0.1 实际文件）；
-- 2. 文件载入时记录基础 glsl-shaders 列表；
-- 3. 根据永久默认值或当前视频临时覆盖，构建 Anime4K 附加链；
-- 4. 关闭 Anime4K 时恢复基础列表，不误删 KumiPlayer 自有 shader；
-- 5. 接收菜单调用与后端 IPC script-message；
-- 6. 文件切换时清除临时覆盖并重新采用永久默认值；
-- 7. 不写配置文件、不访问网络、不扫描媒体库、不持续轮询。

-- 脚本消息契约：
--   kumiplayer_anime4k set-session <mode> <quality> [request_id]   右键临时切换（仅当前视频）
--   kumiplayer_anime4k clear-session [request_id]                  清除临时覆盖（恢复永久默认）
--   kumiplayer_anime4k set-default <mode> <quality> [request_id]   后端保存的下一视频默认值
--   kumiplayer_anime4k get-state [request_id]                      查询当前状态
-- 值域 mode: off|a|b|c|a+a|b+b|c+a ; quality: fast|light|balanced|high
--
-- 状态广播（唯一权威）：脚本在**实际执行后**广播
--   kumiplayer_anime4k-state <mode> <quality> <session_mode> <session_quality> <applied> \
--                            [request_id] [ok] [reason]
-- 调用方（右键菜单）只能按这条广播更新自己的缓存状态，不得在请求发出时乐观写入；
-- 请求 id 让调用方能丢弃过期回执。
-- applied 的语义分三级（F-012）：requested（已收到请求）< installed（glsl-shaders 列表
-- 读回一致）< render_verified（着色器编译/渲染无错误）。当前实现最多能确定 installed；
-- 编译失败由 mpv 日志体现，脚本不把它当 applied。

local mp = require "mp"
local utils = require "mp.utils"

-- Anime4K 着色器目录（KumiPlayer 自有层资源）。
--
-- 默认值 `~~/shaders/anime4k-v4.0.1/` 会解析到**当前 mpv 的配置目录**（内置模式
-- 就是 portable_config），这在分层架构下是错的：着色器属于 KumiPlayer 自有层
-- （kumiplayer/shaders/），一旦用户把 portable_config 整体替换成第三方整合包，
-- `~~/shaders/` 就指向整合包自己的目录，Anime4K 菜单照常出现但链全部加载失败。
-- 因此后端在启动时用追加式官方语法注入绝对路径：
--   --script-opt=kumiplayer_anime4k-shaders_dir=<自有层 shaders 绝对路径>
-- 下面的 `~~/shaders/` 仅作为手工裸跑 mpv（无后端注入）时的兜底。
local ANIME4K_SUBDIR = "anime4k-v4.0.1"
local anime4k_dir = "~~/shaders/" .. ANIME4K_SUBDIR .. "/"

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
local VALID_QUALITIES = { fast = true, light = true, balanced = true, high = true }

local state = {
    default_mode = "off",
    default_quality = "balanced",
    session_mode = nil,     -- 当前视频临时模式（nil=用永久默认）
    session_quality = nil,  -- 当前视频临时质量
    base_shaders = {},      -- 脚本加载时记录的基础 glsl-shaders（只记录一次）
    base_captured = false,  -- 基础列表是否已记录
    applied = false,        -- 当前是否已应用 Anime4K（= glsl-shaders 列表已读回一致）
    applied_key = nil,      -- 已应用链的 "mode|quality"，用于避免重复重挂
    last_error = "",        -- 最近一次失败原因（脱敏，界面只显示“未能应用”）
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
    shaders_dir = "",
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
    -- 自有层着色器目录：后端注入的是 Windows 绝对路径，统一成正斜杠并补尾斜杠，
    -- 因为链里的条目会用字符串拼接到 glsl-shaders 属性上。
    if options.shaders_dir ~= "" then
        local dir = tostring(options.shaders_dir):gsub("\\", "/")
        if dir:sub(-1) ~= "/" then
            dir = dir .. "/"
        end
        anime4k_dir = dir .. ANIME4K_SUBDIR .. "/"
    end
    mp.msg.info("[kumiplayer_anime4k] loaded with script-opts default_mode="
        .. state.default_mode .. " default_quality=" .. state.default_quality
        .. " shaders_dir=" .. anime4k_dir)
end
read_injected_defaults()

-- 从基础列表构建附加链：Append 方式保证不覆盖基础 shader
local function build_chain(mode, quality)
    local mode_cfg = MODES[mode]
    local q = QUALITIES[quality] or QUALITIES.balanced
    local chain = {}

    local function add(name)
        table.insert(chain, anime4k_dir .. name)
    end

    -- 低配档参考整合包的单 shader 策略，不串联两次放大与二次修复。
    -- 增强模式在此档保留对应的基础算法风格；需要完整增强链可选轻量及以上。
    if quality == "fast" then
        if mode_cfg.kind == "restore_soft" then
            add("Anime4K_Restore_CNN_Soft_S.glsl")
        elseif mode_cfg.kind == "upscale_denoise" then
            add("Anime4K_Upscale_CNN_x2_S.glsl")
        else
            add("Anime4K_Restore_CNN_S.glsl")
        end
        return chain
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

-- 唯一状态广播出口。request_id 可为 nil（旧调用方）；ok/reason 让调用方知道这次
-- 请求是被拒绝、失败还是成功，而不是把“已下发”当成“已生效”。
local function broadcast_state(request_id, ok, reason)
    mp.commandv("script-message", "kumiplayer_anime4k-state",
        current_mode(), current_quality(),
        state.session_mode or "nil", state.session_quality or "nil",
        tostring(state.applied),
        tostring(request_id or ""),
        ok and "true" or "false",
        tostring(reason or ""))
end

-- 读回 glsl-shaders 列表（分号分隔）。读回失败返回 nil，表示无法确认。
local function read_glsl_shaders()
    local current = mp.get_property("glsl-shaders")
    if current == nil then
        return nil
    end
    local items = {}
    for item in (current .. "; "):gmatch("(.-);%s*") do
        if item ~= "" then
            table.insert(items, item)
        end
    end
    return items
end

-- 链一旦应用，glsl-shaders 应当且仅当是我们刚写入的这条链。
-- 注意：apply 先 `clr` 再逐条 append，因此生效期间基础 shader 不在列表里
-- （关闭时再由 clear_anime4k 恢复），所以这里做的是与 chain 的严格等长等值比较。
local function list_matches_expected(expected)
    local actual = read_glsl_shaders()
    if actual == nil or #actual ~= #expected then
        return false
    end
    for index, value in ipairs(expected) do
        if actual[index] ~= value then
            return false
        end
    end
    return true
end

-- 着色器文件必须真的存在：路径错误时 mpv 只在日志里报错，属性列表
-- 依旧“设置成功”，不能据此报 applied。
local function missing_shader(chain)
    for _, shader in ipairs(chain) do
        local info = utils.file_info(shader)
        if not info or not info.is_file then
            return shader
        end
    end
    return nil
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

-- 应用 Anime4K 链（Append 追加，不覆盖基础）。
-- 返回 true 表示“已安装且读回一致”；false 表示失败（内部已回滚）。
local function apply_anime4k()
    local mode = current_mode()
    local quality = current_quality()
    if mode == "off" or not VALID_MODES[mode] then
        return true
    end
    local key = mode .. "|" .. quality
    if state.applied and state.applied_key == key then
        -- 同一条链已经在生效：不要再 clr/append。
        -- mpv 的 glsl-shaders 是全局属性、会跨文件保持，切集时重复重挂整条链
        -- 只会带来无谓的着色器列表重建（起播顿挫的嫌疑点之一）。
        return true
    end
    local chain = build_chain(mode, quality)
    local missing = missing_shader(chain)
    if missing then
        state.last_error = "shader_missing"
        log_warn("shader file missing: " .. tostring(missing))
        clear_anime4k()
        return false
    end
    -- 先清除本脚本可能已追加的旧链，再逐条追加；逐条检查命令是否被接受。
    local cleared, clear_error = mp.commandv("change-list", "glsl-shaders", "clr", "")
    if cleared == false then
        state.last_error = "command_rejected"
        log_warn("clearing glsl-shaders was rejected: " .. tostring(clear_error))
        return false
    end
    for _, shader in ipairs(chain) do
        local appended, append_error = mp.commandv("change-list", "glsl-shaders", "append", shader)
        if appended == false then
            state.last_error = "command_rejected"
            log_warn("appending " .. tostring(shader) .. " was rejected: " .. tostring(append_error))
            clear_anime4k()
            return false
        end
    end
    -- 读回核对。读回为空不等于失败：无视频输出时 glsl-shaders 属性不可观测
    -- （实测 `--force-window=no` + 音频输入下 mpv 日志报 Set property 成功但读回为空串），
    -- 此时只能确认“已安装”，不能核对列表结果。
    local actual = read_glsl_shaders()
    if actual == nil or #actual == 0 then
        state.applied = true
        state.applied_key = key
        state.last_error = ""
        mp.msg.warn("[kumiplayer_anime4k] glsl-shaders 无法读回（无视频输出？）：只能确认已安装，未核对列表")
        return true
    end
    if not list_matches_expected(chain) then
        -- 列表读回不一致：可能被其他脚本改写或路径非法。不得报 applied，
        -- 回滚到基础列表，并向界面回一个明确失败原因。
        state.last_error = "list_mismatch"
        log_warn("glsl-shaders list did not match after apply; rolling back")
        clear_anime4k()
        return false
    end
    state.applied = true
    state.applied_key = key
    state.last_error = ""
    mp.msg.info("[kumiplayer_anime4k] applied mode=" .. mode .. " quality=" .. quality .. " shaders=" .. #chain)
    return true
end

-- 刷新当前视频的 Anime4K 状态；返回 true 表示本次状态已生效。
local function refresh()
    local mode = current_mode()
    if mode == "off" then
        if state.applied then
            clear_anime4k()
        end
        return true
    end
    return apply_anime4k()
end

-- 脚本消息入口。request_id 由调用方（右键菜单）生成，回执原样带回，
-- 让调用方能丢弃过期回执并区分“已下发”与“已生效”。
mp.register_script_message("set-session", function(mode, quality, request_id)
    if not VALID_MODES[mode] or not VALID_QUALITIES[quality] then
        log_warn("invalid set-session: " .. tostring(mode) .. " " .. tostring(quality))
        state.last_error = "invalid_enum"
        broadcast_state(request_id, false, "invalid_enum")
        return
    end
    state.session_mode = mode
    state.session_quality = quality
    local ok = refresh()
    broadcast_state(request_id, ok, state.last_error)
end)

mp.register_script_message("clear-session", function(request_id)
    state.session_mode = nil
    state.session_quality = nil
    local ok = refresh()
    broadcast_state(request_id, ok, state.last_error)
end)

mp.register_script_message("set-default", function(mode, quality, request_id)
    if not VALID_MODES[mode] or not VALID_QUALITIES[quality] then
        log_warn("invalid set-default: " .. tostring(mode) .. " " .. tostring(quality))
        broadcast_state(request_id, false, "invalid_enum")
        return
    end
    state.default_mode = mode
    state.default_quality = quality
    -- 仅更新默认值；当前视频保持不变，下一 file-loaded 生效
    mp.msg.info("[kumiplayer_anime4k] default updated: mode=" .. mode .. " quality=" .. quality)
    broadcast_state(request_id, true, "")
end)

mp.register_script_message("get-state", function(request_id)
    broadcast_state(request_id, true, "")
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
