"""Real MPV smoke check: the context menu opens before a state refresh returns."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MPV = ROOT / "mpv/runtime/mpv.exe"
CONFIG = ROOT / "mpv/config/portable_config"
SCRIPTS = ROOT / "mpv/config/kumiplayer/scripts"


def test_menu_open_does_not_wait_for_anime4k_state(tmp_path: Path) -> None:
    trigger = tmp_path / "trigger.lua"
    trigger.write_text(
        'mp.register_event("file-loaded", function()\n'
        '    mp.add_timeout(0.6, function()\n'
        '        mp.msg.warn("MENU_LATENCY_TRIGGER")\n'
        '        mp.commandv("script-message-to", "kumiplayer_uosc_menu", "open-anime4k-menu")\n'
        '    end)\n'
        'end)\n',
        encoding="utf-8",
    )
    log_file = tmp_path / "mpv-menu-latency.log"
    result = subprocess.run(
        [
            str(MPV),
            "--no-terminal",
            "--force-window=no",
            "--vo=null",
            "--no-audio",
            f"--config-dir={CONFIG}",
            "av://lavfi:testsrc=duration=3",
            f"--script={SCRIPTS / 'kumiplayer_uosc_menu.lua'}",
            f"--script={SCRIPTS / 'kumiplayer_anime4k.lua'}",
            f"--script={trigger}",
            f"--log-file={log_file}",
            "-v",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    assert result.returncode == 0
    log = log_file.read_text(encoding="utf-8", errors="replace")
    after_trigger = log.split("MENU_LATENCY_TRIGGER", 1)[1]
    opened_at = after_trigger.find('args="open-menu"')
    refreshed_at = after_trigger.find('args="get-state"')
    assert opened_at >= 0, "The right-click entry did not open a menu"
    assert refreshed_at >= 0, "The menu did not request a fresh Anime4K state"
    assert opened_at < refreshed_at, "Menu opening waits for an asynchronous state round-trip"
