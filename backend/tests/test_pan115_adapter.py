# -*- coding: utf-8 -*-
"""Pan115Adapter 单元测试

覆盖 M02 设计说明要求的全部测试项。
"""

import sys
import os
import tempfile
from pathlib import Path

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 测试用 115 目录树样本
# ============================================================

SMALL_TREE = """\
|——根目录
| |-动画
| | |-冰菓.2012
| | | |-冰菓.S01E01.深具传统的古籍研究社之重生.mkv
| | | |-冰菓.S01E02.神秘的古典文学部之.mkv
| | | |-OP＆ED
| | | | |-NCOP01.mkv
| | | | |-NCED01.mkv
"""

DEEP_TREE = """\
|——根目录
| |-动画
| | |-AIR.2005
| | | |-OP＆ED
| | | | |-[Ygm]AIR [NCOP01][Ma10_1080p][2flac 5.1ch AC3].mkv
| | | |-AIR.S01E01.微风～breeze～.mkv
| | | |-字幕
| | | | |-AIR.S01E01.ass
| | | | |-AIR.S01E01.srt
"""

FILE_TYPE_TREE = """\
|——根目录
| |-动画
| | |-测试作品
| | | |-视频.mkv
| | | |-视频.mp4
| | | |-视频.avi
| | | |-字幕.ass
| | | |-字幕.srt
| | | |-字幕.ssa
| | | |-字幕.vtt
| | | |-信息.nfo
| | | |-图片.jpg
| | | |-图片.jpeg
| | | |-图片.png
| | | |-图片.gif
| | | |-图片.bmp
| | | |-图片.webp
| | | |-字体.ttf
| | | |-字体.ttc
| | | |-字体.otf
| | | |-压缩.zip
| | | |-压缩.rar
| | | |-压缩.7z
| | | |-安装.exe
| | | |-音频.mp3
| | | |-说明.txt
"""

SYSTEM_FILE_TREE = """\
|——根目录
| |-动画
| | |-测试作品
| | | |-Thumbs.db
| | | |-Desktop.ini
| | | |-正常视频.mkv
| | | | |-.DS_Store
"""

NO_EXT_DIR_TREE = """\
|——根目录
| |-动画
| | |-[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]
| | | |-正常视频.mkv
"""

# ============================================================
# 辅助函数
# ============================================================

def _write_temp_tree(content: str, suffix: str = ".txt", encoding: str = "utf-8") -> str:
    """写入临时目录树文件，返回路径"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    data = content.encode(encoding)
    Path(path).write_bytes(data)
    return path


# ============================================================
# 测试函数
# ============================================================

def test_basic_parse():
    """基础解析：小型 115 树，输出 RawSnapshot，source=pan115"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        assert snap.source == "pan115", f"source 应为 pan115，实际: {snap.source}"
        assert snap.source_root == r"H:\115open"
        # SMALL_TREE 有 4 个文件：2 个正片 + NCOP + NCED（目录不计入）
        assert snap.file_count == 4, f"file_count 应为 4，实际: {snap.file_count}"
        assert snap.video_count == 4, f"video_count 应为 4，实际: {snap.video_count}"
        assert snap.snapshot_id != ""
        assert snap.created_at != ""
        assert len(snap.files) == 4
    finally:
        os.unlink(tmp)


