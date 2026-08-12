# -*- coding: utf-8 -*-
"""M09 Mirror 扫描器测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_MIRROR = Path(__file__).parent.parent / "data" / "test_mirror"


def _cleanup():
    if _TEST_MIRROR.exists():
        shutil.rmtree(_TEST_MIRROR)


def _setup_mirror():
    """创建测试用 mirror 目录结构"""
    _cleanup()
    ns = _TEST_MIRROR / "115"
    clannad = ns / "CLANNAD"
    (clannad / "Season 1").mkdir(parents=True)
    (clannad / "Season 2").mkdir(parents=True)
    (clannad / "OP-ED").mkdir(parents=True)

    # .strm 文件
    (clannad / "Season 1" / "CLANNAD - S01E01.strm").write_text("H:\\anime\\clannad\\s01e01.mkv\n", encoding="utf-8")
    (clannad / "Season 1" / "CLANNAD - S01E02.strm").write_text("H:\\anime\\clannad\\s01e02.mkv\n", encoding="utf-8")
    (clannad / "Season 2" / "CLANNAD - S02E01.strm").write_text("H:\\anime\\clannad\\s02e01.mkv\n", encoding="utf-8")
    (clannad / "OP-ED" / "CLANNAD - NCOP01.strm").write_text("H:\\anime\\clannad\\ncop01.mkv\n", encoding="utf-8")

    # NFO / 图片
    (clannad / "tvshow.nfo").write_text('<tvshow><title>CLANNAD</title><year>2007</year></tvshow>', encoding="utf-8")
    (clannad / "poster.jpg").write_bytes(b"fake-poster")
    (clannad / "fanart.jpg").write_bytes(b"fake-fanart")

    return ns


def test_scan_strm_files():
    """扫描 .strm 文件"""
    from app.library.scanner import scan_mirror

    _setup_mirror()
    try:
        result = scan_mirror(mirror_root=str(_TEST_MIRROR))
        assert len(result.strm_files) == 4
        assert all(mf.source == "pan115" for mf in result.strm_files)
    finally:
        _cleanup()


def test_read_strm_content():
    """读取 .strm 内容"""
    from app.library.scanner import scan_mirror

    _setup_mirror()
    try:
        result = scan_mirror(mirror_root=str(_TEST_MIRROR))
        strm = next(mf for mf in result.strm_files if "S01E01" in mf.strm_path)
        assert strm.real_path == "H:\\anime\\clannad\\s01e01.mkv"
    finally:
        _cleanup()


def test_scan_assets():
    """识别 NFO / poster / fanart"""
    from app.library.scanner import scan_mirror

    _setup_mirror()
    try:
        result = scan_mirror(mirror_root=str(_TEST_MIRROR))
        kinds = {a.kind for a in result.assets}
        assert "tvshow_nfo" in kinds
        assert "poster" in kinds
        assert "fanart" in kinds
    finally:
        _cleanup()


def test_empty_mirror():
    """空 mirror 返回空结果"""
    from app.library.scanner import scan_mirror

    _cleanup()
    _TEST_MIRROR.mkdir(parents=True, exist_ok=True)
    try:
        result = scan_mirror(mirror_root=str(_TEST_MIRROR))
        assert len(result.strm_files) == 0
        assert len(result.assets) == 0
    finally:
        _cleanup()


def test_orphan_strm():
    """orphan strm 可被扫描到"""
    from app.library.scanner import scan_mirror

    _setup_mirror()
    try:
        # 添加一个 orphan
        orphan = _TEST_MIRROR / "115" / "unknown" / "orphan.strm"
        orphan.parent.mkdir(parents=True)
        orphan.write_text("H:\\orphan.mkv\n", encoding="utf-8")

        result = scan_mirror(mirror_root=str(_TEST_MIRROR))
        assert len(result.strm_files) == 5
        orphan_mf = next(mf for mf in result.strm_files if "orphan" in mf.strm_path)
        assert orphan_mf.real_path == "H:\\orphan.mkv"
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [test_scan_strm_files, test_read_strm_content, test_scan_assets, test_empty_mirror, test_orphan_strm]
    passed = failed = 0
    for t in tests:
        try:
            _cleanup()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
