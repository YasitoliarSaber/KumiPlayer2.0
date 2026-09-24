local mp = require 'mp'
local utils = require 'mp.utils'

local integration_marker = 'user-data/kumiplayer/screenshot-plugin-loaded'
if mp.get_property_bool(integration_marker, false) then
    mp.msg.verbose('KumiPlayer 截图插件已由当前 MPV 配置加载，跳过重复实例')
    return
end
mp.set_property_bool(integration_marker, true)

local screenshot_root = nil

local WINDOWS_FILENAME_REPLACEMENTS = {
    ['<'] = '＜',
    ['>'] = '＞',
    [':'] = '：',
    ['"'] = '＂',
    ['/'] = '／',
    ['\\'] = '＼',
    ['|'] = '｜',
    ['?'] = '？',
    ['*'] = '＊',
}

local function get_screenshot_root()
    if screenshot_root and screenshot_root ~= '' then
        return screenshot_root
    end
    screenshot_root = mp.command_native({'expand-path', '~~desktop/动漫截图'})
    return screenshot_root
end

local function get_screenshot_folder_name()
    local media_title = mp.get_property('media-title')
    if media_title and media_title ~= '' then
        return media_title
    end

    local filename = mp.get_property('filename')
    if filename then
        return filename:gsub('%.[^.]+$', '')
    end

    return nil
end

local function sanitize_windows_component(value)
    local cleaned = tostring(value or '')
    cleaned = cleaned:gsub('[<>:"/\\|?*]', WINDOWS_FILENAME_REPLACEMENTS)
    cleaned = cleaned:gsub('[%z\1-\31]', ' ')
    cleaned = cleaned:gsub('%s+', ' ')
    cleaned = cleaned:gsub('[%. ]+$', '')
    if cleaned == '' then return '未命名视频' end
    return cleaned
end

local function update_screenshot_directory()
    local folder_name = get_screenshot_folder_name()
    if not folder_name or folder_name == '' then return false end

    local root = get_screenshot_root()
    if not root or root == '' then return false end

    local screenshot_dir = utils.join_path(root, sanitize_windows_component(folder_name))
    mp.set_property('screenshot-directory', screenshot_dir)
    mp.msg.verbose('KumiPlayer 截图目录: ' .. screenshot_dir)
    return true
end

local function screenshot_video()
    update_screenshot_directory()
    mp.command('no-osd screenshot video')
end

local function screenshot_subtitles()
    update_screenshot_directory()
    mp.command('screenshot subtitles')
end

mp.set_property('screenshot-format', 'jpg')
mp.set_property_number('screenshot-jpeg-quality', 100)
mp.set_property('screenshot-template', '%wH-%wM-%wS.%wT')
mp.register_event('file-loaded', update_screenshot_directory)
mp.observe_property('media-title', 'string', function(_name, value)
    if value and value ~= '' then update_screenshot_directory() end
end)

-- 普通弱绑定：用户 input.conf 中的同名按键始终优先，不改变其个人快捷键。
mp.add_key_binding('F10', 'kumiplayer-screenshot-video', screenshot_video)
mp.add_key_binding('Alt+F10', 'kumiplayer-screenshot-subtitles', screenshot_subtitles)