def test_deep_parse():
    """深度解析：含 OP＆ED/NCOP.mkv，relative_path 包含完整层级"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(DEEP_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        # 检查深度文件的 relative_path
        ncop = [f for f in snap.files if "NCOP" in f.name]
        assert len(ncop) == 1, f"应找到 1 个 NCOP 文件，实际: {len(ncop)}"
        f = ncop[0]
        assert "OP＆ED" in f.relative_path, f"relative_path 应包含 OP＆ED: {f.relative_path}"
        assert f.depth == 5, f"深度应为 5，实际: {f.depth}"
        assert f.virtual_root == "动画"

        # 检查字幕文件
        subs = [f for f in snap.files if f.resource_hint == "subtitle"]
        assert len(subs) == 2, f"应有 2 个字幕文件，实际: {len(subs)}"
    finally:
        os.unlink(tmp)


def test_file_types():
    """文件类型：mkv/ass/nfo/jpg/zip/exe/mp3/txt 等 resource_hint 正确"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(FILE_TYPE_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")

        hints = {f.stem: f.resource_hint for f in snap.files}

        # 视频
        assert hints.get("视频") == "video", f"mkv 应为 video: {hints.get('视频')}"

        # 字幕
        assert hints.get("字幕") == "subtitle"

        # NFO
        assert hints.get("信息") == "nfo"

        # 图片
        assert hints.get("图片") == "image"

        # 字体
        assert hints.get("字体") == "font"

        # 压缩
        assert hints.get("压缩") == "archive"

        # 安装
        assert hints.get("安装") == "archive"

        # 音频
        assert hints.get("音频") == "audio"

        # 文本
        assert hints.get("说明") == "text"
    finally:
        os.unlink(tmp)


def test_no_ext_dir_not_output():
    """目录误判防护：[xxx][MP4] 无扩展名不输出为 RawFile"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(NO_EXT_DIR_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        # [BeanSub...][MP4] 没有扩展名，是目录，不应输出
        names = [f.name for f in snap.files]
        assert "[BeanSub" not in "".join(names), f"不应输出无扩展名目录: {names}"
        # 但目录下的视频应输出
        assert snap.file_count == 1, f"应只有 1 个文件，实际: {snap.file_count}"
    finally:
        os.unlink(tmp)


def test_system_file_skip():
    """系统文件跳过：Thumbs.db / Desktop.ini / .ds_store 不进入 files"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SYSTEM_FILE_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        names_lower = [f.name.lower() for f in snap.files]
        assert "thumbs.db" not in names_lower, "Thumbs.db 应被跳过"
        assert "desktop.ini" not in names_lower, "Desktop.ini 应被跳过"
        assert ".ds_store" not in names_lower, ".ds_store 应被跳过"
        # 正常文件应保留
        assert snap.file_count == 1, f"应只有 1 个正常文件，实际: {snap.file_count}"
    finally:
        os.unlink(tmp)


def test_real_path():
    """real_path 生成：source_root=H:\\115open 时正确拼接"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        video = [f for f in snap.files if f.ext == ".mkv" and "NCOP" not in f.name and "NCED" not in f.name]
        assert len(video) >= 1
        f = video[0]
        assert f.real_path.startswith(r"H:\115open"), f"real_path 应以 source_root 开头: {f.real_path}"
        assert "动画" in f.real_path, f"real_path 应包含 动画: {f.real_path}"
        assert "冰菓.2012" in f.real_path
    finally:
        os.unlink(tmp)


def test_no_duplicate_virtual_root():
    """不重复动画：source_root=H:\\115open + relative_path 包含 动画 时，real_path 只出现一次"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        for f in snap.files:
            # real_path 不应包含 动画\动画 或 动画/动画
            lower = f.real_path.lower()
            assert "动画\\动画" not in lower, f"real_path 重复 动画: {f.real_path}"
            assert "动画/动画" not in lower, f"real_path 重复 动画: {f.real_path}"
    finally:
        os.unlink(tmp)


def test_encoding_utf16():
    """编码检测 UTF-16：UTF-16 BOM 临时文件正常解析"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE, encoding="utf-16")
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        assert snap.file_count == 4, f"UTF-16 解析 file_count 应为 4，实际: {snap.file_count}"
    finally:
        os.unlink(tmp)


def test_encoding_utf8():
    """编码检测 UTF-8：UTF-8 临时文件正常解析"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE, encoding="utf-8")
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        assert snap.file_count == 4, f"UTF-8 解析 file_count 应为 4，实际: {snap.file_count}"
    finally:
        os.unlink(tmp)


