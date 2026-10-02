"""HDR 时间轴预览：用真实 Lua 脚本构造参数，并由内置 MPV 编码验证。"""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MPV = ROOT / "mpv/runtime/mpv.exe"
SCRIPT = ROOT / "mpv/config/portable_config/scripts/thumbfast.lua"
CONF = ROOT / "mpv/config/portable_config/script-opts/thumbfast.conf"


def _probe(tmp_path: Path, primaries: str, actions: str = "") -> dict:
    if not MPV.is_file():
        pytest.skip("内置 MPV 不可用")
    harness = r'''
local real_mp = require 'mp'
local function main()
local utils = require 'mp.utils'
local observers, events, messages, launches, osd = {}, {}, {}, {}, {}
local function noop() end
local function timer()
    return {kill=noop, resume=noop, is_enabled=function() return false end}
end
mp = {
    msg={info=noop, warn=noop, error=noop}, log=real_mp.log,
    get_property=function(name) if name == 'platform' then return 'windows' end end,
    get_property_native=function() return nil end,
    get_property_number=function(_, default) return default or 0 end,
    get_time=function() return 0 end,
    add_timeout=timer, add_periodic_timer=timer,
    command_native=noop,
    command_native_async=function(cmd, callback)
        if cmd.name == 'subprocess' then
            launches[#launches+1] = {args=cmd.args, callback=callback}
        end
    end,
    commandv=function(...) osd[#osd+1]={...} end,
    _commandv=real_mp._commandv,
    observe_property=function(name, _, callback) observers[name]=callback end,
    register_event=function(name, callback) events[name]=callback end,
    register_script_message=function(name, callback) messages[name]=callback end,
    register_idle=noop, dispatch_events=real_mp.dispatch_events,
}
package.loaded['mp.utils'] = {
    getpid=function() return 123 end, file_info=function() return nil end,
    format_json=utils.format_json,
}
package.loaded['mp.options'] = {read_options=function(opts)
    for line in io.lines(CONF) do
        local key, value = line:match('^([%w_]+)=(.+)$')
        if key and opts[key] ~= nil then
            if type(opts[key]) == 'boolean' then opts[key]=value=='yes'
            elseif type(opts[key]) == 'number' then opts[key]=tonumber(value)
            else opts[key]=value end
        end
    end
    opts.mpv_path=MPV_PATH
    opts.thumbnail=OUTPUT
end}
dofile(SCRIPT)
local function load()
    observers['path']('path', 'synthetic.mkv')
    observers['vid']('vid', 1)
    observers['video-params']('video-params', {primaries=PRIMARIES, rotate=0})
    observers['video-out-params']('video-out-params', {dw=64, dh=64, par=1})
    observers['tone-mapping']('tone-mapping', 'auto')
    events['file-loaded']()
end
load()
messages.thumb('1', '0', '0')
ACTIONS
local args={}
for _, request in ipairs(launches) do args[#args+1]=request.args end
local f=assert(io.open(RESULT, 'w'))
f:write(utils.format_json({launches=args, osd=osd})); f:close()
end
local ok, err = xpcall(main, debug.traceback)
if not ok then print(err) end
real_mp.commandv('quit', ok and 0 or 1)
'''
    values = {
        "CONF": CONF.as_posix(), "SCRIPT": SCRIPT.as_posix(),
        "MPV_PATH": MPV.as_posix(), "OUTPUT": (tmp_path / "thumb").as_posix(),
        "RESULT": (tmp_path / "result.json").as_posix(), "PRIMARIES": primaries,
    }
    probe = tmp_path / "probe.lua"
    probe.write_text(
        "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items())
        + "\n" + harness.replace("ACTIONS", actions), encoding="utf-8",
    )
    result = subprocess.run(
        [str(MPV), "--no-config", "--load-scripts=no", "--idle=yes",
         "--vo=null", "--ao=null", "--terminal=yes", f"--script={probe}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "result.json").is_file(), result.stdout + result.stderr
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("primaries,trc,matrix", [
    ("bt.2020", "smpte2084", "bt2020nc"),
    ("bt.2020", "arib-std-b67", "bt2020nc"),
    ("bt.709", "bt709", "bt709"),
])
def test_thumbnail_chain_encodes_hdr_and_sdr(tmp_path, primaries, trc, matrix):
    args = _probe(tmp_path, primaries)["launches"][0]
    vf = next(arg for arg in args if arg.startswith("--vf="))
    color_primaries = primaries.replace(".", "")
    source = (
        "av://lavfi:color=c=white:s=64x64:r=1,format=yuv420p10le,"
        f"setparams=color_primaries={color_primaries}:color_trc={trc}:colorspace={matrix}"
    )
    output = tmp_path / "encoded.bgra"
    # 使用实际脚本的渲染参数；取消预览后端的待机/暂停以输出一帧。
    selected = [arg for arg in args[1:] if arg.startswith(
        ("--vf=", "--target-", "--tone-mapping=", "--sws-", "--video-rotate="))]
    result = subprocess.run(
        [str(MPV), "--no-config", "--no-audio", "--frames=1", *selected,
         "--ovc=rawvideo", "--of=image2", "--ofopts=update=1", f"--o={output}", source],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, vf + "\n" + result.stdout + result.stderr
    assert output.stat().st_size == 256 * 256 * 4
    if primaries == "bt.709":
        assert "gpu=" not in vf and "zscale=" not in vf


def test_hdr_failure_retries_once_without_tone_mapping(tmp_path):
    result = _probe(tmp_path, "bt.2020", """
launches[1].callback(true, {status=1})
assert(#launches == 2, 'HDR preview must retry immediately')
launches[2].callback(true, {status=1})
messages.thumb('2', '0', '0')
assert(#launches == 2, 'failed preview must stop spawning for this file')
load()
messages.thumb('3', '0', '0')
"""
    )
    assert len(result["launches"]) == 3
    filters = [next(arg for arg in args if arg.startswith("--vf="))
               for args in result["launches"]]
    assert "gpu=" in filters[0] and "gpu=" not in filters[1]
    assert "gpu=" in filters[2], "新文件应恢复 HDR 映射"
    assert not any(command[0] == "show-text" for command in result["osd"])


def test_old_file_failure_does_not_disable_new_preview(tmp_path):
    result = _probe(tmp_path, "bt.2020", """
load()
messages.thumb('2', '0', '0')
launches[1].callback(true, {status=1})
messages.thumb('3', '0', '0')
"""
    )
    assert len(result["launches"]) == 2
    assert not any(command[0] == "show-text" for command in result["osd"])


def test_sdr_launch_failure_does_not_loop_or_show_setup_prompt(tmp_path):
    result = _probe(tmp_path, "bt.709", """
launches[1].callback(false, {status=-1})
messages.thumb('2', '0', '0')
messages.thumb('3', '0', '0')
"""
    )
    assert len(result["launches"]) == 1
    assert not any(command[0] == "show-text" for command in result["osd"])


def test_real_hdr_preview_child_produces_overlay(tmp_path):
    if not MPV.is_file():
        pytest.skip("内置 MPV 不可用")
    media = tmp_path / "hdr.mkv"
    source = (
        "av://lavfi:color=c=white:s=64x64:r=1,format=yuv420p10le,"
        "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc"
    )
    def run(args):
        return subprocess.run(
            [str(MPV), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    encoded = run(["--no-config", "--no-audio", "--frames=2", "--ovc=ffv1",
                   "--of=matroska", f"--o={media}", source])
    assert encoded.returncode == 0, encoded.stdout + encoded.stderr
    result_file = tmp_path / "live.json"
    trigger = tmp_path / "trigger.lua"
    trigger.write_text(
        "local result_path = " + json.dumps(result_file.as_posix()) + r'''
local mp = require 'mp'
local utils = require 'mp.utils'
local thumbnail
mp.register_script_message('thumbfast-info', function(json)
    thumbnail = utils.parse_json(json).thumbnail
end)
mp.register_event('file-loaded', function()
    mp.add_timeout(0.1, function()
        mp.commandv('script-message-to', 'thumbfast', 'thumb', '0', '0', '0')
    end)
end)
mp.add_periodic_timer(0.1, function()
    local info = thumbnail and utils.file_info(thumbnail..'.bgra')
    if info and info.size == 64*64*4 then
        local f = assert(io.open(result_path, 'w'))
        f:write(utils.format_json(mp.get_property_native('video-params'))); f:close()
        mp.commandv('quit')
    end
end)
mp.add_timeout(6, function() mp.commandv('quit', '1') end)
''', encoding="utf-8",
    )
    live = run([
        "--no-config", "--load-scripts=no", "--vo=null", "--ao=null",
        "--terminal=yes", "--pause", "--keep-open=yes", f"--script={SCRIPT}",
        f"--script={trigger}", f"--script-opt=thumbfast-mpv_path={MPV}",
        f"--script-opt=thumbfast-thumbnail={tmp_path / 'live-thumb'}",
        "--script-opt=thumbfast-tone_mapping_backend=gpu",
        "--script-opt=thumbfast-quiet_failures=yes",
        "--script-opt=thumbfast-hwdec=yes",
        "--script-opt=thumbfast-max_width=64", "--script-opt=thumbfast-max_height=64",
        str(media),
    ])
    assert live.returncode == 0, live.stdout + live.stderr
    assert json.loads(result_file.read_text(encoding="utf-8"))["primaries"] == "bt.2020"
    assert "Lua error" not in live.stdout + live.stderr