def test_encoding_gbk():
    """编码检测 GBK：GBK 临时文件正常解析"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE, encoding="gbk")
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        assert snap.file_count == 4, f"GBK 解析 file_count 应为 4，实际: {snap.file_count}"
    finally:
        os.unlink(tmp)


def test_adapter_interface():
    """Pan115Adapter 可实例化，source_id=pan115，mirror_namespace=115"""
    from app.sources.pan115 import Pan115Adapter
    from app.sources.base import SourceAdapter

    adapter = Pan115Adapter()
    assert adapter.source_id == "pan115"
    assert adapter.mirror_namespace == "115"
    assert isinstance(adapter, SourceAdapter)


def test_build_real_path():
    """build_real_path 方法独立测试"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()

    # 方案 A
    result = adapter.build_real_path("动画/冰菓.2012/视频.mkv", r"H:\115open")
    assert "动画" in result
    assert "冰菓.2012" in result
    assert "视频.mkv" in result
    assert result.startswith(r"H:\115open")

    # 不应重复
    assert "动画\\动画" not in result
    assert "动画/动画" not in result


def test_stable_id():
    """同一个 relative_path 重复解析时 RawFile.id 应稳定"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap1 = adapter.parse(tmp, r"H:\115open")
        snap2 = adapter.parse(tmp, r"H:\115open")

        # 同一文件的 id 应相同
        ids1 = {f.relative_path: f.id for f in snap1.files}
        ids2 = {f.relative_path: f.id for f in snap2.files}

        for path in ids1:
            assert ids1[path] == ids2[path], f"ID 不稳定: {path}"
    finally:
        os.unlink(tmp)


def test_source_path_parts():
    """source_path_parts 正确反映路径层级"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(DEEP_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")

        # 检查深度文件的 source_path_parts
        ncop = [f for f in snap.files if "NCOP" in f.name]
        assert len(ncop) == 1
        f = ncop[0]
        assert f.source_path_parts[0] == "动画", f"第一层应为 动画: {f.source_path_parts}"
        assert "AIR.2005" in f.source_path_parts
        assert "OP＆ED" in f.source_path_parts
    finally:
        os.unlink(tmp)


def test_no_media_recognition():
    """不做媒体识别：输出字段不包含 season_number、episode_number、group_type"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")
        for f in snap.files:
            # RawFile 不应有这些字段（它们属于 ImportPlanItem）
            # 通过检查属性不存在来验证
            assert not hasattr(f, "season_number") or f.season_number is None
            assert not hasattr(f, "episode_number") or f.episode_number is None
            assert not hasattr(f, "group_type") or f.group_type is None
    finally:
        os.unlink(tmp)


def test_parent_path():
    """parent_path 正确生成"""
    from app.sources.pan115 import Pan115Adapter

    adapter = Pan115Adapter()
    tmp = _write_temp_tree(SMALL_TREE)
    try:
        snap = adapter.parse(tmp, r"H:\115open")

        # 深度 3 的文件（冰菓.2012 下的文件）
        ep1 = [f for f in snap.files if "S01E01" in f.name]
        assert len(ep1) == 1
        assert ep1[0].parent_path == "动画/冰菓.2012", f"parent_path: {ep1[0].parent_path}"

        # 深度 5 的文件（OP＆ED 下的文件）
        ncop = [f for f in snap.files if "NCOP" in f.name]
        assert len(ncop) == 1
        assert "OP＆ED" in ncop[0].parent_path
    finally:
        os.unlink(tmp)


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_basic_parse,
        test_deep_parse,
        test_file_types,
        test_no_ext_dir_not_output,
        test_system_file_skip,
        test_real_path,
        test_no_duplicate_virtual_root,
        test_encoding_utf16,
        test_encoding_utf8,
        test_encoding_gbk,
        test_adapter_interface,
        test_build_real_path,
        test_stable_id,
        test_source_path_parts,
        test_no_media_recognition,
        test_parent_path,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
